"""CLI entry point: python -m mailbrief <command>.

Commands built so far: config, store-credential, check (stage 1), fetch
(stage 2), parse and show (stage 3). Commands from later stages print a clear
"not built yet" message instead of crashing, so the tool stays repairable
at 8am.
"""

from __future__ import annotations

import argparse
import datetime
import email
import os
import pathlib
import shutil
import sys
from email import policy

from . import auth
from . import classify
from . import config as cfg
from . import imapclient
from . import log
from . import parse
from . import packet as packet_mod
from . import store as store_mod
from . import telegram
from . import threads


def _setup_logging():
    logger = log.setup_logging(cfg.data_dir())
    path = cfg.config_path()
    if path.exists():
        try:
            conf = cfg.load()
        except cfg.ConfigError:
            conf = None
        if conf:
            try:
                import keyring

                for account in conf.accounts:
                    try:
                        pw = keyring.get_password("mailbrief", account.username)
                    except Exception:
                        continue
                    if pw:
                        log.register_secret(logger, pw)
            except Exception:
                pass
    try:
        import keyring

        tok = keyring.get_password("mailbrief", telegram.TOKEN_USERNAME)
    except Exception:
        tok = None
    if tok:
        log.register_secret(logger, tok)
    return logger


def cmd_config(args) -> int:
    print(cfg.config_path())
    return 0


def cmd_store_credential(args) -> int:
    conf = cfg.load()
    account = conf.account(args.account)
    auth.store_credential(account, file_path=args.file)
    return 0


def _check_account(account) -> bool:
    client = imapclient.ImapClient(account)
    try:
        client.connect()
    except imapclient.ImapError as e:
        print(f"Account {account.name}: CONNECT FAILED - {e}")
        return False
    print(f"Account: {account.name}")
    print(f"  host:        {account.host}:{account.port}")
    print(f"  TLS:         {client.tls_version}, verification ON")
    print(f"  certificate: {imapclient.certificate_summary(client.certificate)}")
    print(f"  greeting:    {client.welcome()}")
    try:
        client.login(auth.for_account(account))
    except (auth.AuthError, imapclient.ImapError) as e:
        print(f"  login:       FAILED - {e}")
        client.disconnect()
        return False
    print(f"  login:       OK ({account.username})")
    try:
        caps = client.post_login_capabilities()
        print("  capabilities (post-login): " + " ".join(caps))
    except imapclient.ImapError as e:
        print(f"  capabilities: FAILED - {e}")
    try:
        names = client.folders()
        for name in names:
            count, err = client.examine_count(name)
            if err:
                print(f"    {name}: cannot examine ({err})")
            else:
                print(f"    {name}: {count} messages")
        missing = [f for f in account.folders if f not in names]
        for f in missing:
            print(f"  WARNING: configured folder {f!r} not found on the server.")
            print("           Pick the real name from the folder list above and fix config.toml.")
    except imapclient.ImapError as e:
        print(f"  folders:     FAILED - {e}")
    finally:
        client.disconnect()
    return True


def cmd_check(args) -> int:
    conf = cfg.load()
    ok = True
    for account in conf.accounts:
        if not _check_account(account):
            ok = False
    return 0 if ok else 1


def _clip(s: str, n: int) -> str:
    return s if len(s) <= n else s[: n - 3] + "..."


def _fmt_date(iso: str | None) -> str:
    if not iso:
        return "?"
    s = iso[:16].replace("T", " ")
    if iso.endswith("+00:00"):
        s += "Z"
    return s


