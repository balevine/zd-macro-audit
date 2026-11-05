"""Keeps sign-in secrets in the macOS keychain, using Apple's `security` command.

Secrets reach `security -i` hex-encoded over standard input, so they never
appear on a command line where other programs could see them.
"""
from __future__ import annotations

import re
import subprocess
import sys

SERVICE = "zd-macro-audit"
OAUTH_TOKEN = "oauth_token"
SECURITY = "/usr/bin/security"
KEYCHAIN: str | None = None  # None is the user's default keychain. Tests use a throwaway one.
TIMEOUT = 120  # seconds, long enough for someone to answer an unlock prompt

# `security -i` cuts a line at 4,095 characters and runs the rest as a separate
# command, which would lose the end of the secret. Stay under that.
MAX_LINE = 4000
NOT_FOUND = 44


class KeychainError(Exception):
    pass


def supported() -> bool:
    return sys.platform == "darwin"


def save(subdomain: str, name: str, secret: str) -> None:
    """Save a secret, replacing any older one. An empty secret removes it."""
    if not secret:
        delete(subdomain, name)
        return
    # `security` hands back anything else as hex, so it wouldn't read back the same.
    if not (secret.isascii() and secret.isprintable()):
        raise KeychainError(f"The {name} has characters the macOS keychain can't store. Check what you pasted.")
    line = " ".join(["add-generic-password", "-U", "-s", SERVICE, "-a", _account(subdomain, name),
                     "-X", secret.encode().hex(), *_keychain()]) + "\n"
    if len(line) > MAX_LINE:
        raise KeychainError(f"The {name} is too long to save in the macOS keychain.")
    result = _run(["-i"], line)
    if result.returncode:
        raise KeychainError(f"Couldn't save the {name} in the macOS keychain (error {result.returncode}). "
                            "Check that the keychain is unlocked.")


def load(subdomain: str, name: str) -> str:
    """The saved secret, or "" if there isn't one."""
    result = _run(["find-generic-password", "-s", SERVICE, "-a", _account(subdomain, name), "-w", *_keychain()])
    if result.returncode == NOT_FOUND:
        return ""
    if result.returncode:
        raise KeychainError(f"Couldn't read the {name} from the macOS keychain (error {result.returncode}). "
                            "Check that the keychain is unlocked.")
    return result.stdout.rstrip("\n")


def delete(subdomain: str, name: str) -> None:
    result = _run(["delete-generic-password", "-s", SERVICE, "-a", _account(subdomain, name), *_keychain()])
    if result.returncode not in (0, NOT_FOUND):
        raise KeychainError(f"Couldn't remove the {name} from the macOS keychain (error {result.returncode}).")


def _account(subdomain: str, name: str) -> str:
    # This goes into a `security -i` command line, so spaces could add options.
    if not re.fullmatch(r"[A-Za-z0-9-]+", subdomain):
        raise KeychainError(f"'{subdomain}' doesn't look like a Zendesk subdomain.")
    return f"{subdomain}/{name}"


def _keychain() -> list[str]:
    return [KEYCHAIN] if KEYCHAIN else []


def _run(args: list[str], stdin: str | None = None) -> subprocess.CompletedProcess:
    try:
        return subprocess.run([SECURITY, *args], input=stdin, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        raise KeychainError("The macOS keychain didn't respond. Check that it's unlocked and try again.") from None
    except OSError as exc:
        raise KeychainError(f"Couldn't run {SECURITY}: {exc.strerror}") from None
