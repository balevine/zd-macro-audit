import csv
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from zdaudit.analyze import analyze, macro_text
from zdaudit.compare import compare, compile_macro
from zdaudit.report import render_html, write_all
from zdaudit.sample import generate

GREETING = "Hi {{ticket.requester.first_name}},\n\nThanks for writing in. We fixed it."
BODY = "Thanks for writing in. We fixed it."
HTML = lambda markup: macro_text({"actions": [{"field": "comment_value_html", "value": markup}]})


def check(macros, sent, dynamic=None):
    macros = [macros] if isinstance(macros, str) else macros
    return compare([compile_macro(m, dynamic) for m in macros], sent)


class CompareBase(unittest.TestCase):
    def assertUnedited(self, macro, sent, dynamic=None):
        for result in check(macro, sent, dynamic).results:
            self.assertTrue(result.intact, f"expected a match, got {result.pct_edited}% edited")
            self.assertEqual(result.pct_edited, 0.0)

    def assertEdited(self, macro, sent, dynamic=None):
        result = check(macro, sent, dynamic).results[0]
        self.assertFalse(result.intact)
        self.assertGreater(result.pct_edited, 0)


class PlaceholderTests(CompareBase):
    def test_filled_in(self):
        self.assertUnedited(GREETING, "Hi Priya,\n\nThanks for writing in. We fixed it.")

    def test_rendered_empty(self):
        self.assertUnedited(GREETING, "Hi ,\n\nThanks for writing in. We fixed it.")
        self.assertUnedited("Hello\n{{ticket.custom}}\nThanks", "Hello\nThanks")

    def test_with_filter_and_spaces(self):
        self.assertUnedited("Hi {{ ticket.requester.first_name | capitalize }}, done.", "Hi Sam, done.")

    def test_touching_punctuation_and_digits(self):
        self.assertUnedited("Order #{{ticket.id}}.", "Order #1234.")
        self.assertUnedited("See https://x.y/t/{{ticket.id}}/view now.", "See https://x.y/t/1234/view now.")

    def test_within_its_limit(self):
        self.assertUnedited("Hi {{ticket.requester.first_name}}, done.", "Hi Mary Jane Ann, done.")
        self.assertUnedited("From {{ticket.requester.email}} now.", "From jane.doe@mail.example.co.uk now.")
        self.assertUnedited("About {{ticket.title}} now.", "About my order never arrived and nobody replied now.")

    def test_over_its_limit_is_an_edit(self):
        self.assertEdited("Hi {{ticket.requester.first_name}}, done.", "Hi Mary Jane Ann Lee, done.")
        self.assertEdited("Case {{ticket.custom}} now.", "Case one two three four five six now.")

    def test_inserted_text_does_not_pass_as_a_placeholder(self):
        macro = "Hi {{ticket.requester.first_name}}, your refund is approved. Order {{ticket.id}} is closed."
        self.assertEdited(macro, "Hi Sam, your refund is approved. Order 12, which I reopened twice "
                                 "and escalated to my manager, is closed.")
        self.assertEdited(macro, "Hi Sam, I looked into this and the policy says no, your refund is "
                                 "approved. Order 12 is closed.")


class AddedTextTests(CompareBase):
    def test_around_the_macro_is_not_an_edit(self):
        for sent in (f"Hi Priya,\n\n{BODY}", f"{BODY}\n\nSam\nAcme Support", f"Hi Priya,\n\n{BODY}\n\nSam"):
            self.assertUnedited(BODY, sent)
            self.assertTrue(check(BODY, sent).added_text)
        self.assertUnedited(GREETING, f"Good news!\n\nHi Priya,\n\n{BODY}\n\nSam")

    def test_macro_alone_has_no_added_text(self):
        self.assertFalse(check(GREETING, f"Hi Priya,\n\n{BODY}").added_text)
        macro = "Thanks.{% if ticket.priority == 'urgent' %} We'll call you today.{% endif %}"
        self.assertFalse(check(macro, "Thanks. We'll call you today.").added_text)
        self.assertFalse(check(macro, "Thanks.").added_text)

    def test_placeholder_at_the_end_claims_its_value(self):
        self.assertFalse(check("Thanks, {{current_user.first_name}}", "Thanks, Sam").added_text)
        self.assertTrue(check("Thanks, {{current_user.first_name}}", "Thanks, Sam Lee from the Acme team").added_text)

    def test_placeholder_at_the_start_claims_its_value(self):
        self.assertFalse(check("{{ticket.requester.first_name}}, thanks.", "Sam, thanks.").added_text)

    def test_outer_placeholder_does_not_take_another_macros_text(self):
        comparison = check(["Thanks, {{current_user.first_name}}", "Second part."], "Thanks, Sam Second part.")
        self.assertEqual([r.intact for r in comparison.results], [True, True])
        self.assertFalse(comparison.added_text)

    def test_inside_the_macro_is_an_edit(self):
        self.assertEdited(BODY, "Thanks for writing in. Sorry! We fixed it.")

    def test_macro_must_match_whole_words(self):
        self.assertEdited("Thanks", "Thanksgiving hours are posted.")


