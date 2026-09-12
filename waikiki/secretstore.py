"""Secrets that must not live in a wiki file — currently the Kahala refresh token.

Everything else Waikiki remembers goes in one of two places: per-wiki rows in the
wiki's own SQLite file, or ``app_config.json`` beside it. Neither can hold a
credential:

* the wiki file **is** the export. "Save wiki" copies that database, and
  ``store.export_wiki_bundle`` reads from it — so a settings row containing a
  refresh token would be handed to whoever you shared a wiki with.
* ``app_config.json`` is plain JSON in the data directory. It never leaves the
  machine on its own, but it is world-readable-to-you plaintext that lands in
  Time Machine and any folder backup.

So credentials go to the macOS Keychain instead, via the ``security`` tool.

What this actually buys, stated honestly
----------------------------------------
The Keychain protects the token **at rest and in transit off the machine**: it
is not in a file you can accidentally export, mail, or commit, and a stolen
backup of the data directory contains nothing to sign in with.

It does **not** protect against code already running as you. A same-user process
can shell out to ``security`` exactly as this module does and read the item back
without a prompt. That is the same trust boundary the rest of the app already
sits on — ``auth.py`` grants loopback callers owner rights — so this is not a new
exposure, and it would be dishonest to describe it as one. Don't write docs that
imply more.

The secret never appears in ``argv``. ``security -i`` reads its commands from
stdin, so the token is not visible in ``ps`` output while the write happens.
That stdin is a *command stream*, though -- one command per line -- so a value
carrying a newline would end the write command and have its remainder read as
the next ``security`` command. The refresh token is minted by the sign-in server,
which makes it the one value here that arrives from off the machine, so values
are checked (:func:`_sendable`) rather than escaped: no quoting makes a newline
part of a word in that parser, and a credential that can't be stored safely must
fail loudly rather than creatively.

No silent fallback
------------------
:func:`available` reports whether there is a real secure store here. When there
isn't — a future Linux or Windows build, or a Mac where ``security`` is missing —
callers must refuse to sign in and say so. Writing the token to a file "just for
now" is exactly the outcome this module exists to prevent, so there is no file
backend to fall back to, not even a hidden one.
"""
from __future__ import annotations

import shutil
import subprocess

SERVICE = "Waikiki"
"""Keychain service name. Every item we own carries it, so a user can find and
delete them all in Keychain Access without guessing."""

_TIMEOUT = 10


def _security() -> str | None:
    return shutil.which("security")


def available() -> bool:
    """True when this machine has a secure store we can use."""
    return _security() is not None


def unavailable_reason() -> str:
    """Why :func:`available` is False, in words a person can act on."""
    if available():
        return ""
    return ("This build has no secure place to keep a sign-in token. Waikiki "
            "uses the macOS Keychain, and the `security` tool it needs isn't "
            "here.")


def _sendable(value: str) -> bool:
    """Whether ``value`` can cross into ``security`` unchanged.

    Control characters are refused rather than escaped. A newline is the one that
    matters -- ``security -i`` reads one command per line, so anything after it
    in a value would be parsed as another ``security`` command -- but none of
    them can legitimately appear in what we store: the account is a Keycloak
    issuer URL and the secret is an OAuth refresh token.

    The ``find``/``delete`` paths pass their arguments as a list, where no
    splitting happens and nothing could be injected in the first place; they
    check too, so that "what may be stored" and "what may be looked up" cannot
    drift apart into an item that can be written and never read back.
    """
    return bool(value) and not any(ch < " " or ch == "\x7f" for ch in value)


def set_secret(account: str, secret: str) -> bool:
    """Store (or replace) ``secret`` under ``account``. True when it landed.

    ``-U`` updates an existing item rather than erroring, so re-signing in
    replaces the old token instead of accumulating duplicates.
    """
    sec = _security()
    if not sec or not account or not secret:
        return False
    if not _sendable(account) or not _sendable(secret):
        return False
    # Interactive mode: the command (and so the secret) arrives on stdin, never
    # in argv where `ps` would show it to every process running as this user.
    script = (f'add-generic-password -U -s {_q(SERVICE)} -a {_q(account)} '
              f'-w {_q(secret)}\n')
    try:
        done = subprocess.run([sec, "-i"], input=script, text=True,
                              capture_output=True, timeout=_TIMEOUT)
    except Exception:
        return False
    return done.returncode == 0


def get_secret(account: str) -> str | None:
    """The secret stored under ``account``, or None if there isn't one."""
    sec = _security()
    if not sec or not account or not _sendable(account):
        return None
    try:
        done = subprocess.run(
            [sec, "find-generic-password", "-s", SERVICE, "-a", account, "-w"],
            text=True, capture_output=True, timeout=_TIMEOUT)
    except Exception:
        return None
    if done.returncode != 0:
        return None
    # `-w` prints the password and a newline; a trailing strip is safe because
    # we only ever store tokens, which never carry meaningful whitespace.
    return done.stdout.strip() or None


def delete_secret(account: str) -> bool:
    """Remove ``account``'s secret. True if something was removed."""
    sec = _security()
    if not sec or not account or not _sendable(account):
        return False
    try:
        done = subprocess.run(
            [sec, "delete-generic-password", "-s", SERVICE, "-a", account],
            text=True, capture_output=True, timeout=_TIMEOUT)
    except Exception:
        return False
    return done.returncode == 0


def _q(value: str) -> str:
    r"""Quote one argument for ``security -i``'s own command parser.

    This is not a shell -- no subshell runs -- but ``-i`` does split its input
    into words and honours double quotes, so a token containing a space or a
    quote would otherwise be truncated or mis-parsed. Escape the two characters
    that mean something inside a quoted word (``\`` and ``"``) and wrap.

    Quoting is not enough on its own: a newline ends the *command*, whatever it
    is quoted inside. :func:`_sendable` refuses those before this is reached.
    """
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
