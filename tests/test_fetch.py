import unittest
from datetime import datetime, timedelta, timezone

from zdaudit import zendesk
from zdaudit.analyze import analyze

NOW = datetime.now(timezone.utc)
SINCE = NOW - timedelta(days=30)
MACRO_PATH = "/api/v2/macros.json?page%5Bsize%5D=100"


def iso(days_ago):
    return (NOW - timedelta(days=days_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def audit(audit_id, ticket_id, days_ago, macro_type=None, macro_id=55, body="Hi Sam, done."):
    events = [{"type": "Comment", "body": body, "public": True, "html_body": "<p>ignored</p>"},
              {"type": "Change", "field_name": "status", "value": "solved"}]
    if macro_type:
        events.append({"type": macro_type, "macro_id": macro_id, "macro_title": "Done"})
    return {"id": audit_id, "ticket_id": ticket_id, "created_at": iso(days_ago), "author_id": 7, "events": events}


def audits_path(ticket_id):
    return f"/api/v2/tickets/{ticket_id}/audits.json?page%5Bsize%5D=100"


class FakeZendesk:
    """Answers the same URLs the real client asks for, from a dict."""
    paginate = zendesk.Zendesk.paginate

    def __init__(self, pages, errors=None):
        self.pages, self.errors, self.calls = pages, errors or {}, []

    def get(self, url):
        self.calls.append(url)
        if url in self.errors:
            raise zendesk.ZendeskError("fake error", self.errors[url])
        return self.pages[url]


def base_pages():
    start = f"/api/v2/incremental/tickets/cursor?start_time={int(SINCE.timestamp())}"
    return {
        start: {"tickets": [{"id": 1}, {"id": 2}, {"id": 9, "status": "deleted"}],
                "after_url": "EXPORT2", "end_of_stream": False},
        "EXPORT2": {"tickets": [{"id": 2}, {"id": 3}], "after_url": "EXPORT3", "end_of_stream": True},
        # Ticket 1 has two pages of audits. One macro use is too old to count.
        audits_path(1): {"audits": [audit(10, 1, 45, "MacroReference"), audit(11, 1, 5, "MacroReference")],
                         "meta": {"has_more": True}, "links": {"next": "T1PAGE2"}},
        "T1PAGE2": {"audits": [audit(12, 1, 2), audit(13, 1, 1, "AgentMacroReference", macro_id=56)],
                    "meta": {"has_more": False}},
        audits_path(2): {"audits": [audit(20, 2, 3, "AgentMacroReference", body="Hi Sam, all done now.")],
                         "meta": {"has_more": False}},
        MACRO_PATH + "&include=usage_30d": {
            "macros": [{"id": 55, "title": "Done", "usage_30d": 4,
                        "actions": [{"field": "comment_value", "value": "Hi {{ticket.requester.first_name}}, done."}]}],
            "meta": {"has_more": False}},
        "/api/v2/users/show_many.json?ids=7": {"users": [{"id": 7, "name": "Ana", "email": "ana@example.com"}]},
    }


class FetchTests(unittest.TestCase):
    def run_fetch(self, pages, errors):
        fake = FakeZendesk(pages, errors)
        data = zendesk.fetch_all(fake, SINCE, log=lambda _: None, workers=2)
        return fake, data

    def test_full_fetch(self):
        errors = {audits_path(3): 404, "/api/v2/macros/56.json": 404}
        fake, data = self.run_fetch(base_pages(), errors)

        self.assertNotIn("EXPORT3", fake.calls, "should stop at end_of_stream")
        self.assertNotIn(audits_path(9), fake.calls, "deleted tickets are skipped")
        self.assertEqual(fake.calls.count(audits_path(2)), 1, "duplicate tickets are read once")
        # Both event names are found, the old use and the audit with no macro are dropped.
        self.assertEqual(sorted(a["id"] for a in data["audits"]), [11, 13, 20])
        self.assertEqual(set(data["macros"]), {"55"})
        self.assertEqual(data["users"]["7"]["name"], "Ana")
        # Only macro and comment events are kept.
        for a in data["audits"]:
            self.assertTrue(all(e["type"] != "Change" for e in a["events"]))

        result = analyze(**data, since=SINCE, until=NOW)
        statuses = {u.audit_id: u.status for u in result.uses}
        self.assertEqual(statuses, {11: "unedited", 13: "unavailable", 20: "edited"})
        done = next(m for m in result.macros if m["macro_id"] == "55")
        self.assertEqual(done["zendesk_usage_30d"], 4)

    def test_usage_counts_not_on_plan(self):
        pages = base_pages()
        usage = pages.pop(MACRO_PATH + "&include=usage_30d")
        pages[MACRO_PATH] = {**usage, "macros": [{k: v for k, v in m.items() if k != "usage_30d"}
                                                  for m in usage["macros"]]}
        errors = {audits_path(3): 404, "/api/v2/macros/56.json": 404, MACRO_PATH + "&include=usage_30d": 403}
        _, data = self.run_fetch(pages, errors)
        result = analyze(**data, since=SINCE, until=NOW)
        self.assertIsNone(result.macros[0]["zendesk_usage_30d"])

    def test_unexpected_errors_stop_the_run(self):
        with self.assertRaises(zendesk.ZendeskError):
            self.run_fetch(base_pages(), {audits_path(3): 400})

    def test_server_errors_on_the_macro_list_stop_the_run(self):
        pages = base_pages()
        errors = {audits_path(3): 404, MACRO_PATH + "&include=usage_30d": 500}
        with self.assertRaises(zendesk.ZendeskError):
            self.run_fetch(pages, errors)

    def test_a_use_exactly_at_the_start_of_the_period_counts(self):
        at_start = {"id": 1, "ticket_id": 1, "created_at": SINCE.isoformat(), "author_id": 7,
                    "events": [{"type": "MacroReference", "macro_id": 55}]}
        fake = FakeZendesk({audits_path(1): {"audits": [at_start], "meta": {"has_more": False}}})
        self.assertEqual(len(zendesk.fetch_ticket_macro_audits(fake, 1, SINCE)), 1)

    def test_hidden_ticket_is_skipped(self):
        fake = FakeZendesk({}, {audits_path(1): 403})
        self.assertEqual(zendesk.fetch_ticket_macro_audits(fake, 1, SINCE), [])

    def test_progress_is_reported(self):
        messages = []
        fake = FakeZendesk(base_pages(), {audits_path(3): 404, "/api/v2/macros/56.json": 404})
        zendesk.fetch_all(fake, SINCE, log=messages.append, workers=1)
        self.assertIn("Finding tickets updated in this period...", messages)
        self.assertIn("Reading the history of 3 tickets...", messages)
        self.assertTrue(any(m.startswith("  3 of 3 tickets read. 3 macro uses so far.") for m in messages))


class FetchMacrosTests(unittest.TestCase):
    def test_macro_missing_from_the_list_is_fetched_directly(self):
        fake = FakeZendesk({MACRO_PATH + "&include=usage_30d": {"macros": [], "meta": {}},
                            "/api/v2/macros/7.json": {"macro": {"id": 7, "title": "Direct"}}})
        self.assertEqual(zendesk.fetch_macros(fake, ["7"], print)["7"]["title"], "Direct")

    def test_only_wanted_macros_are_kept(self):
        fake = FakeZendesk({MACRO_PATH + "&include=usage_30d": {"macros": [{"id": 1}, {"id": 2}], "meta": {}}})
        self.assertEqual(set(zendesk.fetch_macros(fake, ["1"], print)), {"1"})

    def test_unexpected_error_on_a_single_macro_stops_the_run(self):
        fake = FakeZendesk({MACRO_PATH + "&include=usage_30d": {"macros": [], "meta": {}}},
                           {"/api/v2/macros/7.json": 400})
        with self.assertRaises(zendesk.ZendeskError):
            zendesk.fetch_macros(fake, ["7"], print)

    def test_no_usage_counts_is_reported(self):
        messages = []
        fake = FakeZendesk({MACRO_PATH: {"macros": [], "meta": {}}}, {MACRO_PATH + "&include=usage_30d": 403})
        zendesk.fetch_macros(fake, [], messages.append)
        self.assertEqual(len(messages), 1)


class DynamicContentTests(unittest.TestCase):
    PATH = "/api/v2/dynamic_content/items.json"

    def test_default_variant_comes_first(self):
        item = {"placeholder": "{{dc.Bye}}", "variants": [{"content": "Salut"}, {"content": "Bye", "default": True},
                                                          {"content": ""}]}
        fake = FakeZendesk({self.PATH: {"items": [item]}})
        self.assertEqual(zendesk.fetch_dynamic_content(fake, print), {"dc.bye": ["Bye", "Salut"]})

    def test_item_with_nothing_usable_is_left_out(self):
        fake = FakeZendesk({self.PATH: {"items": [{"placeholder": "{{dc.a}}", "variants": []},
                                                  {"placeholder": "", "variants": [{"content": "x"}]}]}})
        self.assertEqual(zendesk.fetch_dynamic_content(fake, print), {})

    def test_error_is_reported_and_keeps_what_it_has(self):
        messages = []
        fake = FakeZendesk({}, {self.PATH: 403})
        self.assertEqual(zendesk.fetch_dynamic_content(fake, messages.append), {})
        self.assertIn("403", messages[0])

    def test_only_fetched_when_a_macro_uses_it(self):
        pages = base_pages()
        errors = {audits_path(3): 404, "/api/v2/macros/56.json": 404}
        fake = FakeZendesk(pages, errors)
        self.assertEqual(zendesk.fetch_all(fake, SINCE, log=lambda _: None)["dynamic"], {})
        self.assertNotIn(self.PATH, fake.calls)

        macro = pages[MACRO_PATH + "&include=usage_30d"]["macros"][0]
        macro["actions"][0]["value"] = "Hi {{ dc.bye }}"
        pages[self.PATH] = {"items": [{"placeholder": "{{dc.bye}}", "variants": [{"content": "Bye"}]}]}
        data = zendesk.fetch_all(FakeZendesk(pages, errors), SINCE, log=lambda _: None)
        self.assertEqual(data["dynamic"], {"dc.bye": ["Bye"]})


class FetchUsersTests(unittest.TestCase):
    def test_asks_in_groups_of_100(self):
        ids = [str(i) for i in range(1, 102)]
        by_order = sorted(ids)
        pages = {f"/api/v2/users/show_many.json?ids={','.join(by_order[:100])}": {"users": [{"id": 1}]},
                 f"/api/v2/users/show_many.json?ids={by_order[100]}": {"users": [{"id": 2}]}}
        fake = FakeZendesk(pages)
        self.assertEqual(set(zendesk.fetch_users(fake, ids + ["1", None])), {"1", "2"})
        self.assertEqual(len(fake.calls), 2)


if __name__ == "__main__":
    unittest.main()
