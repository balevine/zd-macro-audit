"""Made-up Zendesk data, so the tool can be tried without a Zendesk account.

The data has the same shape as what the Zendesk API returns, and it covers
the tricky cases: placeholders, dynamic content, internal notes, several
macros in one update, a deleted macro, and a macro edited mid-period.
"""
from __future__ import annotations

import random
import re
from collections import Counter
from datetime import datetime, timedelta

from .compare import html_to_text
from .zendesk import MACRO_EVENT_TYPES

# id, name, email, how likely this agent is to edit a macro
AGENTS = [
    (1001, "Ana Ruiz", "ana@example.com", 0.10),
    (1002, "Ben Okafor", "ben@example.com", 0.55),
    (1003, "Chloe Park", "chloe@example.com", 0.25),
    (1004, "Dev Mehta", "dev@example.com", 0.80),
    (1005, "Erin Walsh", "erin@example.com", 0.0),
    (1006, "Femi Adeyemi", "femi@example.com", 0.40),
]

REQUESTERS = ["Sam", "Priya", "Jordan", "Lena", "Marco", "Aiko", "Tom", "Grace"]

DYNAMIC = {
    "dc.sign_off": [
        "Thanks for your patience,\nThe Support Team",
        "Merci de votre patience,\nL'équipe d'assistance",
    ],
}

SHIPPING_OLD = ("Hi {{ticket.requester.first_name}},\n\nSorry your order is running late. "
                "Our carrier is behind this week, and we expect it to ship within 5 business days.")

EDITS = [
    lambda t: t + " Let me know if you have any other questions!",
    lambda t: t.replace(" the ", " our ", 1),
    lambda t: "Thanks for reaching out. " + t,
    lambda t: t.split(". ")[0] + ". I've also added a $10 credit to your account for the trouble.",
    lambda t: t.replace("Hi ", "Hello ", 1),
    lambda t: re.sub(r"\d+", "7", t, count=1),
]

# macro id: (weight, how much more or less often agents edit it)
USAGE = {2001: (30, 0.6), 2002: (18, 1.0), 2003: (12, 0.3), 2004: (10, 0), 2005: (12, 1.4),
         2006: (15, 0.5), 2007: (8, 1.8), 2099: (3, 1.0)}


def _macros(old: str, changed: str) -> dict[str, dict]:
    def macro(mid, title, actions, updated=old):
        return {"id": mid, "title": title, "actions": actions, "updated_at": updated, "active": True}

    items = [
        macro(2001, "Password reset steps", [{"field": "comment_value_html", "value":
            "<p>Hi {{ticket.requester.first_name}},</p><p>You can reset your password from the sign-in "
            "page. Click <strong>Forgot password</strong> and follow the link we email you. The link "
            "works for 24 hours.</p><p>Thanks,<br>{{current_user.first_name}}</p>"}]),
        macro(2002, "Refund approved", [{"field": "comment_value", "value":
            "Hi {{ticket.requester.first_name}},\n\nGood news. Your refund for order #{{ticket.id}} is "
            "approved. You'll see it on your statement in 5 to 10 business days.\n\n{{dc.sign_off}}"}]),
        macro(2003, "Escalate to billing", [
            {"field": "comment_value", "value":
                "Escalating to billing. The customer reports a double charge. Please check the payment log."},
            {"field": "comment_mode_is_public", "value": "false"},
            {"field": "group_id", "value": "360001"}]),
        macro(2004, "Set priority to urgent", [{"field": "priority", "value": "urgent"}]),
        macro(2005, "Shipping delay apology", [{"field": "comment_value", "value":
            "Hi {{ticket.requester.first_name}},\n\nSorry your order is running late. Our carrier is "
            "behind this week, and we expect it to ship within 3 business days."}], updated=changed),
        macro(2006, "Closing, no response", [{"field": "comment_value", "value":
            "Hi {{ticket.requester.first_name}},\n\nWe haven't heard back, so we're closing this ticket "
            "for now. Just reply to this email if you still need help and it will reopen."},
            {"field": "status", "value": "solved"}]),
        macro(2007, "Ask for a screenshot", [{"field": "comment_value", "value":
            "Hi {{ticket.requester.first_name}},\n\nCould you send a screenshot of what you're seeing? "
            "It helps us find the problem faster.{% if ticket.requester.language == 'English' %} Thanks!{% endif %}"}]),
    ]
    return {str(m["id"]): m for m in items}


