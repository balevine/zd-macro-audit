"""Turn raw audits and macros into per-use results and summary stats."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from statistics import mean
from typing import Iterable

from .compare import compare, compile_macro, html_to_text, macro_marker
from .zendesk import Coverage, is_macro_event, parse_time

# Only these two statuses count toward the edited percentage.
COUNTED = ("edited", "unedited")

STATUS_LABELS = {
    "unedited": "Kept intact",
    "edited": "Edited",
    "no_text": "Macro has no comment text",
    "only_placeholders": "Macro is only placeholders",
    "unavailable": "Macro deleted or unreadable",
    "changed": "Macro changed since use",
}

STATUS_HELP = {
    "no_text": "The macro only changes fields or tags, so there's no text to compare.",
    "only_placeholders": "All of the macro's text is filled in by Zendesk, so any comment could match it.",
    "unavailable": "The macro was deleted or this account can't read it, so its text is unknown.",
    "changed": "The comment doesn't match, but the macro was edited after this use. "
               "Zendesk only gives us the current version, so we can't tell.",
}


@dataclass
class Use:
    audit_id: int
    ticket_id: int
    created_at: datetime
    agent_id: str
    macro_id: str
    macro_title: str
    comment_type: str            # "public", "internal", or "" when no comment was saved
    status: str
    pct_of_macro: float | None   # how much of the macro was edited, where 0 means intact
    combined_with: list[str] = field(default_factory=list)
    diff: list[tuple[str, list[str]]] | None = None
    added_text: bool = False     # kept intact, with the agent's own text around it
    combined_with_ids: list[str] = field(default_factory=list)  # the IDs of the macros in combined_with
    diff_macro_ids: list[str] = field(default_factory=list)     # the macro behind each ("macro", [title]) step of diff
    requester_id: str | None = None


@dataclass
class Result:
    since: datetime
    until: datetime
    uses: list[Use]
    users: dict[str, dict]
    overall: dict
    agents: list[dict]
    macros: list[dict]
    coverage: Coverage = field(default_factory=Coverage)
    all_macros: dict[str, dict] = field(default_factory=dict)  # every macro fetched, used or not
    word_spans: dict[str, list | None] = field(default_factory=dict)  # per compared macro, see compile_macro


def macro_text(macro: dict) -> str | None:
    """The comment text a macro inserts, or None if it doesn't add a comment."""
    plain = rich = None
    for action in macro.get("actions", []):
        value = action.get("value")
        if isinstance(value, list):  # older macros store ["channel:all", "text"]
            value = value[-1] if value else ""
        if action.get("field") == "comment_value":
            plain = value
        elif action.get("field") == "comment_value_html":
            rich = value
    # The rich text version is what the agent workspace inserts when both exist.
    text = html_to_text(rich) if rich and rich.strip() else plain
    return text if text and text.strip() else None


def agent_name(users: dict[str, dict], agent_id: str) -> str:
    return users.get(agent_id, {}).get("name") or f"User {agent_id}"


def analyze(audits: list[dict], macros: dict[str, dict], users: dict[str, dict],
            dynamic: dict[str, list[str]], since: datetime, until: datetime,
            coverage: Coverage | None = None, requesters: dict[int, str] | None = None) -> Result:
    requesters = requesters or {}
    # `macros` can hold every macro in the account. Only the used ones are compared.
    used = {str(e["macro_id"]) for a in audits for e in a["events"] if is_macro_event(e)}
    texts = {mid: macro_text(m) for mid, m in macros.items() if mid in used}
    compiled = {mid: compile_macro(text, dynamic) for mid, text in texts.items() if text}
    uses: list[Use] = []

    for audit in sorted(audits, key=lambda a: a["created_at"]):
        created = parse_time(audit["created_at"])
        # Only the report's period counts. Audits saved while the run was fetching fall after it.
        if not since <= created <= until:
            continue
        refs = [e for e in audit["events"] if is_macro_event(e)]
        # A macro referenced twice in one audit is still one use.
        ref_ids = list(dict.fromkeys(str(e["macro_id"]) for e in refs))
        comment = next((e for e in audit["events"] if e["type"] == "Comment"), None)
        comment_type = "" if comment is None else ("public" if comment.get("public") else "internal")
        titles = {
            str(e["macro_id"]): macros.get(str(e["macro_id"]), {}).get("title")
            or e.get("macro_title") or f"Macro {e['macro_id']}"
            for e in refs
        }

        # Each macro gets its own result.
        to_compare = [mid for mid in ref_ids if mid in compiled and compiled[mid].comparable]
        comparison = results = None
        if comment is not None and to_compare:
            comparison = compare([compiled[mid] for mid in to_compare], comment.get("body") or "")
            results = dict(zip(to_compare, comparison.results))
        # Text from a macro we can't read would look like text the agent added.
        added_text = bool(comparison and comparison.added_text) and all(mid in macros for mid in ref_ids)

        for mid in ref_ids:
            pct_of_macro = diff = None
            diff_macro_ids: list[str] = []
            if mid not in macros:
                status = "unavailable"
            elif not texts[mid]:
                status = "no_text"
            elif not compiled[mid].comparable:
                status = "only_placeholders"
            elif comment is None:
                # The macro put text in the reply box, but the update was saved
                # without a comment, so the agent deleted all of it.
                status, pct_of_macro = "edited", 100.0
                diff = [("delete", compiled[mid].display_words)]
            else:
                result = results[mid]
                pct_of_macro = result.pct_of_macro
                diff, diff_macro_ids = _name_other_macros(result.diff, to_compare, titles)
                updated_at = macros[mid].get("updated_at")
                if result.intact:
                    status = "unedited"
                elif updated_at and parse_time(updated_at) > created:
                    status = "changed"
                else:
                    status = "edited"
            uses.append(Use(
                audit_id=audit["id"],
                ticket_id=audit["ticket_id"],
                created_at=created,
                agent_id=str(audit.get("author_id")),
                macro_id=mid,
                macro_title=titles[mid],
                comment_type=comment_type,
                status=status,
                pct_of_macro=pct_of_macro,
                combined_with=[titles[o] for o in ref_ids if o != mid],
                diff=diff,
                added_text=status == "unedited" and added_text,
                combined_with_ids=[o for o in ref_ids if o != mid],
                diff_macro_ids=diff_macro_ids,
                requester_id=requesters.get(audit["ticket_id"]),
            ))

    return Result(since, until, uses, users, _overall(uses), _by_agent(uses, users), _by_macro(uses, macros),
                  coverage or Coverage(), macros, {mid: m.word_spans for mid, m in compiled.items()})


