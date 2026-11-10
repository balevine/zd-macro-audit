"""audit.json, and the fetching behind it: every macro, and the requesters of tickets with a macro use."""
import json
import os
import re
import stat
import tempfile
import unittest
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from tests.helpers import SINCE, UNTIL, make_audit, use_macro
from tests.test_fetch import MACRO_PATH, NOW, SINCE as FETCH_SINCE, FakeZendesk, audits_path, base_pages, gave_up
from zdaudit import zendesk
from zdaudit.analyze import analyze
from zdaudit.report import _render_diff, audit_data, write_all
from zdaudit.sample import SHIPPED_ADDITIONS, generate

USAGE_PATH = MACRO_PATH + "&include=usage_30d"
START = f"/api/v2/incremental/tickets/cursor?start_time={int(FETCH_SINCE.timestamp())}"
AGENT_USERS = "/api/v2/users/show_many.json?ids=7"
REQUESTER_USERS = "/api/v2/users/show_many.json?ids=81,82"


class AllMacrosTests(unittest.TestCase):
    LISTED = [
        {"id": 1, "title": "Used", "active": True, "restriction": None},
        {"id": 2, "title": "Inactive", "active": False, "restriction": None},
        {"id": 3, "title": "Personal", "active": True, "restriction": {"type": "User", "id": 7}},
        {"id": 4, "title": "Group only", "active": True, "restriction": {"type": "Group", "id": 5, "ids": [5]}},
    ]

    def test_every_listed_macro_is_kept_with_used_ones_missing_from_the_list(self):
        fake = FakeZendesk({USAGE_PATH: {"macros": self.LISTED, "meta": {}},
                            "/api/v2/macros/9.json": {"macro": {"id": 9, "title": "Deleted"}}})
        macros = zendesk.fetch_macros(fake, ["1", "9"], print)
        self.assertEqual(macros, {**{str(m["id"]): m for m in self.LISTED}, "9": {"id": 9, "title": "Deleted"}})
        self.assertEqual(fake.calls, [USAGE_PATH, "/api/v2/macros/9.json"], "no extra call for listed macros")

    def test_unused_macros_dont_change_the_analysis(self):
        pages = base_pages()
        unused = {"id": 57, "title": "Unused", "active": False,
                  "actions": [{"field": "comment_value", "value": "Hi {{dc.unused}}, bye."}]}
        pages[USAGE_PATH]["macros"].append(unused)
        fake = FakeZendesk(pages, {audits_path(3): 404, "/api/v2/macros/56.json": 404})
        data = zendesk.fetch_all(fake, FETCH_SINCE, log=lambda _: None, workers=2)
        self.assertEqual(set(data["macros"]), {"55", "57"})
        # An unused macro's dynamic content never matters, so it isn't fetched.
        self.assertNotIn("/api/v2/dynamic_content/items.json", fake.calls)
        result = analyze(**data, since=FETCH_SINCE, until=NOW)
        self.assertEqual({u.audit_id: u.status for u in result.uses}, {11: "unedited", 13: "unavailable", 20: "edited"})
        self.assertEqual([m["macro_id"] for m in result.macros], ["55", "56"])


