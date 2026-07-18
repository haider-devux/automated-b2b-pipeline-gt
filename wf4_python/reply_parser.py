r"""
Reply detector — closes the outreach loop for a free Gmail (Feature A · "Close the loop").

Scans the Gmail inbox over IMAP for inbound mail from leads we've CONTACTED and classifies each:
  * real reply            -> mark REPLIED (+ sentiment) — this STOPS the follow-up drip
  * out-of-office / auto   -> logged, NOT counted as a reply — the drip keeps going (reschedule)
  * unsubscribe request    -> suppressed (never emailed again)

Read-only against the mailbox (never deletes/sends); it only writes results to the DB. Sentiment of a
real reply is scored by the local Ollama model when available, with a keyword fallback so the critical
out-of-office / unsubscribe detection never depends on the LLM.

  python reply_parser.py                     # scan the last 30 days (needs GMAIL_ADDRESS + GMAIL_APP_PASSWORD)
  python reply_parser.py --days 7
  python reply_parser.py --dry-run           # detect + classify + print, but change nothing
  python reply_parser.py --simulate a@b.com --body "Sounds great, let's talk"   # test, no IMAP
"""
import argparse
import email
import email.utils
import imaplib
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

from psycopg2.extras import Json, RealDictCursor
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
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434/api/generate")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")

# out-of-office / auto-responder signals (headers are the reliable ones; text is the backstop)
_OOO_HEADERS = ("auto-submitted", "x-autoreply", "x-autorespond", "x-autoresponder")
_OOO_TEXT = (
    "out of office", "out-of-office", "outofoffice", "on vacation", "on holiday", "on annual leave",
    "on leave", "away until", "away from my desk", "automatic reply", "auto-reply", "autoreply",
    "auto response", "currently unavailable", "maternity leave", "parental leave", "i am away",
    "i'm away", "back in the office", "no longer with", "has left the company", "off until",
)
_UNSUB_TEXT = ("unsubscribe", "remove me", "take me off", "stop emailing", "do not contact",
               "opt out", "opt-out", "please remove", "no more emails")


# --------------------------------------------------------------------------- message parsing
def _plain_body(msg):
    """Best-effort plain-text body of an email (prefers text/plain), first-message content only."""
    text = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                try:
                    payload = part.get_payload(decode=True)
                    text = payload.decode(part.get_content_charset() or "utf-8", "ignore") if payload else ""
                    break
                except Exception:              # noqa: BLE001
                    continue
    else:
        try:
            payload = msg.get_payload(decode=True)
            text = payload.decode(msg.get_content_charset() or "utf-8", "ignore") if payload else ""
        except Exception:                      # noqa: BLE001
            text = ""
    # strip the quoted original ("On ... wrote:" + '>' lines) so we classify only the NEW reply
    lines = []
    for line in text.splitlines():
        low = line.strip().lower()
        if low.startswith(">") or re.match(r"on .+wrote:$", low) or low.startswith("-----original"):
            break
        lines.append(line)
    return "\n".join(lines).strip()


def _from_address(msg):
    _, addr = email.utils.parseaddr(msg.get("From", ""))
    return (addr or "").strip().lower()


def _is_auto(msg, subject, body):
    for h in _OOO_HEADERS:
        if msg.get(h):
            return True
    if (msg.get("Precedence", "").lower() in ("auto_reply", "bulk", "junk")):
        return True
    blob = f"{subject}\n{body}".lower()
    return any(k in blob for k in _OOO_TEXT)


def _is_unsub(subject, body):
    blob = f"{subject}\n{body}".lower()
    return any(k in blob for k in _UNSUB_TEXT)


# --------------------------------------------------------------------------- sentiment (optional LLM)
def _sentiment(text):
    """positive | neutral | negative for a REAL reply. Ollama if reachable, else a keyword fallback."""
    snippet = (text or "").strip()[:600]
    prompt = ("Classify the sentiment of this reply to a cold sales email as exactly one word: "
              "positive, neutral, or negative. Positive = interested/wants to talk. "
              "Negative = not interested/annoyed. Reply:\n\n" + snippet + "\n\nOne word:")
    try:
        req = urllib.request.Request(
            OLLAMA_URL,
            data=json.dumps({"model": OLLAMA_MODEL, "prompt": prompt, "stream": False,
                             "options": {"temperature": 0}}).encode(),
            headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=30) as r:
            out = json.loads(r.read()).get("response", "").strip().lower()
        for s in ("positive", "negative", "neutral"):
            if s in out:
                return s
    except Exception:                          # noqa: BLE001 — LLM down -> keyword fallback
        pass
    low = (text or "").lower()
    if any(k in low for k in ("not interested", "no thanks", "no thank you", "remove", "stop", "not a fit")):
        return "negative"
    if any(k in low for k in ("interested", "sounds good", "let's talk", "lets talk", "sure", "yes",
                              "happy to", "book", "call me", "great")):
        return "positive"
    return "neutral"


