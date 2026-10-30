"""Write the HTML report and the CSV files."""
from __future__ import annotations

import csv
from datetime import datetime
from html import escape
from pathlib import Path

from .analyze import STATUS_HELP, STATUS_LABELS, Result, agent_name
from .compare import is_placeholder

# A macro needs at least this many compared uses to appear in the
# "often edited" and "usually sent as is" lists.
MIN_USES = 5

# Hover text for every label in the report, so readers can tell what each number means.
TIPS = {
    # Summary tiles
    "uses": "Every time an agent applied a macro in this period. An update that applied two macros counts as two uses.",
    "sent_edited": "The share of compared uses where the agent changed the macro's text before saving. "
                   "Filled-in placeholders and formatting don't count as changes.",
    "sent_as_is": "The share of compared uses where the saved text matched the macro exactly, apart from "
                  "placeholders Zendesk filled in.",
    "agents_editing": "Agents who edited a macro's text at least once, out of all agents with a compared use.",
    "compared_sub": "Compared uses are the ones the tool could check. Uses of macros with no text, deleted macros, "
                    "and macros changed since use are left out. See \"Not counted in the percentages\" below.",
    "kept_avg": "Similarity between the macro and the saved text, averaged over edited uses. 100% would mean no "
                "change, and 0% means none of the macro's words were kept.",
    # Ranked lists
    "edited_often": "Macros with the highest share of edited uses. A macro agents change this often may need rewriting.",
    "as_is_often": "Macros with the lowest share of edited uses. These work well as written.",
    # Comment type table
    "comment_type": "Whether the agent's text went out as a public reply the customer sees, or as an internal "
                    "note only agents see.",
    "type_compared": "Uses of this comment type that the tool could check against the macro's text.",
    "type_edited": "Compared uses of this comment type where the agent changed the macro's text.",
    "type_as_is": "Compared uses of this comment type where the text matched the macro exactly.",
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
    "macro_pct": "Edited divided by Compared, for this macro.",
    "kept": "When agents edited this macro, how much of its wording they kept, on average. "
            "Low numbers mean heavy rewrites.",
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
    "similarity": "How much of the macro's wording was kept. 100% is unchanged, and 0% means none of it was kept.",
    "ticket": "The ticket where the macro was used. Click to open it in Zendesk.",
    # Not counted
    "reason": "Why these uses couldn't be compared.",
    "excluded_uses": "How many uses were left out for this reason.",
    "why": "More detail on the reason.",
}

BADGE_TIPS = {
    "unedited": "The saved text matched the macro exactly, apart from placeholders Zendesk filled in.",
    "edited": "The agent changed the macro's text. Click to see what changed.",
    **STATUS_HELP,
}


def tip(label: str, key: str) -> str:
    """A label with hover text. Tabbing to it also shows the text, for keyboard users."""
    return f'<span class="tip" tabindex="0" data-tip="{escape(TIPS[key])}">{escape(label)}</span>'


def th(label: str, key: str, right: bool = False) -> str:
    cls = ' class="r"' if right else ""
    return f"<th{cls}>{tip(label, key)}</th>"


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
        "macro_id", "title", "uses", "compared", "edited", "unedited", "pct_edited",
        "avg_similarity_edited", "public", "internal", "excluded", "zendesk_usage_30d",
    ])
    _write_rows_csv(paths["agents"], result.agents, [
        "agent_id", "name", "email", "uses", "compared", "edited", "unedited", "pct_edited",
    ])
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
            "macro_id", "macro_title", "comment_type", "status", "edited", "similarity_pct",
            "combined_with", "audit_id",
        ])
        for u in result.uses:
            edited = {"edited": "yes", "unedited": "no"}.get(u.status, "")
            writer.writerow([
                u.created_at.strftime("%Y-%m-%d %H:%M:%S"), u.ticket_id,
                ticket_url(subdomain, u.ticket_id), u.agent_id,
                agent_name(result.users, u.agent_id),
                result.users.get(u.agent_id, {}).get("email", ""),
                u.macro_id, u.macro_title, u.comment_type, u.status, edited,
                "" if u.similarity is None else u.similarity,
                " | ".join(u.combined_with), u.audit_id,
            ])


def _write_rows_csv(path: Path, rows: list[dict], columns: list[str]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: "" if row.get(k) is None else row.get(k) for k in columns})


