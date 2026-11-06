import csv
import json
import re
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from tests.helpers import UNTIL, fields_only_macro, make_audit, run_audit, run_audits, use_macro
from zdaudit.zendesk import Coverage
from zdaudit.report import _bar, _fmt_macro_pct, _render_diff, fmt_pct, render_html, ticket_url, write_all


def page_data(html):
    return json.loads(re.search(r'id="report-data">(.*?)</script>', html, re.S).group(1))


def macro_uses(mid, edited, intact):
    """Audits for one macro with a set number of edited and intact uses."""
    return [make_audit([mid], "Other words.", audit_id=mid * 100 + i) for i in range(edited)] + \
           [make_audit([mid], "Fixed text.", audit_id=mid * 100 + edited + i) for i in range(intact)]


class FormatTests(unittest.TestCase):
    def test_pct(self):
        self.assertEqual([fmt_pct(v) for v in (None, 0.0, 12.4, 100.0)], ["n/a", "0%", "12%", "100%"])

    def test_tiny_edit_reads_as_under_1(self):
        self.assertEqual([_fmt_macro_pct(v) for v in (None, 0.0, 0.1, 0.9, 1.0, 45.0)],
                         ["n/a", "0%", "<1%", "<1%", "1%", "45%"])

    def test_ticket_url(self):
        self.assertEqual(ticket_url("acme", 9), "https://acme.zendesk.com/agent/tickets/9")
        self.assertEqual(ticket_url(None, 9), "")


class BarTests(unittest.TestCase):
    def test_bar_shows_the_value(self):
        self.assertEqual(_bar(25.04), '<span class="pctcell"><span class="minibar"><span style="width:25.0%"></span>'
                                      '</span><span class="num">25%</span></span>')

    def test_no_value(self):
        self.assertEqual(_bar(None), '<span class="muted">n/a</span>')


class DiffTests(unittest.TestCase):
    def test_changes_are_marked(self):
        self.assertEqual(_render_diff([("equal", ["a"]), ("delete", ["b"]), ("insert", ["c"])]),
                         "<span>a</span> <del>b</del> <ins>c</ins>")

    def test_text_is_escaped(self):
        self.assertEqual(_render_diff([("insert", ["<b>"]), ("equal", ["&"]), ("delete", ["<i>"])]),
                         "<ins>&lt;b&gt;</ins> <span>&amp;</span> <del>&lt;i&gt;</del>")

    def test_filled_placeholder_is_neutral(self):
        html = _render_diff([("equal", ["Hi"]), ("delete", ["{{name}}"]), ("insert", ["Sam"]), ("equal", ["bye"])])
        self.assertEqual(html, '<span>Hi</span> <span class="fill" data-tip="Zendesk filled in {{name}}">Sam</span> '
                               '<span>bye</span>')

    def test_placeholder_replaced_by_more_words_is_a_change(self):
        html = _render_diff([("delete", ["{{name}}"]), ("insert", ["Sam", "Lee"])])
        self.assertEqual(html, '<del><span class="ph">{{name}}</span></del> <ins>Sam Lee</ins>')

    def test_ordinary_replacement_is_a_change(self):
        self.assertEqual(_render_diff([("delete", ["old"]), ("insert", ["new"])]), "<del>old</del> <ins>new</ins>")

    def words(self, count):
        return [f"w{i}" for i in range(count)]

    def test_long_unchanged_text_is_shortened_at_both_ends(self):
        words = self.words(20)
        html = _render_diff([("insert", ["x"]), ("equal", words), ("insert", ["y"])], context=2)
        self.assertEqual(html, '<ins>x</ins> <span>w0 w1 <span class="gap">…</span> w18 w19</span> <ins>y</ins>')

    def test_first_and_last_stretches_only_keep_the_side_near_a_change(self):
        words = self.words(20)
        first = _render_diff([("equal", words), ("insert", ["y"])], context=2)
        self.assertEqual(first, '<span> <span class="gap">…</span> w18 w19</span> <ins>y</ins>')
        last = _render_diff([("insert", ["y"]), ("equal", words)], context=2)
        self.assertEqual(last, '<ins>y</ins> <span>w0 w1 <span class="gap">…</span> </span>')

    def test_short_unchanged_text_is_kept(self):
        words = self.words(7)  # 2 * context + 3
        self.assertEqual(_render_diff([("equal", words)], context=2), f'<span>{" ".join(words)}</span>')
        self.assertIn("gap", _render_diff([("equal", self.words(8))], context=2))

    def test_long_insert_is_shortened_with_a_count(self):
        html = _render_diff([("equal", ["a"]), ("insert", self.words(20))], context=2)
        self.assertEqual(html, '<span>a</span> <ins>w0 w1 <span class="gap">… 16 more words</span> w18 w19</ins>')

    def test_long_delete_is_shortened_with_a_count(self):
        html = _render_diff([("delete", self.words(11)), ("equal", ["a"])], context=3)
        self.assertEqual(html, '<del>w0 w1 w2 <span class="gap">… 5 more words</span> w8 w9 w10</del> <span>a</span>')

    def test_change_is_shortened_only_past_the_limit(self):
        words = self.words(7)  # 2 * context + 3
        self.assertEqual(_render_diff([("insert", words)], context=2), '<ins>w0 w1 w2 w3 w4 w5 w6</ins>')
        self.assertEqual(_render_diff([("insert", self.words(8))], context=2),
                         '<ins>w0 w1 <span class="gap">… 4 more words</span> w6 w7</ins>')

    def test_placeholder_kept_in_a_shortened_delete_is_marked(self):
        words = ["{{name}}"] + self.words(9)
        html = _render_diff([("delete", words)], context=2)
        self.assertEqual(html, '<del><span class="ph">{{name}}</span> w0 <span class="gap">… 6 more words</span> '
                               'w7 w8</del>')


