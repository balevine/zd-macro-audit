"""Turn raw audits and macros into per-use results and summary stats."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from statistics import mean
from typing import Iterable

from .compare import compare, compile_macro, html_to_text
from .zendesk import is_macro_event, parse_time

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
    pct_edited: float | None     # how much of the macro was edited, where 0 means intact
    combined_with: list[str] = field(default_factory=list)
    diff: list[tuple[str, list[str]]] | None = None
    added_text: bool = False     # kept intact, with the agent's own text around it


@dataclass
class Result:
    since: datetime
    until: datetime
    uses: list[Use]
    users: dict[str, dict]
    overall: dict
    agents: list[dict]
    macros: list[dict]


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


def pct(part: int, whole: int) -> float | None:
    return round(100 * part / whole, 1) if whole else None


def agent_name(users: dict[str, dict], agent_id: str) -> str:
    return users.get(agent_id, {}).get("name") or f"User {agent_id}"


def analyze(audits: list[dict], macros: dict[str, dict], users: dict[str, dict],
            dynamic: dict[str, list[str]], since: datetime, until: datetime) -> Result:
    texts = {mid: macro_text(m) for mid, m in macros.items()}
    compiled = {mid: compile_macro(text, dynamic) for mid, text in texts.items() if text}
    uses: list[Use] = []

    for audit in sorted(audits, key=lambda a: a["created_at"]):
        created = parse_time(audit["created_at"])
        refs = [e for e in audit["events"] if is_macro_event(e) and e.get("macro_id") is not None]
        ref_ids = [str(e["macro_id"]) for e in refs]
        comment = next((e for e in audit["events"] if e["type"] == "Comment"), None)
        comment_type = "" if comment is None else ("public" if comment.get("public") else "internal")
        titles = {
            str(e["macro_id"]): macros.get(str(e["macro_id"]), {}).get("title")
            or e.get("macro_title") or f"Macro {e['macro_id']}"
            for e in refs
        }

        # Each macro gets its own result. A macro applied twice is compared once.
        to_compare = [mid for mid in dict.fromkeys(ref_ids) if mid in compiled and compiled[mid].comparable]
        comparison = results = None
        if comment is not None and to_compare:
            comparison = compare([compiled[mid] for mid in to_compare], comment.get("body") or "")
            results = dict(zip(to_compare, comparison.results))
        # Text from a macro we can't read would look like text the agent added.
        added_text = bool(comparison and comparison.added_text) and all(mid in macros for mid in ref_ids)

        for mid in ref_ids:
            pct_edited = diff = None
            if mid not in macros:
                status = "unavailable"
            elif not texts[mid]:
                status = "no_text"
            elif not compiled[mid].comparable:
                status = "only_placeholders"
            elif comment is None:
                # The macro put text in the reply box, but the update was saved
                # without a comment, so the agent deleted all of it.
                status, pct_edited = "edited", 100.0
                diff = [("delete", compiled[mid].display_words)]
            else:
                result = results[mid]
                pct_edited, diff = result.pct_edited, result.diff
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
                pct_edited=pct_edited,
                combined_with=[titles[o] for o in ref_ids if o != mid],
                diff=diff,
                added_text=status == "unedited" and added_text,
            ))

    return Result(since, until, uses, users, _overall(uses), _by_agent(uses, users), _by_macro(uses, macros))


def _average(values: Iterable[float]) -> float | None:
    values = list(values)
    return round(mean(values), 1) if values else None


def _overall(uses: list[Use]) -> dict:
    counted = [u for u in uses if u.status in COUNTED]
    edited = [u for u in counted if u.status == "edited"]
    agents_using = {u.agent_id for u in counted}
    agents_editing = {u.agent_id for u in edited}
    by_type = {}
    # "none" is a use whose macro text was deleted entirely, so no comment was saved.
    for kind, comment_type in (("public", "public"), ("internal", "internal"), ("none", "")):
        kind_counted = [u for u in counted if u.comment_type == comment_type]
        kind_edited = sum(u.status == "edited" for u in kind_counted)
        by_type[kind] = {
            "compared": len(kind_counted),
            "edited": kind_edited,
            "unedited": len(kind_counted) - kind_edited,
            "pct_edited": pct(kind_edited, len(kind_counted)),
        }
    return {
        "uses": len(uses),
        "compared": len(counted),
        "edited": len(edited),
        "unedited": len(counted) - len(edited),
        "added_text": sum(u.added_text for u in counted),
        "pct_edited": pct(len(edited), len(counted)),
        "pct_unedited": pct(len(counted) - len(edited), len(counted)),
        "excluded": Counter(u.status for u in uses if u.status not in COUNTED),
        "by_type": by_type,
        "agents_using": len(agents_using),
        "agents_editing": len(agents_editing),
        "pct_agents_editing": pct(len(agents_editing), len(agents_using)),
        "macros_used": len({u.macro_id for u in uses}),
        "avg_pct_of_macro": _average(u.pct_edited for u in counted),
        "avg_pct_of_macro_when_edited": _average(u.pct_edited for u in edited),
    }


def _by_agent(uses: list[Use], users: dict[str, dict]) -> list[dict]:
    rows: dict[str, dict] = defaultdict(lambda: {"uses": 0, "edited": 0, "unedited": 0})
    for u in uses:
        row = rows[u.agent_id]
        row["uses"] += 1
        if u.status in COUNTED:
            row[u.status] += 1
    out = []
    for agent_id, row in rows.items():
        compared = row["edited"] + row["unedited"]
        out.append({
            "agent_id": agent_id,
            "name": agent_name(users, agent_id),
            "email": users.get(agent_id, {}).get("email", ""),
            **row,
            "compared": compared,
            "pct_edited": pct(row["edited"], compared),
        })
    return sorted(out, key=lambda r: (-r["uses"], r["name"]))


def _by_macro(uses: list[Use], macros: dict[str, dict]) -> list[dict]:
    rows: dict[str, dict] = {}
    for u in uses:
        row = rows.setdefault(u.macro_id, {
            "macro_id": u.macro_id, "title": u.macro_title, "uses": 0, "edited": 0,
            "unedited": 0, "public": 0, "internal": 0, "excluded": 0, "_scores": [],
        })
        row["uses"] += 1
        if u.comment_type:
            row[u.comment_type] += 1
        if u.status in COUNTED:
            row[u.status] += 1
            row["_scores"].append(u.pct_edited)
        else:
            row["excluded"] += 1
    out = []
    for row in rows.values():
        scores = row.pop("_scores")
        compared = row["edited"] + row["unedited"]
        row.update(
            compared=compared,
            pct_edited=pct(row["edited"], compared),
            avg_pct_of_macro=_average(scores),
            avg_pct_of_macro_when_edited=_average(s for s in scores if s > 0),
            # Zendesk's own count, as a check that we found every use.
            zendesk_usage_30d=macros.get(row["macro_id"], {}).get("usage_30d"),
        )
        out.append(row)
    return sorted(out, key=lambda r: (-r["uses"], r["title"].lower()))