# ---- HTML ----------------------------------------------------------------------

def _fmt_pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.0f}%"


def _bar(value: float | None) -> str:
    if value is None:
        return '<span class="muted">n/a</span>'
    return (f'<span class="pctcell"><span class="minibar"><span style="width:{value:.1f}%"></span>'
            f'</span><span class="num">{value:.0f}%</span></span>')


def _render_diff(ops: list[tuple[str, list[str]]], context: int = 8) -> str:
    """Show the changes with long unchanged stretches shortened.

    A placeholder replaced by the same number of words is Zendesk filling it
    in, not an edit, so those words are shown in a neutral style.
    """
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
            rendered = " ".join(
                f'<span class="ph">{escape(w)}</span>' if is_placeholder(w) else escape(w)
                for w in words
            )
            parts.append(f"<{tag}>{rendered}</{tag}>")
    return " ".join(parts)


def _macro_list(rows: list[dict], empty: str) -> str:
    if not rows:
        return f'<p class="muted">{escape(empty)}</p>'
    items = "".join(
        f'<li><span class="lt">{escape(r["title"])}</span>'
        f'<span class="lv">{r["edited"]} of {r["compared"]} edited ({_fmt_pct(r["pct_edited"])})</span></li>'
        for r in rows
    )
    return f'<ul class="ranked">{items}</ul>'


