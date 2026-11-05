import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

import zd_macro_audit
from tests.helpers import fake_keychain, make_audit, use_macro
from tests.test_auth import mode, modes_while_writing
from tests.test_fetch import MACRO_PATH, FakeZendesk, audits_path, base_pages, gave_up
from zdaudit import keychain
from zdaudit.zendesk import AuthError, Coverage, ZendeskError

NO_ENV = {env: "" for env in zd_macro_audit.ENV_VARS.values()}
BAD_SUBDOMAINS = ("evil.com#", "evil.com?", "acme corp", "acme.", "acme.zendesk.com.", "-acme", "a_b",
                  "acme.evil.com", "https://")


def bad_subdomain(value):
    return (f'Zendesk subdomain "{value}" isn\'t valid. Use just the "acme" part of acme.zendesk.com '
            "(letters, numbers and hyphens).")


class TempFolderTest(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dir = Path(folder.name)
        self.path = self.dir / "config.json"
        patcher = mock.patch.dict(os.environ, NO_ENV)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.keychain = fake_keychain(self)


class SubdomainTests(unittest.TestCase):
    def test_accepts_what_people_paste(self):
        for value in ("acme", " ACME ", "acme.zendesk.com", "https://acme.zendesk.com/agent/tickets/1",
                      "http://acme.zendesk.com"):
            self.assertEqual(zd_macro_audit.clean_subdomain(value), "acme", value)

    def test_hyphens_and_numbers_are_kept(self):
        self.assertEqual(zd_macro_audit.clean_subdomain("https://Acme-2.zendesk.com/agent"), "acme-2")

    def test_blank_stays_blank_so_the_run_asks(self):
        self.assertEqual(zd_macro_audit.clean_subdomain("  "), "")

    def test_anything_else_stops_the_run(self):
        for value in BAD_SUBDOMAINS:
            with self.subTest(value):
                with self.assertRaises(SystemExit) as caught:
                    zd_macro_audit.clean_subdomain(f" {value} ")
                self.assertEqual(caught.exception.code, bad_subdomain(value))


class ConfigTests(TempFolderTest):
    def load(self, auth=None, answers=(), secrets=()):
        with mock.patch("builtins.input", side_effect=list(answers)), \
                mock.patch("getpass.getpass", side_effect=list(secrets)) as self.getpass, mock.patch("builtins.print"):
            return zd_macro_audit.load_config(self.path, auth)

    def test_first_run_with_token(self):
        config = self.load(answers=["acme.zendesk.com", "2", "admin@acme.com"], secrets=["abc123"])
        self.assertEqual((config["subdomain"], config["email"], config["api_token"]),
                         ("acme", "admin@acme.com", "abc123"))
        self.assertEqual(json.loads(self.path.read_text()), {"subdomain": "acme", "email": "admin@acme.com"})
        self.assertEqual(self.keychain, {("acme", "api_token"): "abc123"})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_config_file_is_never_readable_by_others(self):
        self.path.write_text(json.dumps({"subdomain": "acme"}))
        os.chmod(self.path, 0o644)
        with modes_while_writing() as seen:
            self.load(answers=["2", "admin@acme.com"], secrets=["abc123"])
        self.assertEqual(seen, [0o600])
        self.assertEqual(mode(self.path), 0o600)
        self.assertEqual(self.path.read_text(), json.dumps(
            {"subdomain": "acme", "email": "admin@acme.com"}, indent=2) + "\n")
        self.assertEqual(self.keychain, {("acme", "api_token"): "abc123"})

    def test_first_run_with_oauth_is_the_default_choice(self):
        config = self.load(answers=["acme", "", "my_client"], secrets=["shh"])
        self.assertEqual((config["client_id"], config["client_secret"], config["api_token"]), ("my_client", "shh", ""))
        self.assertEqual(json.loads(self.path.read_text()), {"subdomain": "acme", "client_id": "my_client"})
        self.assertEqual(self.keychain, {("acme", "client_secret"): "shh"})

    def test_public_client_clears_an_old_secret(self):
        self.keychain["acme", "client_secret"] = "old"
        self.path.write_text(json.dumps({"subdomain": "acme"}))
        config = self.load("oauth", answers=["my_client"], secrets=[""])
        self.assertEqual(config["client_secret"], "")
        self.assertEqual(self.keychain, {})

    def test_secrets_are_not_shown_while_typing(self):
        self.load(answers=["acme", "", "my_client"], secrets=["shh"])
        self.getpass.assert_called_once()

    def test_saved_token_is_used_without_asking(self):
        self.path.write_text(json.dumps({"subdomain": "acme", "client_id": "c", "email": "a@acme.com"}))
        self.keychain["acme", "api_token"] = "t"
        self.assertEqual(self.load()["api_token"], "t")

    def test_saved_oauth_client_is_used_without_asking(self):
        self.path.write_text(json.dumps({"subdomain": "acme", "client_id": "c"}))
        self.keychain["acme", "client_secret"] = "s"
        config = self.load()
        self.assertEqual((config["client_id"], config["client_secret"]), ("c", "s"))

    def test_another_accounts_secrets_are_not_used(self):
        self.path.write_text(json.dumps({"subdomain": "acme", "client_id": "c"}))
        self.keychain["other", "client_secret"] = "s"
        self.assertEqual(self.load()["client_secret"], "")

    def test_secrets_in_the_file_move_to_the_keychain(self):
        self.path.write_text(json.dumps({"subdomain": "acme", "client_id": "c", "client_secret": "s", "api_token": "t",
                                         "email": "a@acme.com"}))
        config = self.load()
        self.assertEqual((config["client_secret"], config["api_token"]), ("s", "t"))
        self.assertEqual(self.keychain, {("acme", "client_secret"): "s", ("acme", "api_token"): "t"})
        self.assertEqual(json.loads(self.path.read_text()), {"subdomain": "acme", "client_id": "c", "email": "a@acme.com"})
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_blank_secrets_in_the_file_are_dropped(self):
        self.path.write_text(json.dumps({"subdomain": "acme", "client_id": "c", "client_secret": "", "api_token": ""}))
        self.keychain["acme", "client_secret"] = "s"
        self.assertEqual(self.load()["client_secret"], "s")
        self.assertEqual(json.loads(self.path.read_text()), {"subdomain": "acme", "client_id": "c"})

    def test_file_keeps_its_secrets_if_the_keychain_fails(self):
        before = json.dumps({"subdomain": "acme", "client_id": "c", "client_secret": "s"})
        self.path.write_text(before)
        with mock.patch.object(keychain, "save", side_effect=keychain.KeychainError("locked")):
            with self.assertRaisesRegex(keychain.KeychainError, "locked"):
                self.load()
        self.assertEqual(self.path.read_text(), before)

    def test_oauth_flag_ignores_saved_token(self):
        self.path.write_text(json.dumps({"subdomain": "acme", "client_id": "c", "email": "a@acme.com"}))
        self.keychain["acme", "api_token"] = "t"
        config = self.load("oauth")
        self.assertEqual((config["api_token"], config["client_id"]), ("", "c"))
        self.assertEqual(self.keychain, {("acme", "api_token"): "t"})

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
        self.assertEqual(self.keychain, {})

    def test_environment_wins_over_the_file_and_is_cleaned_up(self):
        self.path.write_text(json.dumps({"subdomain": "old", "client_id": "c"}))
        with mock.patch.dict(os.environ, {"ZENDESK_SUBDOMAIN": "https://new.zendesk.com"}):
            self.assertEqual(self.load()["subdomain"], "new")

    def test_bad_subdomain_in_the_file_stops_the_run(self):
        for value in BAD_SUBDOMAINS:
            with self.subTest(value):
                original = json.dumps({"subdomain": value, "client_id": "c"})
                self.path.write_text(original)
                with self.assertRaises(SystemExit) as caught:
                    self.load()
                self.assertEqual(caught.exception.code, bad_subdomain(value))
                self.assertEqual(self.path.read_text(), original)

    def test_bad_subdomain_in_the_environment_stops_the_run(self):
        self.path.write_text(json.dumps({"subdomain": "acme", "client_id": "c"}))
        for value in BAD_SUBDOMAINS:
            with self.subTest(value):
                with mock.patch.dict(os.environ, {"ZENDESK_SUBDOMAIN": value}), \
                        self.assertRaises(SystemExit) as caught:
                    self.load()
                self.assertEqual(caught.exception.code, bad_subdomain(value))

    def test_bad_subdomain_typed_at_the_prompt_stops_the_run_and_is_not_saved(self):
        original = json.dumps({"client_id": "c"})
        for value in BAD_SUBDOMAINS:
            with self.subTest(value):
                self.path.write_text(original)
                with mock.patch("builtins.input", side_effect=[value, "acme"]) as typed, \
                        mock.patch("builtins.print"), self.assertRaises(SystemExit) as caught:
                    zd_macro_audit.load_config(self.path)
                self.assertEqual(caught.exception.code, bad_subdomain(value))
                self.assertEqual(typed.call_count, 1)
                self.assertEqual(self.path.read_text(), original)

    def test_bad_subdomain_on_a_first_run_writes_no_file(self):
        with mock.patch("builtins.input", side_effect=["evil.com#"]), mock.patch("builtins.print"), \
                self.assertRaises(SystemExit) as caught:
            zd_macro_audit.load_config(self.path)
        self.assertEqual(caught.exception.code, bad_subdomain("evil.com#"))
        self.assertFalse(self.path.exists())

    def test_blank_answers_stop_the_run(self):
        with self.assertRaisesRegex(SystemExit, "Missing settings: client_id"):
            self.load(answers=["acme", "", ""], secrets=[""])
        self.assertFalse(self.path.exists())
        self.assertEqual(self.keychain, {})

    def test_blank_subdomain_stops_the_run(self):
        with self.assertRaisesRegex(SystemExit, "Missing settings: subdomain"):
            self.load(answers=[""])
        self.assertFalse(self.path.exists())


class SignOutTests(TempFolderTest):
    def setUp(self):
        super().setUp()
        self.keychain.update({("acme", keychain.OAUTH_TOKEN): "o", ("acme", "client_secret"): "s",
                              ("acme", "api_token"): "t", ("other", "api_token"): "keep"})

    def test_removes_only_this_accounts_secrets(self):
        self.path.write_text(json.dumps({"subdomain": "acme"}))
        with mock.patch("builtins.print"):
            self.assertEqual(zd_macro_audit.main(["--sign-out", "--config", str(self.path)]), 0)
        self.assertEqual(self.keychain, {("other", "api_token"): "keep"})

    def test_subdomain_from_the_environment_is_cleaned_up(self):
        with mock.patch.dict(os.environ, {"ZENDESK_SUBDOMAIN": "https://acme.zendesk.com"}), mock.patch("builtins.print"):
            zd_macro_audit.main(["--sign-out", "--config", str(self.path)])
        self.assertEqual(self.keychain, {("other", "api_token"): "keep"})

    def test_no_subdomain(self):
        with self.assertRaisesRegex(SystemExit, "Nothing to sign out of"):
            zd_macro_audit.main(["--sign-out", "--config", str(self.path)])


class RunTests(TempFolderTest):
    def setUp(self):
        super().setUp()
        # A real .tokens/ next to the tool would add a warning to every run.
        patcher = mock.patch.object(zd_macro_audit, "HERE", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)

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
        self.path.write_text(json.dumps({"subdomain": "acme", "email": "a@acme.com"}))
        self.keychain["acme", "api_token"] = "t"
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
        self.path.write_text(json.dumps({"subdomain": "acme", "client_id": "c", "email": "a@acme.com"}))
        for flag, auth in (("--api-token", "token"), ("--oauth", "oauth")):
            with mock.patch.object(zd_macro_audit, "load_config", wraps=zd_macro_audit.load_config) as load:
                self.run_main(flag)
            self.assertEqual(load.call_args[0][1], auth)

    def test_macro_count_is_singular_for_one(self):
        macros = {"1": use_macro(1, "Fixed text.")}
        self.signed_in({"audits": [make_audit([1], "Fixed text.")], "macros": macros, "users": {}, "dynamic": {}})
        _, out, _ = self.run_main()
        self.assertIn("\nMacro uses found:     1 (1 macro)\n", out)

    def test_macro_count_is_plural_for_several(self):
        macros = {"1": use_macro(1, "Fixed text."), "2": use_macro(2, "Other text.")}
        audits = [make_audit([1], "Fixed text.", audit_id=1), make_audit([2], "Other text.", audit_id=2)]
        self.signed_in({"audits": audits, "macros": macros, "users": {}, "dynamic": {}})
        _, out, _ = self.run_main()
        self.assertIn("\nMacro uses found:     2 (2 macros)\n", out)

    def fetched(self, unread, tickets=100, **coverage):
        return {"audits": [], "macros": {}, "users": {}, "dynamic": {},
                "coverage": Coverage(tickets, unread, **coverage)}

    def report_folder(self):
        return next((self.dir / "reports").iterdir())

    def test_unread_tickets_are_warned_about(self):
        self.signed_in(self.fetched([5, 1234]))
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        folder = self.report_folder()
        self.assertIn("\nWarning: 2 tickets couldn't be read after several retries, so macro uses on them are "
                      "missing. Running again usually picks them up.\n  Unread tickets: 5, 1234\n"
                      f"  All listed in {folder / 'unread_tickets.csv'}\n\nReport: ", out)
        self.assertIn('<a href="https://acme.zendesk.com/agent/tickets/1234" target="_blank" rel="noopener">1234</a>',
                      (folder / "report.html").read_text(encoding="utf-8"))
        self.assertEqual((folder / "unread_tickets.csv").read_text(encoding="utf-8").splitlines()[1:],
                         ["5,https://acme.zendesk.com/agent/tickets/5", "1234,https://acme.zendesk.com/agent/tickets/1234"])

    def test_one_unread_ticket(self):
        self.signed_in(self.fetched([5]))
        _, out, _ = self.run_main()
        self.assertIn("Warning: 1 ticket couldn't be read after several retries, so macro uses on it are missing. "
                      "Running again usually picks it up.\n  Unread ticket: 5\n", out)

    def test_long_list_of_unread_tickets_is_cut_short(self):
        self.signed_in(self.fetched(list(range(1, 26))))
        _, out, _ = self.run_main()
        self.assertIn("Warning: 25 tickets couldn't", out)
        shown = ", ".join(str(i) for i in range(1, 21))
        self.assertIn(f"  Unread tickets: {shown}, and 5 more\n  All listed in ", out)

    def test_exactly_twenty_unread_tickets_are_all_shown(self):
        self.signed_in(self.fetched(list(range(1, 21))))
        _, out, _ = self.run_main()
        self.assertIn("  Unread tickets: " + ", ".join(str(i) for i in range(1, 21)) + "\n", out)

    def test_stopped_early_writes_a_partial_report_and_exits_1(self):
        self.signed_in(self.fetched(list(range(1, 31)), tickets=100, stopped_early=True))
        code, out, err = self.run_main()
        self.assertEqual((code, err), (1, ""))
        folder = self.report_folder()
        self.assertIn("\nWarning: the run stopped early because Zendesk stopped answering. 70 of 100 tickets read, "
                      "so these numbers only cover those. A later run should pick everything up.\n"
                      "  Unread tickets: " + ", ".join(str(i) for i in range(1, 21)) + ", and 10 more\n"
                      f"  All listed in {folder / 'unread_tickets.csv'}\n", out)
        self.assertNotIn("couldn't be read", out)
        self.assertIn("70 of 100 tickets read.", (folder / "report.html").read_text(encoding="utf-8"))
        self.assertEqual(len((folder / "unread_tickets.csv").read_text(encoding="utf-8").splitlines()), 31)

    def test_names_missing_is_warned_about(self):
        self.signed_in(self.fetched([], names_missing=True))
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("\nWarning: Zendesk stopped answering while the tool was fetching agent names, so agents "
                      "without a name are shown by ID.\n\nReport: ", out)
        self.assertNotIn("Unread", out)
        self.assertFalse((self.report_folder() / "unread_tickets.csv").exists())

    def test_skipped_tickets_get_a_note(self):
        self.signed_in(self.fetched([], skipped=[4, 8, 15]))
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("\n\nNote: 3 tickets were skipped because they were deleted or this sign-in can't see "
                      "them.\n\nReport: ", out)
        self.assertNotIn("Warning", out)

    def test_one_skipped_ticket(self):
        self.signed_in(self.fetched([], skipped=[4]))
        _, out, _ = self.run_main()
        self.assertIn("\n\nNote: 1 ticket was skipped because it was deleted or this sign-in can't see it.\n\n"
                      "Report: ", out)

    def test_skipped_note_comes_after_the_unread_tickets(self):
        self.signed_in(self.fetched([5], skipped=[4, 8], names_missing=True))
        _, out, _ = self.run_main()
        self.assertIn(f"  All listed in {self.report_folder() / 'unread_tickets.csv'}\n"
                      "\nNote: 2 tickets were skipped because they were deleted or this sign-in can't see them.\n"
                      "\nWarning: Zendesk stopped answering while the tool was fetching agent names", out)

    def test_no_note_when_nothing_was_skipped(self):
        self.signed_in(self.fetched([5]))
        _, out, _ = self.run_main()
        self.assertNotIn("Note:", out)
        self.assertNotIn("skipped", out)
        _, out, _ = self.run_main("--sample")
        self.assertNotIn("Note:", out)

    def test_no_warning_when_every_ticket_was_read(self):
        self.signed_in(self.fetched([]))
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertNotIn("Warning", out)
        self.assertNotIn("Unread", out)
        code, out, _ = self.run_main("--sample")
        self.assertEqual(code, 0)
        self.assertNotIn("Warning", out)

    def test_network_error_is_reported_without_a_traceback(self):
        self.signed_in(error=ZendeskError("Couldn't find acme.zendesk.com. Check the Zendesk subdomain."))
        code, out, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(err, "\nError: Couldn't find acme.zendesk.com. Check the Zendesk subdomain.\n")

    def fake_account(self, errors):
        """A run against made-up Zendesk answers, from sign-in to report."""
        self.path.write_text(json.dumps({"subdomain": "acme", "email": "a@acme.com"}))
        self.keychain["acme", "api_token"] = "t"
        fake = FakeZendesk(base_pages(), {audits_path(3): 404, "/api/v2/macros/56.json": 404, **errors})
        fake.authenticate = lambda force=False: None
        # main() picks its own start time, so answer the export whatever second it asks for.
        export = next(url for url in fake.pages if url.startswith("/api/v2/incremental/"))
        answer = fake.get
        fake.get = lambda url: answer(export if url.startswith("/api/v2/incremental/") else url)
        patcher = mock.patch.object(zd_macro_audit.zendesk, "Zendesk", return_value=fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_names_that_never_arrive_still_give_a_report(self):
        self.fake_account({"/api/v2/users/show_many.json?ids=7": gave_up()})
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        html = (self.report_folder() / "report.html").read_text(encoding="utf-8")
        self.assertIn("Not every agent name could be fetched.", html)
        self.assertIn("<td>User 7<div class='sub'></div></td>", html)
        self.assertIn("Warning: Zendesk stopped answering while the tool was fetching agent names, so agents "
                      "without a name are shown by ID.", out)

    def test_deleted_ticket_is_noted_from_sign_in_to_report(self):
        self.fake_account({})  # ticket 3 answers 404
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("\nNote: 1 ticket was skipped because it was deleted or this sign-in can't see it.\n", out)
        html = (self.report_folder() / "report.html").read_text(encoding="utf-8")
        self.assertIn('<strong>1 ticket was skipped.</strong>', html)
        self.assertIn('<div class="ids"><a href="https://acme.zendesk.com/agent/tickets/3" target="_blank" '
                      'rel="noopener">3</a></div>', html)

    def test_macros_that_never_arrive_give_no_report(self):
        self.fake_account({MACRO_PATH + "&include=usage_30d": gave_up()})
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(err, "\nError: Zendesk stopped answering while the tool was fetching macros, so there's no "
                              "report. 3 of 3 tickets were read, but the report needs the macros' text. A later run "
                              "should pick everything up.\n")
        self.assertFalse((self.dir / "reports").exists())

    def test_no_ticket_read_gives_no_report(self):
        self.fake_account({audits_path(t): gave_up() for t in (1, 2, 3)})
        code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("none of the 3 tickets could be read", err)
        self.assertFalse((self.dir / "reports").exists())

    def test_a_few_unread_tickets_after_a_full_run_exit_0(self):
        self.fake_account({audits_path(2): gave_up()})
        code, out, _ = self.run_main()
        self.assertEqual(code, 0)
        self.assertIn("  Unread ticket: 2\n", out)

    def test_link_to_another_site_stops_the_run_without_a_traceback(self):
        """A real client, so the check in Zendesk.get is what stops it."""
        self.path.write_text(json.dumps({"subdomain": "acme", "email": "a@acme.com"}))
        self.keychain["acme", "api_token"] = "t"
        sent = []

        def urlopen(request, timeout=None):
            sent.append(request.full_url)
            if request.full_url.endswith("/users/me.json"):
                return io.BytesIO(json.dumps({"user": {"id": 1, "role": "admin"}}).encode())
            if request.full_url.startswith("https://evil.com/"):
                return io.BytesIO(json.dumps({"tickets": [], "end_of_stream": True}).encode())
            return io.BytesIO(json.dumps({"tickets": [{"id": 1}], "end_of_stream": False,
                                          "after_url": "https://evil.com/api/v2/incremental/tickets/cursor"}).encode())

        with mock.patch("urllib.request.urlopen", urlopen):
            code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertEqual(err, "\nError: Zendesk sent a next-page link to https://evil.com instead of "
                              "https://acme.zendesk.com, so the run stopped rather than send the sign-in there.\n")
        self.assertEqual([url.split("?")[0] for url in sent],
                         ["https://acme.zendesk.com/api/v2/users/me.json",
                          "https://acme.zendesk.com/api/v2/incremental/tickets/cursor"])
        self.assertFalse((self.dir / "reports").exists())

    def test_real_runs_stop_on_other_systems(self):
        self.signed_in({"audits": [], "macros": {}, "users": {}, "dynamic": {}})
        with mock.patch.object(keychain, "supported", return_value=False):
            for args in ((), ("--sign-out",), ("--api-token",)):
                code, _, err = self.run_main(*args)
                self.assertEqual(code, 1)
                self.assertIn("Windows and Linux aren't available", err)
        self.client.assert_not_called()
        self.assertEqual(self.keychain[("acme", "api_token")], "t")

    def test_sample_still_runs_on_other_systems(self):
        with mock.patch.object(keychain, "supported", return_value=False):
            code, _, _ = self.run_main("--sample")
        self.assertEqual(code, 0)

    def test_keychain_error_is_reported(self):
        self.signed_in()
        with mock.patch.object(keychain, "load", side_effect=keychain.KeychainError("locked")):
            code, _, err = self.run_main()
        self.assertEqual(code, 1)
        self.assertIn("Error: locked", err)

    def test_old_plain_text_sign_in_gets_a_warning(self):
        self.signed_in({"audits": [], "macros": {}, "users": {}, "dynamic": {}})
        with mock.patch.object(zd_macro_audit, "HERE", self.dir):
            self.assertNotIn("old sign-in", self.run_main()[1])
            (self.dir / ".tokens").mkdir()
            self.assertIn("old sign-in", self.run_main()[1])

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
