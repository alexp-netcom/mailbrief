"""One-shot setup for mailbrief.

Run this once, after you have:
  1. copied config.template.toml to config.toml and filled in your details
  2. put your mailbox password in password.txt (first line only), if you use one

The script installs the one dependency (keyring) into your system Python,
creates the folders the app uses, moves config.toml into
%USERPROFILE%\\.mailbrief\\config.toml, and moves password.txt into Windows
Credential Manager (then deletes it). Your password is never printed or stored
as text.

password.txt sets up only the first account in config.toml; extra accounts are
stored with `python -m mailbrief store-credential <name>`.
"""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys

if sys.version_info < (3, 11):
    sys.exit(
        "mailbrief needs Python 3.11 or newer (it uses tomllib). You have "
        + sys.version.split()[0]
        + ". Install a newer Python and run this again."
    )

import tomllib  # needs 3.11+; checked above

REPO_ROOT = pathlib.Path(__file__).resolve().parent
DATA_DIR = pathlib.Path.home() / ".mailbrief"
CONFIG_TEMPLATE = REPO_ROOT / "config.template.toml"
REPO_CONFIG = REPO_ROOT / "config.toml"
PASSWORD_FILE = REPO_ROOT / "password.txt"
REQUIREMENTS = REPO_ROOT / "requirements.txt"


def run(cmd: list[str]) -> int:
    print("+ " + " ".join(cmd))
    return subprocess.call(cmd, cwd=REPO_ROOT)


def step_install_deps() -> None:
    print("\n== 1. Installing the one dependency (keyring) ==")
    if not REQUIREMENTS.exists():
        sys.exit("requirements.txt not found - is this the mailbrief folder?")
    if run([sys.executable, "-m", "pip", "install", "-r", str(REQUIREMENTS)]) != 0:
        sys.exit("pip install failed. Check that Python and pip are installed and on PATH.")


def step_folders() -> None:
    print("\n== 2. Creating runtime folders ==")
    for name in ("logs", "locks", "packets"):
        path = DATA_DIR / name
        path.mkdir(parents=True, exist_ok=True)
        print(f"  {path}")


def step_config() -> pathlib.Path:
    print("\n== 3. Installing config.toml ==")
    target = DATA_DIR / "config.toml"
    if target.exists():
        print(f"  already present at {target} - leaving it alone")
        return target
    if not REPO_CONFIG.exists():
        if not CONFIG_TEMPLATE.exists():
            sys.exit("no config.template.toml and no config.toml found. Re-copy the project.")
        shutil.copy2(CONFIG_TEMPLATE, REPO_CONFIG)
        print(f"  no config.toml found - copied the template to {REPO_CONFIG}")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(REPO_CONFIG), str(target))
    print(f"  installed config at {target}")
    return target


def _primary_username(config_path: pathlib.Path) -> str:
    try:
        with open(config_path, "rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        print(f"  could not read {config_path}: {e}")
        return ""
    accounts = raw.get("accounts", [])
    if not accounts:
        return ""
    return str(accounts[0].get("username", ""))


def step_password(config_path: pathlib.Path) -> None:
    print("\n== 4. Moving password.txt into Windows Credential Manager ==")
    if not PASSWORD_FILE.exists():
        print("  no password.txt found - skipped.")
        print("  (If your mailbox needs a password, create password.txt and re-run,")
        print("  or run: python -m mailbrief store-credential)")
        return
    username = _primary_username(config_path)
    if not username or "example.com" in username:
        print(f"  WARNING: config still has a placeholder username ({username or '(none)'}).")
        print(f"  Fill in your real username in {config_path}, then re-run this script.")
        print("  password.txt was left in place.")
        return
    if (
        run([sys.executable, "-m", "mailbrief", "store-credential", "--file", str(PASSWORD_FILE)])
        != 0
    ):
        print("  store-credential failed (see above). password.txt was left in place.")
        return
    if PASSWORD_FILE.exists():
        print("  note: password.txt is still there; it may not have been deleted.")


def main() -> None:
    print("mailbrief setup")
    print(f"  project folder: {REPO_ROOT}")
    print(f"  data folder:    {DATA_DIR}")
    step_install_deps()
    step_folders()
    config_path = step_config()
    step_password(config_path)

    print("\n== Done ==")
    print(f"Settings live in {config_path} (run 'python -m mailbrief config' to see it).")
    print("If you have not filled in real values yet, edit that file now.")
    print("\nCheck it connects (optional but recommended):")
    print("  python -m mailbrief check")
    print("\nDaily use:")
    print("  In Claude Code:       /brief morning   or   /brief eod")
    print("  Without Claude Code:  python -m mailbrief brief")


if __name__ == "__main__":
    main()
