import threading
import unittest
from datetime import datetime, timedelta, timezone

from unittest import mock

from zdaudit import zendesk
from zdaudit.analyze import analyze
from zdaudit.report import render_html

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


def gave_up():
    return zendesk.ZendeskError("Gave up after several retries.", gave_up=True)


class FakeZendesk:
    """Answers the same URLs the real client asks for, from a dict.

    An error is a status code, an exception, or a list with one of those (or None to answer) per call.
    """
    paginate = zendesk.Zendesk.paginate

    def __init__(self, pages, errors=None):
        self.pages, self.errors, self.calls = pages, errors or {}, []

    def get(self, url):
        self.calls.append(url)
        error = self.errors.get(url)
        if isinstance(error, list):
            error = error.pop(0) if error else None
        if isinstance(error, int):
            raise zendesk.ZendeskError("fake error", error)
        if error is not None:
            raise error
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

    def test_hidden_or_deleted_ticket_is_skipped(self):
        for status in (403, 404):
            with self.subTest(status):
                fake = FakeZendesk({}, {audits_path(1): status})
                self.assertIsNone(zendesk.fetch_ticket_macro_audits(fake, 1, SINCE))

    def test_ticket_with_no_macro_uses_is_not_skipped(self):
        fake = FakeZendesk({audits_path(1): {"audits": [audit(10, 1, 2)], "meta": {"has_more": False}}})
        self.assertEqual(zendesk.fetch_ticket_macro_audits(fake, 1, SINCE), [])

    def test_progress_is_reported(self):
        messages = []
        fake = FakeZendesk(base_pages(), {audits_path(3): 404, "/api/v2/macros/56.json": 404})
        zendesk.fetch_all(fake, SINCE, log=messages.append, workers=1)
        self.assertIn("Finding tickets updated in this period...", messages)
        self.assertIn("Reading the history of 3 tickets...", messages)
        self.assertTrue(any(m.startswith("  3 of 3 tickets read. 3 macro uses so far.") for m in messages))


class UnreadTicketTests(unittest.TestCase):
    """Tickets Zendesk keeps failing on are tried again at the end, then listed instead of stopping the run."""
    ERRORS = {audits_path(3): 404, "/api/v2/macros/56.json": 404}

    def run_fetch(self, errors, workers=2):
        self.messages = []
        self.fake = FakeZendesk(base_pages(), {**self.ERRORS, **errors})
        return zendesk.fetch_all(self.fake, SINCE, log=self.messages.append, workers=workers)

    def test_ticket_that_keeps_failing_is_listed(self):
        data = self.run_fetch({audits_path(2): gave_up()})
        self.assertEqual(data["coverage"].unread, [2])
        self.assertEqual(self.fake.calls.count(audits_path(2)), 2)
        self.assertEqual(sorted(a["id"] for a in data["audits"]), [11, 13])
        self.assertIn("Trying 1 failed ticket again...", self.messages)

    def test_ticket_that_works_at_the_end_is_not_listed(self):
        data = self.run_fetch({audits_path(2): [gave_up()]})
        self.assertEqual(data["coverage"].unread, [])
        self.assertEqual(self.fake.calls.count(audits_path(2)), 2)
        self.assertEqual(sorted(a["id"] for a in data["audits"]), [11, 13, 20])

    def test_failure_partway_through_a_ticket_keeps_nothing_twice(self):
        # Ticket 1's second page fails the first time. The retry reads both pages again.
        data = self.run_fetch({"T1PAGE2": [gave_up()]})
        self.assertEqual(data["coverage"].unread, [])
        self.assertEqual(sorted(a["id"] for a in data["audits"]), [11, 13, 20])
        self.assertEqual(self.fake.calls.count(audits_path(1)), 2)

    def test_listed_in_order(self):
        # Fewer than GIVE_UP_STREAK, so the run finishes and retries them all.
        ids = [9_000_017, 15, 700_003, 4_000_000, 8, 123_456, 99, 5_000_001, 31, 2_000_002]
        fake = FakeZendesk(export(ids), {audits_path(i): gave_up() for i in ids})
        audits, coverage = zendesk.fetch_macro_audits(fake, SINCE, lambda _: None, workers=4)
        self.assertEqual((audits, coverage.unread, coverage.stopped_early), ([], sorted(ids), False))
        self.assertEqual(len(fake.calls), 21)
        self.assertEqual(fake.calls[11:], [audits_path(i) for i in sorted(ids)], "retried one at a time, in order")

    def test_no_failures_lists_nothing(self):
        self.assertEqual(self.run_fetch({})["coverage"].unread, [])
        self.assertFalse(any(m.startswith("Trying") for m in self.messages))

    def test_sign_in_failure_still_stops_the_run(self):
        with self.assertRaisesRegex(zendesk.AuthError, "expired"):
            self.run_fetch({audits_path(2): zendesk.AuthError("expired")})

    def test_errors_retrying_wont_fix_still_stop_the_run(self):
        # The ticket would work on a second try, so only stopping at once raises.
        for error in (zendesk.ZendeskError("Couldn't verify the security certificate"), 400, 401):
            with self.subTest(error):
                with self.assertRaises(zendesk.ZendeskError):
                    self.run_fetch({audits_path(2): [error]})
                self.assertEqual(self.fake.calls.count(audits_path(2)), 1)

    def test_errors_at_the_end_retry_that_retrying_wont_fix_stop_the_run(self):
        with self.assertRaisesRegex(zendesk.ZendeskError, "fake error"):
            self.run_fetch({audits_path(2): [gave_up(), 400]})
        with self.assertRaisesRegex(zendesk.AuthError, "expired"):
            self.run_fetch({audits_path(2): [gave_up(), zendesk.AuthError("expired")]})

    def test_unread_tickets_reach_the_result(self):
        data = self.run_fetch({audits_path(2): gave_up()})
        self.assertEqual(analyze(**data, since=SINCE, until=NOW).coverage.unread, [2])

    def test_skipped_ticket_counts_as_read(self):
        coverage = self.run_fetch({audits_path(2): gave_up()})["coverage"]
        self.assertEqual((coverage.tickets, coverage.read, coverage.stopped_early), (3, 2, False))


