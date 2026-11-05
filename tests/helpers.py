from datetime import datetime, timedelta, timezone

from zdaudit.analyze import analyze

UNTIL = datetime(2026, 10, 30, tzinfo=timezone.utc)
SINCE = UNTIL - timedelta(days=30)
USED_AT = "2026-10-20T00:00:00Z"


def use_macro(mid, text, updated="2026-01-01T00:00:00Z", title=None, **extra):
    return {"id": mid, "title": title or f"Macro {mid}", "updated_at": updated,
            "actions": [{"field": "comment_value", "value": text}], **extra}


def fields_only_macro(mid):
    return {"id": mid, "title": f"Macro {mid}", "updated_at": "2026-01-01T00:00:00Z",
            "actions": [{"field": "priority", "value": "urgent"}]}


def make_audit(refs, body=None, public=True, audit_id=5, ticket_id=9, agent=7, created=USED_AT):
    events = [{"type": "MacroReference", "macro_id": mid} for mid in refs]
    if body is not None:
        events.append({"type": "Comment", "body": body, "public": public})
    return {"id": audit_id, "ticket_id": ticket_id, "created_at": created, "author_id": agent, "events": events}


def run_audits(macros, audits, users=None, since=SINCE, until=UNTIL):
    return analyze(audits, {str(m["id"]): m for m in macros}, users or {}, {}, since, until)


def run_audit(macros, refs, body=None, **audit):
    return run_audits(macros, [make_audit(refs, body, **audit)])