class PageDataTests(unittest.TestCase):
    def test_a_use_has_what_the_tables_need(self):
        users = {"7": {"name": "Ana"}}
        result = run_audits([use_macro(1, "First part."), use_macro(2, "Second part.")],
                            [make_audit([1, 2], "Hello. First part. Second piece.")], users)
        one, two = page_data(render_html(result, "acme"))["uses"]
        self.assertEqual(one, {
            "macro": "1", "title": "Macro 1", "when": "2026-10-20 00:00", "agent": "Ana", "type": "public",
            "status": "unedited", "pct": 0.0, "ticket": 9, "url": "https://acme.zendesk.com/agent/tickets/9",
            "added": True, "with": ["Macro 2"], "diff": "",
        })
        self.assertEqual((two["status"], two["pct"], two["with"]), ("edited", 50.0, ["Macro 1"]))
        self.assertIn("<del>", two["diff"])

    def test_no_comment_has_its_own_type(self):
        data = page_data(render_html(run_audit([use_macro(1, "First part.")], [1]), "acme"))
        self.assertEqual(data["uses"][0]["type"], "none")

    def test_newest_first(self):
        audits = [make_audit([1], "First part.", audit_id=i, created=f"2026-10-{day}T00:00:00Z")
                  for i, day in enumerate((10, 20, 15))]
        uses = page_data(render_html(run_audits([use_macro(1, "First part.")], audits), "acme"))["uses"]
        self.assertEqual([u["when"][:10] for u in uses], ["2026-10-20", "2026-10-15", "2026-10-10"])

    def test_sample_report_has_no_links(self):
        data = page_data(render_html(run_audit([use_macro(1, "First part.")], [1], "First part."), None))
        self.assertEqual(data["uses"][0]["url"], "")

    def test_every_status_has_a_label_and_tip(self):
        data = page_data(render_html(run_audit([], [1], "x"), "acme"))
        self.assertEqual(set(data["labels"]), set(data["tips"]))

    def test_text_cannot_break_out_of_the_page_data(self):
        # A macro title isn't escaped on the way into the data, so it needs this protection.
        result = run_audit([use_macro(1, "First part.", title="</script><b>bad</b>")], [1], "First part.")
        html = render_html(result, "acme")
        self.assertNotIn("</script><b>", html)
        self.assertEqual(html.count("</script>"), 2)
        self.assertEqual(page_data(html)["uses"][0]["title"], "</script><b>bad</b>")

    def test_comment_markers_cannot_keep_the_page_data_open(self):
        # After "<!--" and "<script", a browser no longer treats "</script>" as the end of the block.
        title = "Refund <!--<script> note </script><b>bad</b>"
        agent = "Ana <!--<script>"
        result = run_audits([use_macro(1, "First part.", title=title)], [make_audit([1], "First part.")],
                            {"7": {"name": agent}})
        html = render_html(result, "acme")
        block = re.search(r'id="report-data">(.*?)</script>', html, re.S).group(1)
        self.assertEqual(block.count("<"), 0)
        self.assertEqual(html.count("</script>"), 2)
        use = page_data(html)["uses"][0]
        self.assertEqual((use["title"], use["agent"]), (title, agent))