def _fetch_folder(client, store, account_name: str, folder: str, args) -> None:
    exists, uidvalidity, _uidnext = client.select_folder(folder)
    prev_validity, highest = store.folder_state(folder)
    resync = prev_validity != 0 and prev_validity != uidvalidity
    if resync:
        print(
            f"  {folder}: UIDVALIDITY changed {prev_validity} -> {uidvalidity}; "
            "full resync (Message-ID dedupe on)"
        )
    start = 1 if resync else highest + 1
    uids = client.search_uids(start)
    if args.limit:
        uids = uids[: args.limit]
    print(
        f"  {folder}: {exists} messages on server, uidvalidity={uidvalidity}, "
        f"new since highest_uid={highest}: {len(uids)}"
    )
    if not uids:
        return
    if args.dry_run:
        for uid, header, internaldate in client.fetch_headers(uids):
            rec = parse.envelope(header, uid, internaldate)
            print(
                f"    {uid:>6}  {_fmt_date(rec['date']):17}  "
                f"{_clip(rec['from'], 38):38}  {_clip(rec['subject'], 60)}"
            )
        return
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    stored = 0
    skipped = 0
    to_store: list[tuple[int, str | None]] = []
    for uid, header, internaldate in client.fetch_headers(uids):
        rec = parse.envelope(header, uid, internaldate)
        if store.stored(folder, uid, rec["message_id"], uid_reliable=not resync):
            skipped += 1
            continue
        to_store.append((uid, internaldate))
    raws = client.fetch_raws([uid for uid, _ in to_store])  # one round trip for all bodies
    for uid, internaldate in to_store:
        raw = raws.get(uid)
        if raw is None:
            print(f"    {uid}: no message data returned; skipped")
            continue
        rec = parse.envelope(raw, uid, internaldate)  # re-read from the full message
        rec["account"] = account_name
        rec["folder"] = folder
        rec["fetched_at"] = now
        warn = store.save_message(folder, uid, rec, raw, uidvalidity)
        if warn:
            print(f"    {uid}: {warn}")
        else:
            stored += 1
    print(f"  {folder}: stored {stored} new, {skipped} already present")
    hist, corrupt = store.day_counts(folder)
    if hist:
        total = sum(c for _, c in hist)
        print(f"  {folder}: {total} messages stored over {len(hist)} distinct UTC days")
        for day, count in hist:
            print(f"    {day}  {count}")
    if corrupt:
        print(f"  WARNING: {corrupt} unreadable line(s) in messages.jsonl")


def _fetch_account(account, args) -> bool:
    print(f"Account: {account.name}")
    client = imapclient.ImapClient(account)
    try:
        client.connect()
        client.login(auth.for_account(account))
    except (auth.AuthError, imapclient.ImapError) as e:
        print(f"  CONNECT/LOGIN FAILED - {e}")
        return False
    store = store_mod.Store(account, cfg.data_dir())
    ok = True
    try:
        folders = [args.folder] if args.folder else list(account.folders)
        for folder in folders:
            try:
                _fetch_folder(client, store, account.name, folder, args)
            except (imapclient.ImapError, store_mod.StoreError) as e:
                print(f"  {folder}: FAILED - {e}")
                ok = False
    finally:
        client.disconnect()
    return ok


def cmd_fetch(args) -> int:
    conf = cfg.load()
    ok = True
    for account in conf.accounts:
        if args.account and account.name != args.account:
            continue
        if not _fetch_account(account, args):
            ok = False
    return 0 if ok else 1


def cmd_collect(args) -> int:
    """Run the whole collector pipeline: fetch, parse, classify. Stop on failure.

    This is what schtasks and the /brief skill invoke: one command that brings
    the store up to date before a packet is rendered. Read-only throughout.
    """
    ok = True
    # Each stage expects its own parser's flags; build minimal namespaces so
    # collect's small parser can drive them unchanged. Stages return 0 on
    # success, so test `!= 0`, never truthiness.
    if cmd_fetch(
        argparse.Namespace(account=args.account, folder=None, limit=None, dry_run=False)
    ) != 0:
        ok = False
    if ok and cmd_parse(argparse.Namespace(account=args.account)) != 0:
        ok = False
    if ok and cmd_classify(argparse.Namespace(account=args.account, explain=False)) != 0:
        ok = False
    return 0 if ok else 1