class RequesterTests(unittest.TestCase):
    def pages(self):
        pages = base_pages()
        # Ticket 3 has no macro use, so its requester isn't fetched.
        pages[START]["tickets"] = [{"id": 1, "requester_id": 81}, {"id": 2, "requester_id": 7},
                                   {"id": 9, "status": "deleted", "requester_id": 90}]
        pages["EXPORT2"]["tickets"] = [{"id": 2, "requester_id": 82}, {"id": 3, "requester_id": 83}]
        pages[REQUESTER_USERS] = {"users": [{"id": 81, "name": "Dana Whitfield", "email": "dana@example.net"}]}
        return pages

    def run_fetch(self, errors=None):
        self.messages = []
        self.fake = FakeZendesk(self.pages(), {audits_path(3): 404, "/api/v2/macros/56.json": 404, **(errors or {})})
        return zendesk.fetch_all(self.fake, FETCH_SINCE, log=self.messages.append, workers=2)

    def test_requester_map_and_names(self):
        data = self.run_fetch()
        # The later listing of ticket 2 wins, as the export gives its newest state last.
        self.assertEqual(data["requesters"], {1: "81", 2: "82", 3: "83"})
        self.assertEqual([c for c in self.fake.calls if "users" in c], [AGENT_USERS, REQUESTER_USERS])
        self.assertEqual(data["users"], {"7": {"id": 7, "name": "Ana", "email": "ana@example.com"},
                                         "81": {"id": 81, "name": "Dana Whitfield", "email": "dana@example.net"}})
        self.assertEqual((data["coverage"].names_missing, data["coverage"].requester_names_missing), (False, False))
        self.assertIn("Fetching requester names...", self.messages)

        result = analyze(**data, since=FETCH_SINCE, until=NOW)
        self.assertEqual({u.audit_id: u.requester_id for u in result.uses}, {11: "81", 13: "81", 20: "82"})
        self.assertEqual([a["name"] for a in result.agents], ["Ana"])

    def test_requester_names_zendesk_gave_up_on(self):
        data = self.run_fetch({REQUESTER_USERS: gave_up()})
        self.assertEqual((data["coverage"].names_missing, data["coverage"].requester_names_missing), (False, True))
        self.assertEqual(set(data["users"]), {"7"})
        self.assertEqual(self.messages[-1], "  Zendesk stopped answering, so some requester names are missing "
                                            "from audit.json.")

    def test_no_requester_fetch_once_zendesk_stopped_answering_for_agents(self):
        data = self.run_fetch({AGENT_USERS: gave_up()})
        self.assertEqual((data["coverage"].names_missing, data["coverage"].requester_names_missing), (True, True))
        self.assertNotIn(REQUESTER_USERS, self.fake.calls)


def hand_built():
    """Two macros in one update, a deleted macro, and an unused personal one."""
    macros = {
        "1": use_macro(1, "Thanks for writing in.", created_at="2026-01-01T00:00:00Z", active=True,
                       restriction=None, usage_30d=3),
        "2": use_macro(2, "Your refund is on its way.", title="Refund", active=True,
                       restriction={"type": "Group", "id": 40, "ids": [40, 41]}),
        "3": use_macro(3, "Personal note.", active=False, restriction={"type": "User", "id": 7}),
    }
    audits = [make_audit([1, 2], "Hi Dana, thanks for writing in. Your refund is now on its way.", ticket_id=9),
              make_audit([4], "Hello.", audit_id=6, ticket_id=10, agent=15, created="2026-10-21T00:00:00Z")]
    users = {"7": {"id": 7, "name": "Ana", "email": "ana@example.com"},
             "8": {"id": 8, "name": "Dana Ruiz", "email": "dana@example.net"}}
    coverage = zendesk.Coverage(5, [3], False, False, [4], requester_names_missing=True)
    return analyze(audits, macros, users, {}, SINCE, UNTIL, coverage, {9: "8", 10: "7", 11: "12"})


NO_STATS = {"uses": 0, "compared": 0, "edited": 0, "unedited": 0, "pct_uses_edited": None, "avg_pct_of_macro": None,
            "avg_pct_of_macro_when_edited": None, "public": 0, "internal": 0}

