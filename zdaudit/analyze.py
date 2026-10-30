"""Turn raw audits and macros into per-use results and summary stats."""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from statistics import mean

from .compare import Comparison, build_template, compare, html_to_text, normalize, word_diff
from .zendesk import is_macro_event, parse_time

# Only these two statuses count toward the edited percentage.
COUNTED = ("edited", "unedited")

STATUS_LABELS = {
    "unedited": "Sent as is",
    "edited": "Edited",
    "no_text": "Macro has no comment text",
    "unavailable": "Macro deleted or unreadable",
    "changed": "Macro changed since use",
}

STATUS_HELP = {
    "no_text": "The macro only changes fields or tags, so there's no text to compare.",
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
    similarity: float | None
    combined_with: list[str] = field(default_factory=list)
    diff: list[tuple[str, list[str]]] | None = None


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

        missing = any(mid not in macros for mid in ref_ids)
        with_text = [texts[mid] for mid in ref_ids if mid in macros and texts[mid]]
        comparison = diff = None
        if not missing and with_text and comment is None:
            # The macro put text in the reply box, but the update was saved
            # without a comment, so the agent deleted all of it.
            template = build_template("\n\n".join(with_text), dynamic)
            comparison = Comparison(False, 0.0, template)
            diff = [("delete", template.display_words)]
        elif not missing and with_text:
            body = comment.get("body") or ""
            comparison = compare(with_text, body, dynamic)
            if not comparison.matched:
                diff = word_diff(comparison.template.display_words, normalize(body).split())
        changed_since = any(
            macros[mid].get("updated_at") and parse_time(macros[mid]["updated_at"]) > created
            for mid in ref_ids if mid in macros
        )

        for mid in ref_ids:
            if mid not in macros:
                status = "unavailable"
            elif not texts[mid]:
                status = "no_text"
            elif missing:
                status = "unavailable"   # combined with a macro whose text we can't read
            elif comment is None:
                status = "edited"        # all of the macro's text was deleted
            elif comparison.matched:
                status = "unedited"
            elif changed_since:
                status = "changed"
            else:
                status = "edited"
            has_score = status in ("edited", "unedited", "changed")
            uses.append(Use(
                audit_id=audit["id"],
                ticket_id=audit["ticket_id"],
                created_at=created,
                agent_id=str(audit.get("author_id")),
                macro_id=mid,
                macro_title=titles[mid],
                comment_type=comment_type,
                status=status,
                similarity=comparison.similarity if has_score else None,
                combined_with=[titles[o] for o in ref_ids if o != mid],
                diff=diff if status in ("edited", "changed") else None,
            ))

    return Result(since, until, uses, users, _overall(uses), _by_agent(uses, users), _by_macro(uses, macros))


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
        "pct_edited": pct(len(edited), len(counted)),
        "pct_unedited": pct(len(counted) - len(edited), len(counted)),
        "excluded": Counter(u.status for u in uses if u.status not in COUNTED),
        "by_type": by_type,
        "agents_using": len(agents_using),
        "agents_editing": len(agents_editing),
        "pct_agents_editing": pct(len(agents_editing), len(agents_using)),
        "macros_used": len({u.macro_id for u in uses}),
        "avg_similarity_edited": round(mean(u.similarity for u in edited), 1) if edited else None,
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
            if u.status == "edited":
                row["_scores"].append(u.similarity)
        else:
            row["excluded"] += 1
    out = []
    for row in rows.values():
        scores = row.pop("_scores")
        compared = row["edited"] + row["unedited"]
        row.update(
            compared=compared,
            pct_edited=pct(row["edited"], compared),
            avg_similarity_edited=round(mean(scores), 1) if scores else None,
            # Zendesk's own count, as a check that we found every use.
            zendesk_usage_30d=macros.get(row["macro_id"], {}).get("usage_30d"),
        )
        out.append(row)
    return sorted(out, key=lambda r: (-r["uses"], r["title"].lower()))
