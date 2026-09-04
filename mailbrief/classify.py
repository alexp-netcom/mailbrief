"""Message classification: bulk / direct / cc.

Rules from docs/plan.md section 7, evaluated in order - first match wins:

  bulk   any automation signal (list headers, Precedence, Auto-Submitted,
         X-Auto-Response-Suppress, null Return-Path, an automated sender
         localpart, a configured bulk sender domain) - unless the sender is
         on the never_bulk_senders allowlist
  direct one of the user's configured addresses is in To: AND To+Cc is at
         most direct_max_recipients
  cc     everything else (Cc only, large To, or delivery via a header the
         user's address is not visible in - BCC, alias, catch-all)

The label is a hint for the briefing, never a verdict. Every call returns the
evidence - the header that triggered the label - so a wrong call is visible
in `classify --explain`, not buried.

Matching is always on the address, never the display name, so encoded-word
Greek or Russian display names cannot break it.
"""

from __future__ import annotations

import email
import re
from email import policy
from email.utils import getaddresses

from .config import Config

_DELIVERY_HEADERS = ("To", "Cc", "Delivered-To", "X-Delivered-To", "X-Envelope-To", "X-Original-To")

# Automated-sender localparts, applied to the part before the @.
_AUTOMATED_LOCALPART = re.compile(
    r"no-?reply|donotreply|notifications?|mailer-daemon|postmaster|bounce|alerts?",
    re.IGNORECASE,
)


def _addresses(header: str | None) -> list[str]:
    """Lowercased addresses from one header; display names ignored."""
    if not header:
        return []
    try:
        pairs = getaddresses([header])
    except Exception:
        return []
    return [addr.lower() for _name, addr in pairs if addr]


def sender(msg) -> str | None:
    """Lowercased From address of the message, or None. Never raises."""
    try:
        value = msg.get("From", "")
    except Exception:
        return None
    addrs = _addresses(value)
    return addrs[0] if addrs else None


def classify(msg, conf: Config) -> dict:
    """Classify one parsed message; return {"label", "evidence"}. Never raises.

    msg is an email.message.Message (the full raw message or its headers).
    """
    try:
        my = {a.lower() for a in conf.addresses}
        bulk_domains = {d.lower() for d in conf.bulk_domains}
        never_bulk = {s.lower() for s in conf.never_bulk_senders}
    except Exception:
        my, bulk_domains, never_bulk = set(), set(), set()

    from_addr = sender(msg)

    # -- bulk / automated ---------------------------------------------------
    if from_addr not in never_bulk:
        try:
            list_hits = [h for h in ("List-Id", "List-Unsubscribe", "List-Post") if msg.get(h)]
            if list_hits:
                return {"label": "bulk", "evidence": f"{list_hits[0]} present"}
            precedence = (msg.get("Precedence") or "").strip().lower()
            if precedence in ("bulk", "list", "junk"):
                return {"label": "bulk", "evidence": f"Precedence: {precedence}"}
            auto = msg.get("Auto-Submitted")
            if auto is not None and auto.strip().lower() != "no":
                return {"label": "bulk", "evidence": f"Auto-Submitted: {auto.strip()}"}
            if msg.get("X-Auto-Response-Suppress"):
                return {
                    "label": "bulk",
                    "evidence": f"X-Auto-Response-Suppress: {msg.get('X-Auto-Response-Suppress')}",
                }
            return_path = msg.get("Return-Path")
            if return_path is not None and return_path.strip() in ("", "<>"):
                return {"label": "bulk", "evidence": "Return-Path: <>"}
            if from_addr:
                local, _, _ = from_addr.partition("@")
                if _AUTOMATED_LOCALPART.search(local):
                    return {"label": "bulk", "evidence": f"automated sender localpart: {local}@"}
                domain = from_addr.split("@", 1)[-1]
                if domain in bulk_domains:
                    return {"label": "bulk", "evidence": f"sender domain in bulk_domains: {domain}"}
        except Exception:
            pass  # a broken header degrades to direct/cc, never a crash

    # -- direct -------------------------------------------------------------
    try:
        to_addrs = _addresses(msg.get("To"))
        cc_addrs = _addresses(msg.get("Cc"))
    except Exception:
        to_addrs, cc_addrs = [], []
    matched = next((a for a in my if a in to_addrs), None)
    if matched:
        total = len(to_addrs) + len(cc_addrs)
        if total <= conf.direct_max_recipients:
            return {
                "label": "direct",
                "evidence": f"{matched} in To, To+Cc {total} <= {conf.direct_max_recipients}",
            }
        return {
            "label": "cc",
            "evidence": f"{matched} in To but To+Cc {total} > {conf.direct_max_recipients}",
        }

    # -- cc / fyi -----------------------------------------------------------
    for header in _DELIVERY_HEADERS:
        try:
            value = msg.get(header)
        except Exception:
            value = None
        if not value:
            continue
        for addr in _addresses(value):
            if addr in my:
                if header == "Cc":
                    return {"label": "cc", "evidence": f"{addr} in Cc only"}
                return {"label": "cc", "evidence": f"{addr} in {header} (not in To/Cc)"}
    return {
        "label": "cc",
        "evidence": "address not in any visible header (BCC, alias or catch-all delivery)",
    }


def delivery_addresses(raw: bytes) -> list[str]:
    """Lowercased addresses from every delivery header of a raw message.
    Used by `whoami` to discover which addresses actually deliver here."""
    try:
        msg = email.message_from_bytes(raw, policy=policy.default)
    except Exception:
        return []
    seen: set[str] = set()
    out = []
    for header in _DELIVERY_HEADERS:
        try:
            value = msg.get(header)
        except Exception:
            value = None
        if not value:
            continue
        for addr in _addresses(value):
            if addr not in seen:
                seen.add(addr)
                out.append(addr)
    return out