class EditTests(CompareBase):
    def test_one_word_change(self):
        self.assertEdited(GREETING, "Hi Priya,\n\nThanks for writing in. We fixed this.")

    def test_case_change(self):
        self.assertEdited(GREETING, "Hi Priya,\n\nthanks for writing in. We fixed it.")

    def test_reordered(self):
        self.assertEdited("One two three. Four five six.", "Four five six. One two three.")

    def test_added_text_does_not_lower_the_score(self):
        alone = check(GREETING, "Hi Priya,\n\nThanks for writing in. We fixed this.").results[0]
        padded = check(GREETING, "Hello!\n\nHi Priya,\n\nThanks for writing in. We fixed this.\n\nSam").results[0]
        self.assertEqual(alone.pct_edited, padded.pct_edited)

    def test_score_reflects_size_of_edit(self):
        small = check(GREETING, "Hi Priya, Thanks for writing in. We fixed this.").results[0].pct_edited
        large = check(GREETING, "Hi Priya, sorry, still looking into it.").results[0].pct_edited
        self.assertLess(small, large)

    def pct(self, macro, sent):
        return check(macro, sent).results[0].pct_edited

    def test_changed_word_counts_once(self):
        self.assertEqual(self.pct("One two three four five six seven eight nine ten",
                                  "One two three four TYPO six seven eight nine ten"), 10.0)

    def test_inserted_words_count_by_how_many(self):
        macro = "One two three four five six seven eight nine ten"
        self.assertEqual(self.pct(macro, "One two three four five extra six seven eight nine ten"), 10.0)
        self.assertEqual(self.pct(macro, "One two three four five extra words here six seven eight nine ten"), 30.0)

    def test_long_insertion_stops_at_100(self):
        filler = " ".join(["word"] * 50)
        self.assertEqual(self.pct("One two three four", f"One two {filler} three four"), 100.0)

    def test_replacement_counts_its_longer_side(self):
        self.assertEqual(self.pct("One two three four five six seven eight nine ten",
                                  "One two three X Y Z Q six seven eight nine ten"), 40.0)

    def test_filled_placeholder_is_not_counted(self):
        self.assertEqual(self.pct(GREETING, "Hi Priya, Thanks for writing in. We fixed this."), 12.5)

    def test_placeholder_over_its_limit_counts_only_the_extra(self):
        macro = "Case {{ticket.custom}} is open and waiting for a reply"
        self.assertEqual(self.pct(macro, "Case one two three four five six seven is open and waiting for a reply"), 25.0)

    def test_rewrite_is_100(self):
        self.assertEqual(self.pct(BODY, "Completely different words sent instead."), 100.0)

    def test_punctuation_only_change_is_under_1(self):
        self.assertEqual(self.pct("Thanks for writing!", "Thanks for writing."), 0.1)

    def test_empty_comment(self):
        self.assertEdited(BODY, "")


