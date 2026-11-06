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
import email.utils
import hashlib
import http.client
import http.server
import json
import os
import re
import secrets
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Iterator

REDIRECT_PORT = 8765
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}/callback"
SCOPE = "read"
USER_AGENT = "zd-macro-audit/1.0"

# Zendesk's audit events reference names this event "MacroReference", but its
# example and real responses can say "AgentMacroReference", so accept both.
MACRO_EVENT_TYPES = ("MacroReference", "AgentMacroReference")


class AuthError(Exception):
    pass


class ZendeskError(Exception):
    def __init__(self, message: str, status: int | None = None, gave_up: bool = False):
        super().__init__(message)
        self.status = status
        self.gave_up = gave_up  # still failing after every retry, so worth trying again later


TRIES = 8  # attempts per request before giving up
GIVE_UP_STREAK = 20  # tickets in a row that Zendesk doesn't answer before the run stops early

# Network trouble worth retrying. A reply that isn't JSON is usually cut off or a proxy's error page.
RETRYABLE = (OSError, http.client.HTTPException, json.JSONDecodeError, UnicodeDecodeError)


@dataclass
class Coverage:
    """How much of the period the run managed to read."""
    tickets: int = 0                                  # tickets updated in the period
    unread: list[int] = field(default_factory=list)   # tickets Zendesk kept failing on, or never reached
    stopped_early: bool = False                       # Zendesk stopped answering, so the run gave up
    names_missing: bool = False                       # agent names couldn't be fetched
    skipped: list[int] = field(default_factory=list)  # tickets deleted or hidden from this sign-in (403 or 404)

    @property
    def read(self) -> int:
        """Tickets Zendesk answered for, skipped ones included."""
        return self.tickets - len(self.unread)


def parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def is_macro_event(event: dict) -> bool:
    return event.get("type") in MACRO_EVENT_TYPES and event.get("macro_id") is not None


def _backoff(attempt: int) -> int:
    return min(60, 2 ** attempt)


def _retry_after(value: str | None, attempt: int) -> float:
    """Seconds Zendesk asked us to wait, as a number or a date, kept between 0 and 5 minutes."""
    if not value:
        return _backoff(attempt)
    try:
        wait = float(value)
    except ValueError:
        try:
            when = email.utils.parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return _backoff(attempt)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        wait = when.timestamp() - time.time()
    return min(300, max(0, wait))


