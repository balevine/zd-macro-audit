import unittest
from collections import Counter

from tests.helpers import SINCE, UNTIL, fields_only_macro, make_audit, run_audit, run_audits, use_macro
from zdaudit.analyze import agent_name, analyze, macro_text
from zdaudit.sample import generate

TEN_WORDS = "One two three four five six seven eight nine ten"
ONE_TYPO = "One two three four TYPO six seven eight nine ten"
TWO_TYPOS = "One TYPO three four OOPS six seven eight nine ten"


class MacroTextTests(unittest.TestCase):
    def test_no_comment_action(self):
        self.assertIsNone(macro_text({"actions": [{"field": "priority", "value": "urgent"}]}))

    def test_blank_comment_counts_as_none(self):
        self.assertIsNone(macro_text({"actions": [{"field": "comment_value", "value": "  \n"}]}))

    def test_older_macros_store_a_list(self):
        action = {"field": "comment_value", "value": ["channel:all", "Hello there"]}
        self.assertEqual(macro_text({"actions": [action]}), "Hello there")
        self.assertIsNone(macro_text({"actions": [{"field": "comment_value", "value": []}]}))

    def test_rich_text_wins_over_plain(self):
        actions = [{"field": "comment_value", "value": "Plain"}, {"field": "comment_value_html", "value": "<p>Rich</p>"}]
        self.assertEqual(macro_text({"actions": actions}).strip(), "Rich")

    def test_blank_rich_text_falls_back_to_plain(self):
        actions = [{"field": "comment_value", "value": "Plain"}, {"field": "comment_value_html", "value": " "}]
        self.assertEqual(macro_text({"actions": actions}), "Plain")


class StatusTests(unittest.TestCase):
    def test_intact(self):
        use = run_audit([use_macro(1, "First part.")], [1], "First part.").uses[0]
        self.assertEqual((use.status, use.pct_of_macro, use.diff, use.added_text), ("unedited", 0.0, None, False))

    def test_intact_with_text_added(self):
        use = run_audit([use_macro(1, "First part.")], [1], "Hello. First part.").uses[0]
        self.assertEqual((use.status, use.added_text), ("unedited", True))

    def test_edited_use_is_never_marked_as_having_text_added(self):
        use = run_audit([use_macro(1, "First part.")], [1], "First piece. Plus some more words.").uses[0]
        self.assertEqual((use.status, use.added_text), ("edited", False))

    def test_deleted_macro_text_counts_as_edited(self):
        result = run_audit([use_macro(1, "Hi {{ticket.requester.first_name}}, done.")], [1])
        use = result.uses[0]
        self.assertEqual((use.status, use.pct_of_macro, use.comment_type), ("edited", 100.0, ""))
        self.assertEqual(use.diff, [("delete", ["Hi", "{{ticket.requester.first_name}},", "done."])])
        self.assertEqual(result.overall["by_type"]["none"]["edited"], 1)

    def test_fields_only_macro_is_not_compared(self):
        use = run_audit([fields_only_macro(1)], [1], "Any reply.").uses[0]
        self.assertEqual((use.status, use.pct_of_macro), ("no_text", None))

    def test_only_placeholders_is_not_counted(self):
        result = run_audit([use_macro(1, "{{ticket.id}}")], [1], "Completely unrelated reply.")
        self.assertEqual(result.uses[0].status, "only_placeholders")
        self.assertIsNone(result.uses[0].pct_of_macro)
        self.assertEqual(result.overall["compared"], 0)

    def test_deleted_macro_uses_the_title_zendesk_recorded(self):
        audit = make_audit([1], "Reply.")
        audit["events"][0]["macro_title"] = "Old title"
        use = run_audits([], [audit]).uses[0]
        self.assertEqual((use.status, use.macro_title), ("unavailable", "Old title"))

    def test_deleted_macro_with_no_title(self):
        self.assertEqual(run_audit([], [12], "Reply.").uses[0].macro_title, "Macro 12")

    def test_macro_reference_without_an_id_is_ignored(self):
        self.assertEqual(run_audit([], [None], "Reply.").uses, [])