class FormattingTests(CompareBase):
    def test_whitespace_and_quotes(self):
        self.assertUnedited("We're on it.  Talk soon.", "We’re on it.\nTalk soon.")

    def test_markdown(self):
        self.assertUnedited("Click **Forgot password** or see [our guide](https://x.y).",
                            "Click Forgot password or see our guide.")
        self.assertUnedited("Steps:\n1. Open settings\n2. Click reset", "Steps:\nOpen settings\nClick reset")

    def test_html_macro(self):
        self.assertUnedited(HTML("<p>Hi {{ticket.requester.first_name}},</p><p>Done.</p>"), "Hi Sam,\n\nDone.")

    def test_html_macro_sent_as_markdown(self):
        for markup, sent in (
            ("<p>Please <em>do not</em> reply.</p>", "Please *do not* reply."),
            ("<p>Please <em>do not</em> reply.</p>", "Please _do not_ reply."),
            ("<p>Run <code>reset</code> now.</p>", "Run `reset` now."),
            ('<p>See <a href="https://x.y">guide</a>.</p>', "See [guide](https://x.y)."),
            ('<p>Look <img src="u.png" alt="shot"> here.</p>', "Look ![shot](u.png) here."),
            ("<p>A</p><hr><p>B</p>", "A\n\n---\n\nB"),
        ):
            self.assertUnedited(HTML(markup), sent)

    def test_escaped_markdown(self):
        self.assertUnedited("Cost is 5*3 and a_b.", r"Cost is 5\*3 and a\_b.")

    def test_invisible_characters(self):
        self.assertUnedited("Thanks for writing.", "Thanks⁠ for﻿ wri­ting.")


class LiquidTests(CompareBase):
    MACRO = "Thanks.{% if ticket.priority == 'urgent' %} We'll call you today.{% endif %} Bye."

    def test_block_is_optional(self):
        self.assertUnedited(self.MACRO, "Thanks. Bye.")
        self.assertUnedited(self.MACRO, "Thanks. We'll call you today. Bye.")

    def test_rewritten_block_is_an_edit(self):
        self.assertEdited(self.MACRO, "Thanks. Nobody will ever call you. Bye.")

    def test_else_branch_is_one_of_the_options(self):
        macro = "A.{% if x %} B.{% elsif y %} C.{% else %} D.{% endif %} E."
        for sent in ("A. B. E.", "A. C. E.", "A. D. E."):
            self.assertUnedited(macro, sent)
        self.assertEdited(macro, "A. E.")  # with an else, one branch always renders

    def test_case_block(self):
        macro = "A.{% case x %}ignored{% when 1 %} B.{% when 2 %} C.{% endcase %} D."
        self.assertUnedited(macro, "A. C. D.")
        self.assertUnedited(macro, "A. D.")
        self.assertEdited(macro, "A. ignored D.")

    def test_nested_blocks(self):
        macro = "A{% if x %} B{% if y %} C{% endif %} D{% endif %} E"
        for sent in ("A E", "A B D E", "A B C D E"):
            self.assertUnedited(macro, sent)
        self.assertEdited(macro, "A B E")

    def test_blocks_that_render_nothing(self):
        macro = "A.{% capture x %}hidden{% endcapture %}{% comment %}note{% endcomment %}{% assign y = 1 %} B."
        self.assertUnedited(macro, "A. B.")


class DynamicContentTests(CompareBase):
    def test_variants(self):
        dynamic = {"dc.bye": ["Cheers, {{current_user.first_name}}", "Saludos"]}
        self.assertUnedited("Done. {{dc.bye}}", "Done. Cheers, Ana", dynamic)
        self.assertUnedited("Done. {{dc.bye}}", "Done. Saludos", dynamic)
        self.assertEdited("Done. {{dc.bye}}", "Done. Bye", dynamic)

    def test_html_variant(self):
        dynamic = {"dc.reply": ["<p>Hello there.</p><p>Bye.</p>"]}
        self.assertUnedited("{{dc.reply}}", "Hello there.\n\nBye.", dynamic)

    def test_only_a_known_variant_is_comparable(self):
        self.assertTrue(compile_macro("{{dc.x}}", {"dc.x": ["Hello"]}).comparable)
        self.assertFalse(compile_macro("{{dc.x}}", {}).comparable)
        self.assertFalse(compile_macro("{{dc.x}}", {"dc.x": ["Hello", "{{ticket.id}}"]}).comparable)

    def test_unknown_dynamic_content_beside_fixed_text(self):
        self.assertUnedited("Hi. {{dc.unknown}} Bye.", "Hi. A long paragraph we could not read. Bye.")