class PageTests(unittest.TestCase):
    def render(self, macros, audits, subdomain="acme", **run):
        return render_html(run_audits(macros, audits, **run), subdomain)

    def test_styles_and_script_are_inside_the_page(self):
        html = render_html(run_audit([use_macro(1, "First part.")], [1], "First part."), "acme")
        self.assertIn("--edited:#c2410c", html)
        self.assertIn("function pager(", html)
        self.assertIn("function detailRow(", html)
        self.assertNotIn('src="', html)
        self.assertNotIn('rel="stylesheet"', html)

    def test_doubled_braces_do_not_reach_the_browser(self):
        html = render_html(run_audit([use_macro(1, "First part.")], [1], "First part."), "acme")
        style = html.split("<style>")[1].split("</style>")[0]
        self.assertNotIn("{{", style)
        self.assertNotIn("{{", html.split("<script>")[1])

    def test_account_name(self):
        result = run_audit([use_macro(1, "First part.")], [1], "First part.")
        self.assertIn("acme.zendesk.com", render_html(result, "acme"))
        self.assertIn("Sample data", render_html(result, None))

    def test_summary_numbers(self):
        macro = use_macro(1, "Fixed text.")
        html = self.render([macro], macro_uses(1, edited=1, intact=3))
        self.assertIn('<div class="tv edited">25%</div>', html)
        self.assertIn('<div class="tv unedited">75%</div>', html)
        self.assertIn('style="width:25.0%"></span><span class="s-unedited" style="width:75.0%"', html)
        self.assertIn("Edited uses changed 100% of the macro on average", html)

    def test_nothing_compared(self):
        html = self.render([fields_only_macro(1)], [make_audit([1], "Reply.")])
        self.assertIn('<div class="tv edited">n/a</div>', html)
        self.assertNotIn("Edited uses changed", html)
        self.assertIn('style="width:0%"></span><span class="s-unedited" style="width:0%"', html)

    def test_no_comment_row_only_when_needed(self):
        macro = use_macro(1, "Fixed text.")
        self.assertNotIn("No comment (macro text deleted)", self.render([macro], macro_uses(1, 0, 1)))
        self.assertIn("No comment (macro text deleted)", self.render([macro], [make_audit([1])]))

    def test_every_reason_for_not_counting_is_listed_with_its_count(self):
        html = self.render([fields_only_macro(1)], [make_audit([1], "a", audit_id=1), make_audit([1], "b", audit_id=2)])
        self.assertIn("<td>Macro has no comment text</td><td class='r'>2</td>", html)
        for label in ("Macro is only placeholders", "Macro deleted or unreadable", "Macro changed since use"):
            self.assertIn(f"<td>{label}</td><td class='r'>0</td>", html)
        self.assertNotIn("<td>Edited</td>", html)

    def test_macro_row_has_sort_values_and_percentages(self):
        macro = use_macro(1, "One two three four five six seven eight nine ten")
        audits = [make_audit([1], "One two three four TYPO six seven eight nine ten", audit_id=1),
                  make_audit([1], macro["actions"][0]["value"], audit_id=2)]
        html = self.render([macro], audits)
        self.assertIn('data-macro="1" data-title="macro 1" data-uses="2" data-compared="2" data-edited="1" '
                      'data-pct="50.0" data-avg="5.0" data-avgedited="10.0"', html)

    def test_macro_table_has_sort_buttons(self):
        html = render_html(run_audit([use_macro(1, "First part.")], [1], "First part."), "acme")
        headers = html.split('<table id="macros">')[1].split("</thead>")[0]
        self.assertEqual(re.findall(r'data-column="(\w+)"', headers),
                         ["title", "uses", "compared", "edited", "pct", "avg", "avgedited"])
        self.assertEqual(headers.count('class="sort"'), 7)

    def test_macro_with_nothing_compared_sorts_last(self):
        html = self.render([fields_only_macro(1)], [make_audit([1], "Reply.")])
        self.assertIn('data-pct="-1" data-avg="-1" data-avgedited="-1"', html)

    def test_macro_titles_are_escaped_in_rows(self):
        html = self.render([use_macro(1, "Fixed text.", title='A<b>"Q"')], macro_uses(1, 0, 1))
        self.assertIn('data-title="a&lt;b&gt;&quot;q&quot;"', html)
        self.assertIn('aria-expanded="false">A&lt;b&gt;&quot;Q&quot;</button>', html)

    def test_small_average_reads_as_under_1(self):
        words = " ".join(f"w{i}" for i in range(200))
        macro = use_macro(1, words + ".")
        html = self.render([macro], [make_audit([1], words + "!")])
        self.assertIn("<td class='r'>&lt;1%</td>", html)
        self.assertIn("Edited uses changed under 1% of the macro", html)

    def test_agent_row(self):
        html = self.render([use_macro(1, "Fixed text.")], macro_uses(1, 1, 1),
                           users={"7": {"name": "A<na", "email": "a@x.co"}})
        self.assertIn("<td>A&lt;na<div class='sub'>a@x.co</div></td><td class='r'>2</td><td class='r'>2</td>"
                      "<td class='r'>1</td>", html)

    def test_macro_panel_has_a_ticket_column_and_no_macro_column(self):
        html = render_html(run_audit([use_macro(1, "First part.")], [1], "First part."), "acme")
        panel = html.split('<template id="uses-panel">')[1].split("</template>")[0]
        self.assertIn("Ticket", panel)
        self.assertIn("% of macro edited", panel)
        self.assertNotIn('>Macro<', panel)
        every_use = html.split('<table id="uses">')[1].split("</thead>")[0]
        self.assertIn('>Macro<', every_use)