class SkippedTicketTests(unittest.TestCase):
    """Tickets deleted or hidden from this sign-in after the export listed them are skipped and listed."""
    def fetch(self, errors, working=(1, 3, 5), workers=2):
        pages = export([1, 2, 3, 4, 5])
        pages.update({audits_path(t): one_use(t) for t in working})
        self.fake = FakeZendesk(pages, errors)
        return zendesk.fetch_macro_audits(self.fake, SINCE, lambda _: None, workers=workers)

    def test_hidden_and_deleted_tickets_are_listed(self):
        audits, coverage = self.fetch({audits_path(2): 403, audits_path(4): 404})
        self.assertEqual(coverage, zendesk.Coverage(5, [], False, False, [2, 4]))
        self.assertEqual(coverage.read, 5)
        self.assertEqual(sorted(a["ticket_id"] for a in audits), [1, 3, 5])
        self.assertEqual(self.fake.calls.count(audits_path(2)), 1, "not tried again")
        self.assertEqual(self.fake.calls.count(audits_path(4)), 1, "not tried again")

    @mock.patch.object(zendesk, "as_completed", lambda futures: reversed(list(futures)))
    def test_listed_in_order(self):
        # Results arrive last ticket first, so the list has to be sorted.
        audits, coverage = self.fetch({audits_path(t): 404 for t in (5, 1, 3)}, working=(2, 4), workers=1)
        self.assertEqual(coverage.skipped, [1, 3, 5])
        self.assertEqual(sorted(a["ticket_id"] for a in audits), [2, 4])

    def test_skipped_on_the_retry_at_the_end(self):
        for status in (403, 404):
            with self.subTest(status):
                audits, coverage = self.fetch({audits_path(2): [gave_up(), status]}, working=(1, 3, 4, 5))
                self.assertEqual(coverage, zendesk.Coverage(5, [], False, False, [2]))
                self.assertEqual(self.fake.calls.count(audits_path(2)), 2)
                self.assertEqual(sorted(a["ticket_id"] for a in audits), [1, 3, 4, 5])

    def test_skipped_and_unread_are_kept_apart(self):
        audits, coverage = self.fetch({audits_path(2): gave_up(), audits_path(4): 404})
        self.assertEqual(coverage, zendesk.Coverage(5, [2], False, False, [4]))
        self.assertEqual(coverage.read, 4)

    def test_none_skipped(self):
        _, coverage = self.fetch({}, working=(1, 2, 3, 4, 5))
        self.assertEqual(coverage, zendesk.Coverage(5, [], False, False, []))

    def test_skipped_tickets_reach_the_result(self):
        fake = FakeZendesk(base_pages(), {audits_path(3): 404, "/api/v2/macros/56.json": 404})
        data = zendesk.fetch_all(fake, SINCE, log=lambda _: None, workers=2)
        self.assertEqual(data["coverage"], zendesk.Coverage(3, [], False, False, [3]))
        self.assertEqual(analyze(**data, since=SINCE, until=NOW).coverage.skipped, [3])


