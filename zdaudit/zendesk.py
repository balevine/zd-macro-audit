"""Zendesk sign-in and the API calls this tool needs.

Uses only the Python standard library. Sign-in uses the OAuth authorization
code flow with PKCE. The browser sends Zendesk's reply to a small server on
http://localhost:8765/callback, and the token is saved under .tokens/ so later
runs skip the browser step.

An admin who already has a Zendesk API token can use it instead of OAuth.
Requests then use basic auth with "{email}/token:{api_token}".
"""
from __future__ import annotations

import base64
import hashlib
import http.server
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterable, Iterator

REDIRECT_PORT = 8765
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
SCOPE = "read"
USER_AGENT = "zd-macro-audit/1.0"

# Zendesk's audit events reference names this event "MacroReference", but
# "AgentMacroReference" has also been seen in API responses, so accept both.
MACRO_EVENT_TYPES = ("MacroReference", "AgentMacroReference")


class AuthError(Exception):
    pass


class ZendeskError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def is_macro_event(event: dict) -> bool:
    return event.get("type") in MACRO_EVENT_TYPES


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _wait_for_redirect(expected_state: str, timeout: int = 300) -> dict[str, str]:
    """Run a one-off local server until Zendesk redirects the browser back to it."""
    result: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                self.send_response(404)
                self.end_headers()
                return
            params = {k: v[0] for k, v in urllib.parse.parse_qs(parsed.query).items()}
            result.update(params)
            ok = "code" in params and params.get("state") == expected_state
            message = (
                "Signed in. You can close this tab and go back to the terminal."
                if ok
                else "Sign-in didn't finish. Check the terminal for details."
            )
            body = (
                "<!doctype html><meta charset='utf-8'><title>zd-macro-audit</title>"
                f"<body style='font-family:system-ui,sans-serif;padding:3em'><h2>{message}</h2></body>"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    try:
        server = http.server.HTTPServer(("127.0.0.1", REDIRECT_PORT), Handler)
    except OSError as exc:
        raise AuthError(
            f"Couldn't listen on port {REDIRECT_PORT} for the sign-in redirect ({exc}). "
            "Close whatever is using that port and try again."
        ) from exc
    server.timeout = 1
    deadline = time.time() + timeout
    try:
        while "code" not in result and "error" not in result and time.time() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    return result


class Zendesk:
    def __init__(self, subdomain: str, client_id: str = "", client_secret: str = "",
                 token_dir: Path = Path(".tokens"), email: str = "", api_token: str = ""):
        self.subdomain = subdomain
        self.client_id = client_id
        self.client_secret = client_secret
        self.email = email
        self.api_token = api_token
        self.base = f"https://{subdomain}.zendesk.com"
        self.token_path = token_dir / f"{subdomain}.json"
        self.token: dict = {}
        self._refresh_lock = threading.Lock()

    # ---- Sign-in -----------------------------------------------------------

    def authenticate(self, force: bool = False) -> None:
        if self.api_token:
            self._token_login()
            return
        if not force and self.token_path.exists():
            try:
                self.token = json.loads(self.token_path.read_text())
            except (OSError, json.JSONDecodeError):
                self.token = {}
        if not self.token.get("access_token"):
            self.login()
            return
        try:
            me = self.get("/api/v2/users/me.json")["user"]
        except AuthError:
            self.login()
            me = self.get("/api/v2/users/me.json")["user"]
        self._check_role(me)

    def login(self) -> None:
        verifier, challenge = _pkce_pair()
        state = secrets.token_urlsafe(16)
        query = urllib.parse.urlencode({
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPE,
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        })
        url = f"{self.base}/oauth/authorizations/new?{query}"
        print("\nOpening your browser to sign in to Zendesk.")
        print("If it doesn't open, paste this link into your browser:\n")
        print(f"  {url}\n")
        webbrowser.open(url)
        reply = _wait_for_redirect(state)
        if "error" in reply:
            detail = reply.get("error_description", reply["error"]).replace("+", " ")
            raise AuthError(f"Zendesk didn't grant access: {detail}")
        if "code" not in reply:
            raise AuthError("Timed out waiting for the Zendesk sign-in to finish.")
        if reply.get("state") != state:
            raise AuthError("The sign-in reply didn't match this request. Please try again.")

        payload = {
            "grant_type": "authorization_code",
            "code": reply["code"],
            "client_id": self.client_id,
            "redirect_uri": REDIRECT_URI,
            "scope": SCOPE,
            "code_verifier": verifier,
        }
        if self.client_secret:
            payload["client_secret"] = self.client_secret
        self._save_token(self._post_token(payload))
        me = self.get("/api/v2/users/me.json")["user"]
        print(f"Signed in as {me.get('name')} ({me.get('email')}).")
        self._check_role(me)

    def _token_login(self) -> None:
        """Check the API token works. There's nothing to save, since it doesn't expire."""
        try:
            me = self.get("/api/v2/users/me.json")["user"]
        except AuthError:
            raise AuthError(
                f"Zendesk didn't accept the API token for {self.email}. Check the email and "
                "token, and that token access is turned on in Admin Center."
            ) from None
        if not me.get("id"):
            # When Zendesk ignores the credentials, it describes an anonymous user.
            raise AuthError(f"Zendesk didn't accept the API token for {self.email}. Check the email and token.")
        print(f"Signed in with an API token as {me.get('name')} ({me.get('email')}).")
        self._check_role(me)

    def _auth_header(self, access_token: str) -> str:
        if self.api_token:
            pair = f"{self.email}/token:{self.api_token}".encode()
            return "Basic " + base64.b64encode(pair).decode()
        return f"Bearer {access_token}"

    def _check_role(self, me: dict) -> None:
        if me.get("role") != "admin":
            print(
                f"\nWarning: {me.get('email')} is a {me.get('role')}, not an admin. "
                "Agents can't see every ticket or other agents' personal macros, so "
                "the results may be incomplete.\n"
            )

    def _refresh(self, used_token: str) -> bool:
        """Swap the refresh token for a new access token. Safe to call from several threads."""
        with self._refresh_lock:
            if self.token.get("access_token") != used_token:
                return True  # another thread already refreshed it
            refresh_token = self.token.get("refresh_token")
            if not refresh_token:
                return False
            payload = {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.client_id,
                "scope": SCOPE,
            }
            if self.client_secret:
                payload["client_secret"] = self.client_secret
            try:
                self._save_token(self._post_token(payload))
                return True
            except AuthError:
                return False

    def _post_token(self, payload: dict) -> dict:
        request = urllib.request.Request(
            f"{self.base}/oauth/tokens",
            data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise AuthError(f"Zendesk rejected the token request ({exc.code}): {detail}") from exc

    def _save_token(self, token: dict) -> None:
        if not token.get("refresh_token") and self.token.get("refresh_token"):
            token["refresh_token"] = self.token["refresh_token"]
        self.token = token
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(json.dumps(token))
        os.chmod(self.token_path, 0o600)

    # ---- Requests ----------------------------------------------------------

    def get(self, path_or_url: str) -> dict:
        url = path_or_url if path_or_url.startswith("http") else self.base + path_or_url
        refreshed = False
        for attempt in range(8):
            used_token = self.token.get("access_token", "")
            request = urllib.request.Request(url, headers={
                "Authorization": self._auth_header(used_token),
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            })
            try:
                with urllib.request.urlopen(request, timeout=120) as response:
                    return json.load(response)
            except urllib.error.HTTPError as exc:
                if exc.code == 401:
                    if not self.api_token and not refreshed and self._refresh(used_token):
                        refreshed = True
                        continue
                    raise AuthError("Zendesk didn't accept the saved sign-in.") from exc
                if exc.code == 429 or exc.code >= 500:
                    wait = float(exc.headers.get("Retry-After") or min(60, 2 ** attempt))
                    print(f"  Zendesk asked us to slow down. Waiting {wait:.0f}s...")
                    time.sleep(wait)
                    continue
                detail = exc.read().decode(errors="replace")[:300]
                raise ZendeskError(f"Zendesk returned {exc.code} for {url}: {detail}", exc.code) from exc
            except urllib.error.URLError as exc:
                wait = min(60, 2 ** attempt)
                print(f"  Network error ({exc.reason}). Retrying in {wait}s...")
                time.sleep(wait)
        raise ZendeskError(f"Gave up on {url} after several retries.")

    def paginate(self, path: str, key: str) -> Iterator[dict]:
        """Follow either cursor pagination (links.next) or offset pagination (next_page)."""
        url: str | None = path
        while url:
            data = self.get(url)
            yield from data.get(key, [])
            if (data.get("meta") or {}).get("has_more"):
                url = data["links"]["next"]
            else:
                url = data.get("next_page")


# ---- Data this tool needs ----------------------------------------------------

def _slim_audit(audit: dict) -> dict:
    """Keep only the parts of an audit the analysis uses, to save memory."""
    return {
        "id": audit["id"],
        "ticket_id": audit["ticket_id"],
        "created_at": audit["created_at"],
        "author_id": audit.get("author_id"),
        "events": [
            {k: e.get(k) for k in ("type", "macro_id", "macro_title", "body", "public")}
            for e in audit.get("events", [])
            if is_macro_event(e) or e.get("type") == "Comment"
        ],
    }


def fetch_updated_ticket_ids(zd: Zendesk, since: datetime, log: Callable[[str], None]) -> list[int]:
    """Every ticket updated since `since`, from the incremental ticket export."""
    url: str | None = f"/api/v2/incremental/tickets/cursor?start_time={int(since.timestamp())}"
    ids: dict[int, None] = {}  # a dict keeps order and drops tickets listed twice
    while url:
        data = zd.get(url)
        for ticket in data.get("tickets", []):
            if ticket.get("status") != "deleted":
                ids[ticket["id"]] = None
        log(f"  Found {len(ids):,} tickets updated since {since:%Y-%m-%d}...")
        url = None if data.get("end_of_stream") else data.get("after_url")
    return list(ids)


def fetch_ticket_macro_audits(zd: Zendesk, ticket_id: int, since: datetime) -> list[dict]:
    """The audits on one ticket that applied a macro on or after `since`."""
    kept = []
    try:
        for audit in zd.paginate(f"/api/v2/tickets/{ticket_id}/audits.json?page%5Bsize%5D=100", "audits"):
            if parse_time(audit["created_at"]) >= since and any(
                is_macro_event(e) for e in audit.get("events", [])
            ):
                kept.append(_slim_audit(audit))
    except ZendeskError as exc:
        if exc.status not in (403, 404):  # deleted or hidden since the export ran
            raise
    return kept


def fetch_macro_audits(zd: Zendesk, since: datetime, log: Callable[[str], None],
                       workers: int = 4) -> list[dict]:
    ticket_ids = fetch_updated_ticket_ids(zd, since, log)
    log(f"Reading the history of {len(ticket_ids):,} tickets...")
    audits: list[dict] = []
    started = time.time()
    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = [pool.submit(fetch_ticket_macro_audits, zd, tid, since) for tid in ticket_ids]
        for done, future in enumerate(as_completed(futures), 1):
            audits.extend(future.result())
            if done % 250 == 0 or done == len(futures):
                left = (time.time() - started) / done * (len(futures) - done) / 60
                log(f"  {done:,} of {len(futures):,} tickets read. {len(audits):,} macro uses so far. "
                    f"About {left:.0f} min left.")
    except BaseException:
        pool.shutdown(wait=False, cancel_futures=True)
        raise
    pool.shutdown()
    return audits


def fetch_macros(zd: Zendesk, wanted: Iterable[str]) -> dict[str, dict]:
    """The macros that were used, with Zendesk's own 30-day usage count when the plan allows it."""
    wanted = set(wanted)
    path = "/api/v2/macros.json?page%5Bsize%5D=100"
    try:
        listed = list(zd.paginate(path + "&include=usage_30d", "macros"))
    except ZendeskError as exc:
        if exc.status is None or exc.status >= 500:
            raise
        print("  Zendesk's macro usage counts aren't available on this plan. Skipping that check.")
        listed = list(zd.paginate(path, "macros"))
    macros = {str(m["id"]): m for m in listed if str(m["id"]) in wanted}
    # Macros deleted since they were used won't be in the list. Try each one directly.
    for macro_id in sorted(wanted - macros.keys()):
        try:
            macros[macro_id] = zd.get(f"/api/v2/macros/{macro_id}.json")["macro"]
        except ZendeskError as exc:
            if exc.status not in (403, 404):
                raise
    return macros


def fetch_dynamic_content(zd: Zendesk) -> dict[str, list[str]]:
    """Map 'dc.name' to the text of each variant, default variant first."""
    items: dict[str, list[str]] = {}
    try:
        for item in zd.paginate("/api/v2/dynamic_content/items.json", "items"):
            key = item.get("placeholder", "").strip("{} ").lower()
            variants = sorted(
                (v for v in item.get("variants", []) if v.get("content")),
                key=lambda v: not v.get("default"),
            )
            if key and variants:
                items[key] = [v["content"] for v in variants]
    except ZendeskError as exc:
        print(f"  Couldn't read dynamic content ({exc.status}). {{{{dc.*}}}} placeholders will match any text.")
    return items


def fetch_users(zd: Zendesk, ids: Iterable[str]) -> dict[str, dict]:
    ids = sorted({str(i) for i in ids if i})
    users: dict[str, dict] = {}
    for start in range(0, len(ids), 100):
        chunk = ",".join(ids[start:start + 100])
        for user in zd.get(f"/api/v2/users/show_many.json?ids={chunk}").get("users", []):
            users[str(user["id"])] = user
    return users


def fetch_all(zd: Zendesk, since: datetime, log: Callable[[str], None] = print, workers: int = 4) -> dict:
    log("Finding tickets updated in this period...")
    audits = fetch_macro_audits(zd, since, log, workers)
    macro_ids = {
        str(e["macro_id"])
        for a in audits for e in a["events"]
        if is_macro_event(e) and e.get("macro_id") is not None
    }
    log(f"Fetching {len(macro_ids):,} macros...")
    macros = fetch_macros(zd, macro_ids)
    dynamic: dict[str, list[str]] = {}
    if any("{{dc." in json.dumps(m.get("actions", [])) for m in macros.values()):
        log("Fetching dynamic content used in macros...")
        dynamic = fetch_dynamic_content(zd)
    log("Fetching agent names...")
    users = fetch_users(zd, (a["author_id"] for a in audits))
    return {"audits": audits, "macros": macros, "users": users, "dynamic": dynamic}
