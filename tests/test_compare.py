import time
import unittest

from zdaudit.analyze import macro_text
from zdaudit.compare import MAX_EXACT_MACROS, _place, compare, compile_macro, html_to_text, is_placeholder, macro_marker

GREETING = "Hi {{ticket.requester.first_name}},\n\nThanks for writing in. We fixed it."
BODY = "Thanks for writing in. We fixed it."
HTML = lambda markup: macro_text({"actions": [{"field": "comment_value_html", "value": markup}]})


def check(macros, sent, dynamic=None):
    macros = [macros] if isinstance(macros, str) else macros
    return compare([compile_macro(m, dynamic) for m in macros], sent)


class CompareBase(unittest.TestCase):
    def assertUnedited(self, macro, sent, dynamic=None):
        for result in check(macro, sent, dynamic).results:
            self.assertTrue(result.intact, f"expected a match, got {result.pct_of_macro}% edited")
            self.assertEqual(result.pct_of_macro, 0.0)

    def assertEdited(self, macro, sent, dynamic=None):
        result = check(macro, sent, dynamic).results[0]
        self.assertFalse(result.intact)
        self.assertGreater(result.pct_of_macro, 0)


class PlaceholderTests(CompareBase):
    def test_filled_in(self):
        self.assertUnedited(GREETING, "Hi Priya,\n\nThanks for writing in. We fixed it.")

    def test_rendered_empty(self):
        self.assertUnedited(GREETING, "Hi ,\n\nThanks for writing in. We fixed it.")
        self.assertUnedited("Hello\n{{ticket.custom}}\nThanks", "Hello\nThanks")

    def test_limit_for_each_kind(self):
        for placeholder, limit in (
            ("{{ticket.description}}", 200), ("{{ticket.latest_comment}}", 200),
            ("{{current_user.signature}}", 40),
            ("{{ticket.url}}", 15), ("{{ticket.link}}", 15), ("{{ticket.title}}", 15), ("{{ticket.subject}}", 15),
            ("{{ticket.requester.email}}", 8),
            ("{{ticket.requester.phone}}", 6),
            ("{{ticket.requester.first_name}}", 3), ("{{ticket.requester.last_name}}", 3),
            ("{{ticket.requester.name}}", 6),
            ("{{ticket.id}}", 3), ("{{id}}", 3),
            ("{{ticket.ticket_field_123}}", 15), ("{{ ticket.ticket_field_123 | upcase }}", 15),
            ("{{ticket.ticket_field_option_title_123}}", 15),
            ("{{ticket.ticket_field_123_extra}}", 5), ("{{ticket.my_ticket_field_123}}", 5),
            ("{{ticket.status}}", 5), ("{{ticket.valid}}", 5),
            ("{{dc.unknown}}", 100),
            ("{% for c in ticket.comments %}{{c.value}}{% endfor %}", 30),
        ):
            with self.subTest(placeholder):
                macro = f"Start {placeholder} end."
                self.assertUnedited(macro, f"Start {'w ' * limit}end.")
                self.assertEdited(macro, f"Start {'w ' * (limit + 1)}end.")

    def test_punctuation_in_a_value_does_not_use_up_the_limit(self):
        self.assertUnedited("Hi {{ticket.requester.first_name}}, done.", "Hi (Mary) Jane, Ann!, done.")

    def test_hyphenated_and_apostrophe_words_count_once(self):
        macro = "Hi {{ticket.requester.first_name}}, done."
        self.assertUnedited(macro, "Hi Mary-Jane O'Neil Lee, done.")
        self.assertEdited(macro, "Hi Mary-Jane O'Neil Lee Ann, done.")

    def test_curly_apostrophe_counts_once_too(self):
        self.assertUnedited("Hi {{ticket.requester.first_name}}, done.", "Hi O’Neil D’Arcy Lee, done.")

    def test_spaced_hyphen_does_not_join_words(self):
        macro = "Hi {{ticket.requester.first_name}}, done."
        self.assertUnedited(macro, "Hi Mary - Jane Ann, done.")
        self.assertEdited(macro, "Hi Mary - Jane Ann Lee, done.")
        self.assertEdited(macro, "Hi Mary- Jane Ann Lee, done.")
        self.assertEdited(macro, "Hi Mary -Jane Ann Lee, done.")

    def test_quotes_around_a_word_do_not_join_it(self):
        macro = "Hi {{ticket.requester.first_name}}, done."
        self.assertEdited(macro, "Hi Mary 'Jane' Ann Lee, done.")

    def test_outer_placeholder_counts_joined_words_once(self):
        self.assertFalse(check("Thanks, {{current_user.first_name}}", "Thanks, Mary-Jane O'Neil Lee").added_text)
        self.assertTrue(check("Thanks, {{current_user.first_name}}", "Thanks, Mary-Jane O'Neil Lee Ann").added_text)
        self.assertFalse(check("{{ticket.requester.first_name}}, thanks.", "Mary-Jane O'Neil Lee, thanks.").added_text)
        self.assertTrue(check("{{ticket.requester.first_name}}, thanks.", "Hello Mary-Jane O'Neil Lee, thanks.").added_text)

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

    def test_placeholder_at_the_start_stops_at_its_limit(self):
        macro = "{{ticket.requester.first_name}}, thanks."
        self.assertFalse(check(macro, "Mary Jane Ann, thanks.").added_text)
        self.assertTrue(check(macro, "Hello Mary Jane Ann, thanks.").added_text)

    def test_placeholder_at_the_end_stops_at_its_limit(self):
        macro = "Thanks, {{current_user.first_name}}"
        self.assertFalse(check(macro, "Thanks, Mary Jane Ann").added_text)
        self.assertTrue(check(macro, "Thanks, Mary Jane Ann Lee").added_text)

    def test_punctuation_around_the_macro_is_not_added_text(self):
        self.assertFalse(check(BODY, f"-- {BODY} --").added_text)

    def test_placeholder_stops_at_another_macros_text(self):
        comparison = check(["Thanks, {{current_user.name}}", "Second part."], "Thanks, Sam Second part. Extra")
        self.assertEqual([r.intact for r in comparison.results], [True, True])
        self.assertTrue(comparison.added_text)

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

    def test_added_text_does_not_change_the_percent(self):
        alone = check(GREETING, "Hi Priya,\n\nThanks for writing in. We fixed this.").results[0]
        padded = check(GREETING, "Hello!\n\nHi Priya,\n\nThanks for writing in. We fixed this.\n\nSam").results[0]
        self.assertEqual((alone.pct_of_macro, padded.pct_of_macro), (12.5, 12.5))

    def pct(self, macro, sent):
        return check(macro, sent).results[0].pct_of_macro

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

    def test_custom_field_fills_up_to_15_words(self):
        macro = "Case {{ticket.ticket_field_123}} is open and waiting for a reply"
        within = check(macro, f"Case {'w ' * 15}is open and waiting for a reply").results[0]
        over = check(macro, f"Case {'w ' * 16}is open and waiting for a reply").results[0]
        self.assertEqual((within.intact, within.pct_of_macro), (True, 0.0))
        self.assertEqual((over.intact, over.pct_of_macro), (False, 12.5))

    def test_rewrite_is_100(self):
        self.assertEqual(self.pct(BODY, "Completely different words sent instead."), 100.0)

    def test_punctuation_only_change_is_under_1(self):
        self.assertEqual(self.pct("Thanks for writing!", "Thanks for writing."), 0.1)

    def test_empty_comment(self):
        self.assertEdited(BODY, "")

    def test_insertion_at_a_placeholder_counts_beyond_its_limit_only(self):
        macro = "One two {{ticket.requester.first_name}} three four"
        self.assertEqual(self.pct(macro, "One two A B C D E three four"), 50.0)

    def test_percent_counts_a_joined_name_once_at_a_placeholder(self):
        macro = "One two {{ticket.requester.first_name}} three four"
        self.assertEqual(self.pct(macro, "ONE two Mary-Jane O'Neil Lee Ann three four"), 50.0)

    def test_change_at_the_edge_counts_removed_words_only(self):
        macro = "One two three four five six seven eight nine ten"
        self.assertEqual(self.pct(macro, "ONE two three four five six seven eight nine ten"), 10.0)
        self.assertEqual(self.pct(macro, "One two three four five six seven eight nine"), 10.0)

    def test_percent_rounds_to_one_decimal(self):
        self.assertEqual(self.pct("One two three", "One TWO three"), 33.3)

    def test_percent_uses_the_liquid_branch_that_was_sent(self):
        macro = "One two three.{% if x %} Four five.{% else %} Six seven.{% endif %} Eight nine ten."
        self.assertEqual(self.pct(macro, "One two three. Six seven. Eight nine TEN."), 12.5)
        self.assertEqual(self.pct(macro, "One two three. Four five. Eight nine TEN."), 12.5)

    def test_percent_prefers_the_shorter_branch_when_neither_was_sent(self):
        macro = "One two three.{% if x %} Four five six seven eight.{% endif %} Nine ten eleven."
        self.assertEqual(self.pct(macro, "One two three. Nine ten ELEVEN."), 16.7)

    def test_diff_shows_only_what_differs(self):
        result = check("One two three.", "One two three four.").results[0]
        self.assertEqual(result.diff, [("equal", ["One", "two"]), ("delete", ["three."]), ("insert", ["three", "four."])])

    def test_diff_with_only_a_removal_or_an_insertion(self):
        self.assertEqual(check("One two three", "One three").results[0].diff,
                         [("equal", ["One"]), ("delete", ["two"]), ("equal", ["three"])])
        self.assertEqual(check("One three", "One two three").results[0].diff,
                         [("equal", ["One"]), ("insert", ["two"]), ("equal", ["three"])])

    def test_diff_keeps_placeholders_visible(self):
        result = check("Hi {{ticket.requester.first_name}} bye", "Hello Sam bye").results[0]
        self.assertIn(("delete", ["Hi", "{{ticket.requester.first_name}}"]), result.diff)

    def test_diff_marks_another_edited_macros_words(self):
        first = "Thanks for reaching out about your order today."
        second = "Please reply with your account number so we can help."
        sent = "Thanks for reaching out about the order. Please reply with the account number so we can help."
        one, two = check([first, second], sent).results
        self.assertEqual(one.diff, [("equal", ["Thanks", "for", "reaching", "out", "about"]),
                                    ("delete", ["your", "order", "today."]), ("insert", ["the", "order.", "\x001\x00"])])
        self.assertEqual(two.diff, [("insert", ["\x000\x00"]), ("equal", ["Please", "reply", "with"]), ("delete", ["your"]), ("insert", ["the"]),
                                    ("equal", ["account", "number", "so", "we", "can", "help."])])

    def test_diff_marks_an_intact_macro_joined_with_no_space(self):
        # Zendesk inserts macros applied one after another with no space between them.
        sent = "Thanks for writing in.Please reply with the account number. Cheers"
        intact, edited = check(["Thanks for writing in.", "Please reply with your account number."], sent).results
        self.assertTrue(intact.intact)
        self.assertEqual(edited.diff, [("insert", ["\x000\x00"]), ("equal", ["Please", "reply", "with"]), ("delete", ["your"]), ("insert", ["the"]),
                                       ("equal", ["account", "number."]), ("insert", ["Cheers"])])

    def test_diff_marks_a_macro_inside_another_once(self):
        outer = "Here is an update on your request. Our engineers are working on a fix now."
        inner = "We will email you \u2014 as soon as it is resolved."  # a lone dash inside it stays part of it
        sent = "Here is an update on your request. We will email you \u2014 as soon as it is resolved. Our engineers are working on a fix now."
        edited, intact = check([outer, inner], sent).results
        self.assertTrue(intact.intact)
        self.assertEqual(edited.pct_of_macro, 0.1)
        self.assertEqual(edited.diff, [("equal", ["Here", "is", "an", "update", "on", "your", "request."]),
                                       ("insert", ["\x001\x00"]),
                                       ("equal", ["Our", "engineers", "are", "working", "on", "a", "fix", "now."])])

    def test_macro_marker(self):
        self.assertEqual([macro_marker(w) for w in ("\x000\x00", "\x0012\x00", "12", "\x00a\x00")], [0, 12, None, None])