def export(ids):
    start = f"/api/v2/incremental/tickets/cursor?start_time={int(SINCE.timestamp())}"
    return {start: {"tickets": [{"id": i} for i in ids], "end_of_stream": True}}


def one_use(ticket_id):
    return {"audits": [audit(ticket_id * 10, ticket_id, 1, "MacroReference")], "meta": {"has_more": False}}


class GatedZendesk(FakeZendesk):
    """Tickets in `gated` wait until the run says it's stopping, so they can't start before then."""
    def __init__(self, pages, errors, gated):
        super().__init__(pages, errors)
        self.gated, self.stopping = gated, threading.Event()

    def get(self, url):
        if url in self.gated and not self.stopping.wait(5):
            self.stopping.set()  # the run never stopped, so let the rest through and let the test fail
        return super().get(url)

    def log(self, message):
        if message.startswith("Zendesk hasn't answered"):
            self.stopping.set()


@mock.patch.object(zendesk, "as_completed", iter)  # results in the order the tickets were started
class EarlyStopTests(unittest.TestCase):
    STREAK = zendesk.GIVE_UP_STREAK

    def fetch(self, failing, working, gate=False):
        pages = export(failing + working)
        pages.update({audits_path(t): one_use(t) for t in working})
        errors = {audits_path(t): gave_up() for t in failing}
        self.fake = GatedZendesk(pages, errors, {audits_path(t) for t in working} if gate else set())
        self.messages = []

        def log(message):
            self.messages.append(message)
            self.fake.log(message)
        return zendesk.fetch_macro_audits(self.fake, SINCE, log, workers=1)

    def test_stops_at_exactly_the_streak(self):
        failing, working = list(range(1, self.STREAK + 1)), list(range(100, 130))
        audits, coverage = self.fetch(failing, working, gate=True)
        self.assertTrue(coverage.stopped_early)
        self.assertIn(f"Zendesk hasn't answered for the last {self.STREAK} tickets, so the tool is stopping early.",
                      self.messages)
        # No end-of-run retry: each failing ticket was asked for once.
        self.assertEqual([self.fake.calls.count(audits_path(t)) for t in failing], [1] * self.STREAK)
        self.assertFalse(any(m.startswith("Trying") for m in self.messages))

    def test_cancelled_tickets_are_unread_and_a_finished_one_is_kept(self):
        failing, working = list(range(1, self.STREAK + 1)), list(range(100, 130))
        audits, coverage = self.fetch(failing, working, gate=True)
        never_asked = [t for t in working if audits_path(t) not in self.fake.calls]
        asked = [t for t in working if t not in never_asked]
        # Ticket 100 was already waiting when the run stopped. At most one more can slip in before the cancel.
        self.assertIn(100, asked)
        self.assertLessEqual(len(asked), 2)
        self.assertEqual(coverage.unread, failing + never_asked)
        self.assertEqual(sorted(a["ticket_id"] for a in audits), asked)
        self.assertEqual((coverage.tickets, coverage.read), (50, len(asked)))

    def test_one_short_of_the_streak_finishes_the_run(self):
        failing, working = list(range(1, self.STREAK)), [100, 101]
        audits, coverage = self.fetch(failing, working)
        self.assertFalse(coverage.stopped_early)
        self.assertEqual(coverage.unread, failing)
        self.assertEqual([self.fake.calls.count(audits_path(t)) for t in failing], [2] * (self.STREAK - 1))
        self.assertEqual(len(audits), 2)
        self.assertFalse(any(m.startswith("Zendesk hasn't answered") for m in self.messages))

    def test_a_ticket_that_works_resets_the_count(self):
        first, second = list(range(1, self.STREAK)), list(range(200, 200 + self.STREAK - 1))
        pages = export(first + [100] + second)
        pages[audits_path(100)] = one_use(100)
        self.fake = FakeZendesk(pages, {audits_path(t): gave_up() for t in first + second})
        audits, coverage = zendesk.fetch_macro_audits(self.fake, SINCE, lambda _: None, workers=1)
        self.assertFalse(coverage.stopped_early)
        self.assertEqual(coverage.unread, first + second)

    def test_a_ticket_skipped_while_stopping_is_listed_as_skipped(self):
        failing, working = list(range(1, self.STREAK + 1)), list(range(100, 130))
        pages = export(failing + working)
        pages.update({audits_path(t): one_use(t) for t in working})
        errors = {audits_path(t): gave_up() for t in failing}
        errors[audits_path(100)] = 404  # already waiting when the run stops
        self.fake = GatedZendesk(pages, errors, {audits_path(t) for t in working})
        audits, coverage = zendesk.fetch_macro_audits(self.fake, SINCE, self.fake.log, workers=1)
        self.assertTrue(coverage.stopped_early)
        self.assertEqual(coverage.skipped, [100])
        self.assertNotIn(100, coverage.unread)
        self.assertNotIn(100, [a["ticket_id"] for a in audits])

    def test_a_skipped_ticket_resets_the_count(self):
        first, second = list(range(1, self.STREAK)), list(range(200, 200 + self.STREAK - 1))
        errors = {audits_path(t): gave_up() for t in first + second}
        errors[audits_path(100)] = 404
        self.fake = FakeZendesk(export(first + [100] + second), errors)
        _, coverage = zendesk.fetch_macro_audits(self.fake, SINCE, lambda _: None, workers=1)
        self.assertFalse(coverage.stopped_early)
        self.assertEqual(coverage.read, 1)
        self.assertEqual(coverage.skipped, [100])


