"""Telegram briefing bot (docs/plan-telegram.md, approved 2026-09-01).

One-shot poll: getUpdates -> dispatch -> spawn `claude -p` with the /brief
skill -> read the briefing file from the briefing folder -> sendMessage back, chunked
at Telegram's 4096-char limit. Everything exits; schtasks runs it every
5 minutes. The bot token lives in Windows Credential Manager (keyring,
service "mailbrief", username "telegram-bot-token") and is registered in
the log redaction filter - it never appears in source, config, logs, CLI
arguments, or error messages.

The only mutating side effects are: sending chat replies (the bot's own
messages) and writing the offset/lock state files under the data dir.
Nothing here touches a mailbox.
"""

from __future__ import annotations

import dataclasses
import datetime
import getpass
import json
import logging
import os
import pathlib
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request

from .auth import SERVICE

logger = logging.getLogger(__name__)  # "mailbrief.telegram", redaction applies

TELEGRAM_API = "https://api.telegram.org"
TOKEN_USERNAME = "telegram-bot-token"
CHUNK_LIMIT = 4096
WAIT_SECONDS = 10
MAX_WAITS = 60  # ~10 minutes for claude -p to finish and write the file
STALE_LOCK_AGE = 900  # a lock older than 15 minutes belongs to a dead run: a
# briefing takes ~2-5 min of claude plus up to ~10 min of file waiting, so a
# younger lock is a legitimate overlap and an older one is an orphan
FRESH_FILE_GRACE = 30  # seconds of clock skew tolerated on briefing file mtimes

BUSY_REPLY = "briefing already running"
USAGE_REPLY = "unknown command - send 'brief morning' or 'brief eod'"

# The briefing folder is config-driven: `briefing_dir` in config.toml names
# where the /brief skill writes dated files and the bot reads them back. It
# is passed in by the CLI (cmd_telegram), never hardcoded here.


class TelegramError(Exception):
    pass


@dataclasses.dataclass
class PollResult:
    updates: int
    commands: int  # commands seen from allowed chats (handled or refused)
    briefings: int  # briefings actually written and sent


# --- HTTP ---------------------------------------------------------------

def http_transport(base_url: str, token: str, method: str, params: dict) -> dict:
    """POST to the Telegram Bot API; return the parsed JSON body.

    Errors never include the URL, so the token (part of the URL) can not
    leak into messages or logs.
    """
    url = f"{base_url}/bot{token}/{method}"
    body = json.dumps(params).encode("utf-8")
    req = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise TelegramError(
            f"Telegram API {method} failed: HTTP {e.code} {e.reason}"
        ) from e
    except urllib.error.URLError as e:
        raise TelegramError(
            f"cannot reach Telegram API ({method}): {e.reason}. "
            "Fix: check the network connection and retry."
        ) from e


def get_updates(token: str, transport, offset: int) -> list[dict]:
    """One non-blocking getUpdates call (timeout 0 - a poll, not a wait)."""
    body = transport(TELEGRAM_API, token, "getUpdates", {"offset": offset, "timeout": 0})
    if not body.get("ok"):
        raise TelegramError(f"Telegram refused getUpdates: {body}")
    return body.get("result", [])


def send_message(token: str, transport, chat_id: str, text: str) -> dict:
    body = transport(
        TELEGRAM_API, token, "sendMessage", {"chat_id": chat_id, "text": text}
    )
    if not body.get("ok"):
        raise TelegramError(f"Telegram refused sendMessage: {body}")
    logger.info("sent to chat %s: %r", chat_id, text[:80])
    return body


# --- chunking -----------------------------------------------------------

def _hard_split(block: str, limit: int) -> list[str]:
    """Split one block with no usable separators, keeping every char.

    The boundary character (newline or space) travels with the left piece,
    so joining the pieces reproduces the block exactly.
    """
    pieces: list[str] = []
    while len(block) > limit:
        cut = block.rfind("\n", 0, limit)
        if cut == -1:
            cut = block.rfind(" ", 0, limit)
        if cut == -1:
            cut = limit - 1
        pieces.append(block[: cut + 1])
        block = block[cut + 1 :]
    if block:
        pieces.append(block)
    return pieces