class SkippedNoteTests(unittest.TestCase):
    LINK = '<a href="https://acme.zendesk.com/agent/tickets/{0}" target="_blank" rel="noopener">{0}</a>'

    def render(self, skipped, subdomain="acme", usage=4, unread=(), names_missing=False):
        result = run_audit([use_macro(1, "First part.", usage_30d=usage)], [1], "First part.")
        result.coverage = Coverage(10, list(unread), False, names_missing, list(skipped))
        return render_html(result, subdomain)

    def test_lists_and_links_each_ticket(self):
        html = self.render([4, 8, 15])
        self.assertIn(
            '<div class="notice" role="note"><strong>3 tickets were skipped.</strong> They were deleted, or this '
            "sign-in isn't allowed to see them (for example, they're restricted to a group). Macro uses on them "
            'aren\'t counted, which can explain why this report finds fewer uses than the "Zendesk count" column. '
            f'The skipped tickets are listed below.<div class="ids">{self.LINK.format(4)}, {self.LINK.format(8)}, '
            f"{self.LINK.format(15)}</div></div>", html)
        self.assertNotIn('class="warning"', html)

    def test_one_ticket(self):
        self.assertIn(
            '<div class="notice" role="note"><strong>1 ticket was skipped.</strong> It was deleted, or this '
            "sign-in isn't allowed to see it (for example, it's restricted to a group). Macro uses on it "
            'aren\'t counted, which can explain why this report finds fewer uses than the "Zendesk count" column. '
            f'The skipped ticket is listed below.<div class="ids">{self.LINK.format(4)}</div></div>',
            self.render([4]))

    def test_no_zendesk_count_column_means_no_mention_of_it(self):
        html = self.render([4, 8], subdomain=None, usage=None)
        self.assertIn("Macro uses on them aren't counted. The skipped tickets are listed below."
                      '<div class="ids">4, 8</div></div>', html)
        self.assertNotIn("Zendesk count", html)

    def test_comes_after_the_warnings(self):
        html = self.render([4], unread=[5], names_missing=True)
        self.assertEqual(html.count('class="notice"'), 1)
        self.assertLess(html.index("Not every agent name could be fetched"), html.index("1 ticket was skipped"))
        self.assertLess(html.index("1 ticket was skipped"), html.index('class="tiles"'))

    def test_nothing_when_none_were_skipped(self):
        html = self.render([])
        self.assertNotIn('class="notice"', html)
        self.assertNotIn("skipped", html)
        self.assertRegex(html, r'what it means\.</div></div>\s*<div class="tiles">')

    def test_styled_in_light_and_dark(self):
        html = self.render([4])
        self.assertIn(".notice { background:var(--surface); border:1px solid var(--border);", html)
        self.assertIn(".warning .ids, .notice .ids {", html)