def _describe(exc: BaseException) -> str:
    if isinstance(exc, urllib.error.URLError):
        return str(exc.reason)
    if isinstance(exc, ValueError):
        return "the reply was cut off or wasn't JSON"
    return str(exc) or type(exc).__name__


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
        self.client_id = client_id
        self.client_secret = client_secret
        self.email = email
        self.api_token = api_token
        self.base = f"https://{subdomain}.zendesk.com"
        self.token_path = token_dir / f"{subdomain}.json"
        self.token: dict = {}
        self._refresh_lock = threading.Lock()
        self._connected = False  # whether Zendesk has answered yet

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
            me = self._me()
        except AuthError:
            self.login()
            me = self._me()
        self._check_role(me)

    def _me(self) -> dict:
        return self.get("/api/v2/users/me.json")["user"]

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

        self._save_token(self._post_token({
            "grant_type": "authorization_code",
            "code": reply["code"],
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
        }))
        me = self._me()
        print(f"Signed in as {me.get('name')} ({me.get('email')}).")
        self._check_role(me)

    def _token_login(self) -> None:
        """Check the API token works. There's nothing to save, since it doesn't expire."""
        try:
            me = self._me()
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
            try:
                self._save_token(self._post_token({"grant_type": "refresh_token", "refresh_token": refresh_token}))
                return True
            except AuthError:
                return False

    def _post_token(self, payload: dict) -> dict:
        payload = {**payload, "client_id": self.client_id, "scope": SCOPE}
        if self.client_secret:
            payload["client_secret"] = self.client_secret
        url = f"{self.base}/oauth/tokens"

        def build() -> urllib.request.Request:
            return urllib.request.Request(
                url,
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
                method="POST",
            )

        def rejected(exc: urllib.error.HTTPError, attempt: int) -> float | None:
            detail = exc.read().decode(errors="replace")[:500]
            raise AuthError(f"Zendesk rejected the token request ({exc.code}): {detail}") from exc

        return self._send(url, build, 60, rejected)

    def _save_token(self, token: dict) -> None:
        if not token.get("refresh_token") and self.token.get("refresh_token"):
            token["refresh_token"] = self.token["refresh_token"]
        self.token = token
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(json.dumps(token))
        os.chmod(self.token_path, 0o600)

    # ---- Requests ----------------------------------------------------------

    def _send(self, url: str, build: Callable[[], urllib.request.Request], timeout: int,
              on_http_error: Callable[[urllib.error.HTTPError, int], float | None]) -> dict:
        """Send a request and read its JSON, retrying network trouble with growing waits.

        `on_http_error` raises, or returns how long to wait before trying again (None for no wait).
        """
        for attempt in range(TRIES):
            try:
                with urllib.request.urlopen(build(), timeout=timeout) as response:
                    data = json.load(response)
                self._connected = True
                return data
            except urllib.error.HTTPError as exc:
                wait = on_http_error(exc, attempt)
                if wait is None:
                    continue
                message = f"  Zendesk asked us to slow down. Waiting {wait:.0f}s..."
            except RETRYABLE as exc:
                self._stop_if_hopeless(exc, url)
                wait = _backoff(attempt)
                message = f"  Network error ({_describe(exc)}). Retrying in {wait}s..."
            if attempt < TRIES - 1:
                print(message)
                time.sleep(wait)
        raise ZendeskError(f"Gave up on {url} after several retries.", gave_up=True)

    def _stop_if_hopeless(self, exc: BaseException, url: str) -> None:
        """Raise for network errors that retrying won't fix."""
        reason = exc.reason if isinstance(exc, urllib.error.URLError) else exc
        if isinstance(reason, ssl.SSLCertVerificationError):
            raise ZendeskError(
                "Couldn't verify the security certificate of the connection to Zendesk. This often "
                "happens on a company network or proxy that inspects traffic. Try another network, "
                "or ask your IT team."
            ) from exc
        # Once Zendesk has answered, a failed lookup means the network dropped, so keep retrying.
        if isinstance(reason, socket.gaierror) and not self._connected:
            host = urllib.parse.urlparse(url).hostname
            raise ZendeskError(f"Couldn't find {host}. Check the Zendesk subdomain.") from exc

    def _check_link(self, url: str) -> None:
        """Refuse any URL that isn't https on this Zendesk account, so the sign-in is never sent elsewhere."""
        parts = urllib.parse.urlsplit(url)
        try:
            port = parts.port
        except ValueError:  # not a number
            port = -1
        if parts.scheme == "https" and parts.hostname == urllib.parse.urlsplit(self.base).hostname \
                and port in (None, 443):
            return
        where = f"{parts.scheme}://{parts.netloc.rpartition('@')[2]}"
        raise ZendeskError(f"Zendesk sent a next-page link to {where} instead of {self.base}, so the run "
                           "stopped rather than send the sign-in there.")

    def get(self, path_or_url: str) -> dict:
        url = path_or_url if path_or_url.startswith("http") else self.base + path_or_url
        self._check_link(url)
        refreshed = False
        used_token = ""

        def build() -> urllib.request.Request:
            nonlocal used_token
            used_token = self.token.get("access_token", "")
            return urllib.request.Request(url, headers={
                "Authorization": self._auth_header(used_token),
                "Accept": "application/json",
                "User-Agent": USER_AGENT,
            })

        def on_http_error(exc: urllib.error.HTTPError, attempt: int) -> float | None:
            nonlocal refreshed
            if exc.code == 401:
                if not self.api_token and not refreshed and self._refresh(used_token):
                    refreshed = True
                    return
                raise AuthError("Zendesk didn't accept the saved sign-in.") from exc
            if exc.code == 429 or exc.code >= 500:
                return _retry_after(exc.headers.get("Retry-After"), attempt)
            detail = exc.read().decode(errors="replace")[:300]
            raise ZendeskError(f"Zendesk returned {exc.code} for {url}: {detail}", exc.code) from exc

        return self._send(url, build, 120, on_http_error)

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


def fetch_ticket_macro_audits(zd: Zendesk, ticket_id: int, since: datetime) -> list[dict] | None:
    """The audits on one ticket that applied a macro on or after `since`, or None if the ticket can't be seen."""
    kept = []
    try:
        for audit in zd.paginate(f"/api/v2/tickets/{ticket_id}/audits.json?page%5Bsize%5D=100", "audits"):
            if parse_time(audit["created_at"]) >= since and any(
                is_macro_event(e) for e in audit.get("events", [])
            ):
                kept.append(_slim_audit(audit))
    except ZendeskError as exc:
        if exc.status in (403, 404):  # deleted or hidden since the export ran
            return None
        raise
    return kept


