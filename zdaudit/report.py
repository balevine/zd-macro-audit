"""Write the HTML report and the CSV files."""
from __future__ import annotations

import csv
import json
from datetime import datetime
from html import escape
from pathlib import Path

from .analyze import COUNTED, STATUS_HELP, STATUS_LABELS, Result, agent_name
from .compare import is_placeholder

# A macro needs at least this many compared uses to appear in the
# "often edited" and "usually kept intact" lists.
MIN_USES = 5

# Included in the page, so the report is one file that can be shared on its own.
ASSETS = {
    name: (Path(__file__).parent / "assets" / name).read_text(encoding="utf-8")
    for name in ("report.css", "report.js")
}

# Hover text for every label in the report, so readers can tell what each number means.
TIPS = {
    # Summary tiles
    "uses": "Every time an agent applied a macro in this period. An update that applied two macros counts as two uses.",
    "sent_edited": "The share of compared uses where the agent changed the macro's text before saving. "
                   "Filled-in placeholders, formatting and text added around the macro don't count as changes.",
    "kept_intact": "The share of compared uses where the macro's text appears unchanged in the saved text, apart "
                  "from placeholders Zendesk filled in. The agent may have written more around it.",
    "agents_editing": "Agents who edited a macro's text at least once, out of all agents with a compared use.",
    "compared_sub": "Compared uses are the ones the tool could check. Uses of macros with no text or only placeholders, "
                    "deleted macros, and macros changed since use are left out. See \"Not counted in the percentages\" below.",
    "macro_edit_avg": "How much of the macro was edited, averaged over edited uses. Words removed, changed or "
                      "inserted inside the macro count, as a share of the macro's words. Text added around it doesn't.",
    # Ranked lists
    "edited_often": "Macros with the highest share of edited uses. A macro agents change this often may need rewriting.",
    "kept_often": "Macros with the lowest share of edited uses. These work well as written.",
    # Comment type table
    "comment_type": "Whether the agent's text went out as a public reply the customer sees, or as an internal "
                    "note only agents see.",
    "type_compared": "Uses of this comment type that the tool could check against the macro's text.",
    "type_edited": "Compared uses of this comment type where the agent changed the macro's text.",
    "type_intact": "Compared uses of this comment type where the macro's text appears unchanged, with or without added text.",
    "type_pct": "Edited divided by Compared, for this comment type.",
    "public_row": "Uses saved with a public reply, which the customer sees.",
    "internal_row": "Uses saved with an internal note, which only agents see.",
    "none_row": "The macro added text, but the agent deleted all of it and saved the update with no comment. "
                "These count as edited.",
    # Macros table
    "macro": "The macro's name in Zendesk.",
    "macro_uses": "How many times agents applied this macro in this period, found in the tickets' history.",
    "zendesk_count": "Zendesk's own count of this macro's uses in the 30 days before this report was made. "
                     "It's a check on Uses, and the two should be close. A big gap means the tool missed some uses. "
                     "It's blank for deleted macros.",
    "macro_compared": "Uses of this macro that the tool could check. Macros with no text of their own show 0.",
    "macro_edited": "Compared uses where the agent changed this macro's text.",
    "macro_pct": "Edited divided by Compared: how often this macro gets edited at all.",
    "avg_all": "How much of this macro is edited, averaged over all compared uses. A use that kept it intact counts as 0%.",
    "avg_edited": "How much of this macro is edited, averaged over only the uses where it was edited. "
                  "High numbers mean heavy rewrites, and low numbers mean small tweaks.",
    "public": "Uses of this macro saved with a public reply. This counts every use, including ones that weren't "
              "compared. Public, Internal and uses with no comment add up to Uses.",
    "internal": "Uses of this macro saved with an internal note. This counts every use, including ones that "
                "weren't compared. Public, Internal and uses with no comment add up to Uses.",
    # Agents table
    "agent": "The agent who applied the macro.",
    "agent_uses": "How many macros this agent applied in this period.",
    "agent_compared": "This agent's uses that the tool could check.",
    "agent_edited": "Compared uses where this agent changed the macro's text.",
    "agent_pct": "Edited divided by Compared, for this agent.",
    # Every macro use
    "when": "When the update was saved, in UTC.",
    "use_agent": "The agent who applied the macro and saved the update.",
    "use_macro": "The macro applied. \"With\" lists other macros applied in the same update.",
    "use_type": "Public reply, internal note, or no comment if the agent deleted all of the macro's text.",
    "result": "Whether the agent changed the macro's text. Click an Edited badge to see the changes.",
    "pct_of_macro": "How much of the macro was edited in this use, from 0% (intact) to 100% (none of it left, or as "
                    "many words inserted as the macro has). Words removed, changed or inserted inside the macro "
                    "count. Text added around it doesn't.",
    "ticket": "The ticket where the macro was used. Click to open it in Zendesk.",
    # Not counted
    "reason": "Why these uses couldn't be compared.",
    "excluded_uses": "How many uses were left out for this reason.",
    "why": "More detail on the reason.",
}