def cmd_parse(args) -> int:
    """Stage 3: clean every stored body that is not parsed yet. Idempotent."""
    conf = cfg.load()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    ok = True
    for account in conf.accounts:
        if args.account and account.name != args.account:
            continue
        store = store_mod.Store(account, cfg.data_dir())
        recs, corrupt = store.records()
        todo = [r for r in recs if not r.get("parsed_at")]
        print(f"Account {account.name}: {len(todo)} unparsed of {len(recs)} records")
        for rec in todo:
            uid = rec["uid"]
            folder = rec["folder"]
            raw_file = store.root / "raw" / f"{uid}.txt"
            body_file = store.root / rec.get("file", f"bodies/{uid}.txt")
            # raw/ wins when present: a parse that crashed between raw and
            # record must not clean an already-cleaned body.
            src = raw_file if raw_file.exists() else body_file
            src_is_raw = src is raw_file
            try:
                raw = src.read_bytes()
            except OSError as e:
                print(f"  {uid}: cannot read {src} - {e}. Fix: refetch the message.")
                ok = False
                continue
            d = parse.decode_body(raw)
            fields = {
                "parsed_at": now,
                "charset": d["charset"],
                "declared_charset": d["declared_charset"],
                "body_html": d["html"],
                "body_chars": len(d["text"]),
                "trimmed": d["trimmed"],
                "raw_file": f"raw/{uid}.txt",
            }
            try:
                store.save_parsed(folder, uid, d["text"], raw, fields)
            except store_mod.StoreError as e:
                print(f"  {uid}: {e}")
                ok = False
                continue
            source = "raw/" if src_is_raw else "bodies/ (raw)"
            print(
                f"  {uid}: charset {d['charset'] or '-'} "
                f"(declared {d['declared_charset'] or '-'}), html={d['html']}, "
                f"body {len(d['text'])} chars, trimmed {d['trimmed']} "
                f"(parsed from {source})"
            )
        if corrupt:
            print(f"  WARNING: {corrupt} unreadable line(s) in messages.jsonl")
    return 0 if ok else 1


def cmd_show(args) -> int:
    """Print one stored message: headers, charset evidence, cleaned body."""
    conf = cfg.load()
    uid = args.uid
    found = False
    for account in conf.accounts:
        store = store_mod.Store(account, cfg.data_dir())
        recs, _corrupt = store.records()
        for rec in recs:
            if rec.get("uid") != uid:
                continue
            found = True
            print(f"uid {uid}  (account {account.name}, folder {rec.get('folder', '?')})")
            print(f"date:      {rec.get('date') or '?'}")
            print(f"from:      {rec.get('from') or '?'}")
            print(f"subject:   {rec.get('subject') or '(no subject)'}")
            print(f"message-id: {rec.get('message_id') or '?'}")
            parsed_at = rec.get("parsed_at")
            if parsed_at:
                print(
                    f"parsed:    {parsed_at[:19]} - charset {rec.get('charset') or '-'} "
                    f"(declared {rec.get('declared_charset') or '-'}), "
                    f"html source {bool(rec.get('body_html'))}, "
                    f"body {rec.get('body_chars', '?')} chars, "
                    f"trimmed {rec.get('trimmed', 0)} chars"
                )
                print(f"raw:       {rec.get('raw_file') or '?'}")
            else:
                print("parsed:    NOT PARSED - run: python -m mailbrief parse")
            if rec.get("label"):
                print(
                    f"label:     {rec['label']} "
                    f"(evidence: {rec.get('label_evidence') or '?'})"
                )
            body_path = store.root / rec.get("file", f"bodies/{uid}.txt")
            if not body_path.exists():
                print(f"body:      MISSING FILE {body_path}")
                print("           Fix: run 'python -m mailbrief parse' (or refetch).")
                break
            text = body_path.read_text(encoding="utf-8", errors="replace")
            print(f"body ({len(text)} chars):")
            print(text[:8000])
            if len(text) > 8000:
                print(f"... ({len(text)} chars total, showing the first 8000)")
            break
    if not found:
        print(
            f"no message with uid {uid} in any account. Fix: run "
            "'python -m mailbrief fetch --dry-run' or read the messages.jsonl."
        )
        return 1
    return 0


