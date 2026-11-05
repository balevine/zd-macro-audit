import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import zd_macro_audit
from zdaudit.zendesk import AuthError

NO_ENV = {env: "" for env in zd_macro_audit.ENV_VARS.values()}


class TempFolderTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)
        self.path = self.dir / "config.json"
        patcher = mock.patch.dict(os.environ, NO_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)


class SubdomainTests(unittest.TestCase):
    def test_accepts_what_people_paste(self):
        for value in ("acme", " ACME ", "acme.zendesk.com", "https://acme.zendesk.com/agent/tickets/1",
                      "http://acme.zendesk.com"):
            self.assertEqual(zd_macro_audit.clean_subdomain(value), "acme", value)


class ConfigTests(TempFolderTest):
    def load(self, auth=None, answers=(), secrets=()):
        with mock.patch("builtins.input", side_effect=list(answers)), \
                mock.patch("getpass.getpass", side_effect=list(secrets)) as self.getpass, mock.patch("builtins.print"):
            return zd_macro_audit.load_config(self.path, auth)

    def test_first_run_with_token(self):
        config = self.load(answers=["acme.zendesk.com", "2", "admin@acme.com"], secrets=["abc123"])
        self.assertEqual((config["subdomain"], config["email"], config["api_token"]),
                         ("acme", "admin@acme.com", "abc123"))
        saved = json.loads(self.path.read_text())
        self.assertEqual((saved["subdomain"], saved["api_token"]), ("acme", "abc123"))
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_first_run_with_oauth_is_the_default_choice(self):
        config = self.load(answers=["acme", "", "my_client"], secrets=["shh"])
        self.assertEqual((config["client_id"], config["client_secret"], config["api_token"]), ("my_client", "shh", ""))
        self.assertEqual(json.loads(self.path.read_text())["client_id"], "my_client")

    def test_secrets_are_not_shown_while_typing(self):
        self.load(answers=["acme", "", "my_client"], secrets=["shh"])
        self.getpass.assert_called_once()

    def test_saved_token_is_used_without_asking(self):
        self.path.write_text(json.dumps({"subdomain": "acme", "client_id": "c",
                                         "email": "a@acme.com", "api_token": "t"}))
        self.assertEqual(self.load()["api_token"], "t")

    def test_saved_oauth_client_is_used_without_asking(self):
        self.path.write_text(json.dumps({"subdomain": "acme", "client_id": "c", "client_secret": "s"}))
        config = self.load()
        self.assertEqual((config["client_id"], config["client_secret"]), ("c", "s"))

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

    def test_environment_wins_over_the_file_and_is_cleaned_up(self):
        self.path.write_text(json.dumps({"subdomain": "old", "client_id": "c"}))
        with mock.patch.dict(os.environ, {"ZENDESK_SUBDOMAIN": "https://new.zendesk.com"}):
            self.assertEqual(self.load()["subdomain"], "new")

    def test_blank_answers_stop_the_run(self):
        with self.assertRaisesRegex(SystemExit, "Missing settings: client_id"):
            self.load(answers=["acme", "", ""], secrets=[""])
        self.assertFalse(self.path.exists())


class RunTests(TempFolderTest):
    def run_main(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = zd_macro_audit.main(["--out", str(self.dir / "reports"), "--config", str(self.path), *args])
        return code, out.getvalue(), err.getvalue()

    def test_sample_run_writes_everything_and_prints_a_summary(self):
        code, out, _ = self.run_main("--sample")
        self.assertEqual(code, 0)
        folder = next((self.dir / "reports").iterdir())
        self.assertEqual({p.name for p in folder.iterdir()}, {"report.html", "macro_uses.csv", "macros.csv", "agents.csv"})
        self.assertIn("Sample data", (folder / "report.html").read_text(encoding="utf-8"))
        self.assertRegex(out, r"Macro uses found:\s+447 \(8 macros\)")
        self.assertRegex(out, r"Kept intact:\s+\d+ \(\d+%\), \d+ with text added around it")
        self.assertRegex(out, r"Public:\s+\d+ of \d+ edited \(\d+%\)")
        self.assertRegex(out, r"No comment:\s+\d+ of \d+ edited")
        self.assertRegex(out, r"Not compared:\s+\d+")
        self.assertIn(f"Report: {folder / 'report.html'}", out)

    def test_days_sets_the_period(self):
        with mock.patch.object(zd_macro_audit.analyze, "analyze", wraps=zd_macro_audit.analyze.analyze) as run:
            self.run_main("--sample", "--days", "10")
        kwargs = run.call_args.kwargs
        self.assertEqual((kwargs["until"] - kwargs["since"]).days, 10)

    def signed_in(self, fetched=None, error=None):
        self.path.write_text(json.dumps({"subdomain": "acme", "email": "a@acme.com", "api_token": "t"}))
        client = mock.patch.object(zd_macro_audit.zendesk, "Zendesk")
        fetch = mock.patch.object(zd_macro_audit.zendesk, "fetch_all", return_value=fetched, side_effect=error)
        for patcher in (client, fetch):
            self.addCleanup(patcher.stop)
        self.client, self.fetch = client.start(), fetch.start()

    def test_real_run_passes_settings_through(self):
        self.signed_in({"audits": [], "macros": {}, "users": {}, "dynamic": {}})
        code, out, _ = self.run_main("--workers", "0", "--login")
        self.assertEqual(code, 0)
        args, kwargs = self.client.call_args
        self.assertEqual(args[0], "acme")
        self.assertEqual((kwargs["email"], kwargs["api_token"]), ("a@acme.com", "t"))
        self.client.return_value.authenticate.assert_called_once_with(force=True)
        self.assertEqual(self.fetch.call_args.kwargs["workers"], 1)
        self.assertRegex(out, r"Compared:\s+0")
        self.assertRegex(out, r"Sent edited:\s+0 \(n/a\)")
        self.assertNotIn("No comment:", out)
        self.assertNotIn("Not compared:", out)

    def test_defaults(self):
        self.signed_in({"audits": [], "macros": {}, "users": {}, "dynamic": {}})
        with mock.patch.object(zd_macro_audit.analyze, "analyze", wraps=zd_macro_audit.analyze.analyze) as run:
            self.run_main()
        self.assertEqual((run.call_args.kwargs["until"] - run.call_args.kwargs["since"]).days, 30)
        self.assertEqual(self.fetch.call_args.kwargs["workers"], 4)

    def test_sign_in_flags_reach_the_config(self):
        self.signed_in({"audits": [], "macros": {}, "users": {}, "dynamic": {}})
        self.path.write_text(json.dumps({"subdomain": "acme", "client_id": "c", "email": "a@acme.com", "api_token": "t"}))
        for flag, auth in (("--api-token", "token"), ("--oauth", "oauth")):
            with mock.patch.object(zd_macro_audit, "load_config", wraps=zd_macro_audit.load_config) as load:
                self.run_main(flag)
            self.assertEqual(load.call_args[0][1], auth)

    def test_sign_in_error_is_reported(self):
        self.signed_in(error=AuthError("nope"))
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("Error: nope", err)
        self.assertFalse((self.dir / "reports").exists())

    def test_stopping_the_run(self):
        self.signed_in(error=KeyboardInterrupt())
        code, out, _ = self.run_main()
        self.assertEqual(code, 130)
        self.assertIn("Stopped.", out)


if __name__ == "__main__":
    unittest.main()