class AfterTheTicketsTests(unittest.TestCase):
    """What happens when Zendesk stops answering once the tickets are read."""
    ERRORS = {audits_path(3): 404, "/api/v2/macros/56.json": 404}

    def run_fetch(self, errors, pages=None):
        self.messages = []
        self.fake = FakeZendesk(pages or base_pages(), {**self.ERRORS, **errors})
        return zendesk.fetch_all(self.fake, SINCE, log=self.messages.append, workers=2)

    def test_names_missing_still_gives_the_data(self):
        data = self.run_fetch({"/api/v2/users/show_many.json?ids=7": gave_up()})
        self.assertEqual(data["users"], {})
        self.assertTrue(data["coverage"].names_missing)
        self.assertIn("  Zendesk stopped answering, so agents without a name are shown by ID.", self.messages)
        self.assertEqual(sorted(a["id"] for a in data["audits"]), [11, 13, 20])
        result = analyze(**data, since=SINCE, until=NOW)
        self.assertEqual([a["name"] for a in result.agents], ["User 7"])

    def test_other_errors_fetching_names_still_stop_the_run(self):
        with self.assertRaisesRegex(zendesk.ZendeskError, "fake error"):
            self.run_fetch({"/api/v2/users/show_many.json?ids=7": 400})

    def test_names_found_means_nothing_missing(self):
        self.assertFalse(self.run_fetch({})["coverage"].names_missing)
        self.assertFalse(any(m.startswith("Read ") for m in self.messages), "only said when stopped early")

    def test_sample_data_has_nothing_unread(self):
        result = analyze([], {}, {}, {}, SINCE, NOW)
        self.assertEqual(result.coverage, zendesk.Coverage(0, [], False, False))
        self.assertEqual(result.coverage.read, 0)

    def test_no_macro_list_means_no_report(self):
        with self.assertRaises(zendesk.ZendeskError) as caught:
            self.run_fetch({MACRO_PATH + "&include=usage_30d": gave_up(), audits_path(2): gave_up()})
        self.assertEqual(str(caught.exception),
                         "Zendesk stopped answering while the tool was fetching macros, so there's no report. "
                         "2 of 3 tickets were read, but the report needs the macros' text. "
                         "A later run should pick everything up.")
        self.assertFalse(caught.exception.gave_up)

    def test_unanswered_lookup_of_a_missing_macro_means_no_report(self):
        # Without this, macro 56 would be counted as deleted.
        with self.assertRaisesRegex(zendesk.ZendeskError, "^Zendesk stopped answering while the tool was fetching"):
            self.run_fetch({"/api/v2/macros/56.json": gave_up()})

    def test_other_macro_errors_are_unchanged(self):
        with self.assertRaisesRegex(zendesk.ZendeskError, "^fake error$"):
            self.run_fetch({"/api/v2/macros/56.json": 400})

    def test_no_ticket_read_means_no_report(self):
        with self.assertRaises(zendesk.ZendeskError) as caught:
            self.run_fetch({audits_path(t): gave_up() for t in (1, 2, 3)})
        self.assertEqual(str(caught.exception),
                         "Zendesk kept failing, so none of the 3 tickets could be read and there's no report. "
                         "A later run should pick everything up.")
        self.assertNotIn(MACRO_PATH + "&include=usage_30d", self.fake.calls)

    def test_no_tickets_in_the_period_is_not_an_error(self):
        pages = export([])
        pages[MACRO_PATH + "&include=usage_30d"] = {"macros": [], "meta": {}}
        data = self.run_fetch({}, pages)
        self.assertEqual((data["coverage"].tickets, data["coverage"].unread), (0, []))

    @mock.patch.object(zendesk, "GIVE_UP_STREAK", 1)
    def test_stopped_early_still_fetches_what_the_report_needs(self):
        data = self.run_fetch({audits_path(2): gave_up()})
        coverage = data["coverage"]
        self.assertTrue(coverage.stopped_early)
        self.assertIn(f"Read {coverage.read} of 3 tickets. Fetching what the report needs for those...", self.messages)
        self.assertEqual(set(data["macros"]), {"55"})
        self.assertIn(2, coverage.unread)

    def test_dynamic_content_that_never_answers_is_skipped(self):
        messages = []
        fake = FakeZendesk({}, {DynamicContentTests.PATH: gave_up()})
        self.assertEqual(zendesk.fetch_dynamic_content(fake, messages.append), {})
        self.assertEqual(messages, ["  Couldn't read dynamic content (Zendesk didn't answer). "
                                    "{{dc.*}} placeholders will match any text."])


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

    def test_link_to_another_site_stops_the_run(self):
        messages = []
        refused = zendesk.ZendeskError("Zendesk sent a next-page link to https://evil.com instead of "
                                       "https://acme.zendesk.com, so the run stopped rather than send the sign-in there.")
        fake = FakeZendesk({}, {self.PATH: refused})
        with self.assertRaises(zendesk.ZendeskError) as caught:
            zendesk.fetch_dynamic_content(fake, messages.append)
        self.assertIs(caught.exception, refused)
        self.assertEqual(messages, [])

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
        users, complete = zendesk.fetch_users(fake, ids + ["1", None])
        self.assertEqual((set(users), complete), ({"1", "2"}, True))
        self.assertEqual(len(fake.calls), 2)

    def test_keeps_the_users_found_before_zendesk_gives_up(self):
        # 250 IDs from 1001 to 1250, so sorting them as text keeps them in number order.
        batches = [",".join(str(i) for i in range(start, min(start + 100, 1251))) for start in (1001, 1101, 1201)]
        urls = [f"/api/v2/users/show_many.json?ids={b}" for b in batches]
        fake = FakeZendesk({urls[0]: {"users": [{"id": 1001, "name": "Ana"}, {"id": 1100, "name": "Bo"}]},
                            urls[2]: {"users": [{"id": 1201, "name": "Cy"}]}},
                           {urls[1]: gave_up()})
        users, complete = zendesk.fetch_users(fake, [str(i) for i in range(1250, 1000, -1)])
        self.assertEqual(users, {"1001": {"id": 1001, "name": "Ana"}, "1100": {"id": 1100, "name": "Bo"}})
        self.assertFalse(complete)
        self.assertEqual(fake.calls, urls[:2], "stops at the batch Zendesk gave up on")

    def test_other_errors_still_raise(self):
        fake = FakeZendesk({}, {"/api/v2/users/show_many.json?ids=1": 400})
        with self.assertRaisesRegex(zendesk.ZendeskError, "fake error"):
            zendesk.fetch_users(fake, ["1"])