def cmd_classify(args) -> int:
    """Stage 4: label every stored message bulk/direct/cc, keeping the evidence.

    Labels are hints for the briefing, never verdicts. The triggering header
    travels with each label, so a wrong call is visible here, not buried.
    Idempotent: only records whose label changed are rewritten.
    """
    conf = cfg.load()
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    ok = True
    for account in conf.accounts:
        if args.account and account.name != args.account:
            continue
        store = store_mod.Store(account, cfg.data_dir())
        recs, corrupt = store.records()
        counts = {"bulk": 0, "direct": 0, "cc": 0}
        changed = 0
        print(f"Account {account.name}: {len(recs)} messages")
        for rec in recs:
            uid = rec.get("uid")
            folder = rec.get("folder", "?")
            raw_path = store.root / "raw" / f"{uid}.txt"
            if not raw_path.exists():
                print(f"  {uid}: no raw/ file - run 'python -m mailbrief parse' first")
                ok = False
                continue
            try:
                raw = raw_path.read_bytes()
                msg = email.message_from_bytes(raw, policy=policy.default)
                r = classify.classify(msg, conf)
            except Exception as e:
                print(f"  {uid}: classify FAILED - {e}")
                ok = False
                continue
            counts[r["label"]] += 1
            if r["label"] != rec.get("label") or r["evidence"] != rec.get("label_evidence"):
                try:
                    store.save_classified(
                        folder,
                        uid,
                        {
                            "label": r["label"],
                            "label_evidence": r["evidence"],
                            "classified_at": now,
                        },
                    )
                except store_mod.StoreError as e:
                    print(f"  {uid}: {e}")
                    ok = False
                    continue
                changed += 1
            if args.explain:
                print(
                    f"  {uid:>6}  {r['label']:<6}  {_clip(rec.get('subject', '?'), 48):48}  "
                    f"{r['evidence']}"
                )
        print(
            f"  labels: {counts['bulk']} bulk, {counts['direct']} direct, "
            f"{counts['cc']} cc ({changed} records updated)"
        )
        if corrupt:
            print(f"  WARNING: {corrupt} unreadable line(s) in messages.jsonl")
    return 0 if ok else 1


def cmd_whoami(args) -> int:
    """Address discovery: rank addresses that actually deliver to this mailbox.

    Scans delivery headers (To, Cc, Delivered-To, X-Delivered-To,
    X-Envelope-To, X-Original-To) across every stored raw message. The user
    confirms the ones that are really theirs into config as `addresses`.
    """
    conf = cfg.load()
    counts: dict[str, int] = {}
    scanned = 0
    for account in conf.accounts:
        store = store_mod.Store(account, cfg.data_dir())
        raw_dir = store.root / "raw"
        if not raw_dir.exists():
            continue
        for raw_path in sorted(raw_dir.glob("*.txt")):
            try:
                raw = raw_path.read_bytes()
            except OSError:
                continue
            scanned += 1
            for addr in classify.delivery_addresses(raw):
                counts[addr] = counts.get(addr, 0) + 1
    if not counts:
        print(
            "no delivery headers found yet. Fix: run 'python -m mailbrief fetch' "
            "then 'python -m mailbrief parse'."
        )
        return 1
    print(f"addresses seen in delivery headers, across {scanned} raw messages:")
    for addr, n in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])):
        print(f"  {n:>5}  {addr}")
    print("Confirm the ones that are really yours, then add to config.toml:")
    print('  addresses = ["you@example.com", ...]')
    return 0