BADGE_TIPS = {
    "unedited": "The macro's text appears unchanged in the saved text, apart from placeholders Zendesk filled in.",
    "edited": "The agent changed the macro's text. Click to see what changed.",
    **STATUS_HELP,
}


def tip(label: str, key: str) -> str:
    """A label with hover text. Tabbing to it also shows the text, for keyboard users."""
    return f'<span class="tip" tabindex="0" data-tip="{escape(TIPS[key])}">{escape(label)}</span>'


def th(label: str, key: str, right: bool = False) -> str:
    cls = ' class="r"' if right else ""
    return f"<th{cls}>{tip(label, key)}</th>"


def sort_th(label: str, key: str, column: str, right: bool = True) -> str:
    cls = ' class="r"' if right else ""
    return (f'<th{cls} data-column="{column}">{tip(label, key)}'
            f'<button type="button" class="sort" aria-label="Sort by {escape(label)}">↕</button></th>')


def write_all(out_dir: Path, result: Result, subdomain: str | None) -> dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "report": out_dir / "report.html",
        "uses": out_dir / "macro_uses.csv",
        "macros": out_dir / "macros.csv",
        "agents": out_dir / "agents.csv",
    }
    _write_uses_csv(paths["uses"], result, subdomain)
    _write_rows_csv(paths["macros"], result.macros, [
        "macro_id", "title", "uses", "compared", "edited", "unedited", "pct_uses_edited",
        "avg_pct_of_macro", "avg_pct_of_macro_when_edited", "public", "internal", "excluded", "zendesk_usage_30d",
    ])
    _write_rows_csv(paths["agents"], result.agents, [
        "agent_id", "name", "email", "uses", "compared", "edited", "unedited", "pct_uses_edited",
    ])
    if result.coverage.unread:
        paths["unread"] = out_dir / "unread_tickets.csv"
        with paths["unread"].open("w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["ticket_id", "ticket_url"])
            writer.writerows([t, ticket_url(subdomain, t)] for t in result.coverage.unread)
    paths["report"].write_text(render_html(result, subdomain), encoding="utf-8")
    return paths


def ticket_url(subdomain: str | None, ticket_id: int) -> str:
    return f"https://{subdomain}.zendesk.com/agent/tickets/{ticket_id}" if subdomain else ""


# ---- CSV -----------------------------------------------------------------------

def _write_uses_csv(path: Path, result: Result, subdomain: str | None) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            "used_at_utc", "ticket_id", "ticket_url", "agent_id", "agent_name", "agent_email",
            "macro_id", "macro_title", "comment_type", "status", "edited", "text_added", "pct_of_macro_edited",
            "combined_with", "audit_id",
        ])
        for u in result.uses:
            edited = {"edited": "yes", "unedited": "no"}.get(u.status, "")
            text_added = ("yes" if u.added_text else "no") if u.status == "unedited" else ""
            writer.writerow([
                u.created_at.strftime("%Y-%m-%d %H:%M:%S"), u.ticket_id,
                ticket_url(subdomain, u.ticket_id), u.agent_id,
                agent_name(result.users, u.agent_id),
                result.users.get(u.agent_id, {}).get("email", ""),
                u.macro_id, u.macro_title, u.comment_type, u.status, edited, text_added,
                "" if u.pct_of_macro is None else u.pct_of_macro,
                " | ".join(u.combined_with), u.audit_id,
            ])