class ComparableTests(unittest.TestCase):
    def test_nothing_certain_to_compare(self):
        for macro in ("{{ticket.id}}", "{{a}} {{b}},", "{% if x %}Only sometimes{% endif %}"):
            self.assertFalse(compile_macro(macro).comparable, macro)

    def test_fixed_text_is_comparable(self):
        for macro in ("Hi {{a}}", "Thanks!", "{% if x %}A{% else %}B{% endif %}"):
            self.assertTrue(compile_macro(macro).comparable, macro)


class SeveralMacrosTests(CompareBase):
    def test_any_order(self):
        self.assertUnedited(["First part.", "Second part."], "Second part.\n\nFirst part.")

    def test_more_than_three(self):
        self.assertUnedited(["A one.", "B two.", "C three.", "D four."], "D four. A one. B two. C three.")

    def test_text_between(self):
        self.assertUnedited(["First part.", "Second part."],
                            "Hi!\n\nSecond part.\n\nAlso, one more thing.\n\nFirst part.\n\nSam")

    def test_judged_separately(self):
        comparison = check(["First part.", "Second part."], "First part.\n\nSecond piece.")
        self.assertTrue(comparison.results[0].intact)
        self.assertFalse(comparison.results[1].intact)

    def test_no_added_text_when_only_macros(self):
        self.assertFalse(check(["First part.", "Second part."], "Second part. First part.").added_text)
        self.assertTrue(check(["First part.", "Second part."], "Second part. Hi. First part.").added_text)

    def test_shared_sentence_goes_to_one_macro(self):
        one = "Thanks for your patience. We will update you tomorrow."
        two = "We will update you tomorrow. Please keep this open."
        comparison = check([one, two], "Thanks for your patience. We will update you tomorrow. Please keep this open.")
        self.assertEqual(sum(r.intact for r in comparison.results), 1)
        self.assertFalse(comparison.added_text)
        both = check([one, two], f"{one} {two}")
        self.assertEqual(sum(r.intact for r in both.results), 2)

    def test_short_macro_inside_a_longer_one(self):
        short, long = "Thanks for your patience.", "Thanks for your patience. We will update you tomorrow."
        for macros in ([short, long], [long, short]):
            comparison = check(macros, long)
            results = dict(zip(macros, comparison.results))
            self.assertTrue(results[long].intact)
            self.assertFalse(results[short].intact)
            self.assertEqual(results[short].pct_edited, 100.0)
            self.assertFalse(comparison.added_text)

    def test_placement_keeps_the_most_macros_intact(self):
        # The longest macro could take the middle, which would leave no room for the others.
        left, right, middle = "A B C.", "E F G.", "C. D E"
        comparison = check([middle, left, right], "A B C. D E F G.")
        self.assertEqual([r.intact for r in comparison.results], [False, True, True])

    def test_edited_macros_do_not_share_words(self):
        one, two = "Alpha beta gamma delta.", "Alpha beta gamma epsilon."
        comparison = check([one, two], "Alpha beta gamma zeta.")
        self.assertEqual(sorted(r.pct_edited for r in comparison.results), [25.0, 100.0])


def use_macro(mid, text, updated="2026-01-01T00:00:00Z"):
    return {"id": mid, "title": f"Macro {mid}", "updated_at": updated,
            "actions": [{"field": "comment_value", "value": text}]}


def run_audit(macros, refs, body=None):
    until = datetime(2026, 10, 30, tzinfo=timezone.utc)
    events = [{"type": "MacroReference", "macro_id": mid} for mid in refs]
    if body is not None:
        events.append({"type": "Comment", "body": body, "public": True})
    audit = {"id": 5, "ticket_id": 9, "created_at": "2026-10-20T00:00:00Z", "author_id": 7, "events": events}
    return analyze([audit], {str(m["id"]): m for m in macros}, {}, {}, until - timedelta(days=30), until)