class ChangedSinceUseTests(unittest.TestCase):
    def status(self, updated, body="Other words."):
        return run_audit([use_macro(1, "First part.", updated)], [1], body).uses[0].status

    def test_edited_after_use(self):
        self.assertEqual(self.status("2026-10-25T00:00:00Z"), "changed")

    def test_edited_before_or_at_use_is_a_real_edit(self):
        self.assertEqual(self.status("2026-10-19T00:00:00Z"), "edited")
        self.assertEqual(self.status("2026-10-20T00:00:00Z"), "edited")

    def test_match_still_counts_after_a_later_change(self):
        self.assertEqual(self.status("2026-10-25T00:00:00Z", "First part."), "unedited")

    def test_no_updated_date(self):
        macro = use_macro(1, "First part.")
        del macro["updated_at"]
        self.assertEqual(run_audit([macro], [1], "Other words.").uses[0].status, "edited")

    def test_applies_to_one_macro_only(self):
        result = run_audit([use_macro(1, "First part."), use_macro(2, "Second part.", "2026-10-25T00:00:00Z")],
                           [1, 2], "Other words.")
        self.assertEqual([u.status for u in result.uses], ["edited", "changed"])

    def test_left_out_of_averages(self):
        result = run_audit([use_macro(1, "First part.", "2026-10-25T00:00:00Z")], [1], "Other words.")
        self.assertEqual(result.uses[0].status, "changed")
        self.assertIsNone(result.macros[0]["avg_pct_of_macro"])
        self.assertIsNone(result.overall["avg_pct_of_macro"])


class SeveralMacrosTests(unittest.TestCase):
    def test_each_macro_gets_its_own_result(self):
        result = run_audit([use_macro(1, "First part."), use_macro(2, "Second part.")], [1, 2],
                           "First part.\n\nSecond piece.")
        one, two = result.uses
        self.assertEqual((one.status, one.pct_of_macro, one.diff), ("unedited", 0.0, None))
        self.assertEqual(two.status, "edited")
        # The other macro's text shows by name, not as words the agent added.
        self.assertEqual(two.diff, [("macro", ["Macro 1"]), ("equal", ["Second"]), ("delete", ["part."]),
                                    ("insert", ["piece."])])

    def test_other_macro_named_where_it_sits_among_added_words(self):
        result = run_audit([use_macro(1, "One two three four."), use_macro(2, "Middle bit.")], [1, 2],
                           "One two extra Middle bit. more three four.")
        self.assertEqual(result.uses[0].diff, [("equal", ["One", "two"]), ("insert", ["extra"]),
                                               ("macro", ["Macro 2"]), ("insert", ["more"]),
                                               ("equal", ["three", "four."])])

    def test_combined_with_names_the_other_macros(self):
        result = run_audit([use_macro(1, "First part."), use_macro(2, "Second part.")], [1, 2],
                           "First part. Second part.")
        self.assertEqual([u.combined_with for u in result.uses], [["Macro 2"], ["Macro 1"]])

    def test_readable_macro_beside_a_deleted_one(self):
        result = run_audit([use_macro(1, "First part.")], [1, 2], "First part.\n\nText from the deleted macro.")
        self.assertEqual([u.status for u in result.uses], ["unedited", "unavailable"])
        self.assertFalse(result.uses[0].added_text)

    def test_macro_referenced_twice_is_one_use(self):
        result = run_audit([use_macro(1, "First part.")], [1, 1], "First part.")
        self.assertEqual([(u.macro_id, u.status) for u in result.uses], [("1", "unedited")])
        self.assertEqual((result.overall["uses"], result.overall["compared"]), (1, 1))
        self.assertEqual([(m["macro_id"], m["uses"]) for m in result.macros], [("1", 1)])
        self.assertEqual([a["uses"] for a in result.agents], [1])

    def test_repeated_macro_is_named_once_in_combined_with(self):
        result = run_audit([use_macro(1, "First part."), use_macro(2, "Second part.")], [1, 1, 2],
                           "First part. Second part.")
        self.assertEqual([(u.macro_id, u.combined_with) for u in result.uses],
                         [("1", ["Macro 2"]), ("2", ["Macro 1"])])


class PeriodTests(unittest.TestCase):
    def test_only_audits_inside_the_period_count(self):
        # SINCE is 2026-09-30 and UNTIL is 2026-10-30, both at midnight.
        audits = [
            make_audit([1], "First part.", audit_id=1, created="2026-09-29T23:59:59Z"),
            make_audit([1], "First part.", audit_id=2, created="2026-09-30T00:00:00Z"),
            make_audit([1], "First part.", audit_id=3, created="2026-10-30T00:00:00Z"),
            make_audit([1], "First part.", audit_id=4, created="2026-10-30T00:00:01Z"),
        ]
        result = run_audits([use_macro(1, "First part.")], audits)
        self.assertEqual([u.audit_id for u in result.uses], [2, 3])
        self.assertEqual(result.overall["uses"], 2)


