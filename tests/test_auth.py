import base64
import hashlib
import io
import json
import socket
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path
from unittest import mock

from zdaudit import zendesk
from zdaudit.zendesk import AuthError, Zendesk, ZendeskError

ME = {"user": {"id": 1, "name": "Admin", "email": "admin@acme.com", "role": "admin"}}


def http_error(code, body=b"", retry_after=None):
    headers = Message()
    if retry_after:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://x", code, "error", headers, io.BytesIO(body))


def reply(data):
    return io.BytesIO(json.dumps(data).encode())


def sent_header(request):
    return request.get_header("Authorization")


class TokenSignInTests(unittest.TestCase):
    def test_basic_auth_header(self):
        zd = Zendesk("acme", email="admin@acme.com", api_token="abc123")
        scheme, value = zd._auth_header("").split()
        self.assertEqual(scheme, "Basic")
        self.assertEqual(base64.b64decode(value).decode(), "admin@acme.com/token:abc123")

    def test_oauth_header_unchanged(self):
        self.assertEqual(Zendesk("acme", "client")._auth_header("xyz"), "Bearer xyz")

    def test_rejected_token(self):
        zd = Zendesk("acme", email="admin@acme.com", api_token="bad")
        with mock.patch.object(zd, "get", side_effect=AuthError("401")):
            with self.assertRaisesRegex(AuthError, "API token"):
                zd.authenticate()

    def test_anonymous_user_is_rejected(self):
        zd = Zendesk("acme", email="admin@acme.com", api_token="bad")
        with mock.patch.object(zd, "get", return_value={"user": {"id": None, "role": "end-user"}}):
            with self.assertRaisesRegex(AuthError, "API token"):
                zd.authenticate()

    def test_token_skips_oauth(self):
        zd = Zendesk("acme", "client", email="admin@acme.com", api_token="abc123")
        me = {"user": {"id": 1, "name": "Admin", "email": "admin@acme.com", "role": "admin"}}
        with mock.patch.object(zd, "get", return_value=me), mock.patch.object(zd, "login") as login, \
                mock.patch("builtins.print"):
            zd.authenticate(force=True)
        login.assert_not_called()

    def test_non_admin_token_gets_a_warning(self):
        zd = Zendesk("acme", email="a@acme.com", api_token="t")
        agent = {"user": {"id": 2, "email": "a@acme.com", "role": "agent"}}
        with mock.patch.object(zd, "get", return_value=agent), mock.patch("builtins.print") as printed:
            zd.authenticate()
        self.assertIn("not an admin", printed.call_args[0][0])

    def test_error_names_the_email(self):
        zd = Zendesk("acme", email="admin@acme.com", api_token="bad")
        with mock.patch.object(zd, "get", side_effect=AuthError("401")):
            with self.assertRaisesRegex(AuthError, "admin@acme.com"):
                zd.authenticate()


class OAuthTestCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.tokens = Path(folder.name) / "tokens"
        self.zd = Zendesk("acme", "client", "shh", self.tokens)
        for target in ("builtins.print", "time.sleep", "webbrowser.open"):
            patcher = mock.patch(target)
            setattr(self, target.split(".")[1], patcher.start())
            self.addCleanup(patcher.stop)

    def saved(self):
        return json.loads(self.zd.token_path.read_text())