class FormattingTests(CompareBase):
    def test_whitespace_and_quotes(self):
        self.assertUnedited("We're on it.  Talk soon.", "We’re on it.\nTalk soon.")

    def test_markdown(self):
        self.assertUnedited("Click **Forgot password** or see [our guide](https://x.y).",
                            "Click Forgot password or see our guide.")
        self.assertUnedited("Steps:\n1. Open settings\n2. Click reset", "Steps:\nOpen settings\nClick reset")

    def test_html_macro(self):
        self.assertUnedited(HTML("<p>Hi {{ticket.requester.first_name}},</p><p>Done.</p>"), "Hi Sam,\n\nDone.")

    def test_html_blocks_separate_words(self):
        self.assertUnedited(HTML("<p>one</p>two"), "one two")
        self.assertUnedited(HTML("one<p>two</p>"), "one two")
        self.assertEqual(html_to_text("<b>one</b>two"), "onetwo")
        self.assertEqual(html_to_text("a &amp; b<br>c"), "a & b\nc")

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

    def test_else_after_a_nested_block_belongs_to_the_outer_block(self):
        macro = "A{% if x %} B{% if y %} C{% endif %}{% else %} D{% endif %} E"
        for sent in ("A B E", "A B C E", "A D E"):
            self.assertUnedited(macro, sent)
        self.assertEdited(macro, "A E")

    def test_closing_an_outer_block_closes_unfinished_inner_ones(self):
        self.assertUnedited("A{% if x %} B{% if y %} C{% endif x %}{% endif %} D", "A D")
        self.assertUnedited("A{% if x %} B{% unless y %} C{% endif %} D", "A D")

    def test_text_after_closing_an_outer_block_is_required(self):
        macro = "A{% if x %} B{% unless y %} C{% endif %} D"
        self.assertUnedited(macro, "A B C D")
        self.assertEdited(macro, "A")

    def test_stray_closing_tag_is_ignored(self):
        self.assertUnedited("A.{% endif %} B.", "A. B.")

    def test_for_loop_is_not_read_as_a_choice(self):
        macro = "A.{% for c in x %} one two{% endfor %} B."
        self.assertUnedited(macro, "A. something else B.")
        self.assertFalse(check(macro, "A. something else B.").added_text)

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

    def test_dynamic_content_inside_dynamic_content(self):
        dynamic = {"dc.a": ["One {{dc.b}}"], "dc.b": ["two"]}
        self.assertUnedited("{{dc.a}}", "One two", dynamic)
        self.assertEdited("{{dc.a}}", "One three", dynamic)

    def test_nesting_stops_after_three_levels(self):
        dynamic = {"dc.a": ["A {{dc.b}}"], "dc.b": ["B {{dc.c}}"], "dc.c": ["C {{dc.d}}"], "dc.d": ["D"]}
        # The fourth level is treated as dynamic content we couldn't read, so any text fits.
        self.assertUnedited("{{dc.a}}", "A B C anything at all", dynamic)

    def test_dynamic_content_that_includes_itself_finishes(self):
        self.assertTrue(compile_macro("{{dc.a}}", {"dc.a": ["Loop {{dc.a}}"]}).comparable)

    def test_placeholder_name_is_not_case_sensitive(self):
        self.assertUnedited("{{DC.Bye}}", "Saludos", {"dc.bye": ["Saludos"]})

    def test_plain_placeholder_is_not_looked_up_as_dynamic_content(self):
        self.assertUnedited("Hi {{ticket.id}}.", "Hi 12.", {"ticket.id": ["Never used"]})

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
            self.assertEqual(results[short].pct_of_macro, 100.0)
            self.assertFalse(comparison.added_text)

    def test_placement_keeps_the_most_macros_intact(self):
        # The longest macro could take the middle, which would leave no room for the others.
        left, right, middle = "A B C.", "E F G.", "C. D E"
        comparison = check([middle, left, right], "A B C. D E F G.")
        self.assertEqual([r.intact for r in comparison.results], [False, True, True])

    def test_edited_macros_do_not_share_words(self):
        one, two = "Alpha beta gamma delta.", "Alpha beta gamma epsilon."
        comparison = check([one, two], "Alpha beta gamma zeta.")
        self.assertEqual(sorted(r.pct_of_macro for r in comparison.results), [25.0, 100.0])