def cmd_threads(args) -> int:
    """Stage: thread assembly. Print every thread with its messages.

    Threads assemble over ALL stored messages (never a window), because the
    Sent side of a thread can predate the current briefing window. The
    packet stage filters afterwards.
    """
    conf = cfg.load()
    ok = True
    for account in conf.accounts:
        if args.account and account.name != args.account:
            continue
        store = store_mod.Store(account, cfg.data_dir())
        recs, corrupt = store.records()
        raw_dir = store.root / "raw"

        def raw_for(uid, raw_dir=raw_dir):
            path = raw_dir / f"{uid}.txt"
            try:
                return path.read_bytes() if path.exists() else None
            except OSError:
                return None

        ts = threads.assemble(recs, raw_for, my_addresses=conf.addresses)
        print(f"Account {account.name}: {len(ts)} threads over {len(recs)} messages")
        for i, t in enumerate(ts, start=1):
            state = "awaiting my reply" if t.awaiting_my_reply else "no action needed"
            if t.i_replied_at:
                state += f" (you replied {_fmt_date(t.i_replied_at)})"
            subject = t.messages[-1].get("subject") or "?"
            print(
                f"  {i:>3}. [{t.key}] {_clip(subject, 60):60} "
                f"{len(t.messages)} msgs - {state}"
            )
            for m in t.messages:
                sent = "  [sent]" if m["sent"] else ""
                print(
                    f"       {m.get('uid'):>6}  {_fmt_date(m.get('date')):17}  "
                    f"{_clip(m.get('from') or '?', 30):30}  "
                    f"{_clip(m.get('subject') or '?', 50)}{sent}"
                )
        if corrupt:
            print(f"  WARNING: {corrupt} unreadable line(s) in messages.jsonl")
    return 0 if ok else 1


def cmd_packet(args) -> int:
    """Stage: briefing packet. One deterministic markdown file per window.

    --since is local wall time; it is converted to UTC. Without --since, the
    previous packet run's cutoff is used, so "overnight" means "since the
    previous evening run". First run with no cutoff includes everything.
    """
    conf = cfg.load()
    if args.account:
        conf.account(args.account)  # raises a fix-naming ConfigError if unknown
    since_utc = None
    if args.no_cutoff:
        since_utc = None  # overrides --since too, per the help text
    elif args.since:
        try:
            dt = datetime.datetime.fromisoformat(args.since)
        except ValueError:
            print(
                f"--since must be ISO datetime like '2026-09-01T18:00' "
                f"(local time), got {args.since!r}"
            )
            return 1
        since_utc = dt.astimezone(datetime.timezone.utc).isoformat()
    else:
        since_utc = packet_mod.default_since(cfg.data_dir())
        if since_utc is None:
            print(
                "note: no previous packet cutoff found (first run?); "
                "including everything. Next run starts from this one."
            )

    stores = [
        store_mod.Store(a, cfg.data_dir())
        for a in conf.accounts
        if not args.account or a.name == args.account
    ]
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    data = packet_mod.build_packet(conf, since_utc, args.window, stores, now=now)
    text = packet_mod.render(data, conf.packet_max_chars)
    if args.out:
        out = pathlib.Path(args.out)
    else:
        local_now = datetime.datetime.now().astimezone()
        out = cfg.data_dir() / "packets" / f"{local_now:%Y-%m-%d}-{args.window}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(out)
    packet_mod.save_cutoff(cfg.data_dir(), now)

    trims_line = next(
        (l for l in text.splitlines() if l.startswith("Trims: ")), "Trims: ?"
    )
    bulk_total = sum(r["count"] for r in data["bulk_rows"])
    counts = data["new_counts"]
    print(f"packet written to {out}")
    print(
        f"  {counts['total']} new messages ({counts['direct']} direct, "
        f"{counts['cc']} cc, {counts['bulk']} bulk, "
        f"{counts['unclassified']} unclassified), {data['thread_count']} threads, "
        f"{bulk_total} bulk in table"
    )
    print(f"  {len(text)} chars, budget {conf.packet_max_chars} - {trims_line}")
    return 0


def cmd_telegram(args) -> int:
    """One-shot Telegram poll (docs/plan-telegram.md). schtasks runs this.

    getUpdates -> dispatch commands from allowed chats -> spawn claude -p
    with the /brief skill -> send the briefing back in chunks -> exit.
    """
    conf = cfg.load()
    if conf.briefing_dir is None:
        raise telegram.TelegramError(
            "config has no briefing_dir. Fix: add 'briefing_dir = "
            '"C:/path/to/briefings"\' above [[accounts]] in config.toml '
            "(the folder where briefing files are written)."
        )
    token = telegram.token_from_keyring()
    result = telegram.run_poll(
        token=token,
        chat_ids=frozenset(conf.telegram_chat_ids),
        data_dir=cfg.data_dir(),
        briefing_dir=conf.briefing_dir,
        transport=telegram.http_transport,
    )
    print(
        f"updates {result.updates}, commands {result.commands}, "
        f"briefings sent {result.briefings}"
    )
    if not conf.telegram_chat_ids:
        print(
            "WARNING: config has no telegram_chat_ids, so messages are ignored. "
            "Fix: run 'python -m mailbrief telegram-whoami', then add the ids "
            "to telegram_chat_ids ABOVE [[accounts]] in config.toml."
        )
    return 0