def _write_rows_csv(path: Path, rows: list[dict], columns: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: "" if row.get(k) is None else row.get(k) for k in columns})


# ---- HTML pieces ---------------------------------------------------------------

def fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0f}%"


def _fmt_macro_pct(value: float | None) -> str:
    """Like fmt_pct, but a tiny edit reads as under 1% instead of rounding to 0%."""
    if value is not None and 0 < value < 1:
        return "<1%"
    return fmt_pct(value)


def _bar(value: float | None) -> str:
    if value is None:
        return '<span class="muted">n/a</span>'
    return (f'<span class="pctcell"><span class="minibar"><span style="width:{value:.1f}%"></span>'
            f'</span><span class="num">{value:.0f}%</span></span>')


def _render_diff(ops: list[tuple[str, list[str]]], context: int = 8) -> str:
    """Show the changes with long stretches shortened.

    A stretch longer than 2 * context + 3 words keeps context words at each
    end. Unchanged text gets a plain "…" between them. Removed or added text
    gets "… N more words" inside the colouring, so it still reads as removed or
    added. N is always 4 or more there, so the wording is always plural.

    A placeholder replaced by the same number of words is Zendesk filling it
    in, not an edit, so those words are shown in a neutral style.
    """
    def show(words: list[str]) -> str:
        return " ".join(f'<span class="ph">{escape(w)}</span>' if is_placeholder(w) else escape(w) for w in words)

    parts = []
    last = len(ops) - 1
    filled = set()
    for i in range(last):
        (op, words), (next_op, next_words) = ops[i], ops[i + 1]
        if (op == "delete" and next_op == "insert" and len(words) == len(next_words)
                and all(is_placeholder(w) for w in words)):
            filled.update((i, i + 1))
    for i, (op, words) in enumerate(ops):
        if i in filled:
            if op == "insert":
                title = escape(" ".join(ops[i - 1][1]))
                parts.append(f'<span class="fill" data-tip="Zendesk filled in {title}">{escape(" ".join(words))}</span>')
            continue
        if op == "equal":
            if len(words) > 2 * context + 3:
                head = words[:context] if i > 0 else []
                tail = words[-context:] if i < last else []
                text = escape(" ".join(head)) + ' <span class="gap">…</span> ' + escape(" ".join(tail))
            else:
                text = escape(" ".join(words))
            parts.append(f"<span>{text}</span>")
        else:
            tag = "del" if op == "delete" else "ins"
            if len(words) > 2 * context + 3:
                hidden = len(words) - 2 * context
                rendered = (show(words[:context]) + f' <span class="gap">… {hidden} more words</span> '
                            + show(words[-context:]))
            else:
                rendered = show(words)
            parts.append(f"<{tag}>{rendered}</{tag}>")
    return " ".join(parts)


def _macro_list(rows: list[dict], empty: str) -> str:
    if not rows:
        return f'<p class="muted">{escape(empty)}</p>'
    items = "".join(
        f'<li><span class="lt">{escape(r["title"])}</span>'
        f'<span class="lv">{r["edited"]} of {r["compared"]} uses edited ({fmt_pct(r["pct_uses_edited"])})</span></li>'
        for r in rows
    )
    return f'<ul class="ranked">{items}</ul>'


def _ranked_macros(macros: list[dict]) -> tuple[list[dict], list[dict]]:
    """The macros edited most often, and the ones most often kept intact."""
    eligible = [m for m in macros if m["compared"] >= MIN_USES]
    ranked = sorted(eligible, key=lambda m: (-m["pct_uses_edited"], -m["compared"]))
    often = [m for m in ranked if m["edited"]][:5]
    kept = [m for m in reversed(ranked) if m not in often][:5]
    return often, kept