class SavedSignInTests(OAuthTestCase):
    def save(self, token):
        self.tokens.mkdir()
        self.zd.token_path.write_text(token)

    def test_saved_sign_in_skips_the_browser(self):
        self.save(json.dumps({"access_token": "saved"}))
        with mock.patch.object(self.zd, "get", return_value=ME) as get, mock.patch.object(self.zd, "login") as login:
            self.zd.authenticate()
        login.assert_not_called()
        get.assert_called_once_with("/api/v2/users/me.json")
        self.assertEqual(self.zd.token["access_token"], "saved")

    def test_no_saved_sign_in_opens_the_browser(self):
        with mock.patch.object(self.zd, "login") as login:
            self.zd.authenticate()
        login.assert_called_once()

    def test_force_ignores_the_saved_sign_in(self):
        self.save(json.dumps({"access_token": "saved"}))
        with mock.patch.object(self.zd, "login") as login:
            self.zd.authenticate(force=True)
        login.assert_called_once()
        self.assertEqual(self.zd.token, {})

    def test_damaged_file_opens_the_browser(self):
        self.save("{not json")
        with mock.patch.object(self.zd, "login") as login:
            self.zd.authenticate()
        login.assert_called_once()

    def test_rejected_sign_in_opens_the_browser(self):
        self.save(json.dumps({"access_token": "old"}))
        with mock.patch.object(self.zd, "get", side_effect=[AuthError("401"), ME]), \
                mock.patch.object(self.zd, "login") as login:
            self.zd.authenticate()
        login.assert_called_once()

    def test_non_admin_gets_a_warning(self):
        self.save(json.dumps({"access_token": "saved"}))
        agent = {"user": {"id": 2, "email": "a@acme.com", "role": "agent"}}
        with mock.patch.object(self.zd, "get", return_value=agent):
            self.zd.authenticate()
        self.assertIn("not an admin", self.print.call_args[0][0])

    def test_admin_gets_no_warning(self):
        self.save(json.dumps({"access_token": "saved"}))
        with mock.patch.object(self.zd, "get", return_value=ME):
            self.zd.authenticate()
        self.print.assert_not_called()