EXPECTED = {
    "schema_version": 1,
    "since": "2026-09-30T00:00:00+00:00",
    "until": "2026-10-30T00:00:00+00:00",
    "subdomain": "acme",
    "sample": False,
    "coverage": {"tickets": 5, "read": 4, "unread": 1, "skipped": 1, "stopped_early": False, "names_missing": False,
                 "requester_names_missing": True},
    "usage_counts_available": True,
    "macros": [
        {"id": 1, "title": "Macro 1", "active": True, "restriction": None, "shared": True,
         "created_at": "2026-01-01T00:00:00Z", "updated_at": "2026-01-01T00:00:00Z", "usage_30d": 3,
         "text": "Thanks for writing in.", "deleted": False,
         "word_spans": [[0, 6], [7, 10], [11, 18], [19, 22]], "uses": 1, "compared": 1, "edited": 1, "unedited": 0,
         "pct_uses_edited": 100.0, "avg_pct_of_macro": 25.0, "avg_pct_of_macro_when_edited": 25.0, "public": 1,
         "internal": 0},
        {"id": 2, "title": "Refund", "active": True, "restriction": {"type": "Group", "ids": [40, 41]},
         "shared": True, "created_at": None, "updated_at": "2026-01-01T00:00:00Z", "usage_30d": None,
         "text": "Your refund is on its way.", "deleted": False,
         "word_spans": [[0, 4], [5, 11], [12, 14], [15, 17], [18, 21], [22, 26]], "uses": 1, "compared": 1, "edited": 1,
         "unedited": 0, "pct_uses_edited": 100.0, "avg_pct_of_macro": 16.7, "avg_pct_of_macro_when_edited": 16.7,
         "public": 1, "internal": 0},
        {"id": 3, "title": "Macro 3", "active": False, "restriction": {"type": "User", "ids": [7]}, "shared": False,
         "created_at": None, "updated_at": "2026-01-01T00:00:00Z", "usage_30d": None, "text": "Personal note.",
         "deleted": False, "word_spans": None, **NO_STATS},
        {"id": 4, "title": "Macro 4", "active": None, "restriction": None, "shared": None, "created_at": None,
         "updated_at": None, "usage_30d": None, "text": None, "deleted": True, "word_spans": None, **NO_STATS, "uses": 1, "public": 1},
    ],
    "uses": [
        {"audit_id": 5, "ticket_id": 9, "used_at": "2026-10-20T00:00:00+00:00", "agent_id": 7, "requester_id": 8,
         "macro_id": 1, "comment_type": "public", "status": "edited", "pct_of_macro": 25.0, "added_text": False,
         "combined_with": [2],
         "diff": [{"op": "delete", "text": "Thanks"}, {"op": "insert", "text": "Hi Dana, thanks"},
                  {"op": "equal", "text": "for writing in."}, {"op": "macro", "text": "Refund", "macro_id": 2}]},
        {"audit_id": 5, "ticket_id": 9, "used_at": "2026-10-20T00:00:00+00:00", "agent_id": 7, "requester_id": 8,
         "macro_id": 2, "comment_type": "public", "status": "edited", "pct_of_macro": 16.7, "added_text": False,
         "combined_with": [1],
         "diff": [{"op": "insert", "text": "Hi Dana, thanks"}, {"op": "macro", "text": "Macro 1", "macro_id": 1},
                  {"op": "equal", "text": "Your refund is"}, {"op": "insert", "text": "now"},
                  {"op": "equal", "text": "on its way."}]},
        {"audit_id": 6, "ticket_id": 10, "used_at": "2026-10-21T00:00:00+00:00", "agent_id": 15, "requester_id": 7,
         "macro_id": 4, "comment_type": "public", "status": "unavailable", "pct_of_macro": None,
         "added_text": False, "combined_with": [], "diff": None},
    ],
    "people": [
        {"id": 7, "name": "Ana", "email": "ana@example.com", "role": "both"},
        {"id": 8, "name": "Dana Ruiz", "email": "dana@example.net", "role": "requester"},
        {"id": 15, "name": None, "email": None, "role": "agent"},
    ],
}


class AuditJsonTests(unittest.TestCase):
    def test_exact_contents(self):
        data = audit_data(hand_built(), "acme")
        generated = datetime.fromisoformat(data.pop("generated_at"))
        self.assertIsNotNone(generated.utcoffset(), "has an offset")
        self.assertLess(abs(generated - datetime.now().astimezone()), timedelta(minutes=1))
        self.assertEqual(data, EXPECTED)

    def test_sample_has_no_subdomain(self):
        data = audit_data(hand_built(), None)
        self.assertEqual((data["subdomain"], data["sample"]), (None, True))

    def test_no_usage_counts(self):
        result = analyze([], {"1": use_macro(1, "Hi.")}, {}, {}, SINCE, UNTIL)
        self.assertFalse(audit_data(result, "acme")["usage_counts_available"])

    def test_written_only_for_the_owner(self):
        with tempfile.TemporaryDirectory() as folder:
            os.chmod(folder, 0o755)
            paths = write_all(Path(folder), hand_built(), "acme")
            self.assertEqual(paths["audit"], Path(folder) / "audit.json")
            self.assertEqual(stat.S_IMODE(paths["audit"].stat().st_mode), 0o600)
            written = json.loads(paths["audit"].read_text(encoding="utf-8"))
        written.pop("generated_at")
        self.assertEqual(written, EXPECTED)

    def test_full_diff_is_never_shortened(self):
        words = [f"word{i}" for i in range(40)]
        result = analyze([make_audit([1], "Something else entirely.")], {"1": use_macro(1, " ".join(words) + ".")},
                         {}, {}, SINCE, UNTIL)
        # The report shortens it.
        self.assertIn("… 24 more words", _render_diff(result.uses[0].diff))
        self.assertEqual(audit_data(result, "acme")["uses"][0]["diff"],
                         [{"op": "delete", "text": " ".join(words) + "."},
                          {"op": "insert", "text": "Something else entirely."}])


class SampleAuditJsonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.data = audit_data(analyze(**generate(SINCE, UNTIL), since=SINCE, until=UNTIL), None)
        cls.macros = {m["id"]: m for m in cls.data["macros"]}

    def summary(self, mid):
        m = self.macros[mid]
        return (m["title"], m["active"], m["restriction"], m["shared"], m["deleted"], m["uses"], m["usage_30d"])

    def test_macro_cases(self):
        self.assertEqual(self.summary(2009), ("Warranty claim steps", True, None, True, False, 0, 0))
        self.assertEqual(self.summary(2010), ("New returns portal", True, None, True, False, 0, 0))
        self.assertEqual(self.summary(2011), ("Dev's refund note", True, {"type": "User", "ids": [1004]}, False,
                                              False, 0, 0))
        self.assertEqual(self.summary(2012), ("Holiday hours 2024", False, None, True, False, 0, 0))
        self.assertEqual(self.summary(2003)[:5], ("Escalate to billing", True, {"type": "Group", "ids": [360001]},
                                                  True, False))
        self.assertEqual(self.summary(2099), ("Holiday hours 2025", None, None, None, True, 18, None))
        month_ago = (UNTIL - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertLess(self.macros[2009]["created_at"], month_ago)
        self.assertGreater(self.macros[2010]["created_at"], month_ago)
        self.assertEqual(sorted(m["id"] for m in self.data["macros"]), [*range(2001, 2013), 2099])

    def test_rewritten_macro_with_the_requester_named(self):
        shipped = self.macros[2008]
        self.assertEqual((shipped["uses"], shipped["edited"], shipped["pct_uses_edited"]), (24, 18, 75.0))
        names = {p["id"]: p["name"] for p in self.data["people"]}
        uses = [u for u in self.data["uses"] if u["macro_id"] == 2008]
        added = [step["text"] for u in uses if u["status"] == "edited" for step in u["diff"] if step["op"] == "insert"
                 and step["text"] in SHIPPED_ADDITIONS]
        self.assertEqual(len(added), 18, "every edit adds one of a few similar sentences")
        for u in (u for u in uses if u["status"] == "edited"):
            first = {"op": "insert", "text": f"Hi {names[u['requester_id']].split()[0]},"}
            if u["combined_with"]:
                first = {"op": "macro", "text": "Closing, no response", "macro_id": 2006}
            self.assertEqual(u["diff"][0], first, u["audit_id"])
        self.assertEqual([u["combined_with"] for u in uses if u["combined_with"]], [[2006], [2006]])
        self.assertEqual({names[u["requester_id"]] for u in uses}, {"Dana Whitfield", "Omar Haddad", "Ines Castillo"})

    def test_every_use_has_a_named_requester(self):
        people = {p["id"]: p for p in self.data["people"]}
        self.assertEqual(sum(u["requester_id"] is None for u in self.data["uses"]), 0)
        self.assertTrue(all(people[u["requester_id"]]["name"] for u in self.data["uses"]))
        self.assertEqual(Counter(p["role"] for p in self.data["people"]), {"agent": 6, "requester": 11})

    def test_full_diffs_have_no_html(self):
        steps = [s for u in self.data["uses"] if u["diff"] for s in u["diff"]]
        self.assertEqual({s["op"] for s in steps}, {"equal", "delete", "insert", "macro"})
        self.assertFalse(any(re.search(r"<|more words", s["text"]) for s in steps))


if __name__ == "__main__":
    unittest.main()