def cmd_telegram_whoami(args) -> int:
    """Allowlist discovery: list the chats that have messaged the bot."""
    token = telegram.token_from_keyring()
    chats = telegram.whoami(token, transport=telegram.http_transport)
    if not chats:
        print(
            "no messages seen yet. Fix: send any message to your bot in "
            "Telegram, wait a few seconds, then run this again."
        )
        return 1
    print("chats that have messaged the bot:")
    for c in chats:
        label = c["first_name"] or c["username"] or "?"
        print(f"  id={c['id']}  ({label})")
    print("Add your own chat id to config.toml ABOVE [[accounts]]:")
    print(f"  telegram_chat_ids = [{', '.join(repr(c['id']) for c in chats)}]")
    return 0


def cmd_store_telegram_token(args) -> int:
    telegram.store_token(file_path=args.file)
    return 0


def _open_path(path: pathlib.Path) -> None:
    """Open a file or folder in its default Windows app (Explorer, Notepad)."""
    if hasattr(os, "startfile"):
        os.startfile(str(path))


def cmd_brief(args) -> int:
    """One-command briefing: collect + packet, plus Claude prose if available.

    If Claude Code is installed and `briefing_dir` is set, run the /brief skill
    headless and open the written briefing. Otherwise fall back to the
    deterministic packet and open that folder.
    """
    conf = cfg.load()
    data_dir = cfg.data_dir()
    window = args.window

    claude_exe = shutil.which("claude") or shutil.which("claude.cmd")
    if conf.briefing_dir is not None and claude_exe is not None:
        print(f"Writing your {window} briefing (this can take a minute or two)...")
        rc = telegram.run_claude(window, data_dir, conf.briefing_dir)
        path = telegram.briefing_file(conf.briefing_dir, window)
        if rc == 0 and path.exists() and path.stat().st_size > 0:
            print(f"Briefing ready: {path}")
            _open_path(path)
            return 0
        print(
            f"The written briefing did not finish (claude exited {rc}). "
            f"See {data_dir / 'logs' / 'telegram-claude.out.txt'}."
        )
        print("Using the plain digest instead...")

    # Fallback: deterministic packet (also the path when Claude is not installed).
    if cmd_collect(argparse.Namespace(account=None)) != 0:
        return 1
    if cmd_packet(
        argparse.Namespace(account=None, window=window, since=None,
                           no_cutoff=False, out=None)
    ) != 0:
        return 1
    packets_dir = data_dir / "packets"
    print(f"Plain digest ready: {packets_dir}")
    _open_path(packets_dir)
    return 0