class PlacementTests(CompareBase):
    def test_covers_the_most_text_when_a_placeholder_could_end_in_two_places(self):
        self.assertFalse(check("Hi {{ticket.custom}} bye", "Hi a bye b bye").added_text)

    def test_placed_macro_takes_the_later_copy_when_that_fits_more(self):
        comparison = check(["A B.", "B. C"], "A B. B. C")
        self.assertEqual([r.intact for r in comparison.results], [True, True])

    def test_order_macros_are_applied_does_not_matter(self):
        one, two = "Alpha beta. Gamma delta.", "Gamma delta. Epsilon zeta."
        sent = "Alpha beta. Gamma delta. Epsilon zeta."
        self.assertEqual([r.intact for r in check([one, two], sent).results], [True, False])
        self.assertEqual([r.intact for r in check([two, one], sent).results], [False, True])

    def test_one_macro_only_claims_one_copy(self):
        self.assertTrue(check("Alpha beta.", "Alpha beta. Alpha beta.").added_text)

    def test_longest_placements_are_kept_when_there_are_too_many(self):
        self.assertFalse(check("Hi {{ticket.description}} bye", "Hi " + "bye " * 80).added_text)

    def test_repetitive_text_finishes_quickly(self):
        macros = [f"word word {{{{a}}}} word end{i}." for i in range(5)]
        started = time.monotonic()
        comparison = check(macros, "word " * 1600)
        self.assertLess(time.monotonic() - started, 5)
        self.assertEqual(len(comparison.results), 5)

    def test_macro_must_appear_more_than_the_first_time_it_could(self):
        self.assertUnedited("Hi {{ticket.custom}} bye", "Hi and more. Hi a bye")