def chunk_text(text: str, limit: int = CHUNK_LIMIT) -> list[str]:
    """Split text into Telegram-sized messages, preferring blank lines.

    Chunks never exceed `limit`, and "".join(chunks) reproduces the input
    exactly, so the briefing reads in order with nothing lost.
    """
    if not text:
        return []
    if len(text) <= limit:
        return [text]
    chunks: list[str] = []
    current = ""
    for unit in re.split(r"(\n\n)", text):
        if len(unit) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_hard_split(unit, limit))
        elif current and len(current) + len(unit) > limit:
            chunks.append(current)
            current = unit
        else:
            current += unit
    if current:
        chunks.append(current)
    return chunks


# --- commands -----------------------------------------------------------

def command_of(text: str | None) -> str | None:
    """Map message text to a briefing window: 'morning', 'eod', or None."""
    if text is None:
        return None
    parts = text.strip().casefold().split()
    if len(parts) == 2 and parts[0] == "brief" and parts[1] in ("morning", "eod"):
        return parts[1]
    return None


# --- state: offset + lock ----------------------------------------------

def offset_path(data_dir: pathlib.Path) -> pathlib.Path:
    return data_dir / "telegram_offset.txt"


def read_offset(data_dir: pathlib.Path) -> int:
    """Last processed update_id + 1, or 0 on the first run."""
    try:
        return int(offset_path(data_dir).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def write_offset(data_dir: pathlib.Path, offset: int) -> None:
    path = offset_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(str(offset) + "\n", encoding="utf-8")
    tmp.replace(path)


def lock_path(data_dir: pathlib.Path) -> pathlib.Path:
    return data_dir / "locks" / "telegram.lock"


def acquire_lock(data_dir: pathlib.Path) -> pathlib.Path | None:
    """Take the briefing lock, or None if a run is already in flight.

    A lock older than STALE_LOCK_AGE is left over from a crashed run and is
    taken over so the bot repairs itself instead of dying silently.
    """
    path = lock_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            if time.time() - path.stat().st_mtime > STALE_LOCK_AGE:
                path.unlink()
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            else:
                return None
        except OSError:
            return None
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(
            f"pid={os.getpid()} started={datetime.datetime.now().isoformat()}\n"
        )
    return path


def release_lock(data_dir: pathlib.Path) -> None:
    try:
        lock_path(data_dir).unlink()
    except OSError:
        pass


# --- the claude half ----------------------------------------------------

def run_claude(window: str, data_dir: pathlib.Path, briefing_dir: pathlib.Path) -> int:
    """Spawn headless Claude Code to write the briefing; return its exit code.

    cwd is the repo root (derived from this file's location), because the
    skill path is repo-relative. stdout goes to logs/telegram-claude.out.txt
    for debugging. The briefing itself is read back from the briefing file,
    never from claude's stdout.
    """
    exe = shutil.which("claude") or shutil.which("claude.cmd")
    if not exe:
        raise TelegramError(
            "claude CLI not found on PATH. Fix: install Claude Code, or run "
            "/brief locally in Claude Code."
        )
    logger.info("spawning claude: %s (window %s)", exe, window)
    repo = pathlib.Path(__file__).resolve().parent.parent
    prompt = (
        f"Follow .claude/skills/brief/SKILL.md exactly and produce the "
        f"{window} briefing."
    )
    # Headless -p has nobody to approve tool calls, so grant exactly what
    # the skill needs: run the collector/packet commands, read the packet,
    # write the briefing file. Anything else still asks (and blocks).
    allowed = (
        "Read,Write,"
        "Bash(python -m mailbrief collect:*),"
        "Bash(python -m mailbrief packet:*),"
        "Bash(python -m mailbrief config:*)"
    )
    # The briefing lands OUTSIDE the repo (the briefing folder), which -p treats
    # as off-limits without --add-dir: without it the Write call is denied
    # and claude quietly prints the briefing instead of saving it.
    briefing_root = str(briefing_dir)
    out = data_dir / "logs" / "telegram-claude.out.txt"
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "ab") as log:  # no encoding arg: bytes, append mode
        if exe.lower().endswith(".cmd"):
            rc = subprocess.run(
                f'"{exe}" -p "{prompt}" --allowedTools "{allowed}" '
                f'--add-dir "{briefing_root}"',
                cwd=repo, stdout=log, stderr=subprocess.STDOUT, shell=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            ).returncode
        else:
            rc = subprocess.run(
                [exe, "-p", prompt, "--allowedTools", allowed,
                 "--add-dir", briefing_root],
                cwd=repo, stdout=log, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            ).returncode
    logger.info("claude exited with code %s", rc)
    return rc