# --------------------------------------------------------------------------- DB helpers
def _contacted_map(conn):
    """{email(lower): lead_id} for leads still CONTACTED (only these can newly 'reply')."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT id, lower(email::text) AS email FROM leads "
                    "WHERE status='CONTACTED' AND email IS NOT NULL")
        return {r["email"]: r["id"] for r in cur.fetchall()}


def _already_logged(conn, lead_id, msgid):
    if not msgid:
        return False
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM email_events WHERE lead_id=%s AND event_type='reply' "
                    "AND detail->>'msgid'=%s LIMIT 1", (lead_id, msgid))
        return cur.fetchone() is not None


def _log_reply(conn, lead_id, kind, sentiment, subject, snippet, msgid):
    with conn.cursor() as cur:
        cur.execute("INSERT INTO email_events (lead_id, event_type, detail) VALUES (%s,'reply',%s)",
                    (lead_id, Json({"kind": kind, "sentiment": sentiment, "subject": subject[:200],
                                    "snippet": (snippet or "")[:600], "msgid": msgid})))
    conn.commit()


def _act(conn, lead_id, kind, sentiment, subject, snippet, msgid, dry):
    """Apply the classification. Returns a short verb for the console."""
    if dry:
        return f"would-{kind}"
    if _already_logged(conn, lead_id, msgid):
        return "seen"
    _log_reply(conn, lead_id, kind, sentiment, subject, snippet, msgid)
    if kind == "unsubscribe":
        db.suppress(conn, lead_id, "reply-optout")          # -> SUPPRESSED + suppression_list
        return "unsubscribed"
    if kind == "auto":
        return "ooo (drip continues)"                        # do NOT mark REPLIED — reschedule
    db.mark_replied(conn, lead_id, sentiment)                # real reply -> REPLIED, stops the drip
    return f"REPLIED/{sentiment}"


def classify(msg):
    """(kind, sentiment, subject, snippet). kind in real|auto|unsubscribe."""
    subject = str(email.header.make_header(email.header.decode_header(msg.get("Subject", ""))))
    body = _plain_body(msg)
    if _is_unsub(subject, body):
        return "unsubscribe", "negative", subject, body
    if _is_auto(msg, subject, body):
        return "auto", "neutral", subject, body
    return "real", _sentiment(body or subject), subject, body


# --------------------------------------------------------------------------- scan
def scan(days=30, dry=False):
    if not GMAIL_APP_PASSWORD:
        raise SystemExit("Missing GMAIL_APP_PASSWORD (a Google App Password). Set it in .env or the env.")
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%d-%b-%Y")
    conn = db.get_connection()
    imap = imaplib.IMAP4_SSL(IMAP_HOST)
    real = ooo = unsub = 0
    try:
        contacted = _contacted_map(conn)
        if not contacted:
            print("No CONTACTED leads to match replies against.")
            return
        imap.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        imap.select("INBOX", readonly=True)
        typ, data = imap.search(None, f'(SINCE {since})')
        nums = data[0].split() if typ == "OK" else []
        for num in nums:
            typ, msgdata = imap.fetch(num, "(RFC822)")
            if typ != "OK" or not msgdata or not msgdata[0]:
                continue
            msg = email.message_from_bytes(msgdata[0][1])
            sender = _from_address(msg)
            lead_id = contacted.get(sender)
            if not lead_id:                     # not from a contacted lead -> ignore
                continue
            kind, sentiment, subject, body = classify(msg)
            msgid = (msg.get("Message-ID") or "").strip()
            verb = _act(conn, lead_id, kind, sentiment, subject, body, msgid, dry)
            if kind == "real":
                real += 1
            elif kind == "auto":
                ooo += 1
            else:
                unsub += 1
            print(f"  {verb:<22} {sender:<34} \"{(subject or '')[:40]}\"")
        print(f"\nScanned {len(nums)} inbox message(s). Replies: {real} real, {ooo} out-of-office, "
              f"{unsub} unsubscribe. {'(dry-run — no DB changes)' if dry else ''}")
    finally:
        try:
            imap.logout()
        except Exception:                       # noqa: BLE001
            pass
        conn.close()


def main():
    ap = argparse.ArgumentParser(description="Gmail IMAP reply detector — closes the outreach loop")
    ap.add_argument("--days", type=int, default=30, help="How many days back to scan (default 30).")
    ap.add_argument("--dry-run", action="store_true", help="Detect + classify + print; change nothing.")
    ap.add_argument("--simulate", metavar="EMAIL", help="Inject a fake reply for EMAIL (test, no IMAP).")
    ap.add_argument("--body", default="Sounds great, when can we talk?", help="Body for --simulate.")
    args = ap.parse_args()
    if args.simulate:
        conn = db.get_connection()
        try:
            lead = _contacted_map(conn).get(args.simulate.lower())
            if not lead:
                print(f"No CONTACTED lead with email {args.simulate}.")
                return
            fake = email.message_from_string(f"Subject: Re: test\n\n{args.body}")
            kind, sentiment, subject, body = classify(fake)
            verb = _act(conn, lead, kind, sentiment, subject, body, f"<sim-{args.simulate}>", args.dry_run)
            print(f"Simulated reply for {args.simulate}: {kind}/{sentiment} -> {verb}")
        finally:
            conn.close()
        return
    scan(days=args.days, dry=args.dry_run)


if __name__ == "__main__":
    main()