def _name_other_macros(diff: list[tuple[str, list[str]]] | None, compared: list[str],
                       titles: dict[str, str]) -> tuple[list[tuple[str, list[str]]] | None, list[str]]:
    """Turn each marker for another macro's text into a ("macro", [title]) step of the diff.

    `compared` lists the macro IDs in the order given to compare(). Also returns
    the ID behind each "macro" step, in order.
    """
    if diff is None:
        return None, []
    named: list[tuple[str, list[str]]] = []
    ids: list[str] = []
    for op, words in diff:
        run: list[str] = []
        for word in words:
            other = macro_marker(word)
            if other is None:
                run.append(word)
                continue
            if run:
                named.append((op, run))
                run = []
            named.append(("macro", [titles[compared[other]]]))
            ids.append(compared[other])
        if run:
            named.append((op, run))
    return named, ids


def _pct(part: int, whole: int) -> float | None:
    return round(100 * part / whole, 1) if whole else None


def _average(values: Iterable[float]) -> float | None:
    values = list(values)
    return round(mean(values), 1) if values else None


def _tally(uses: list[Use]) -> dict:
    """The compared, edited and intact counts for any group of uses."""
    compared = [u for u in uses if u.status in COUNTED]
    edited = sum(u.status == "edited" for u in compared)
    return {
        "compared": len(compared),
        "edited": edited,
        "unedited": len(compared) - edited,
        "pct_uses_edited": _pct(edited, len(compared)),
    }


def _averages(uses: list[Use]) -> dict:
    """How much of the macro was edited, over all compared uses and over only the edited ones."""
    compared = [u for u in uses if u.status in COUNTED]
    return {
        "avg_pct_of_macro": _average(u.pct_of_macro for u in compared),
        "avg_pct_of_macro_when_edited": _average(u.pct_of_macro for u in compared if u.status == "edited"),
    }


def _group(uses: list[Use], key: str) -> dict[str, list[Use]]:
    groups: dict[str, list[Use]] = defaultdict(list)
    for u in uses:
        groups[getattr(u, key)].append(u)
    return groups


def _overall(uses: list[Use]) -> dict:
    tally = _tally(uses)
    counted = [u for u in uses if u.status in COUNTED]
    agents_using = {u.agent_id for u in counted}
    agents_editing = {u.agent_id for u in counted if u.status == "edited"}
    return {
        "uses": len(uses),
        **tally,
        "pct_uses_intact": _pct(tally["unedited"], tally["compared"]),
        "added_text": sum(u.added_text for u in counted),
        "excluded": Counter(u.status for u in uses if u.status not in COUNTED),
        # "none" is a use whose macro text was deleted entirely, so no comment was saved.
        "by_type": {kind: _tally([u for u in uses if u.comment_type == comment_type])
                    for kind, comment_type in (("public", "public"), ("internal", "internal"), ("none", ""))},
        "agents_using": len(agents_using),
        "agents_editing": len(agents_editing),
        "pct_agents_editing": _pct(len(agents_editing), len(agents_using)),
        "macros_used": len({u.macro_id for u in uses}),
        **_averages(uses),
    }


def _by_agent(uses: list[Use], users: dict[str, dict]) -> list[dict]:
    rows = [
        {
            "agent_id": agent_id,
            "name": agent_name(users, agent_id),
            "email": users.get(agent_id, {}).get("email", ""),
            "uses": len(own),
            **_tally(own),
        }
        for agent_id, own in _group(uses, "agent_id").items()
    ]
    return sorted(rows, key=lambda r: (-r["uses"], r["name"]))


def _by_macro(uses: list[Use], macros: dict[str, dict]) -> list[dict]:
    rows = []
    for macro_id, own in _group(uses, "macro_id").items():
        tally = _tally(own)
        rows.append({
            "macro_id": macro_id,
            "title": own[0].macro_title,
            "uses": len(own),
            **tally,
            **_averages(own),
            "public": sum(u.comment_type == "public" for u in own),
            "internal": sum(u.comment_type == "internal" for u in own),
            "excluded": len(own) - tally["compared"],
            # Zendesk's own count, as a check that we found every use.
            "zendesk_usage_30d": macros.get(macro_id, {}).get("usage_30d"),
        })
    return sorted(rows, key=lambda r: (-r["uses"], r["title"].lower()))