class ManyMacrosTests(unittest.TestCase):
    """Past MAX_EXACT_MACROS in one update, macros are placed greedily instead of exactly."""

    # A long middle range that blocks a range on each side of it, and one-token ranges before them all.
    MIDDLE, LEFT, RIGHT = [(12, 16)], [(10, 13)], [(15, 18)]

    def fillers(self, count):
        return [[(2 * i, 2 * i + 1)] for i in range(count)]

    def test_at_the_cap_the_exact_search_still_runs(self):
        candidates = [self.MIDDLE, self.LEFT, self.RIGHT, *self.fillers(MAX_EXACT_MACROS - 3)]
        expected = {1: (10, 13), 2: (15, 18), **{3 + i: (2 * i, 2 * i + 1) for i in range(MAX_EXACT_MACROS - 3)}}
        self.assertEqual(_place(candidates), expected)

    def test_greedy_takes_the_longest_then_the_earliest_and_shares_no_words(self):
        candidates = [
            self.MIDDLE,                # 4 tokens, the longest, so placed first
            self.LEFT,                  # overlaps the middle, so not placed
            self.RIGHT,                 # overlaps the middle, so not placed
            [(20, 23)],                 # same length as the next and earlier, so it wins
            [(21, 24)],                 # overlaps the one before, so not placed
            [(30, 32), (33, 35)],       # takes its earlier range only
            [(34, 36)],                 # free because the macro before only took one range
        ]
        candidates += self.fillers(MAX_EXACT_MACROS + 1 - len(candidates))
        self.assertEqual(len(candidates), MAX_EXACT_MACROS + 1)
        # In text order, as the exact search gives them, though the fillers were placed last.
        self.assertEqual(list(_place(candidates).items()), [
            (7, (0, 1)), (8, (2, 3)), (0, (12, 16)), (3, (20, 23)), (5, (30, 32)), (6, (34, 36)),
        ])

    def test_whole_comparison_switches_to_greedy_past_the_cap(self):
        left, right, middle = "A B C.", "F G H.", "C. D E F"
        fillers = [f"Filler {i}." for i in range(MAX_EXACT_MACROS - 2)]
        sent = "A B C. D E F G H. " + " ".join(fillers)
        at_cap = check([middle, left, right, *fillers[:-1]], sent)
        past_cap = check([middle, left, right, *fillers], sent)
        self.assertEqual([r.intact for r in at_cap.results], [False, True, True] + [True] * (MAX_EXACT_MACROS - 3))
        self.assertEqual([r.intact for r in past_cap.results], [True, False, False] + [True] * (MAX_EXACT_MACROS - 2))
        self.assertTrue(at_cap.added_text)  # the last filler belongs to no macro
        self.assertEqual([r.pct_of_macro for r in past_cap.results[:3]], [0.0, 33.3, 33.3])

    def test_many_short_overlapping_macros_finish_quickly(self):
        # Each "w w" could sit at any of 24 places, which the exact search would take seconds over.
        started = time.perf_counter()
        comparison = check(["w w"] * 12, "w " * 25)
        self.assertLess(time.perf_counter() - started, 0.5)
        self.assertEqual([r.intact for r in comparison.results], [True] * 12)
        self.assertTrue(comparison.added_text)  # 24 of the 25 words are placed