def briefing_file(
    briefing_dir: pathlib.Path, window: str, now: datetime.datetime | None = None
) -> pathlib.Path:
    """The dated file /brief wrote for this window (local date, like the skill)."""
    local = (now or datetime.datetime.now()).astimezone()
    name = "Morning" if window == "morning" else "Evening"
    return briefing_dir / f"{local:%Y-%m-%d} {name}.md"


def _dispatch_briefing(
    *,
    token: str,
    transport,
    chat_id: str,
    window: str,
    data_dir: pathlib.Path,
    briefing_dir: pathlib.Path,
    claude_runner,
    wait,
    max_waits: int,
) -> bool:
    """Run one briefing on demand and send it back. True if sent."""
    # Windows NTFS mtimes are tick-quantized and can lag the precise wall
    # clock by up to ~16 ms, so a file written milliseconds after `start`
    # can still stat OLDER than it. A 30-second grace absorbs that skew
    # while still rejecting a genuinely stale briefing (hours old).
    start = time.time() - FRESH_FILE_GRACE
    out_log = data_dir / "logs" / "telegram-claude.out.txt"
    try:
        rc = claude_runner(window)
    except TelegramError as e:
        send_message(token, transport, chat_id, f"briefing failed: {e}")
        return False
    if rc != 0:
        send_message(
            token,
            transport,
            chat_id,
            f"briefing failed: claude exited with code {rc}. "
            f"Fix: check {out_log}, or run /brief locally in Claude Code.",
        )
        return False
    path = briefing_file(briefing_dir, window)
    for _ in range(max_waits):
        try:
            if path.exists() and path.stat().st_mtime >= start:
                text = path.read_text(encoding="utf-8", errors="replace")
                if not text.strip():
                    send_message(
                        token, transport, chat_id,
                        f"briefing file {path} is empty. "
                        "Fix: run /brief locally in Claude Code.",
                    )
                    return False
                chunks = chunk_text(text)
                for chunk in chunks:
                    send_message(token, transport, chat_id, chunk)
                send_message(
                    token, transport, chat_id,
                    f"briefing sent ({len(chunks)} messages)",
                )
                return True
        except OSError:
            pass
        wait(WAIT_SECONDS)
    send_message(
        token,
        transport,
        chat_id,
        f"briefing file {path} did not appear. "
        f"Fix: run /brief locally in Claude Code, or check {out_log}.",
    )
    return False


# --- the poll -----------------------------------------------------------

def run_poll(
    *,
    token: str,
    chat_ids: frozenset[str],
    data_dir: pathlib.Path,
    transport,
    briefing_dir: pathlib.Path,
    claude_runner=None,
    wait=time.sleep,
    max_waits: int = MAX_WAITS,
) -> PollResult:
    """Poll Telegram once, dispatch commands from allowed chats, exit.

    The update offset advances as soon as an update is read - a command is
    never run twice, even if the run after it crashes. Chat ids outside the
    allowlist are ignored silently (no info leak about the system).
    """
    offset = read_offset(data_dir)
    updates = get_updates(token, transport, offset)
    result = PollResult(updates=len(updates), commands=0, briefings=0)
    if updates:
        logger.info(
            "poll: offset %s, %s update(s), allowlist %s",
            offset, len(updates), sorted(chat_ids),
        )
    if not updates:
        return result
    if claude_runner is None:
        claude_runner = lambda window: run_claude(window, data_dir, briefing_dir)
    highest = offset
    for upd in updates:
        highest = max(highest, upd.get("update_id", 0) + 1)
    # Consume on read, BEFORE the slow dispatch: a briefing run outlives the
    # poll interval, and an overlapping poll must not re-run the command.
    # If this run then crashes, the command is lost - the user re-sends,
    # which is cheaper than a duplicate claude run.
    write_offset(data_dir, highest)
    for upd in updates:
        update_id = upd.get("update_id", 0)
        if update_id < offset:
            continue  # belt and braces: the API already respects offset
        msg = upd.get("message") or {}
        chat_id = str(msg.get("chat", {}).get("id", ""))
        if chat_id not in chat_ids:
            logger.info("ignoring update %s from chat %s (not in allowlist)", update_id, chat_id)
            continue
        text = msg.get("text")
        window = command_of(text)
        if window is None:
            if text is not None:  # a text message we do not understand
                logger.info("update %s: no command in %r - usage reply", update_id, text)
                send_message(token, transport, chat_id, USAGE_REPLY)
                result.commands += 1
            continue
        result.commands += 1
        logger.info("update %s: command %r from chat %s", update_id, window, chat_id)
        lock = acquire_lock(data_dir)
        if lock is None:
            logger.warning("lock busy - replying 'already running' for update %s", update_id)
            send_message(token, transport, chat_id, BUSY_REPLY)
            continue
        try:
            if _dispatch_briefing(
                token=token,
                transport=transport,
                chat_id=chat_id,
                window=window,
                data_dir=data_dir,
                briefing_dir=briefing_dir,
                claude_runner=claude_runner,
                wait=wait,
                max_waits=max_waits,
            ):
                result.briefings += 1
        finally:
            release_lock(data_dir)
    return result


