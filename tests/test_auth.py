import base64
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import zd_macro_audit
from zdaudit.zendesk import AuthError, Zendesk

NO_ENV = {env: "" for env in zd_macro_audit.ENV_VARS.values()}


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


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "config.json"
        patcher = mock.patch.dict(os.environ, NO_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(self.dir.cleanup)

    def load(self, auth=None, answers=(), secrets=()):
        with mock.patch("builtins.input", side_effect=list(answers)), \
                mock.patch("getpass.getpass", side_effect=list(secrets)), mock.patch("builtins.print"):
            return zd_macro_audit.load_config(self.path, auth)

    def test_first_run_with_token(self):
        config = self.load(answers=["acme.zendesk.com", "2", "admin@acme.com"], secrets=["abc123"])
        self.assertEqual((config["subdomain"], config["email"], config["api_token"]),
                         ("acme", "admin@acme.com", "abc123"))
        self.assertEqual(json.loads(self.path.read_text())["api_token"], "abc123")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_saved_token_is_used_without_asking(self):
        self.path.write_text(json.dumps({"subdomain": "acme", "client_id": "c",
                                         "email": "a@acme.com", "api_token": "t"}))
        self.assertEqual(self.load()["api_token"], "t")

    def test_oauth_flag_ignores_saved_token(self):
        self.path.write_text(json.dumps({"subdomain": "acme", "client_id": "c",
                                         "email": "a@acme.com", "api_token": "t"}))
        config = self.load("oauth")
        self.assertEqual((config["api_token"], config["client_id"]), ("", "c"))

    def test_token_flag_asks_for_missing_token(self):
        self.path.write_text(json.dumps({"subdomain": "acme", "client_id": "c"}))
        config = self.load("token", answers=["a@acme.com"], secrets=["t"])
        self.assertEqual(config["api_token"], "t")
        self.assertEqual(json.loads(self.path.read_text())["client_id"], "c")

    def test_token_from_environment_is_not_saved(self):
        with mock.patch.dict(os.environ, {"ZENDESK_SUBDOMAIN": "acme", "ZENDESK_EMAIL": "a@acme.com",
                                          "ZENDESK_API_TOKEN": "t"}):
            self.assertEqual(self.load()["api_token"], "t")
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