def render_html(result: Result, subdomain: str | None) -> str:
    o = result.overall
    day = lambda d: f"{d:%b} {d.day}, {d.year}"  # %-d isn't supported on Windows
    period = f"{day(result.since)} to {day(result.until)}"
    account = f"{subdomain}.zendesk.com" if subdomain else "Sample data (not a real account)"
    now = datetime.now().astimezone()
    generated = f"{day(now)} at {now:%H:%M %Z}"

    eligible = [m for m in result.macros if m["compared"] >= MIN_USES]
    ranked = sorted(eligible, key=lambda m: (-m["pct_edited"], -m["compared"]))
    often = [m for m in ranked if m["edited"]][:5]
    as_is = [m for m in reversed(ranked) if m not in often][:5]

    edited_w = o["pct_edited"] or 0
    kept_note = ""
    if o["avg_similarity_edited"] is not None:
        kept = f"Edited messages kept {o['avg_similarity_edited']:.0f}% of the macro text on average"
        kept_note = f'<span class="muted">{tip(kept, "kept_avg")}</span>'
    tiles = f"""
    <div class="tiles">
      <div class="tile"><div class="tv">{o['uses']:,}</div><div class="tl">{tip("macro uses", "uses")}</div>
        <div class="ts">{o['macros_used']:,} different macros</div></div>
      <div class="tile"><div class="tv edited">{_fmt_pct(o['pct_edited'])}</div><div class="tl">{tip("sent edited", "sent_edited")}</div>
        <div class="ts">{o['edited']:,} of {o['compared']:,} {tip("compared uses", "compared_sub")}</div></div>
      <div class="tile"><div class="tv unedited">{_fmt_pct(o['pct_unedited'])}</div><div class="tl">{tip("sent as is", "sent_as_is")}</div>
        <div class="ts">{o['unedited']:,} of {o['compared']:,} {tip("compared uses", "compared_sub")}</div></div>
      <div class="tile"><div class="tv">{_fmt_pct(o['pct_agents_editing'])}</div><div class="tl">{tip("of agents edit macros", "agents_editing")}</div>
        <div class="ts">{o['agents_editing']:,} of {o['agents_using']:,} agents edited at least once</div></div>
    </div>
    <div class="split" role="img" aria-label="{_fmt_pct(o['pct_edited'])} edited, {_fmt_pct(o['pct_unedited'])} sent as is">
      <span class="s-edited" style="width:{edited_w}%"></span><span class="s-unedited" style="width:{100 - edited_w if o['compared'] else 0}%"></span>
    </div>
    <div class="legend"><span><i class="k-edited"></i>Edited</span><span><i class="k-unedited"></i>Sent as is</span>
      {kept_note}</div>
    """

    type_rows = "".join(
        f"<tr><td>{tip(label, key)}</td><td class='r'>{t['compared']:,}</td><td class='r'>{t['edited']:,}</td>"
        f"<td class='r'>{t['unedited']:,}</td><td>{_bar(t['pct_edited'])}</td></tr>"
        for label, key, t in (("Public replies", "public_row", o["by_type"]["public"]),
                              ("Internal notes", "internal_row", o["by_type"]["internal"]),
                              ("No comment (macro text deleted)", "none_row", o["by_type"]["none"]))
        if t["compared"] or key != "none_row"
    )

    excluded_rows = "".join(
        f"<tr><td>{escape(STATUS_LABELS[s])}</td><td class='r'>{o['excluded'].get(s, 0):,}</td>"
        f"<td class='muted'>{escape(STATUS_HELP[s])}</td></tr>"
        for s in ("no_text", "unavailable", "changed")
    )

    # Zendesk's usage counts always cover the last 30 days, and need the Professional plan or above.
    has_usage = any(m["zendesk_usage_30d"] is not None for m in result.macros)
    usage_th = th("Zendesk count", "zendesk_count", right=True) if has_usage else ""
    def usage_cell(m: dict) -> str:
        if not has_usage:
            return ""
        count = m["zendesk_usage_30d"]
        return f"<td class='r'>{'' if count is None else format(count, ',')}</td>"
    usage_note = ""
    if has_usage:
        usage_note = (" \"Zendesk count\" is Zendesk's own tally of uses in the last 30 days. It should be "
                      "close to Uses. A big gap means some uses were missed.")
        if (result.until - result.since).days != 30:
            usage_note += " This report doesn't cover exactly 30 days, so expect the two to differ."
    macro_rows = "".join(
        f"<tr><td>{escape(m['title'])}</td><td class='r'>{m['uses']:,}</td>{usage_cell(m)}<td class='r'>{m['compared']:,}</td>"
        f"<td class='r'>{m['edited']:,}</td><td>{_bar(m['pct_edited'])}</td>"
        f"<td class='r'>{'' if m['avg_similarity_edited'] is None else _fmt_pct(m['avg_similarity_edited'])}</td>"
        f"<td class='r'>{m['public']:,}</td><td class='r'>{m['internal']:,}</td></tr>"
        for m in result.macros
    )

    agent_rows = "".join(
        f"<tr><td>{escape(a['name'])}<div class='sub'>{escape(a['email'])}</div></td>"
        f"<td class='r'>{a['uses']:,}</td><td class='r'>{a['compared']:,}</td><td class='r'>{a['edited']:,}</td>"
        f"<td>{_bar(a['pct_edited'])}</td></tr>"
        for a in result.agents
    )

    use_rows = []
    for u in sorted(result.uses, key=lambda u: u.created_at, reverse=True):
        name = agent_name(result.users, u.agent_id)
        url = ticket_url(subdomain, u.ticket_id)
        ticket = f'<a href="{escape(url)}" target="_blank" rel="noopener">#{u.ticket_id}</a>' if url else f"#{u.ticket_id}"
        badge = (f'<span class="badge b-{u.status}" data-tip="{escape(BADGE_TIPS[u.status])}">'
                 f'{escape(STATUS_LABELS[u.status])}</span>')
        if u.diff:
            badge = (f'<details><summary>{badge}</summary>'
                     f'<div class="diff">{_render_diff(u.diff)}</div></details>')
        combined = (f"<div class='sub'>with {escape(', '.join(u.combined_with))}</div>"
                    if u.combined_with else "")
        kind = {"public": "Public", "internal": "Internal"}.get(u.comment_type, "No comment")
        search = f"{name} {u.macro_title} {u.ticket_id}".lower()
        use_rows.append(
            f'<tr data-status="{u.status}" data-type="{u.comment_type or "none"}" data-search="{escape(search)}">'
            f"<td class='nowrap'>{u.created_at:%Y-%m-%d %H:%M}</td><td>{escape(name)}</td>"
            f"<td>{escape(u.macro_title)}{combined}</td><td>{kind}</td><td>{badge}</td>"
            f"<td class='r'>{'' if u.similarity is None else f'{u.similarity:.0f}%'}</td><td>{ticket}</td></tr>"
        )

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
:root {{
  --bg:#f6f6f3; --surface:#ffffff; --text:#1c1c1a; --muted:#6c6b66; --border:#e2e1db;
  --edited:#c2410c; --edited-soft:#fde7d9; --unedited:#15803d; --unedited-soft:#dcf3e3;
  --neutral-soft:#ecebe6; --accent:#2f5bd3;
  --del-bg:#fbe0de; --del:#8f1f1a; --ins-bg:#d9f2df; --ins:#14532d;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg:#151513; --surface:#1f1f1c; --text:#ebeae4; --muted:#a3a29b; --border:#34332e;
    --edited:#fb923c; --edited-soft:#4a2a17; --unedited:#4ade80; --unedited-soft:#173a24;
    --neutral-soft:#2b2a26; --accent:#8fb0ff;
    --del-bg:#4a1f1d; --del:#ffcfc9; --ins-bg:#173a24; --ins:#c4f1d2;
  }}
}}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--text);
  font:15px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }}
