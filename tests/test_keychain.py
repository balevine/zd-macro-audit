import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from zdaudit import keychain
from zdaudit.keychain import KeychainError


def result(code=0, stdout="", stderr=""):
    return subprocess.CompletedProcess([], code, stdout, stderr)


class CommandTests(unittest.TestCase):
    """What gets sent to `security`, without running it."""

    def run_with(self, *answers):
        patcher = mock.patch("subprocess.run", side_effect=list(answers))
        self.run_mock = patcher.start()
        self.addCleanup(patcher.stop)

    def test_save_sends_the_secret_hex_encoded_over_stdin(self):
        self.run_with(result())
        keychain.save("acme", "api_token", "s3cret")
        args, kwargs = self.run_mock.call_args
        self.assertEqual(args[0], [keychain.SECURITY, "-i"])
        self.assertEqual(kwargs["input"], "add-generic-password -U -s zd-macro-audit -a acme/api_token -X "
                                          + b"s3cret".hex() + "\n")
        self.assertNotIn("s3cret", kwargs["input"])
        self.assertTrue(kwargs["capture_output"])

    def test_limit_stays_under_where_security_cuts_lines(self):
        self.assertLess(keychain.MAX_LINE, 4095)

    def test_every_command_names_the_chosen_keychain(self):
        self.run_with(result(), result(), result())
        with mock.patch.object(keychain, "KEYCHAIN", "/x/t.keychain-db"):
            keychain.save("acme", "t", "s")
            keychain.load("acme", "t")
            keychain.delete("acme", "t")
        save, load, delete = self.run_mock.call_args_list
        self.assertTrue(save.kwargs["input"].endswith(" /x/t.keychain-db\n"))
        self.assertEqual(load[0][0][-1], "/x/t.keychain-db")
        self.assertEqual(delete[0][0][-1], "/x/t.keychain-db")

    def test_load_returns_the_secret_without_the_newline(self):
        self.run_with(result(stdout="s3cret\n"))
        self.assertEqual(keychain.load("acme", "api_token"), "s3cret")
        self.assertEqual(self.run_mock.call_args[0][0], [keychain.SECURITY, "find-generic-password", "-s",
                                                         "zd-macro-audit", "-a", "acme/api_token", "-w"])

    def test_load_of_nothing_saved_is_empty(self):
        self.run_with(result(code=44))
        self.assertEqual(keychain.load("acme", "api_token"), "")

    def test_empty_secret_deletes(self):
        self.run_with(result())
        keychain.save("acme", "client_secret", "")
        self.assertEqual(self.run_mock.call_args[0][0][1], "delete-generic-password")

    def test_delete_of_nothing_saved_is_fine(self):
        self.run_with(result(code=44))
        keychain.delete("acme", "api_token")

    def test_longest_secret_that_fits_is_sent(self):
        self.run_with(result())
        prefix = len("add-generic-password -U -s zd-macro-audit -a acme/t -X \n")
        keychain.save("acme", "t", "x" * ((keychain.MAX_LINE - prefix) // 2))
        self.run_mock.assert_called_once()
        self.assertLessEqual(len(self.run_mock.call_args.kwargs["input"]), keychain.MAX_LINE)

    def test_too_long_is_refused_before_anything_runs(self):
        self.run_with()
        prefix = len("add-generic-password -U -s zd-macro-audit -a acme/t -X \n")
        with self.assertRaisesRegex(KeychainError, "too long"):
            keychain.save("acme", "t", "x" * ((keychain.MAX_LINE - prefix) // 2 + 1))
        self.run_mock.assert_not_called()

    def test_characters_that_would_not_read_back_are_refused(self):
        self.run_with()
        for secret in ("café", "two\nlines", "tab\there"):
            with self.assertRaisesRegex(KeychainError, "can't store"):
                keychain.save("acme", "t", secret)
        self.run_mock.assert_not_called()

    def test_subdomain_cannot_add_options(self):
        self.run_with()
        for subdomain in ("", "acme -T /bin/sh", "acme\nadd-generic-password", "../x"):
            for call in (lambda: keychain.save(subdomain, "t", "s"), lambda: keychain.load(subdomain, "t"),
                         lambda: keychain.delete(subdomain, "t")):
                with self.assertRaisesRegex(KeychainError, "subdomain"):
                    call()
        self.run_mock.assert_not_called()

    def test_failures_report_the_code_and_not_the_output(self):
        for call in (lambda: keychain.save("acme", "api_token", "s3cret"),
                     lambda: keychain.load("acme", "api_token"), lambda: keychain.delete("acme", "api_token")):
            self.run_with(result(code=51, stderr="leaked 733363726574"))
            with self.assertRaises(KeychainError) as caught:
                call()
            self.assertIn("error 51", str(caught.exception))
            self.assertNotIn("leaked", str(caught.exception))

    def test_a_stuck_keychain_is_explained(self):
        self.run_with(subprocess.TimeoutExpired("security", 1))
        with self.assertRaisesRegex(KeychainError, "didn't respond"):
            keychain.load("acme", "t")

    def test_missing_command_is_explained(self):
        self.run_with(FileNotFoundError(2, "No such file or directory"))
        with self.assertRaisesRegex(KeychainError, "No such file"):
            keychain.load("acme", "t")

    def test_supported_only_on_macos(self):
        for platform, expected in (("darwin", True), ("win32", False), ("linux", False)):
            with mock.patch.object(sys, "platform", platform):
                self.assertEqual(keychain.supported(), expected)


@unittest.skipUnless(sys.platform == "darwin", "needs macOS")
class RealKeychainTests(unittest.TestCase):
    """Runs the real `security` command against a throwaway keychain, never the user's own."""

    PASSWORD = "throwaway-test-only"

    @classmethod
    def setUpClass(cls):
        cls.folder = tempfile.TemporaryDirectory(prefix="zdkc")
        cls.path = os.path.join(cls.folder.name, "t.keychain-db")
        cls.security = "/usr/bin/security"
        made = subprocess.run([cls.security, "create-keychain", "-p", cls.PASSWORD, cls.path], capture_output=True)
        if made.returncode:
            cls.folder.cleanup()
            raise unittest.SkipTest("can't create a keychain here")
        subprocess.run([cls.security, "unlock-keychain", "-p", cls.PASSWORD, cls.path], check=True, capture_output=True)

    @classmethod
    def tearDownClass(cls):
        subprocess.run([cls.security, "delete-keychain", cls.path], capture_output=True)
        cls.folder.cleanup()

    def setUp(self):
        for name, value in (("SECURITY", self.security), ("KEYCHAIN", self.path)):
            patcher = mock.patch.object(keychain, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.addCleanup(keychain.delete, "acme", "t")

    def test_save_load_replace_delete(self):
        self.assertEqual(keychain.load("acme", "t"), "")
        keychain.save("acme", "t", "first")
        self.assertEqual(keychain.load("acme", "t"), "first")
        keychain.save("acme", "t", "second")
        self.assertEqual(keychain.load("acme", "t"), "second")
        keychain.delete("acme", "t")
        self.assertEqual(keychain.load("acme", "t"), "")
        keychain.delete("acme", "t")

    def test_accounts_are_separate(self):
        self.addCleanup(keychain.delete, "other", "t")
        keychain.save("acme", "t", "a")
        keychain.save("other", "t", "b")
        self.assertEqual((keychain.load("acme", "t"), keychain.load("other", "t")), ("a", "b"))

    def test_awkward_secrets_come_back_exactly(self):
        for secret in ('{"access_token": "a b\'c\\"d", "n": 1}', "$(touch x) `y` ; & | > #", "  spaces around  "):
            keychain.save("acme", "t", secret)
            self.assertEqual(keychain.load("acme", "t"), secret)

    def test_longest_secret_comes_back_exactly(self):
        prefix = len(f"add-generic-password -U -s zd-macro-audit -a acme/t -X  {self.path}\n")
        secret = "x" * ((keychain.MAX_LINE - prefix) // 2)
        keychain.save("acme", "t", secret)
        self.assertEqual(keychain.load("acme", "t"), secret)


if __name__ == "__main__":
    unittest.main()