class IsPlaceholderTests(unittest.TestCase):
    def test_recognizes_both_kinds(self):
        self.assertTrue(is_placeholder("{{ticket.id}},"))
        self.assertTrue(is_placeholder("{%if…%}"))
        self.assertFalse(is_placeholder("{word}"))


class WordSpanTests(unittest.TestCase):
    def spans(self, text):
        macro = compile_macro(text)
        return [None if s is None else text[s[0]:s[1]] for s in macro.word_spans]

    def test_spans_cover_the_original_characters(self):
        text = "Hi {{ ticket.id }},\n1. Click “Save”\n2. Wait"
        self.assertEqual(compile_macro(text).display_words, ["Hi", "{{ticket.id}},", "Click", '"Save"', "Wait"])
        self.assertEqual(self.spans(text), ["Hi", "{{ ticket.id }},", "Click", "“Save”", "Wait"])

    def test_liquid_label_has_no_span(self):
        self.assertEqual(self.spans("Hi {% if ticket.id %}there{% endif %} bye."), ["Hi", None, "bye."])

    def test_mid_line_quote_mark_has_a_span(self):
        text = "You can update your card in Settings > Billing. Click Save."
        self.assertEqual(self.spans(text), text.split())
        self.assertEqual(self.spans("Go to Settings &gt; Billing."), ["Go", "to", "Settings", "&gt;", "Billing."])

    def test_mid_line_dashes_have_a_span(self):
        self.assertEqual(self.spans("Wait --- then click."), ["Wait", "---", "then", "click."])

    def test_real_line_marks_are_left_out(self):
        text = "Thanks.\n> quoted text > here\n>tight\n---\nBye."
        self.assertEqual(compile_macro(text).display_words, ["Thanks.", "quoted", "text", ">", "here", "tight", "Bye."])
        self.assertEqual(self.spans(text), ["Thanks.", "quoted", "text", ">", "here", "tight", "Bye."])

    def test_markup_inside_a_word_has_no_span(self):
        self.assertEqual(self.spans("Click **Save**. Then [here](url). Done"), ["Click", None, "Then", None, "Done"])


if __name__ == "__main__":
    unittest.main()