class UnreadWarningTests(unittest.TestCase):
    LINK = '<a href="https://acme.zendesk.com/agent/tickets/{0}" target="_blank" rel="noopener">{0}</a>'

    def result(self, unread=(), tickets=0, stopped_early=False, names_missing=False):
        result = run_audit([use_macro(1, "First part.")], [1], "First part.")
        result.coverage = Coverage(tickets, list(unread), stopped_early, names_missing)
        return result

    def render(self, unread, subdomain="acme", **coverage):
        return render_html(self.result(unread, **coverage), subdomain)

    def test_lists_and_links_each_ticket(self):
        html = self.render([5, 1234], tickets=10)
        self.assertIn(
            '<div class="warning" role="note"><strong>2 tickets couldn\'t be read.</strong> Zendesk kept failing even '
            "after several retries, so macro uses on the tickets below are missing from this report. Running it again "
            "usually picks them up. The unread tickets are listed below and in <code>unread_tickets.csv</code>."
            f'<div class="ids">{self.LINK.format(5)}, {self.LINK.format(1234)}</div></div>', html)

    def test_one_ticket(self):
        html = self.render([5], tickets=10)
        self.assertIn("<strong>1 ticket couldn't be read.</strong>", html)
        self.assertIn("macro uses on the ticket below", html)
        self.assertIn("The unread ticket is listed below", html)

    def test_stopped_early(self):
        html = self.render([3, 4, 5], tickets=10, stopped_early=True)
        self.assertIn(
            '<div class="warning" role="note"><strong>This report is incomplete. The run stopped early because '
            "Zendesk stopped answering.</strong> 7 of 10 tickets read. The percentages only cover the tickets that "
            "were read. A later run should pick everything up. The unread tickets are listed below and in "
            f'<code>unread_tickets.csv</code>.<div class="ids">{self.LINK.format(3)}, {self.LINK.format(4)}, '
            f"{self.LINK.format(5)}</div></div>", html)
        self.assertNotIn("couldn't be read", html)

    def test_names_missing(self):
        html = self.render([], names_missing=True)
        self.assertIn('<div class="warning" role="note"><strong>Not every agent name could be fetched.</strong> '
                      "Zendesk stopped answering while the tool was fetching them, so agents without a name are "
                      "shown by ID.</div>", html)
        self.assertEqual(html.count('class="warning"'), 1)

    def test_both_boxes(self):
        html = self.render([5], tickets=10, names_missing=True)
        self.assertEqual(html.count('class="warning"'), 2)
        self.assertLess(html.index("couldn't be read"), html.index("Not every agent name could be fetched"))

    def test_no_links_without_a_subdomain(self):
        self.assertIn('<div class="ids">5, 6</div>', self.render([5, 6], subdomain=None))

    def test_comes_before_the_summary_tiles(self):
        html = self.render([5], names_missing=True)
        self.assertLess(html.index("Not every agent name could be fetched"), html.index('class="tiles"'))

    def test_text_is_escaped(self):
        self.assertIn('<div class="ids">1&lt;b&gt;</div>', self.render(["1<b>"], subdomain=None))
        self.assertIn('<a href="https://acme.zendesk.com/agent/tickets/&quot;x" target="_blank" rel="noopener">&quot;x</a>', self.render(['"x']))

    def test_nothing_when_every_ticket_was_read(self):
        html = self.render([])
        self.assertNotIn('class="warning"', html)
        self.assertNotIn("couldn't be read", html)
        self.assertRegex(html, r'what it means\.</div></div>\s*<div class="tiles">')

    def test_styled_in_light_and_dark(self):
        html = self.render([5])
        self.assertIn("--warn-soft:#fdf3d7", html)
        self.assertIn("--warn-soft:#3a2e12", html)
        self.assertIn(".warning { background:var(--warn-soft)", html)
        self.assertIn(".warning + .warning, .warning + .notice { margin-top:-12px; }", html)

    def test_unread_tickets_csv(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = write_all(Path(folder), self.result([5, 1234], tickets=10), "acme")
            self.assertEqual(paths["unread"], Path(folder) / "unread_tickets.csv")
            self.assertEqual(paths["unread"].read_bytes().decode(),
                             "ticket_id,ticket_url\r\n5,https://acme.zendesk.com/agent/tickets/5\r\n"
                             "1234,https://acme.zendesk.com/agent/tickets/1234\r\n")

    def test_unread_tickets_csv_without_a_subdomain(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = write_all(Path(folder), self.result([5]), None)
            self.assertEqual(paths["unread"].read_bytes().decode(), "ticket_id,ticket_url\r\n5,\r\n")

    def test_no_unread_tickets_csv_when_everything_was_read(self):
        with tempfile.TemporaryDirectory() as folder:
            paths = write_all(Path(folder), self.result([], names_missing=True), "acme")
            self.assertNotIn("unread", paths)
            self.assertFalse((Path(folder) / "unread_tickets.csv").exists())


class ZendeskCountTests(unittest.TestCase):
    def test_shown_when_available(self):
        html = render_html(run_audit([use_macro(1, "First part.", usage_30d=1234)], [1], "First part."), "acme")
        self.assertIn(">Zendesk count<", html)
        self.assertIn("<td class='r'>1,234</td>", html)
        self.assertIn("It should be close to Uses", html)
        self.assertNotIn("doesn't cover exactly 30 days", html)

    def test_left_out_when_the_plan_has_none(self):
        html = render_html(run_audit([use_macro(1, "First part.")], [1], "First part."), "acme")
        self.assertNotIn("Zendesk count", html)
        self.assertEqual(html.split('class="macro"')[1].split("</tr>")[0].count("<td"), 9)

    def test_blank_for_a_deleted_macro_beside_a_known_one(self):
        result = run_audits([use_macro(1, "First part.", usage_30d=3)],
                            [make_audit([1], "First part.", audit_id=1), make_audit([1], "First part.", audit_id=2),
                             make_audit([2], "Reply.", audit_id=3)])
        html = render_html(result, "acme")
        rows = [row.split("</tr>")[0] for row in html.split('class="macro"')[1:]]
        self.assertEqual([row.count("<td") for row in rows], [10, 10])
        self.assertIn("<td class='r'>3</td>", rows[0])
        self.assertIn("<td class='r'></td>", rows[1])

    def test_warns_when_the_period_is_not_30_days(self):
        result = run_audits([use_macro(1, "First part.", usage_30d=3)], [make_audit([1], "First part.")],
                            since=UNTIL - timedelta(days=60))
        self.assertIn("doesn't cover exactly 30 days", render_html(result, "acme"))


class RankedListTests(unittest.TestCase):
    def lists(self, counts):
        macros = [use_macro(mid, "Fixed text.") for mid in counts]
        audits = [a for mid, (edited, intact) in counts.items() for a in macro_uses(mid, edited, intact)]
        html = render_html(run_audits(macros, audits), "acme")
        often, kept = html.split('class="cols"')[1].split("<h2>")[0].split("Kept intact most often")
        return re.findall(r'class="lt">(.*?)<', often), re.findall(r'class="lt">(.*?)<', kept)

    def test_needs_five_compared_uses(self):
        often, kept = self.lists({1: (2, 3), 2: (2, 2)})
        self.assertEqual((often, kept), (["Macro 1"], []))

    def test_order_and_separation(self):
        often, kept = self.lists({1: (1, 4), 2: (4, 1), 3: (0, 5)})
        self.assertEqual(often, ["Macro 2", "Macro 1"])
        self.assertEqual(kept, ["Macro 3"])

    def test_a_tie_goes_to_the_macro_with_more_uses(self):
        often, _ = self.lists({1: (5, 5), 2: (10, 10)})
        self.assertEqual(often, ["Macro 2", "Macro 1"])

    def test_at_most_five_in_each_list(self):
        often, _ = self.lists({mid: (mid, 5) for mid in range(1, 8)})
        self.assertEqual(often, [f"Macro {mid}" for mid in (7, 6, 5, 4, 3)])
        _, kept = self.lists({mid: (0, 4 + mid) for mid in range(1, 8)})
        self.assertEqual(len(kept), 5)

    def test_messages_when_empty(self):
        html = render_html(run_audit([use_macro(1, "Fixed text.")], [1], "Fixed text."), "acme")
        self.assertIn('<p class="muted">No macro has been edited yet.</p>', html)
        self.assertIn('<p class="muted">Not enough uses yet.</p>', html)

    def test_row_text(self):
        macros = [use_macro(1, "Fixed text.")]
        html = render_html(run_audits(macros, macro_uses(1, 1, 4)), "acme")
        self.assertIn('<span class="lt">Macro 1</span><span class="lv">1 of 5 uses edited (20%)</span>', html)


class CsvTests(unittest.TestCase):
    def write(self, result, subdomain="acme"):
        with tempfile.TemporaryDirectory() as folder:
            paths = write_all(Path(folder) / "new" / "folder", result, subdomain)
            self.assertTrue(paths["report"].read_text(encoding="utf-8").startswith("<!doctype html>"))
            return {name: list(csv.DictReader(path.read_text(encoding="utf-8").splitlines()))
                    for name, path in paths.items() if name != "report"}

    def test_uses(self):
        users = {"7": {"name": "Ana", "email": "ana@example.com"}}
        macros = [use_macro(1, "First part."), use_macro(2, "Second part.")]
        rows = self.write(run_audits(macros, [make_audit([1, 2], "Hello. First part. Second piece.")], users))["uses"]
        self.assertEqual(rows[0], {
            "used_at_utc": "2026-10-20 00:00:00", "ticket_id": "9",
            "ticket_url": "https://acme.zendesk.com/agent/tickets/9", "agent_id": "7", "agent_name": "Ana",
            "agent_email": "ana@example.com", "macro_id": "1", "macro_title": "Macro 1", "comment_type": "public",
            "status": "unedited", "edited": "no", "text_added": "yes", "pct_of_macro_edited": "0.0",
            "combined_with": "Macro 2", "audit_id": "5",
        })
        self.assertEqual((rows[1]["edited"], rows[1]["text_added"], rows[1]["pct_of_macro_edited"]), ("yes", "", "50.0"))

    def test_uses_not_compared_leave_the_result_blank(self):
        row = self.write(run_audit([fields_only_macro(1)], [1], "Reply."))["uses"][0]
        self.assertEqual((row["status"], row["edited"], row["text_added"], row["pct_of_macro_edited"]),
                         ("no_text", "", "", ""))

    def test_intact_with_nothing_added(self):
        row = self.write(run_audit([use_macro(1, "First part.")], [1], "First part."))["uses"][0]
        self.assertEqual(row["text_added"], "no")

    def test_macros(self):
        row = self.write(run_audit([use_macro(1, "First part.")], [1], "Other words."))["macros"][0]
        self.assertEqual(row, {
            "macro_id": "1", "title": "Macro 1", "uses": "1", "compared": "1", "edited": "1", "unedited": "0",
            "pct_uses_edited": "100.0", "avg_pct_of_macro": "100.0", "avg_pct_of_macro_when_edited": "100.0",
            "public": "1", "internal": "0", "excluded": "0", "zendesk_usage_30d": "",
        })

    def test_agents(self):
        users = {"7": {"name": "Ana", "email": "ana@example.com"}}
        result = run_audits([use_macro(1, "First part.")], [make_audit([1], "First part.")], users)
        self.assertEqual(self.write(result)["agents"][0], {
            "agent_id": "7", "name": "Ana", "email": "ana@example.com", "uses": "1", "compared": "1",
            "edited": "0", "unedited": "1", "pct_uses_edited": "0.0",
        })

    def test_no_subdomain_means_no_ticket_link(self):
        row = self.write(run_audit([use_macro(1, "First part.")], [1], "First part."), None)["uses"][0]
        self.assertEqual(row["ticket_url"], "")


if __name__ == "__main__":
    unittest.main()