class AnalyzeTests(unittest.TestCase):
    def test_every_status_appears_in_sample(self):
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
        result = run_audit([use_macro(1, "Hi {{ticket.requester.first_name}}, done.")], [1])
        use = result.uses[0]
        self.assertEqual((use.status, use.pct_edited, use.comment_type), ("edited", 100.0, ""))
        self.assertEqual(use.diff, [("delete", ["Hi", "{{ticket.requester.first_name}},", "done."])])
        self.assertEqual(result.overall["by_type"]["none"]["edited"], 1)

    def test_each_macro_gets_its_own_result(self):
        result = run_audit([use_macro(1, "First part."), use_macro(2, "Second part.")], [1, 2],
                           "First part.\n\nSecond piece.")
        one, two = result.uses
        self.assertEqual((one.status, one.pct_edited, one.diff), ("unedited", 0.0, None))
        self.assertEqual(two.status, "edited")
        # The diff shows only this macro's changes, not the other macro's text.
        self.assertEqual(two.diff, [("equal", ["Second"]), ("delete", ["part."]), ("insert", ["piece."])])

    def test_changed_since_applies_to_one_macro(self):
        result = run_audit([use_macro(1, "First part."), use_macro(2, "Second part.", "2026-10-25T00:00:00Z")],
                           [1, 2], "Other words.")
        self.assertEqual([u.status for u in result.uses], ["edited", "changed"])

    def test_readable_macro_beside_a_deleted_one(self):
        result = run_audit([use_macro(1, "First part.")], [1, 2], "First part.\n\nText from the deleted macro.")
        self.assertEqual([u.status for u in result.uses], ["unedited", "unavailable"])
        self.assertFalse(result.uses[0].added_text)

    def test_only_placeholders_is_not_counted(self):
        result = run_audit([use_macro(1, "{{ticket.id}}")], [1], "Completely unrelated reply.")
        self.assertEqual(result.uses[0].status, "only_placeholders")
        self.assertIsNone(result.uses[0].pct_edited)
        self.assertEqual(result.overall["compared"], 0)

    def test_macro_applied_twice_needs_one_copy(self):
        result = run_audit([use_macro(1, "First part.")], [1, 1], "First part.")
        self.assertEqual([u.status for u in result.uses], ["unedited", "unedited"])

    def test_macro_averages(self):
        until = datetime(2026, 10, 30, tzinfo=timezone.utc)
        macro = use_macro(1, "One two three four five six seven eight nine ten")
        audit = lambda i, body: {"id": i, "ticket_id": i, "created_at": "2026-10-20T00:00:00Z", "author_id": 7,
                                 "events": [{"type": "MacroReference", "macro_id": 1},
                                            {"type": "Comment", "body": body, "public": True}]}
        audits = [audit(1, "One two three four five six seven eight nine ten"),
                  audit(2, "One two three four TYPO six seven eight nine ten"),
                  audit(3, "One TYPO three four OOPS six seven eight nine ten")]
        result = analyze(audits, {"1": macro}, {}, {}, until - timedelta(days=30), until)
        row = result.macros[0]
        self.assertEqual((row["avg_pct_of_macro"], row["avg_pct_of_macro_when_edited"]), (10.0, 15.0))
        self.assertEqual(result.overall["avg_pct_of_macro"], 10.0)

    def test_macro_changed_since_use_is_left_out_of_averages(self):
        result = run_audit([use_macro(1, "First part.", "2026-10-25T00:00:00Z")], [1], "Other words.")
        self.assertEqual(result.uses[0].status, "changed")
        self.assertIsNone(result.macros[0]["avg_pct_of_macro"])

    def test_macro_without_comment_text(self):
        self.assertIsNone(macro_text({"actions": [{"field": "priority", "value": "urgent"}]}))


class ReportTests(unittest.TestCase):
    def test_uses_link_to_their_tickets(self):
        html = render_html(run_audit([use_macro(1, "First part.")], [1], "Other words."), "acme")
        self.assertIn("https://acme.zendesk.com/agent/tickets/9", html)
        self.assertIn('data-macro="1"', html)

    def test_comment_text_cannot_break_out_of_the_page_data(self):
        html = render_html(run_audit([use_macro(1, "First part.")], [1], "</script><b>hi</b>"), "acme")
        self.assertNotIn("</script><b>", html)
        self.assertEqual(html.count("</script>"), 2)

    def test_csvs_have_the_new_columns(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = write_all(Path(folder), run_audit([use_macro(1, "First part.")], [1], "Other words."), "acme")
            use = next(csv.DictReader(paths["uses"].read_text(encoding="utf-8").splitlines()))
            macro = next(csv.DictReader(paths["macros"].read_text(encoding="utf-8").splitlines()))
        self.assertEqual(use["pct_of_macro_edited"], "100.0")
        self.assertEqual(macro["avg_pct_of_macro_when_edited"], "100.0")


if __name__ == "__main__":
    unittest.main()