class LoginTests(OAuthTestCase):
    def login(self, redirect, token=None):
        self.requests = []

        def urlopen(request, timeout=None):
            self.requests.append(request)
            return reply(token if request.full_url.endswith("/oauth/tokens") else ME)

        with mock.patch.object(zendesk, "_wait_for_redirect", side_effect=lambda state: redirect(state)), \
                mock.patch("urllib.request.urlopen", urlopen):
            self.zd.login()

    def test_success_exchanges_the_code_and_saves_the_token(self):
        self.login(lambda state: {"code": "abc", "state": state}, {"access_token": "new", "refresh_token": "r"})
        url = self.webbrowser_url()
        self.assertIn("https://acme.zendesk.com/oauth/authorizations/new?", url)
        self.assertIn("scope=read", url)
        self.assertIn("redirect_uri=http%3A%2F%2Flocalhost%3A8765%2Fcallback", url)
        payload = json.loads(self.requests[0].data)
        self.assertEqual({k: payload[k] for k in ("grant_type", "code", "client_id", "client_secret", "scope")},
                         {"grant_type": "authorization_code", "code": "abc", "client_id": "client",
                          "client_secret": "shh", "scope": "read"})
        challenge = hashlib.sha256(payload["code_verifier"].encode()).digest()
        self.assertIn(base64.urlsafe_b64encode(challenge).rstrip(b"=").decode(), url)
        self.assertEqual(self.saved()["access_token"], "new")
        self.assertEqual(self.zd.token_path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(sent_header(self.requests[1]), "Bearer new")

    def webbrowser_url(self):
        return self.open.call_args[0][0]

    def test_public_client_sends_no_secret(self):
        self.zd.client_secret = ""
        self.login(lambda state: {"code": "abc", "state": state}, {"access_token": "new"})
        self.assertNotIn("client_secret", json.loads(self.requests[0].data))

    def test_zendesk_refusal(self):
        with self.assertRaisesRegex(AuthError, "access denied"):
            self.login(lambda state: {"error": "x", "error_description": "access+denied"})

    def test_timeout(self):
        with self.assertRaisesRegex(AuthError, "Timed out"):
            self.login(lambda state: {})

    def test_state_mismatch(self):
        with self.assertRaisesRegex(AuthError, "didn't match"):
            self.login(lambda state: {"code": "abc", "state": "other"})

    def test_rejected_token_request(self):
        def urlopen(request, timeout=None):
            raise http_error(400, b"bad code")
        with mock.patch.object(zendesk, "_wait_for_redirect", side_effect=lambda state: {"code": "a", "state": state}), \
                mock.patch("urllib.request.urlopen", urlopen):
            with self.assertRaisesRegex(AuthError, r"\(400\): bad code"):
                self.zd.login()


class RefreshTests(OAuthTestCase):
    def test_keeps_the_old_refresh_token_when_none_comes_back(self):
        self.zd.token = {"access_token": "old", "refresh_token": "keep"}
        with mock.patch("urllib.request.urlopen", return_value=reply({"access_token": "new"})) as urlopen:
            self.assertTrue(self.zd._refresh("old"))
        payload = json.loads(urlopen.call_args[0][0].data)
        self.assertEqual((payload["grant_type"], payload["refresh_token"], payload["client_secret"]),
                         ("refresh_token", "keep", "shh"))
        self.assertEqual(self.saved(), {"access_token": "new", "refresh_token": "keep"})

    def test_uses_the_new_refresh_token_when_one_comes_back(self):
        self.zd.token = {"access_token": "old", "refresh_token": "old-r"}
        with mock.patch("urllib.request.urlopen", return_value=reply({"access_token": "n", "refresh_token": "new-r"})):
            self.zd._refresh("old")
        self.assertEqual(self.saved()["refresh_token"], "new-r")

    def test_another_thread_already_refreshed(self):
        self.zd.token = {"access_token": "newer", "refresh_token": "r"}
        with mock.patch("urllib.request.urlopen") as urlopen:
            self.assertTrue(self.zd._refresh("old"))
        urlopen.assert_not_called()

    def test_no_refresh_token(self):
        self.zd.token = {"access_token": "old"}
        self.assertFalse(self.zd._refresh("old"))

    def test_rejected_refresh(self):
        self.zd.token = {"access_token": "old", "refresh_token": "r"}
        with mock.patch("urllib.request.urlopen", side_effect=http_error(400)):
            self.assertFalse(self.zd._refresh("old"))


class RequestTests(OAuthTestCase):
    def get(self, *answers, path="/api/v2/x.json"):
        with mock.patch("urllib.request.urlopen", side_effect=list(answers)) as self.urlopen:
            return self.zd.get(path)

    def test_relative_and_full_urls(self):
        self.zd.token = {"access_token": "t"}
        self.get(reply({}))
        request = self.urlopen.call_args[0][0]
        self.assertEqual(request.full_url, "https://acme.zendesk.com/api/v2/x.json")
        self.assertEqual(sent_header(request), "Bearer t")
        self.assertEqual(request.get_header("User-agent"), "zd-macro-audit/1.0")
        self.get(reply({}), path="https://other.example/next")
        self.assertEqual(self.urlopen.call_args[0][0].full_url, "https://other.example/next")

    def test_rate_limit_waits_as_long_as_zendesk_asks(self):
        self.assertEqual(self.get(http_error(429, retry_after="7"), reply({"ok": 1})), {"ok": 1})
        self.sleep.assert_called_once_with(7.0)

    def test_server_error_backs_off(self):
        self.assertEqual(self.get(http_error(500), http_error(503), reply({"ok": 1})), {"ok": 1})
        self.assertEqual([c[0][0] for c in self.sleep.call_args_list], [1, 2])

    def test_network_error_backs_off(self):
        self.assertEqual(self.get(urllib.error.URLError("down"), reply({"ok": 1})), {"ok": 1})
        self.sleep.assert_called_once_with(1)

    def test_gives_up_after_eight_tries(self):
        with self.assertRaisesRegex(ZendeskError, "Gave up"):
            self.get(*[http_error(500) for _ in range(8)])
        self.assertEqual(self.urlopen.call_count, 8)
        self.assertEqual(self.sleep.call_args[0][0], 60)

    def test_client_error_stops_at_once(self):
        with self.assertRaises(ZendeskError) as caught:
            self.get(http_error(404, b"not here"))
        self.assertEqual(caught.exception.status, 404)
        self.assertIn("not here", str(caught.exception))
        self.sleep.assert_not_called()

    def test_expired_sign_in_refreshes_once_and_retries(self):
        self.zd.token = {"access_token": "old"}
        with mock.patch.object(self.zd, "_refresh", return_value=True) as refresh:
            self.assertEqual(self.get(http_error(401), reply({"ok": 1})), {"ok": 1})
        refresh.assert_called_once_with("old")

    def test_rejected_twice_gives_up(self):
        self.zd.token = {"access_token": "old"}
        with mock.patch.object(self.zd, "_refresh", return_value=True) as refresh:
            with self.assertRaises(AuthError):
                self.get(http_error(401), http_error(401))
        refresh.assert_called_once()

    def test_failed_refresh_gives_up(self):
        with mock.patch.object(self.zd, "_refresh", return_value=False):
            with self.assertRaises(AuthError):
                self.get(http_error(401))

    def test_api_token_never_refreshes(self):
        self.zd.api_token = "t"
        with mock.patch.object(self.zd, "_refresh") as refresh:
            with self.assertRaises(AuthError):
                self.get(http_error(401))
        refresh.assert_not_called()


class RedirectTests(unittest.TestCase):
    """Runs the real local server that catches Zendesk's reply, on a free port."""

    def catch(self, *paths, expected_state="good"):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", 0))
            except OSError as exc:
                self.skipTest(f"can't open a local port here: {exc}")
            port = probe.getsockname()[1]
        answers = []
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

        def browser():
            for path in paths:
                for _ in range(100):
                    try:
                        answers.append(opener.open(f"http://127.0.0.1:{port}{path}", timeout=5).read().decode())
                        break
                    except urllib.error.HTTPError as exc:
                        answers.append(exc.code)
                        break
                    except urllib.error.URLError:
                        time.sleep(0.05)

        thread = threading.Thread(target=browser)
        with mock.patch.object(zendesk, "REDIRECT_PORT", port):
            thread.start()
            result = zendesk._wait_for_redirect(expected_state, timeout=10)
        thread.join()
        return result, answers

    def test_returns_the_code(self):
        result, answers = self.catch("/callback?code=abc&state=good")
        self.assertEqual(result, {"code": "abc", "state": "good"})
        self.assertIn("Signed in", answers[0])

    def test_wrong_state_tells_the_person_in_the_browser(self):
        result, answers = self.catch("/callback?code=abc&state=bad")
        self.assertEqual(result["state"], "bad")
        self.assertIn("didn't finish", answers[0])

    def test_error_ends_the_wait(self):
        result, answers = self.catch("/callback?error=denied")
        self.assertEqual(result, {"error": "denied"})
        self.assertIn("didn't finish", answers[0])

    def test_other_paths_are_ignored(self):
        result, answers = self.catch("/favicon.ico", "/callback?code=abc&state=good")
        self.assertEqual(answers[0], 404)
        self.assertEqual(result["code"], "abc")

    def test_times_out_with_nothing(self):
        with mock.patch.object(zendesk.time, "time", side_effect=[0, 100, 100, 100, 100]):
            result, _ = self.catch(expected_state="good")
        self.assertEqual(result, {})

    def test_port_in_use_is_explained(self):
        with mock.patch("http.server.HTTPServer", side_effect=OSError("in use")):
            with self.assertRaisesRegex(AuthError, "Couldn't listen on port 8765"):
                zendesk._wait_for_redirect("good")


class MacroEventTests(unittest.TestCase):
    def test_both_names_with_an_id(self):
        self.assertTrue(zendesk.is_macro_event({"type": "MacroReference", "macro_id": 1}))
        self.assertTrue(zendesk.is_macro_event({"type": "AgentMacroReference", "macro_id": 0}))

    def test_other_events_and_missing_ids(self):
        self.assertFalse(zendesk.is_macro_event({"type": "Comment", "macro_id": 1}))
        self.assertFalse(zendesk.is_macro_event({"type": "MacroReference", "macro_id": None}))


if __name__ == "__main__":
    unittest.main()
