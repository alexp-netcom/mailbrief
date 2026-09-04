"""Authentication seam.

PasswordAuth logs in with a password from Windows Credential Manager via
keyring. OAuth2Auth is the future Microsoft 365 path; the seam exists now so
nothing downstream of "give me an authenticated connection" changes when it
lands. See docs/plan.md section 12.

The password is never printed, logged, or passed on a command line. It goes
from Credential Manager straight into imaplib's LOGIN command.
"""

from __future__ import annotations

import getpass
import pathlib

import keyring

from .config import Account

SERVICE = "mailbrief"


class AuthError(Exception):
    pass


class PasswordAuth:
    def __init__(self, account: Account) -> None:
        self.account = account

    def password(self) -> str:
        try:
            pw = keyring.get_password(SERVICE, self.account.username)
        except Exception as e:
            raise AuthError(
                f"cannot read the credential store: {e}. Check that keyring works: "
                'python -c "import keyring; print(keyring.get_keyring())"'
            ) from e
        if pw is None:
            raise AuthError(
                f"no password stored for {self.account.username}. "
                f"Run: python -m mailbrief store-credential {self.account.name}"
            )
        return pw

    def login(self, client) -> None:
        client.login(self.account.username, self.password())


class OAuth2Auth:
    """Stub. The Microsoft 365 account does not exist yet."""

    def __init__(self, account: Account) -> None:
        self.account = account

    def login(self, client) -> None:
        raise AuthError(
            f"account {self.account.name!r} uses auth=oauth2, which is not built yet. "
            "See docs/plan.md section 12 (Microsoft 365)."
        )


def for_account(account: Account) -> PasswordAuth | OAuth2Auth:
    if account.auth == "oauth2":
        return OAuth2Auth(account)
    return PasswordAuth(account)


def store_credential(account: Account, file_path: str | None = None) -> None:
    """Store the mailbox password in Windows Credential Manager.

    Interactive by default (getpass, hidden input). With --file, read the
    password from a text file and delete that file immediately. This is the
    fallback for consoles where getpass cannot read hidden input. The
    password value is never printed, logged, or echoed.
    """
    if file_path is not None:
        path = pathlib.Path(file_path)
        if not path.exists():
            raise AuthError(
                f"credential file not found at {path}. Create it with Notepad: type the "
                "password on the first line and save, then run this command again."
            )
        text = path.read_text(encoding="utf-8")
        pw = text.splitlines()[0] if text.splitlines() else ""
        if not pw:
            raise AuthError(f"credential file {path} is empty. Put the password on the first line.")
        keyring.set_password(SERVICE, account.username, pw)
        try:
            path.unlink()
        except OSError:
            pass  # deleting it is best-effort; the credential is already stored
        check = keyring.get_password(SERVICE, account.username)
        if check == pw:
            print(
                f"Stored credential for {account.username} in Windows Credential Manager. "
                f"Deleted {path}."
            )
        else:
            raise AuthError("credential write did not verify on read-back; check the keyring backend.")
        return
    pw = getpass.getpass(f"Password for {account.username} (hidden input): ")
    if not pw:
        print("No input received; nothing stored.")
        return
    keyring.set_password(SERVICE, account.username, pw)
    check = keyring.get_password(SERVICE, account.username)
    if check == pw:
        print(f"Stored credential for {account.username} in Windows Credential Manager.")
    else:
        raise AuthError("credential write did not verify on read-back; check the keyring backend.")