main {{ max-width:1120px; margin:0 auto; padding:32px 16px 64px; }}
h1 {{ font-size:28px; margin:0 0 4px; letter-spacing:-0.01em; }}
h2 {{ font-size:19px; margin:40px 0 4px; }}
h3 {{ font-size:15px; margin:0 0 8px; }}
p {{ margin:4px 0 12px; }}
a {{ color:var(--accent); }}
.muted, .sub {{ color:var(--muted); }}
.sub {{ font-size:12.5px; }}
.meta {{ color:var(--muted); margin-bottom:24px; }}
.card {{ background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:16px; }}
.tiles {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(200px, 1fr)); gap:12px; }}
.tile {{ background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:16px; }}
.tv {{ font-size:32px; font-weight:650; line-height:1.1; font-variant-numeric:tabular-nums; }}
.tv.edited {{ color:var(--edited); }} .tv.unedited {{ color:var(--unedited); }}
.tl {{ font-weight:600; margin-top:2px; }}
.ts {{ color:var(--muted); font-size:13px; margin-top:4px; }}
.split {{ display:flex; height:14px; border-radius:7px; overflow:hidden; margin:20px 0 8px; background:var(--neutral-soft); }}
.s-edited {{ background:var(--edited); }} .s-unedited {{ background:var(--unedited); }}
.legend {{ display:flex; flex-wrap:wrap; gap:16px; font-size:13px; }}
.legend i {{ display:inline-block; width:10px; height:10px; border-radius:3px; margin-right:6px; vertical-align:-1px; }}
.k-edited {{ background:var(--edited); }} .k-unedited {{ background:var(--unedited); }}
.cols {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(300px, 1fr)); gap:12px; margin-top:12px; }}
ul.ranked {{ list-style:none; margin:0; padding:0; }}
ul.ranked li {{ display:flex; justify-content:space-between; gap:12px; padding:7px 0; border-top:1px solid var(--border); }}
ul.ranked li:first-child {{ border-top:0; }}
.lv {{ color:var(--muted); white-space:nowrap; font-variant-numeric:tabular-nums; }}
.tablewrap {{ overflow-x:auto; background:var(--surface); border:1px solid var(--border); border-radius:12px; margin-top:12px; }}
table {{ width:100%; border-collapse:collapse; font-size:14px; }}
th, td {{ text-align:left; padding:9px 12px; border-top:1px solid var(--border); vertical-align:top; }}
thead th {{ border-top:0; font-size:12px; text-transform:uppercase; letter-spacing:0.04em; color:var(--muted); font-weight:600; white-space:nowrap; }}
.r {{ text-align:right; font-variant-numeric:tabular-nums; }}
.nowrap {{ white-space:nowrap; font-variant-numeric:tabular-nums; }}
.pctcell {{ display:inline-flex; align-items:center; gap:8px; white-space:nowrap; }}
.minibar {{ display:inline-block; width:80px; height:8px; border-radius:4px; background:var(--unedited-soft); overflow:hidden; }}
.minibar span {{ display:block; height:100%; background:var(--edited); }}
.num {{ font-variant-numeric:tabular-nums; min-width:36px; }}
.badge {{ display:inline-block; padding:1px 8px; border-radius:999px; font-size:12.5px; white-space:nowrap; background:var(--neutral-soft); }}
.b-edited, .b-changed {{ background:var(--edited-soft); color:var(--edited); }}
.b-unedited {{ background:var(--unedited-soft); color:var(--unedited); }}
details summary {{ cursor:pointer; list-style:none; }}
details summary::-webkit-details-marker {{ display:none; }}
details summary::after {{ content:" ▸"; color:var(--muted); font-size:12px; }}
details[open] summary::after {{ content:" ▾"; }}
.diff {{ margin-top:8px; padding:10px 12px; border-radius:8px; background:var(--bg); font-size:13.5px; line-height:1.7; max-width:520px; }}
del {{ background:var(--del-bg); color:var(--del); text-decoration:line-through; border-radius:3px; padding:0 2px; }}
ins {{ background:var(--ins-bg); color:var(--ins); text-decoration:none; border-radius:3px; padding:0 2px; }}
.ph {{ font-family:ui-monospace, Menlo, monospace; font-size:12px; opacity:0.75; }}
.gap {{ color:var(--muted); }}
.fill {{ border-bottom:1px dotted var(--muted); cursor:help; }}
.tip {{ border-bottom:1px dotted var(--muted); cursor:help; }}
.tip:focus-visible {{ outline:2px solid var(--accent); outline-offset:2px; border-radius:2px; }}
#tip {{ position:fixed; z-index:10; display:none; max-width:300px; padding:8px 11px; border-radius:8px;
  background:var(--text); color:var(--bg); font-size:13px; line-height:1.45; font-weight:400;
  text-transform:none; letter-spacing:normal; box-shadow:0 4px 16px rgba(0,0,0,0.2); pointer-events:none; }}
