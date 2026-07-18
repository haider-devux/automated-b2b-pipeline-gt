"""
Bounce handler / dead-email extraction (Max Plan · Phase 6).

Scans the Gmail inbox over IMAP for bounce notifications (Mailer-Daemon / Delivery Status Notifications),
extracts the undeliverable recipient address, and marks that lead BOUNCED + suppressed so we never mail
a dead address again. 100% free — Gmail IMAP + Python's stdlib imaplib/email, no paid validation service.

  python bounce_parser.py                    # scan the inbox (needs GMAIL_ADDRESS + GMAIL_APP_PASSWORD)
  python bounce_parser.py --days 14          # only look at the last 14 days
  python bounce_parser.py --simulate x@y.com # inject a fake bounce for x@y.com (test; no IMAP)

Read-only against the mailbox (never deletes/sends); it only writes bounce results to the DB.
"""
import argparse
import email
import imaplib
import os
import re
import sys
from datetime import datetime, timedelta, timezone

import db

IMAP_HOST = os.getenv("IMAP_HOST", "imap.gmail.com")
try:  # load project-root .env so GMAIL_* reach this script when run standalone
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
                override=False)
except Exception:  # noqa: BLE001 — dotenv is optional; real env vars still work
    pass

GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD")

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# DSN headers / phrases that carry the failed recipient
_RECIP_RE = re.compile(r"(?:Final-Recipient|Original-Recipient)\s*:\s*[^;]*;\s*(.+)", re.I)


def _addresses_from_message(msg):
    """Pull candidate failed-recipient addresses out of a bounce email."""
    found = set()
    for part in msg.walk():
        ctype = part.get_content_type()
        if ctype in ("message/delivery-status", "text/plain", "text/rfc822-headers"):
            try:
                payload = part.get_payload(decode=True)
                text = payload.decode("utf-8", "ignore") if payload else ""
            except Exception:              # noqa: BLE001
                text = ""
            for m in _RECIP_RE.findall(text):
                found.update(_EMAIL_RE.findall(m))
            # also grab any address after "to " lines in a plain bounce body
            for line in text.splitlines():
                if " to " in line.lower() and "@" in line:
                    found.update(_EMAIL_RE.findall(line))
    # never suppress our own sending address
    found.discard(GMAIL_ADDRESS.lower())
    return {a.lower() for a in found}


def scan(days=30):
    if not GMAIL_APP_PASSWORD:
        raise SystemExit("Missing GMAIL_APP_PASSWORD (a Google App Password). Set it in .env or the env, "
                         "then re-run. (Reading bounces needs the same credentials as sending.)")
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")
    conn = db.get_connection()
    imap = imaplib.IMAP4_SSL(IMAP_HOST)
    try:
        imap.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        imap.select("INBOX")
        # bounces come from mailer-daemon / postmaster, or carry a DSN subject
        seen, dead, matched = set(), set(), 0
        for crit in (f'(SINCE {since} FROM "mailer-daemon")',
                     f'(SINCE {since} FROM "postmaster")',
                     f'(SINCE {since} SUBJECT "Delivery Status Notification")',
                     f'(SINCE {since} SUBJECT "Undelivered")'):
            typ, data = imap.search(None, crit)
            if typ != "OK":
                continue
            for num in data[0].split():
                if num in seen:
                    continue
                seen.add(num)
                typ, msgdata = imap.fetch(num, "(RFC822)")
                if typ != "OK" or not msgdata or not msgdata[0]:
                    continue
                msg = email.message_from_bytes(msgdata[0][1])
                for addr in _addresses_from_message(msg):
                    dead.add(addr)
        for addr in sorted(dead):
            if db.mark_bounced(conn, addr, "imap-dsn"):
                matched += 1
                print(f"  BOUNCED  {addr}  -> suppressed")
            else:
                print(f"  (bounce for {addr} — no matching lead)")
        print(f"\nScanned {len(seen)} bounce message(s); {len(dead)} dead address(es), {matched} matched a lead.")
    finally:
        try:
            imap.logout()
        except Exception:                  # noqa: BLE001
            pass
        conn.close()


def main():
    ap = argparse.ArgumentParser(description="Gmail IMAP bounce parser / dead-email extraction")
    ap.add_argument("--days", type=int, default=30, help="How many days back to scan (default 30).")
    ap.add_argument("--simulate", metavar="EMAIL", help="Inject a fake bounce for EMAIL (test, no IMAP).")
    args = ap.parse_args()
    if args.simulate:
        conn = db.get_connection()
        try:
            ok = db.mark_bounced(conn, args.simulate.lower(), "simulated")
            print(f"Simulated bounce for {args.simulate}: {'lead suppressed' if ok else 'no matching lead'}")
        finally:
            conn.close()
        return
    scan(days=args.days)


if __name__ == "__main__":
    main()
