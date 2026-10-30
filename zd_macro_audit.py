#!/usr/bin/env python3
"""Measure how often Zendesk macros are edited before they're sent.

    python3 zd_macro_audit.py              # run against your Zendesk account
    python3 zd_macro_audit.py --api-token  # sign in with an existing API token
    python3 zd_macro_audit.py --sample     # try it with made-up data

See README.md for the one-time Zendesk setup.
"""
from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from zdaudit import analyze, report, sample, zendesk

HERE = Path(__file__).resolve().parent


def clean_subdomain(value: str) -> str:
    """Accept 'acme', 'acme.zendesk.com' or 'https://acme.zendesk.com/agent'."""
    value = re.sub(r"^https?://", "", value.strip().lower()).split("/")[0]
    return re.sub(r"\.zendesk\.com$", "", value)


ENV_VARS = {"subdomain": "ZENDESK_SUBDOMAIN", "client_id": "ZENDESK_CLIENT_ID",
            "client_secret": "ZENDESK_CLIENT_SECRET", "email": "ZENDESK_EMAIL",
            "api_token": "ZENDESK_API_TOKEN"}


def load_config(path: Path, auth: str | None = None) -> dict:
    """Settings from config.json and the environment, asking for anything missing.

    `auth` is "token" or "oauth" when chosen on the command line. Otherwise a
    saved API token wins, then a saved OAuth client, and a first run asks.
    """
    saved = json.loads(path.read_text()) if path.exists() else {}
    config = {key: os.environ.get(env) or saved.get(key, "") for key, env in ENV_VARS.items()}
    asked = False

    def ask(key: str, prompt: str, hidden: bool = False) -> None:
        nonlocal asked
        value = (getpass.getpass if hidden else input)(prompt).strip()
        config[key] = saved[key] = clean_subdomain(value) if key == "subdomain" else value
        asked = True

    if not config["subdomain"]:
        print("First-time setup. README.md explains where to find each of these.\n")
        ask("subdomain", "Zendesk subdomain (the 'acme' in acme.zendesk.com): ")
    if not auth:
        if config["api_token"]:
            auth = "token"
        elif config["client_id"]:
            auth = "oauth"
        else:
            choice = input("Sign in with (1) OAuth or (2) an existing API token? [1]: ").strip()
            auth = "token" if choice == "2" else "oauth"

    if auth == "token":
        if not config["email"]:
            ask("email", "Email of the admin who owns the API token: ")
        if not config["api_token"]:
            ask("api_token", "Zendesk API token (hidden): ", hidden=True)
        required = ("subdomain", "email", "api_token")
    else:
        config["api_token"] = ""
        if not config["client_id"]:
            ask("client_id", "OAuth client unique identifier: ")
            ask("client_secret", "OAuth client secret (hidden, leave blank for a public client): ", hidden=True)
        required = ("subdomain", "client_id")

    if not all(config[key] for key in required):
        sys.exit("Missing settings: " + ", ".join(k for k in required if not config[k]))
    config["subdomain"] = clean_subdomain(config["subdomain"])
    if asked:
        path.write_text(json.dumps(saved, indent=2) + "\n")
        os.chmod(path, 0o600)
        print(f"Saved to {path.name}. Delete that file to start over.\n")
    return config


def print_summary(result: analyze.Result, paths: dict[str, Path]) -> None:
    o = result.overall
    fmt = lambda v: "n/a" if v is None else f"{v:.0f}%"
    print(f"\nMacro uses found:     {o['uses']:,} ({o['macros_used']:,} macros)")
    print(f"Compared:             {o['compared']:,}")
    print(f"Sent edited:          {o['edited']:,} ({fmt(o['pct_edited'])})")
    print(f"Sent as is:           {o['unedited']:,} ({fmt(o['pct_unedited'])})")
    print(f"Agents who edit:      {o['agents_editing']:,} of {o['agents_using']:,} ({fmt(o['pct_agents_editing'])})")
    for kind, label in (("public", "Public"), ("internal", "Internal"), ("none", "No comment")):
        t = o["by_type"][kind]
        if t["compared"] or kind != "none":
            print(f"  {label + ':':<19} {t['edited']:,} of {t['compared']:,} edited ({fmt(t['pct_edited'])})")
    skipped = sum(o["excluded"].values())
    if skipped:
        print(f"Not compared:         {skipped:,} (see the report for why)")
    print(f"\nReport: {paths['report']}")
    print(f"CSVs:   {paths['uses'].parent}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Measure how often Zendesk macros are edited before sending.")
    parser.add_argument("--days", type=int, default=30, help="how many days back to look (default 30)")
    parser.add_argument("--out", default=str(HERE / "reports"), help="folder for reports (default ./reports)")
    parser.add_argument("--config", default=str(HERE / "config.json"), help="settings file (default ./config.json)")
    sign_in = parser.add_mutually_exclusive_group()
    sign_in.add_argument("--api-token", dest="auth", action="store_const", const="token",
                         help="sign in with an admin's existing Zendesk API token instead of OAuth")
    sign_in.add_argument("--oauth", dest="auth", action="store_const", const="oauth",
                         help="sign in with OAuth even if an API token is saved")
    parser.add_argument("--login", action="store_true", help="OAuth only. Sign in again even if a saved sign-in exists")
    parser.add_argument("--sample", action="store_true", help="use made-up data instead of Zendesk")
    parser.add_argument("--workers", type=int, default=4,
                        help="tickets to read at once (default 4). Lower it if other tools share your API limit")
    args = parser.parse_args(argv)

    until = datetime.now(timezone.utc)
    since = until - timedelta(days=args.days)

    try:
        if args.sample:
            print("Using made-up sample data.")
            data, subdomain = sample.generate(since, until), None
        else:
            config = load_config(Path(args.config), args.auth)
            subdomain = config["subdomain"]
            zd = zendesk.Zendesk(subdomain, config["client_id"], config["client_secret"], HERE / ".tokens",
                                 email=config["email"], api_token=config["api_token"])
            zd.authenticate(force=args.login)
            data = zendesk.fetch_all(zd, since, workers=max(1, args.workers))
    except (zendesk.AuthError, zendesk.ZendeskError) as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nStopped.")
        return 130

    print("Comparing messages with macro text...")
    result = analyze.analyze(**data, since=since, until=until)
    out_dir = Path(args.out) / datetime.now().strftime("%Y-%m-%d_%H%M%S")
    paths = report.write_all(out_dir, result, subdomain)
    print_summary(result, paths)
    return 0


if __name__ == "__main__":
    sys.exit(main())