.filters {{ display:flex; flex-wrap:wrap; gap:8px; margin-top:12px; align-items:center; }}
.filters input, .filters select {{ font:inherit; font-size:14px; padding:7px 10px; border-radius:8px;
  border:1px solid var(--border); background:var(--surface); color:var(--text); }}
.filters input {{ flex:1 1 220px; }}
#count {{ color:var(--muted); font-size:13px; }}
.notes li {{ margin-bottom:6px; }}
</style>
</head>
<body>
<main>
  <h1>Macro edit report</h1>
  <div class="meta">{escape(account)} · {period} · generated {escape(generated)}
    <div class="sub">Point to any underlined label to see what it means.</div></div>

  {tiles}

  <h2>Which macros get edited</h2>
  <p class="muted">Macros with at least {MIN_USES} compared uses. Ones that are often edited may need rewriting.</p>
  <div class="cols">
    <div class="card"><h3>{tip("Edited most often", "edited_often")}</h3>{_macro_list(often, "No macro has been edited yet.")}</div>
    <div class="card"><h3>{tip("Sent as is most often", "as_is_often")}</h3>{_macro_list(as_is, "Not enough uses yet.")}</div>
  </div>

  <h2>Public replies and internal notes</h2>
  <div class="tablewrap"><table>
    <thead><tr>{th("Comment type", "comment_type")}{th("Compared", "type_compared", True)}{th("Edited", "type_edited", True)}{th("Sent as is", "type_as_is", True)}{th("% edited", "type_pct")}</tr></thead>
    <tbody>{type_rows}</tbody>
  </table></div>

  <h2>Macros</h2>
  <p class="muted">Every macro used in this period, most used first. "Kept when edited" is how much of the macro's text survived, on average, when an agent changed it.{usage_note}</p>
  <div class="tablewrap"><table>
    <thead><tr>{th("Macro", "macro")}{th("Uses", "macro_uses", True)}{usage_th}{th("Compared", "macro_compared", True)}{th("Edited", "macro_edited", True)}{th("% edited", "macro_pct")}
      {th("Kept when edited", "kept", True)}{th("Public", "public", True)}{th("Internal", "internal", True)}</tr></thead>
    <tbody>{macro_rows}</tbody>
  </table></div>

  <h2>Agents</h2>
  <div class="tablewrap"><table>
    <thead><tr>{th("Agent", "agent")}{th("Uses", "agent_uses", True)}{th("Compared", "agent_compared", True)}{th("Edited", "agent_edited", True)}{th("% edited", "agent_pct")}</tr></thead>
    <tbody>{agent_rows}</tbody>
  </table></div>

  <h2>Every macro use</h2>
  <p class="muted">Click an "Edited" badge to see what changed. Struck-out text was in the macro but not sent. Highlighted text was added by the agent. Underlined text is a placeholder Zendesk filled in.</p>
  <div class="filters">
    <input id="q" type="search" placeholder="Search agent, macro or ticket number">
    <select id="status"><option value="">All results</option>{status_options}</select>
    <select id="type"><option value="">All comment types</option><option value="public">Public</option>
      <option value="internal">Internal</option><option value="none">No comment</option></select>
    <span id="count"></span>
  </div>
  <div class="tablewrap"><table id="uses">
    <thead><tr>{th("When (UTC)", "when")}{th("Agent", "use_agent")}{th("Macro", "use_macro")}{th("Type", "use_type")}{th("Result", "result")}{th("Similarity", "similarity", True)}{th("Ticket", "ticket")}</tr></thead>
    <tbody>{''.join(use_rows)}</tbody>
  </table></div>

  <h2>Not counted in the percentages</h2>
  <div class="tablewrap"><table>
    <thead><tr>{th("Reason", "reason")}{th("Uses", "excluded_uses", True)}{th("Why", "why")}</tr></thead>
    <tbody>{excluded_rows}</tbody>
  </table></div>

  <h2>How this was measured</h2>
  <ul class="notes muted">
    <li>A macro use is any update where Zendesk recorded that an agent applied a macro. The text saved in that same update is compared with the macro's comment text.</li>
    <li>Placeholders such as <code>{{{{ticket.requester.first_name}}}}</code> match whatever Zendesk filled in, so they never count as edits. Dynamic content matches any of its language versions.</li>
    <li>Changes to spacing, bold, links, list bullets and curly quotes are ignored. Every other change counts as an edit, however small. Similarity shows how much of the macro's wording was kept.</li>
    <li>If a macro adds text but the update was saved with no comment, the agent deleted all of the macro's text. That counts as edited, with 0% similarity.</li>
    <li>If several macros were applied in one update, the text is compared with all of them together, and each macro is counted as a use.</li>
    <li>Zendesk only returns a macro's current text. If a macro changed during this period, older uses that don't match are listed as "Macro changed since use" instead of being counted.</li>
  </ul>
