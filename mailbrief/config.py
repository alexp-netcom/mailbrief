"""Load and validate the real configuration.

The config lives outside the repo at %USERPROFILE%\\.mailbrief\\config.toml.
Every error names the file and the fix. Nothing in here knows a password;
credentials come from Windows Credential Manager via keyring at runtime.
"""

from __future__ import annotations

import dataclasses
import os
import pathlib
import tomllib


def data_dir() -> pathlib.Path:
    override = os.environ.get("MAILBRIEF_DATA")
    if override:
        return pathlib.Path(override)
    return pathlib.Path.home() / ".mailbrief"


def config_path() -> pathlib.Path:
    return data_dir() / "config.toml"


class ConfigError(Exception):
    pass


@dataclasses.dataclass(frozen=True)
class Account:
    name: str
    host: str
    port: int
    username: str
    auth: str
    folders: tuple[str, ...]


@dataclasses.dataclass(frozen=True)
class Config:
    accounts: tuple[Account, ...]
    bulk_domains: tuple[str, ...]
    never_bulk_senders: tuple[str, ...]
    direct_max_recipients: int
    packet_max_chars: int
    addresses: tuple[str, ...] = ()  # user's own addresses, confirmed via `whoami`
    telegram_chat_ids: tuple[str, ...] = ()  # chat ids allowed to command the bot
    briefing_dir: pathlib.Path | None = None  # folder where the /brief skill writes briefings

    def account(self, name: str | None) -> Account:
        if name is None:
            if not self.accounts:
                raise ConfigError("no accounts in config; add an [[accounts]] section")
            return self.accounts[0]
        for a in self.accounts:
            if a.name == name:
                return a
        raise ConfigError(
            f"no account named {name!r}; config has: {', '.join(a.name for a in self.accounts)}"
        )


def load() -> Config:
    path = config_path()
    if not path.exists():
        raise ConfigError(
            f"config not found at {path}. Copy config.template.toml from the repo to "
            f"{path} and fill in host, username and folders."
        )
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"config {path} is not valid TOML: {e}") from e
    return parse(raw, path)


def parse(raw: dict, path: pathlib.Path) -> Config:
    accounts = []
    for i, entry in enumerate(raw.get("accounts", []), start=1):
        name = str(entry.get("name", f"account{i}"))
        host = entry.get("host")
        if not host:
            raise ConfigError(
                f"account {name!r} (entry {i}) is missing 'host'. Fix {path}."
            )
        username = entry.get("username")
        if not username:
            raise ConfigError(
                f"account {name!r} (entry {i}) is missing 'username'. Fix {path}."
            )
        try:
            port = int(entry.get("port", 993))
        except (TypeError, ValueError) as e:
            raise ConfigError(
                f"account {name!r}: 'port' must be a number, got {entry.get('port')!r}. Fix {path}."
            ) from e
        if not 1 <= port <= 65535:
            raise ConfigError(
                f"account {name!r}: port {port} is not a valid port. Fix {path}."
            )
        auth = str(entry.get("auth", "password"))
        if auth not in ("password", "oauth2"):
            raise ConfigError(
                f"account {name!r}: auth must be 'password' or 'oauth2', got {auth!r}. Fix {path}."
            )
        folders = tuple(str(f) for f in entry.get("folders", ["INBOX"]))
        # A [[accounts]] table stays open until the next table header, so a
        # top-level key written after it is silently absorbed into the account.
        # Catch that here instead of letting the defaults mask it.
        for key in ("addresses", "bulk_domains", "never_bulk_senders",
                    "direct_max_recipients", "packet_max_chars",
                    "telegram_chat_ids", "briefing_dir"):
            if key in entry:
                raise ConfigError(
                    f"account {name!r}: '{key}' must be a top-level key ABOVE "
                    f"[[accounts]] in {path} - keys after [[accounts]] are "
                    "absorbed into the account table and ignored."
                )
        accounts.append(
            Account(name=name, host=host, port=port, username=username, auth=auth, folders=folders)
        )
    if not accounts:
        raise ConfigError(f"config {path} has no [[accounts]] section. Add at least one account.")

    try:
        direct_max = int(raw.get("direct_max_recipients", 5))
    except (TypeError, ValueError) as e:
        raise ConfigError(
            f"'direct_max_recipients' must be a number, got {raw.get('direct_max_recipients')!r}. Fix {path}."
        ) from e
    try:
        packet_max = int(raw.get("packet_max_chars", 40000))
    except (TypeError, ValueError) as e:
        raise ConfigError(
            f"'packet_max_chars' must be a number, got {raw.get('packet_max_chars')!r}. Fix {path}."
        ) from e

    briefing_raw = raw.get("briefing_dir")
    briefing_dir = pathlib.Path(str(briefing_raw)) if briefing_raw else None

    return Config(
        accounts=tuple(accounts),
        bulk_domains=tuple(str(d) for d in raw.get("bulk_domains", [])),
        never_bulk_senders=tuple(str(s) for s in raw.get("never_bulk_senders", [])),
        direct_max_recipients=direct_max,
        packet_max_chars=packet_max,
        addresses=tuple(str(a) for a in raw.get("addresses", [])),
        telegram_chat_ids=tuple(str(c) for c in raw.get("telegram_chat_ids", [])),
        briefing_dir=briefing_dir,
    )