def _render(text: str, requester: str, agent: str, ticket_id: int, rng: random.Random) -> str:
    if text.lstrip().startswith("<"):
        text = html_to_text(text)
    text = text.replace("{{ticket.requester.first_name}}", requester)
    text = text.replace("{{current_user.first_name}}", agent.split()[0])
    text = text.replace("{{ticket.id}}", str(ticket_id))
    text = text.replace("{{dc.sign_off}}", rng.choice(DYNAMIC["dc.sign_off"]))
    return re.sub(r"\{%.*?%\}", "", text)


def generate(since: datetime, until: datetime, seed: int = 7, count: int = 420) -> dict:
    rng = random.Random(seed)
    stamp = lambda d: d.strftime("%Y-%m-%dT%H:%M:%SZ")
    changed_at = since + (until - since) * 0.4
    macros = _macros(stamp(since - timedelta(days=90)), stamp(changed_at))
    ids, weights = list(USAGE), [w for w, _ in USAGE.values()]
    audits = []

    for n in range(count):
        created = since + timedelta(seconds=rng.uniform(0, (until - since).total_seconds()))
        agent_id, agent, email, edit_rate = rng.choice(AGENTS)
        ticket_id = 48000 + rng.randrange(5000)
        refs = [rng.choices(ids, weights)[0]]
        if refs[0] == 2004 and rng.random() < 0.5:
            refs.append(2003)

        texts = []
        for mid in refs:
            if mid == 2099:
                texts.append("Hi {{ticket.requester.first_name}}, our office is closed for the holidays.")
            elif mid == 2005 and created < changed_at:
                texts.append(SHIPPING_OLD)
            else:
                action = next((a for a in macros[str(mid)]["actions"]
                               if a["field"] in ("comment_value", "comment_value_html")), None)
                if action:
                    texts.append(action["value"])

        requester = rng.choice(REQUESTERS)
        body = "\n\n".join(_render(t, requester, agent, ticket_id, rng) for t in texts)
        factor = max(USAGE[mid][1] for mid in refs)
        if body and rng.random() < edit_rate * factor:
            body = rng.choice(EDITS)(body)
        if not body and rng.random() < 0.6:
            body = "Bumping this to urgent. The customer is blocked."

        public = 2003 not in refs and rng.random() > 0.05
        events = [{"type": rng.choice(MACRO_EVENT_TYPES), "macro_id": mid,
                   "macro_title": "Holiday hours 2025" if mid == 2099 else macros[str(mid)]["title"]}
                  for mid in refs]
        # Now and then an agent who edits deletes the macro's text entirely.
        deleted_all = bool(texts) and rng.random() < edit_rate * 0.08
        if body and not deleted_all:
            events.append({"type": "Comment", "body": body, "public": public})
        audits.append({"id": 9_000_000 + n, "ticket_id": ticket_id, "created_at": stamp(created),
                       "author_id": agent_id, "events": events})

    # Zendesk's own counts include a few uses on tickets we didn't read.
    used = Counter(str(e["macro_id"]) for a in audits for e in a["events"] if "macro_id" in e)
    for mid, macro in macros.items():
        macro["usage_30d"] = used[mid] + rng.randrange(3)

    users = {str(a[0]): {"id": a[0], "name": a[1], "email": a[2], "role": "agent"} for a in AGENTS}
    return {"audits": audits, "macros": macros, "users": users, "dynamic": DYNAMIC}