class PartialNamesTests(unittest.TestCase):
    """150 agents each used a macro on ticket 1. Zendesk answers for the first 100 names only."""
    FIRST = "/api/v2/users/show_many.json?ids=" + ",".join(str(i) for i in range(1001, 1101))
    SECOND = "/api/v2/users/show_many.json?ids=" + ",".join(str(i) for i in range(1101, 1151))

    def run_fetch(self, errors):
        start = f"/api/v2/incremental/tickets/cursor?start_time={int(SINCE.timestamp())}"
        audits = [{**audit(i, 1, 2, "MacroReference"), "author_id": i} for i in range(1001, 1151)]
        macro = {"id": 55, "title": "Done",
                 "actions": [{"field": "comment_value", "value": "Hi {{ticket.requester.first_name}}, done."}]}
        pages = {
            start: {"tickets": [{"id": 1}], "end_of_stream": True},
            audits_path(1): {"audits": audits, "meta": {"has_more": False}},
            MACRO_PATH + "&include=usage_30d": {"macros": [macro], "meta": {"has_more": False}},
            self.FIRST: {"users": [{"id": 1001, "name": "Ana", "email": "ana@example.com"}]},
            self.SECOND: {"users": [{"id": 1101, "name": "Bo", "email": "bo@example.com"}]},
        }
        self.messages = []
        self.fake = FakeZendesk(pages, errors)
        return zendesk.fetch_all(self.fake, SINCE, log=self.messages.append, workers=2)

    def test_names_found_before_zendesk_gave_up_are_kept(self):
        data = self.run_fetch({self.SECOND: gave_up()})
        self.assertEqual(data["users"], {"1001": {"id": 1001, "name": "Ana", "email": "ana@example.com"}})
        self.assertTrue(data["coverage"].names_missing)
        self.assertEqual(self.messages[-2:], ["Fetching agent names...",
                                              "  Zendesk stopped answering, so agents without a name are shown by ID."])

        result = analyze(**data, since=SINCE, until=NOW)
        names = {a["agent_id"]: a["name"] for a in result.agents}
        self.assertEqual((names["1001"], names["1101"], names["1150"]), ("Ana", "User 1101", "User 1150"))
        html = render_html(result, "acme")
        self.assertIn('<div class="warning" role="note"><strong>Not every agent name could be fetched.</strong> '
                      "Zendesk stopped answering while the tool was fetching them, so agents without a name are "
                      "shown by ID.</div>", html)
        self.assertIn("<td>Ana<div class='sub'>ana@example.com</div></td>", html)
        self.assertIn("<td>User 1101<div class='sub'></div></td>", html)
        self.assertNotIn("<td>Bo<", html)
        self.assertIn('"agent": "Ana"', html)
        self.assertIn('"agent": "User 1101"', html)

    def test_every_batch_answered_means_nothing_missing(self):
        data = self.run_fetch({})
        self.assertEqual(set(data["users"]), {"1001", "1101"})
        self.assertFalse(data["coverage"].names_missing)
        self.assertEqual([c for c in self.fake.calls if "users" in c], [self.FIRST, self.SECOND])
        html = render_html(analyze(**data, since=SINCE, until=NOW), "acme")
        self.assertNotIn("Not every agent name", html)


if __name__ == "__main__":
    unittest.main()