def _tiles(o: dict) -> str:
    edited_w = o["pct_uses_edited"] or 0
    edit_note = ""
    if o["avg_pct_of_macro_when_edited"] is not None:
        average = _fmt_macro_pct(o["avg_pct_of_macro_when_edited"]).replace("<", "under ")
        edit_note = f'<span class="muted">{tip(f"Edited uses changed {average} of the macro on average", "macro_edit_avg")}</span>'
    return f"""
    <div class="tiles">
      <div class="tile"><div class="tv">{o['uses']:,}</div><div class="tl">{tip("macro uses", "uses")}</div>
        <div class="ts">{o['macros_used']:,} different macros</div></div>
      <div class="tile"><div class="tv edited">{fmt_pct(o['pct_uses_edited'])}</div><div class="tl">{tip("sent edited", "sent_edited")}</div>
        <div class="ts">{o['edited']:,} of {o['compared']:,} {tip("compared uses", "compared_sub")}</div></div>
      <div class="tile"><div class="tv unedited">{fmt_pct(o['pct_uses_intact'])}</div><div class="tl">{tip("kept intact", "kept_intact")}</div>
        <div class="ts">{o['unedited']:,} of {o['compared']:,} {tip("compared uses", "compared_sub")}, {o['added_text']:,} with text added</div></div>
      <div class="tile"><div class="tv">{fmt_pct(o['pct_agents_editing'])}</div><div class="tl">{tip("of agents edit macros", "agents_editing")}</div>
        <div class="ts">{o['agents_editing']:,} of {o['agents_using']:,} agents edited at least once</div></div>
    </div>
    <div class="split" role="img" aria-label="{fmt_pct(o['pct_uses_edited'])} edited, {fmt_pct(o['pct_uses_intact'])} kept intact">
      <span class="s-edited" style="width:{edited_w}%"></span><span class="s-unedited" style="width:{100 - edited_w if o['compared'] else 0}%"></span>
    </div>
    <div class="legend"><span><i class="k-edited"></i>Edited</span><span><i class="k-unedited"></i>Kept intact</span>
      {edit_note}</div>
    """


def _type_rows(by_type: dict) -> str:
    return "".join(
        f"<tr><td>{tip(label, key)}</td><td class='r'>{t['compared']:,}</td><td class='r'>{t['edited']:,}</td>"
        f"<td class='r'>{t['unedited']:,}</td><td>{_bar(t['pct_uses_edited'])}</td></tr>"
        for label, key, t in (("Public replies", "public_row", by_type["public"]),
                              ("Internal notes", "internal_row", by_type["internal"]),
                              ("No comment (macro text deleted)", "none_row", by_type["none"]))
        if t["compared"] or key != "none_row"
    )


def _excluded_rows(excluded: dict) -> str:
    return "".join(
        f"<tr><td>{escape(STATUS_LABELS[s])}</td><td class='r'>{excluded.get(s, 0):,}</td>"
        f"<td class='muted'>{escape(STATUS_HELP[s])}</td></tr>"
        for s in STATUS_LABELS if s not in COUNTED
    )