class TotalsTests(unittest.TestCase):
    """One small data set with every kind of use, checked number by number."""

    @classmethod
    def setUpClass(cls):
        macros = [use_macro(1, TEN_WORDS, usage_30d=4), fields_only_macro(2)]
        users = {"7": {"name": "Ana", "email": "ana@example.com"}}
        audits = [
            make_audit([1], TEN_WORDS, audit_id=1, agent=7),
            make_audit([1], f"Hello. {TEN_WORDS}", audit_id=2, agent=7),
            make_audit([1], ONE_TYPO, audit_id=3, agent=8),
            make_audit([1], TWO_TYPOS, public=False, audit_id=4, agent=8),
            make_audit([1], None, audit_id=5, agent=9),
            make_audit([2], "Bump.", public=False, audit_id=6, agent=9),
            make_audit([3], "Reply.", audit_id=7, agent=10),
        ]
        cls.result = run_audits(macros, audits, users)

    def test_overall(self):
        o = self.result.overall
        self.assertEqual((o["uses"], o["compared"], o["edited"], o["unedited"], o["added_text"]), (7, 5, 3, 2, 1))
        self.assertEqual((o["pct_uses_edited"], o["pct_uses_intact"]), (60.0, 40.0))
        self.assertEqual(o["macros_used"], 3)
        self.assertEqual(dict(o["excluded"]), {"no_text": 1, "unavailable": 1})

    def test_agents_editing_only_counts_compared_uses(self):
        o = self.result.overall
        self.assertEqual((o["agents_using"], o["agents_editing"], o["pct_agents_editing"]), (3, 2, 66.7))

    def test_averages(self):
        o = self.result.overall
        # 0, 0, 10, 20 and 100 for the use with no comment.
        self.assertEqual((o["avg_pct_of_macro"], o["avg_pct_of_macro_when_edited"]), (26.0, 43.3))

    def test_by_type(self):
        by_type = self.result.overall["by_type"]
        self.assertEqual(by_type["public"], {"compared": 3, "edited": 1, "unedited": 2, "pct_uses_edited": 33.3})
        self.assertEqual(by_type["internal"], {"compared": 1, "edited": 1, "unedited": 0, "pct_uses_edited": 100.0})
        self.assertEqual(by_type["none"], {"compared": 1, "edited": 1, "unedited": 0, "pct_uses_edited": 100.0})

    def test_macro_row(self):
        row = self.result.macros[0]
        self.assertEqual(row, {
            "macro_id": "1", "title": "Macro 1", "uses": 5, "compared": 5, "edited": 3, "unedited": 2,
            "pct_uses_edited": 60.0, "avg_pct_of_macro": 26.0, "avg_pct_of_macro_when_edited": 43.3,
            "public": 3, "internal": 1, "excluded": 0, "zendesk_usage_30d": 4,
        })

    def test_macro_with_nothing_compared(self):
        row = next(m for m in self.result.macros if m["macro_id"] == "2")
        self.assertEqual((row["uses"], row["compared"], row["excluded"], row["internal"]), (1, 0, 1, 1))
        self.assertIsNone(row["pct_uses_edited"])
        self.assertIsNone(row["avg_pct_of_macro"])

    def test_agent_rows(self):
        rows = {r["agent_id"]: r for r in self.result.agents}
        self.assertEqual(rows["7"], {"agent_id": "7", "name": "Ana", "email": "ana@example.com", "uses": 2,
                                     "compared": 2, "edited": 0, "unedited": 2, "pct_uses_edited": 0.0})
        self.assertEqual((rows["8"]["uses"], rows["8"]["edited"], rows["8"]["name"]), (2, 2, "User 8"))
        self.assertEqual((rows["10"]["uses"], rows["10"]["compared"]), (1, 0))

    def test_most_used_come_first(self):
        self.assertEqual([m["macro_id"] for m in self.result.macros][0], "1")
        self.assertEqual([a["uses"] for a in self.result.agents], [2, 2, 2, 1])

    def test_nothing_to_report(self):
        o = run_audits([], []).overall
        self.assertEqual((o["uses"], o["compared"]), (0, 0))
        self.assertIsNone(o["pct_uses_edited"])
        self.assertIsNone(o["avg_pct_of_macro"])


class AgentNameTests(unittest.TestCase):
    def test_falls_back_to_the_id(self):
        self.assertEqual(agent_name({"7": {"name": "Ana"}}, "7"), "Ana")
        self.assertEqual(agent_name({}, "7"), "User 7")
        self.assertEqual(agent_name({"7": {"name": ""}}, "7"), "User 7")


class SampleDataTests(unittest.TestCase):
    def test_sample_covers_the_main_outcomes(self):
        result = analyze(**generate(SINCE, UNTIL), since=SINCE, until=UNTIL)
        statuses = Counter(u.status for u in result.uses)
        for status in ("edited", "unedited", "no_text", "unavailable", "changed"):
            self.assertGreater(statuses[status], 0, status)
        self.assertGreater(result.overall["added_text"], 0)
        self.assertGreater(result.overall["by_type"]["none"]["compared"], 0)

    def test_agent_who_never_edits(self):
        result = analyze(**generate(SINCE, UNTIL), since=SINCE, until=UNTIL)
        erin = next(a for a in result.agents if a["name"] == "Erin Walsh")
        self.assertEqual(erin["edited"], 0)
        self.assertGreater(erin["unedited"], 0)


if __name__ == "__main__":
    unittest.main()