def fetch_macro_audits(zd: Zendesk, since: datetime, log: Callable[[str], None],
                       workers: int = 4) -> tuple[list[dict], Coverage]:
    """Macro audits from every updated ticket, and which tickets couldn't be read or were skipped.

    Stops early if Zendesk hasn't answered for GIVE_UP_STREAK tickets in a row.
    """
    ticket_ids = fetch_updated_ticket_ids(zd, since, log)
    log(f"Reading the history of {len(ticket_ids):,} tickets...")
    audits: list[dict] = []
    read: set[int] = set()
    skipped: list[int] = []
    set_aside: list[int] = []
    streak = 0
    stopped = False
    started = time.time()

    def keep(ticket_id: int, found: list[dict] | None) -> None:
        read.add(ticket_id)
        if found is None:
            skipped.append(ticket_id)
        else:
            audits.extend(found)

    pool = ThreadPoolExecutor(max_workers=workers)
    try:
        futures = {pool.submit(fetch_ticket_macro_audits, zd, tid, since): tid for tid in ticket_ids}
        for done, future in enumerate(as_completed(futures), 1):
            try:
                keep(futures[future], future.result())
                streak = 0
            except ZendeskError as exc:
                if not exc.gave_up:
                    raise
                set_aside.append(futures[future])
                streak += 1
                if streak >= GIVE_UP_STREAK:
                    stopped = True
                    break
            if done % 250 == 0 or done == len(futures):
                left = (time.time() - started) / done * (len(futures) - done) / 60
                log(f"  {done:,} of {len(futures):,} tickets read. {len(audits):,} macro uses so far. "
                    f"About {left:.0f} min left.")
        if stopped:
            log(f"Zendesk hasn't answered for the last {GIVE_UP_STREAK} tickets, so the tool is stopping early.")
        pool.shutdown(cancel_futures=stopped)
    except BaseException:
        pool.shutdown(wait=False, cancel_futures=True)
        raise

    if stopped:
        # Keep any ticket that finished while the pool was stopping.
        for future, ticket_id in futures.items():
            if ticket_id not in read and not future.cancelled() and future.exception() is None:
                keep(ticket_id, future.result())
    elif set_aside:
        log(f"Trying {len(set_aside):,} failed ticket{'' if len(set_aside) == 1 else 's'} again...")
        for ticket_id in sorted(set_aside):
            try:
                keep(ticket_id, fetch_ticket_macro_audits(zd, ticket_id, since))
            except ZendeskError as exc:
                if not exc.gave_up:
                    raise
    unread = sorted(set(ticket_ids) - read)
    return audits, Coverage(len(ticket_ids), unread, stopped, skipped=sorted(skipped))


def fetch_macros(zd: Zendesk, wanted: Iterable[str], log: Callable[[str], None]) -> dict[str, dict]:
    """The macros that were used, with Zendesk's own 30-day usage count when the plan allows it."""
    wanted = set(wanted)
    path = "/api/v2/macros.json?page%5Bsize%5D=100"
    try:
        listed = list(zd.paginate(path + "&include=usage_30d", "macros"))
    except ZendeskError as exc:
        if exc.status is None or exc.status >= 500:
            raise
        log("  Zendesk's macro usage counts aren't available on this plan. Skipping that check.")
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


def fetch_dynamic_content(zd: Zendesk, log: Callable[[str], None]) -> dict[str, list[str]]:
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
        if exc.status is None and not exc.gave_up:  # retrying won't fix it, like a link to another site
            raise
        reason = exc.status or "Zendesk didn't answer"
        log(f"  Couldn't read dynamic content ({reason}). {{{{dc.*}}}} placeholders will match any text.")
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
    """Everything the analysis needs. Raises ZendeskError when too little was read for a report."""
    log("Finding tickets updated in this period...")
    audits, coverage = fetch_macro_audits(zd, since, log, workers)
    if coverage.tickets and not coverage.read:
        raise ZendeskError(f"Zendesk kept failing, so none of the {coverage.tickets:,} tickets could be read and "
                           "there's no report. A later run should pick everything up.")
    if coverage.stopped_early:
        log(f"Read {coverage.read:,} of {coverage.tickets:,} tickets. Fetching what the report needs for those...")
    macro_ids = {str(e["macro_id"]) for a in audits for e in a["events"] if is_macro_event(e)}
    log(f"Fetching {len(macro_ids):,} macros...")
    try:
        macros = fetch_macros(zd, macro_ids, log)
    except ZendeskError as exc:
        if not exc.gave_up:
            raise
        raise ZendeskError(f"Zendesk stopped answering while the tool was fetching macros, so there's no report. "
                           f"{coverage.read:,} of {coverage.tickets:,} tickets were read, but the report needs "
                           "the macros' text. A later run should pick everything up.") from exc
    dynamic: dict[str, list[str]] = {}
    if any(re.search(r"\{\{-?\s*dc\.", json.dumps(m.get("actions", []))) for m in macros.values()):
        log("Fetching dynamic content used in macros...")
        dynamic = fetch_dynamic_content(zd, log)
    log("Fetching agent names...")
    try:
        users = fetch_users(zd, (a["author_id"] for a in audits))
    except ZendeskError as exc:
        if not exc.gave_up:
            raise
        log("  Zendesk didn't answer, so agents are shown by ID instead of by name.")
        users, coverage.names_missing = {}, True
    return {"audits": audits, "macros": macros, "users": users, "dynamic": dynamic, "coverage": coverage}