def _warnings(result: Result, subdomain: str | None) -> str:
    """Boxes at the top of the page for anything the run couldn't read."""
    cov = result.coverage
    boxes = []
    if cov.unread:
        links = ", ".join(
            f'<a href="{escape(ticket_url(subdomain, t))}" target="_blank" rel="noopener">{escape(str(t))}</a>'
            if subdomain else escape(str(t))
            for t in cov.unread
        )
        noun, verb = ("ticket", "is") if len(cov.unread) == 1 else ("tickets", "are")
        if cov.stopped_early:
            text = (f"<strong>This report is incomplete. The run stopped early because Zendesk stopped "
                    f"answering.</strong> {cov.read:,} of {cov.tickets:,} tickets read. The percentages only "
                    f"cover the tickets that were read. A later run should pick everything up.")
        else:
            text = (f"<strong>{len(cov.unread):,} {noun} couldn't be read.</strong> Zendesk kept failing even "
                    f"after several retries, so macro uses on the {noun} below are missing from this report. "
                    "Running it again usually picks them up.")
        boxes.append(f'<div class="warning" role="note">{text} The unread {noun} {verb} listed below and in '
                     f'<code>unread_tickets.csv</code>.<div class="ids">{links}</div></div>')
    if cov.names_missing:
        boxes.append('<div class="warning" role="note"><strong>Agent names are missing.</strong> Zendesk didn\'t '
                     "answer when the tool asked for them, so agents are shown by ID.</div>")
    return "\n  ".join(boxes)


def _usage_note(result: Result) -> str:
    if not any(m["zendesk_usage_30d"] is not None for m in result.macros):
        return ""
    note = (" \"Zendesk count\" is Zendesk's own tally of uses in the last 30 days. It should be "
            "close to Uses. A big gap means some uses were missed.")
    if (result.until - result.since).days != 30:
        note += " This report doesn't cover exactly 30 days, so expect the two to differ."
    return note


def _macro_rows(macros: list[dict], has_usage: bool) -> str:
    """Macro rows. The data attributes are what the sort buttons read, with -1 for "nothing to show"."""
    def sort_value(value: float | None) -> float:
        return -1 if value is None else value

    def usage_cell(count: int | None) -> str:
        return f"<td class='r'>{'' if count is None else format(count, ',')}</td>" if has_usage else ""

    return "".join(
        f'<tr class="macro" data-macro="{escape(m["macro_id"])}" data-title="{escape(m["title"].lower())}" '
        f'data-uses="{m["uses"]}" data-compared="{m["compared"]}" data-edited="{m["edited"]}" '
        f'data-pct="{sort_value(m["pct_uses_edited"])}" '
        f'data-avg="{sort_value(m["avg_pct_of_macro"])}" '
        f'data-avgedited="{sort_value(m["avg_pct_of_macro_when_edited"])}">'
        f'<td><button type="button" class="open" aria-expanded="false">{escape(m["title"])}</button></td>'
        f"<td class='r'>{m['uses']:,}</td>{usage_cell(m['zendesk_usage_30d'])}<td class='r'>{m['compared']:,}</td>"
        f"<td class='r'>{m['edited']:,}</td><td>{_bar(m['pct_uses_edited'])}</td>"
        f"<td class='r'>{escape(_fmt_macro_pct(m['avg_pct_of_macro']))}</td>"
        f"<td class='r'>{escape(_fmt_macro_pct(m['avg_pct_of_macro_when_edited']))}</td>"
        f"<td class='r'>{m['public']:,}</td><td class='r'>{m['internal']:,}</td></tr>"
        for m in macros
    )


def _agent_rows(agents: list[dict]) -> str:
    return "".join(
        f"<tr><td>{escape(a['name'])}<div class='sub'>{escape(a['email'])}</div></td>"
        f"<td class='r'>{a['uses']:,}</td><td class='r'>{a['compared']:,}</td><td class='r'>{a['edited']:,}</td>"
        f"<td>{_bar(a['pct_uses_edited'])}</td></tr>"
        for a in agents
    )


def _use_headers(with_macro: bool) -> str:
    """The columns of a use table: every use on the page, or one macro's own uses."""
    macro = th("Macro", "use_macro") if with_macro else ""
    return (f'{th("When (UTC)", "when")}{th("Agent", "use_agent")}{macro}{th("Type", "use_type")}'
            f'{th("Result", "result")}{th("% of macro edited", "pct_of_macro", True)}{th("Ticket", "ticket")}')