def whoami(token: str, transport) -> list[dict]:
    """List the chats that have messaged the bot (allowlist discovery)."""
    updates = get_updates(token, transport, 0)
    seen: dict[str, dict] = {}
    for upd in updates:
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        if not chat_id or chat_id in seen:
            continue
        seen[chat_id] = {
            "id": chat_id,
            "first_name": chat.get("first_name", ""),
            "username": chat.get("username", ""),
        }
    return list(seen.values())


# --- token in Windows Credential Manager --------------------------------

def token_from_keyring() -> str:
    """Read the bot token; every failure message names the fix."""
    import keyring

    try:
        token = keyring.get_password(SERVICE, TOKEN_USERNAME)
    except Exception as e:
        raise TelegramError(
            f"cannot read the credential store: {e}. Fix: check that keyring "
            "works, then run 'python -m mailbrief store-telegram-token --file "
            "<path>'."
        ) from e
    if not token:
        raise TelegramError(
            "no Telegram bot token stored. Fix: create a bot with BotFather, "
            "paste the token into a text file, then run 'python -m mailbrief "
            "store-telegram-token --file <path>'."
        )
    return token


def store_token(file_path: str | None = None) -> None:
    """Store the Telegram bot token in Windows Credential Manager.

    Interactive by default (getpass, hidden input). With --file, read the
    token from a text file and delete that file immediately - the fallback
    for consoles where getpass cannot read hidden input. The token value is
    never printed, logged, or echoed.
    """
    import keyring

    if file_path is not None:
        path = pathlib.Path(file_path)
        if not path.exists():
            raise TelegramError(
                f"token file not found at {path}. Create it with Notepad: "
                "paste the BotFather token on the first line, save, then run "
                "this command again."
            )
        text = path.read_text(encoding="utf-8")
        token = text.splitlines()[0].strip() if text.splitlines() else ""
        if not token:
            raise TelegramError(
                f"token file {path} is empty. Put the token on the first line."
            )
        keyring.set_password(SERVICE, TOKEN_USERNAME, token)
        try:
            path.unlink()
        except OSError:
            pass  # deleting it is best-effort; the token is already stored
        if keyring.get_password(SERVICE, TOKEN_USERNAME) == token:
            print(
                "Stored the Telegram bot token in Windows Credential Manager. "
                f"Deleted {path}."
            )
        else:
            raise TelegramError(
                "token write did not verify on read-back; check the keyring backend."
            )
        return
    token = getpass.getpass("Telegram bot token (hidden input): ")
    if not token:
        print("No input received; nothing stored.")
        return
    keyring.set_password(SERVICE, TOKEN_USERNAME, token)
    if keyring.get_password(SERVICE, TOKEN_USERNAME) == token:
        print("Stored the Telegram bot token in Windows Credential Manager.")
    else:
        raise TelegramError(
            "token write did not verify on read-back; check the keyring backend."
        )