NOT_BUILT = {
    "reindex": "a later stage",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m mailbrief",
        description="Read-only email collector for briefings. See docs/plan.md.",
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("config", help="print the real config location")
    p = sub.add_parser(
        "store-credential",
        help="store the mailbox password in Windows Credential Manager (hidden input)",
    )
    p.add_argument("account", nargs="?", help="account name from config (default: first)")
    p.add_argument(
        "--file",
        help="read the password from this text file and delete the file (fallback "
        "for consoles where hidden input hangs)",
    )
    sub.add_parser(
        "check", help="connect to every account: TLS, certificate, capabilities, folder counts"
    )
    p = sub.add_parser(
        "fetch", help="fetch new mail from every configured account (EXAMINE, read-only)"
    )
    p.add_argument("--account", help="only this account (default: all)")
    p.add_argument("--folder", help="only this configured folder (default: all)")
    p.add_argument("--limit", type=int, help="at most this many new messages per folder")
    p.add_argument("--dry-run", action="store_true", help="print what would be stored, write nothing")
    p = sub.add_parser(
        "collect", help="run the full collector: fetch, parse, classify (read-only)"
    )
    p.add_argument("--account", help="only this account (default: all)")
    p = sub.add_parser(
        "parse", help="clean stored message bodies (charsets, HTML, quote stripping; idempotent)"
    )
    p.add_argument("--account", help="only this account (default: all)")
    p = sub.add_parser(
        "show", help="show one stored message: headers, detected charset, cleaned body, trimmed count"
    )
    p.add_argument("uid", type=int, help="message uid (see messages.jsonl or fetch --dry-run)")
    p = sub.add_parser(
        "classify", help="label every stored message bulk/direct/cc (hint for the briefing, not a verdict)"
    )
    p.add_argument("--account", help="only this account (default: all)")
    p.add_argument(
        "--explain",
        action="store_true",
        help="print every message with the header that triggered its label",
    )
    sub.add_parser(
        "whoami",
        help="rank the addresses that actually deliver to this mailbox (address discovery)",
    )
    p = sub.add_parser(
        "threads", help="show every thread over all stored messages (thread assembly debug view)"
    )
    p.add_argument("--account", help="only this account (default: all)")
    p = sub.add_parser(
        "packet", help="render the briefing packet for the window (deterministic, no AI)"
    )
    p.add_argument(
        "--window", choices=("morning", "eod"), default="morning",
        help="briefing window; morning is 07:00, eod is 18:00 (default: morning)",
    )
    p.add_argument(
        "--since", help="window start, local wall time ISO, e.g. 2026-09-01T18:00 "
        "(default: previous packet run's cutoff)"
    )
    p.add_argument(
        "--no-cutoff", action="store_true",
        help="ignore the previous cutoff and include everything (overrides --since too)",
    )
    p.add_argument("--account", help="only this account (default: all)")
    p.add_argument(
        "--out", help="write the packet here instead of data_dir/packets/YYYY-MM-DD-<window>.md"
    )
    sub.add_parser(
        "telegram",
        help="poll Telegram once for briefing commands, answer, exit (one-shot)",
    )
    sub.add_parser(
        "telegram-whoami",
        help="list the chat ids that have messaged the bot (allowlist discovery)",
    )
    p = sub.add_parser(
        "store-telegram-token",
        help="store the Telegram bot token in Windows Credential Manager",
    )
    p.add_argument(
        "--file",
        help="read the token from this text file and delete the file (fallback "
        "for consoles where hidden input hangs)",
    )
    p = sub.add_parser(
        "brief", help="one-command briefing (collect + packet, plus Claude prose if available)"
    )
    p.add_argument(
        "--window", choices=("morning", "eod"), default="morning",
        help="which briefing (default: morning)",
    )
    for name, stage in NOT_BUILT.items():
        p = sub.add_parser(name, help=f"(not built yet - {stage})")
        p.add_argument("args", nargs="*")

    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # Greek subjects must not mangle
    _setup_logging()

    try:
        if args.command == "config":
            return cmd_config(args)
        if args.command == "store-credential":
            return cmd_store_credential(args)
        if args.command == "check":
            return cmd_check(args)
        if args.command == "fetch":
            return cmd_fetch(args)
        if args.command == "collect":
            return cmd_collect(args)
        if args.command == "parse":
            return cmd_parse(args)
        if args.command == "show":
            return cmd_show(args)
        if args.command == "classify":
            return cmd_classify(args)
        if args.command == "whoami":
            return cmd_whoami(args)
        if args.command == "threads":
            return cmd_threads(args)
        if args.command == "packet":
            return cmd_packet(args)
        if args.command == "telegram":
            return cmd_telegram(args)
        if args.command == "telegram-whoami":
            return cmd_telegram_whoami(args)
        if args.command == "store-telegram-token":
            return cmd_store_telegram_token(args)
        if args.command == "brief":
            return cmd_brief(args)
        print(f"{args.command}: not built yet ({NOT_BUILT[args.command]}). See docs/plan.md.")
        return 1
    except (cfg.ConfigError, auth.AuthError, imapclient.ImapError,
            telegram.TelegramError) as e:
        print(f"Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