def _use_data(result: Result, subdomain: str | None) -> list[dict]:
    """One entry per use, for the script that draws the use tables."""
    return [
        {
            "macro": u.macro_id,
            "title": u.macro_title,
            "when": f"{u.created_at:%Y-%m-%d %H:%M}",
            "agent": agent_name(result.users, u.agent_id),
            "type": u.comment_type or "none",
            "status": u.status,
            "pct": u.pct_of_macro,
            "ticket": u.ticket_id,
            "url": ticket_url(subdomain, u.ticket_id),
            "added": u.added_text,
            "with": u.combined_with,
            "diff": _render_diff(u.diff) if u.diff else "",
        }
        for u in sorted(result.uses, key=lambda u: u.created_at, reverse=True)
    ]


# ---- The page ------------------------------------------------------------------

def render_html(result: Result, subdomain: str | None) -> str:
    o = result.overall
    day = lambda d: f"{d:%b} {d.day}, {d.year}"  # %-d isn't supported on Windows
    period = f"{day(result.since)} to {day(result.until)}"
    account = f"{subdomain}.zendesk.com" if subdomain else "Sample data (not a real account)"
    now = datetime.now().astimezone()
    generated = f"{day(now)} at {now:%H:%M %Z}"
    often, kept = _ranked_macros(result.macros)

    # Zendesk's usage counts always cover the last 30 days, and need the Professional plan or above.
    has_usage = any(m["zendesk_usage_30d"] is not None for m in result.macros)
    usage_th = th("Zendesk count", "zendesk_count", right=True) if has_usage else ""

    # Every "<" becomes the JSON escape \u003c, so text from a macro title, agent name or comment
    # can't close the script tag or confuse the browser with "<!--" and "<script". JSON.parse turns it back.
    report_data = json.dumps({
        "uses": _use_data(result, subdomain),
        "labels": STATUS_LABELS,
        "tips": BADGE_TIPS,
    }).replace("<", "\\u003c")

    status_options = "".join(
        f'<option value="{s}">{escape(label)}</option>' for s, label in STATUS_LABELS.items()
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Macro Edit Report</title>
<style>
{ASSETS["report.css"]}</style>
</head>
<body>
<main>
  <h1>Macro edit report</h1>
  <div class="meta">{escape(account)} · {period} · generated {escape(generated)}
    <div class="sub">Point to any underlined label to see what it means.</div></div>
  {_warnings(result, subdomain)}
  {_tiles(o)}

  <h2>Which macros get edited</h2>
  <p class="muted">Macros with at least {MIN_USES} compared uses. Ones that are often edited may need rewriting.</p>
  <div class="cols">
    <div class="card"><h3>{tip("Edited most often", "edited_often")}</h3>{_macro_list(often, "No macro has been edited yet.")}</div>
    <div class="card"><h3>{tip("Kept intact most often", "kept_often")}</h3>{_macro_list(kept, "Not enough uses yet.")}</div>
  </div>

  <h2>Public replies and internal notes</h2>
  <div class="tablewrap"><table>
    <thead><tr>{th("Comment type", "comment_type")}{th("Compared", "type_compared", True)}{th("Edited", "type_edited", True)}{th("Kept intact", "type_intact", True)}{th("% of uses edited", "type_pct")}</tr></thead>
    <tbody>{_type_rows(o["by_type"])}</tbody>
  </table></div>

  <h2>Macros</h2>
  <p class="muted">Every macro used in this period, most used first. Click a macro to see each of its uses, with how much of the macro was edited and a link to the ticket.{_usage_note(result)}</p>
  <div class="tablewrap"><table id="macros">
    <thead><tr>{sort_th("Macro", "macro", "title", False)}{sort_th("Uses", "macro_uses", "uses")}{usage_th}{sort_th("Compared", "macro_compared", "compared")}{sort_th("Edited", "macro_edited", "edited")}{sort_th("% of uses edited", "macro_pct", "pct", False)}
      {sort_th("Avg % of macro edited", "avg_all", "avg")}{sort_th("Avg when edited", "avg_edited", "avgedited")}{th("Public", "public", True)}{th("Internal", "internal", True)}</tr></thead>
    <tbody>{_macro_rows(result.macros, has_usage)}</tbody>
  </table></div>

  <h2>Agents</h2>
  <div class="tablewrap"><table>
    <thead><tr>{th("Agent", "agent")}{th("Uses", "agent_uses", True)}{th("Compared", "agent_compared", True)}{th("Edited", "agent_edited", True)}{th("% of uses edited", "agent_pct")}</tr></thead>
    <tbody>{_agent_rows(result.agents)}</tbody>
  </table></div>

  <h2>Every macro use</h2>
  <p class="muted">Click an "Edited" badge to see what changed. Struck-out text was in the macro but not sent. Highlighted text was added by the agent. Underlined text is a placeholder Zendesk filled in.</p>
  <div class="filters">
    <input id="q" type="search" placeholder="Search agent, macro or ticket number">
    <select id="status"><option value="">All results</option>{status_options}</select>
    <select id="type"><option value="">All comment types</option><option value="public">Public</option>
      <option value="internal">Internal</option><option value="none">No comment</option></select>
    <select id="order"><option value="new">Newest first</option><option value="most">Most edited first</option></select>
    <span id="count"></span>
  </div>
  <div class="tablewrap"><table id="uses">
    <thead><tr>{_use_headers(with_macro=True)}</tr></thead>
    <tbody></tbody>
  </table></div>
  <button type="button" id="more" class="more" hidden>Show more</button>

  <h2>Not counted in the percentages</h2>
  <div class="tablewrap"><table>
    <thead><tr>{th("Reason", "reason")}{th("Uses", "excluded_uses", True)}{th("Why", "why")}</tr></thead>
    <tbody>{_excluded_rows(o["excluded"])}</tbody>
  </table></div>

  <h2>How this was measured</h2>
  <ul class="notes muted">
    <li>A macro use is any update where Zendesk recorded that an agent applied a macro. The text saved in that same update is compared with the macro's comment text.</li>
    <li>Placeholders such as <code>{{{{ticket.requester.first_name}}}}</code> match what Zendesk filled in, up to a limit that depends on the placeholder (three words for a first name). A longer value counts as an edit, so a few long filled-in values may be flagged by mistake. Dynamic content matches any of its language versions.</li>
    <li>A use counts as kept intact when the macro's text appears anywhere in the saved text, so a greeting or signature added around it isn't an edit. These uses are marked "with text added".</li>
    <li>Changes to spacing, bold, links, list bullets and curly quotes are ignored. Any other change inside the macro's text counts as an edit, however small.</li>
    <li>"% of macro edited" counts the macro's words that were removed or changed, plus words inserted inside it, as a share of the macro's words. A changed word counts once. Words a placeholder can fill and text added around the macro don't count. It tops out at 100%, so it won't show how far a long insertion went past that.</li>
    <li>If a macro adds text but the update was saved with no comment, the agent deleted all of the macro's text. That counts as edited, with 100% of the macro edited.</li>
    <li>If several macros were applied in one update, each one is judged on its own and counted as a use. A word in the saved text can only be credited to one macro.</li>
    <li>Zendesk only returns a macro's current text. If a macro changed during this period, older uses that don't match are listed as "Macro changed since use" instead of being counted.</li>
  </ul>
</main>
<template id="uses-panel"><div class="panel">
  <div class="filters">
    <select aria-label="Order"><option value="most">Most edited first</option><option value="new">Newest first</option><option value="old">Oldest first</option></select>
    <label><input type="checkbox"> Edited only</label><span class="sub"></span>
  </div>
  <div class="tablewrap"><table><thead><tr>{_use_headers(with_macro=False)}</tr></thead><tbody></tbody></table></div>
  <button type="button" class="more" hidden>Show more</button>
</div></template>
<div id="tip" role="tooltip"></div>
<script type="application/json" id="report-data">{report_data}</script>
<script>
{ASSETS["report.js"]}</script>
</body>
</html>
"""