</main>
<div id="tip" role="tooltip"></div>
<script>
(function () {{
  // One floating tooltip for every [data-tip] label. It's positioned against the
  // window, so the scrolling table boxes can't cut it off.
  var tipEl = document.getElementById('tip'), current = null;
  function show(el) {{
    if (current) current.removeAttribute('aria-describedby');
    current = el;
    el.setAttribute('aria-describedby', 'tip');
    tipEl.textContent = el.getAttribute('data-tip');
    tipEl.style.display = 'block';
    var r = el.getBoundingClientRect(), w = tipEl.offsetWidth, h = tipEl.offsetHeight;
    var left = Math.min(Math.max(8, r.left + r.width / 2 - w / 2), window.innerWidth - w - 8);
    var top = r.bottom + 8 + h > window.innerHeight - 8 ? r.top - h - 8 : r.bottom + 8;
    tipEl.style.left = left + 'px';
    tipEl.style.top = top + 'px';
  }}
  function hide() {{
    if (current) current.removeAttribute('aria-describedby');
    current = null;
    tipEl.style.display = 'none';
  }}
  function target(e) {{ return e.target.closest ? e.target.closest('[data-tip]') : null; }}
  document.addEventListener('mouseover', function (e) {{ var el = target(e); if (el) show(el); else if (current) hide(); }});
  document.addEventListener('focusin', function (e) {{ var el = target(e); if (el) show(el); }});
  document.addEventListener('focusout', hide);
  document.addEventListener('keydown', function (e) {{ if (e.key === 'Escape') hide(); }});
  window.addEventListener('scroll', hide, true);
}})();
(function () {{
  var q = document.getElementById('q'), st = document.getElementById('status'),
      ty = document.getElementById('type'), count = document.getElementById('count'),
      rows = Array.prototype.slice.call(document.querySelectorAll('#uses tbody tr'));
  function apply() {{
    var term = q.value.trim().toLowerCase(), s = st.value, t = ty.value, shown = 0;
    rows.forEach(function (r) {{
      var ok = (!s || r.dataset.status === s) && (!t || r.dataset.type === t) &&
               (!term || r.dataset.search.indexOf(term) !== -1);
      r.style.display = ok ? '' : 'none';
      if (ok) shown++;
    }});
    count.textContent = shown.toLocaleString() + ' of ' + rows.length.toLocaleString() + ' uses';
  }}
  [q, st, ty].forEach(function (el) {{ el.addEventListener('input', apply); }});
  apply();
}})();
</script>
</body>
</html>
"""
