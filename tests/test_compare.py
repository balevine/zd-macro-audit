import unittest
from datetime import datetime, timedelta, timezone

from zdaudit.analyze import analyze, macro_text
from zdaudit.compare import compare
from zdaudit.sample import generate

GREETING = "Hi {{ticket.requester.first_name}},\n\nThanks for writing in. We fixed it."


class CompareTests(unittest.TestCase):
    def assertUnedited(self, macro, sent, dynamic=None):
        result = compare([macro] if isinstance(macro, str) else macro, sent, dynamic)
        self.assertTrue(result.matched, f"expected a match, got {result.similarity}%")
        self.assertEqual(result.similarity, 100.0)

    def assertEdited(self, macro, sent, dynamic=None):
        result = compare([macro] if isinstance(macro, str) else macro, sent, dynamic)
        self.assertFalse(result.matched)
        self.assertLess(result.similarity, 100)

    def test_placeholder_filled_in(self):
        self.assertUnedited(GREETING, "Hi Priya,\n\nThanks for writing in. We fixed it.")

    def test_placeholder_rendered_empty(self):
        self.assertUnedited(GREETING, "Hi ,\n\nThanks for writing in. We fixed it.")
        self.assertUnedited("Hello\n{{ticket.custom}}\nThanks", "Hello\nThanks")

    def test_whitespace_and_quotes_ignored(self):
        self.assertUnedited("We're on it.  Talk soon.", "We’re on it.\nTalk soon.")

    def test_formatting_ignored(self):
        self.assertUnedited("Click **Forgot password** or see [our guide](https://x.y).",
                            "Click Forgot password or see our guide.")
        self.assertUnedited("Steps:\n1. Open settings\n2. Click reset", "Steps:\nOpen settings\nClick reset")

    def test_html_macro(self):
        macro = macro_text({"actions": [{"field": "comment_value_html",
                                         "value": "<p>Hi {{ticket.requester.first_name}},</p><p>Done.</p>"}]})
        self.assertUnedited(macro, "Hi Sam,\n\nDone.")

    def test_one_word_change_is_an_edit(self):
        self.assertEdited(GREETING, "Hi Priya,\n\nThanks for writing in. We fixed this.")

    def test_text_added_around_macro_is_not_an_edit(self):
        macro = "Thanks for writing in. We fixed it."
        for sent in ("Hi Priya,\n\nThanks for writing in. We fixed it.",
                     "Thanks for writing in. We fixed it.\n\nSam\nAcme Support",
                     "Hi Priya,\n\nThanks for writing in. We fixed it.\n\nSam"):
            self.assertUnedited(macro, sent)
            self.assertTrue(compare([macro], sent).added_text)
        self.assertUnedited(GREETING, "Good news!\n\nHi Priya,\n\nThanks for writing in. We fixed it.\n\nSam")

    def test_macro_alone_has_no_added_text(self):
        self.assertFalse(compare([GREETING], "Hi Priya,\n\nThanks for writing in. We fixed it.").added_text)
        macro = "Thanks.{% if ticket.priority == 'urgent' %} We'll call you today.{% endif %}"
        self.assertFalse(compare([macro], "Thanks. We'll call you today.").added_text)

    def test_change_inside_macro_with_added_text_is_an_edit(self):
        result = compare([GREETING], "Hello!\n\nHi Priya,\n\nThanks for writing in. We fixed this.\n\nSam")
        self.assertFalse(result.matched)
        # The added text doesn't lower the score.
        alone = compare([GREETING], "Hi Priya,\n\nThanks for writing in. We fixed this.")
        self.assertEqual(result.similarity, alone.similarity)

    def test_text_added_inside_macro_is_an_edit(self):
        self.assertEdited("Thanks for writing in. We fixed it.", "Thanks for writing in. Sorry! We fixed it.")

    def test_macro_must_match_whole_words(self):
        self.assertEdited("Thanks", "Thanksgiving hours are posted.")

    def test_case_change_is_an_edit(self):
        self.assertEdited(GREETING, "Hi Priya,\n\nthanks for writing in. We fixed it.")

    def test_liquid_block_is_optional(self):
        macro = "Thanks.{% if ticket.priority == 'urgent' %} We'll call you today.{% endif %}"
        self.assertUnedited(macro, "Thanks.")
        self.assertUnedited(macro, "Thanks. We'll call you today.")

    def test_dynamic_content_variants(self):
        dynamic = {"dc.bye": ["Cheers, {{current_user.first_name}}", "Saludos"]}
        self.assertUnedited("Done. {{dc.bye}}", "Done. Cheers, Ana", dynamic)
        self.assertUnedited("Done. {{dc.bye}}", "Done. Saludos", dynamic)
        self.assertEdited("Done. {{dc.bye}}", "Done. Bye", dynamic)

    def test_multiple_macros_any_order(self):
        self.assertUnedited(["First part.", "Second part."], "Second part.\n\nFirst part.")

    def test_multiple_macros_with_text_between(self):
        macros = ["First part.", "Second part."]
        self.assertUnedited(macros, "Hi!\n\nSecond part.\n\nAlso, one more thing.\n\nFirst part.\n\nSam")
        self.assertEdited(macros, "Hi!\n\nFirst part.\n\nSam")

    def test_more_than_three_macros(self):
        self.assertUnedited(["A one.", "B two.", "C three.", "D four."], "D four. A one. B two. C three.")

    def test_same_macro_applied_twice(self):
        self.assertUnedited([GREETING, GREETING], "Hi Priya,\n\nThanks for writing in. We fixed it.")

    def test_similarity_reflects_size_of_edit(self):
        small = compare([GREETING], "Hi Priya, Thanks for writing in. We fixed this.").similarity
        large = compare([GREETING], "Hi Priya, sorry, still looking into it.").similarity
        self.assertGreater(small, large)

    def test_macro_without_comment(self):
        self.assertIsNone(macro_text({"actions": [{"field": "priority", "value": "urgent"}]}))


class SampleDataTests(unittest.TestCase):
    def test_every_status_appears(self):
        until = datetime(2026, 10, 30, tzinfo=timezone.utc)
        since = until - timedelta(days=30)
        result = analyze(**generate(since, until), since=since, until=until)
        statuses = {u.status for u in result.uses}
        self.assertEqual(statuses, {"edited", "unedited", "no_text", "unavailable", "changed"})
        # Erin never edits in the sample data.
        erin = next(a for a in result.agents if a["name"] == "Erin Walsh")
        self.assertEqual(erin["edited"], 0)
        self.assertGreater(erin["unedited"], 0)

    def test_deleted_macro_text_counts_as_edited(self):
        until = datetime(2026, 10, 30, tzinfo=timezone.utc)
        since = until - timedelta(days=30)
        macro = {"id": 1, "title": "Done", "updated_at": "2026-01-01T00:00:00Z",
                 "actions": [{"field": "comment_value", "value": "Hi {{ticket.requester.first_name}}, done."}]}
        audit = {"id": 5, "ticket_id": 9, "created_at": "2026-10-20T00:00:00Z", "author_id": 7,
                 "events": [{"type": "MacroReference", "macro_id": 1}]}
        result = analyze([audit], {"1": macro}, {}, {}, since, until)
        use = result.uses[0]
        self.assertEqual((use.status, use.similarity, use.comment_type), ("edited", 0.0, ""))
        self.assertEqual(use.diff, [("delete", ["Hi", "{{ticket.requester.first_name}},", "done."])])
        self.assertEqual(result.overall["by_type"]["none"]["edited"], 1)


if __name__ == "__main__":
    unittest.main()
