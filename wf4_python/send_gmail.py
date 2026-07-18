"""
Real email sender via Gmail SMTP (SSL). Used by wf4.py when outreach.DRY_RUN=False.

Credentials come from environment variables (NEVER hardcode a password in a file):
  GMAIL_ADDRESS        the full Gmail, e.g. granjur.tech.dev@gmail.com
  GMAIL_APP_PASSWORD   a 16-char Google *App Password* (NOT your normal login password)

Google setup (one time): turn ON 2-Step Verification, then create an App Password at
https://myaccount.google.com/apppasswords . Free Gmail allows ~500 recipients/day.

Self-test (sends ONE email to prove the connection works, DB untouched):
  python send_gmail.py you@example.com
"""
import os
import re
import smtplib
import ssl
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, make_msgid

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465  # implicit SSL


def _creds():
    addr = os.getenv("GMAIL_ADDRESS")
    pw = os.getenv("GMAIL_APP_PASSWORD")
    if not addr or not pw:
        raise SystemExit(
            "Missing Gmail credentials. Set env vars GMAIL_ADDRESS and GMAIL_APP_PASSWORD\n"
            "(a 16-char Google App Password) before a live send. See the top of send_gmail.py.")
    return addr, pw.replace(" ", "")   # Google shows the app password with spaces; strip them


def _attach_logo(root, logo_path):
    """Attach the logo as an inline image referenced by <img src='cid:granjurlogo'>."""
    subtype = "png" if logo_path.lower().endswith(".png") else "jpeg"
    with open(logo_path, "rb") as f:
        img = MIMEImage(f.read(), _subtype=subtype)
    img.add_header("Content-ID", "<granjurlogo>")
    img.add_header("Content-Disposition", "inline", filename=os.path.basename(logo_path))
    root.attach(img)


def send(to_email, subject, body, from_name="Granjur Technologies", unsubscribe=None, reply_to=None,
         html=None, logo_path=None, message_id=None, in_reply_to=None, references=None):
    """Send one email via Gmail. Plain text if `html` is None; otherwise a multipart HTML mail (with the
    plain `body` as fallback) and an optional inline `logo_path`. `message_id`/`in_reply_to`/`references`
    thread this into an existing Gmail conversation (follow-ups). Returns the Message-ID; raises on failure."""
    addr, pw = _creds()
    mid = message_id or make_msgid()
    if html:
        msg = MIMEMultipart("related")
        alt = MIMEMultipart("alternative")
        msg.attach(alt)
        embed_logo = bool(logo_path and os.path.isfile(logo_path))
        send_html = html
        if embed_logo:
            # The HTML embeds the logo as a data: URI (so previews render). Gmail can strip data: images
            # on receipt, so for the real send we swap it for a cid: reference + attach the file inline.
            send_html = re.sub(r'src="data:image/[^"]+"', 'src="cid:granjurlogo"', html, count=1)
        alt.attach(MIMEText(body, "plain", "utf-8"))       # fallback for clients that block HTML
        alt.attach(MIMEText(send_html, "html", "utf-8"))
        if embed_logo:
            _attach_logo(msg, logo_path)
    else:
        msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = formataddr((from_name, addr))
    msg["To"] = to_email
    msg["Reply-To"] = reply_to or addr
    msg["Message-ID"] = mid
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
    if references:
        msg["References"] = references
    if unsubscribe:
        # RFC 8058 header — some clients render a native "Unsubscribe" button; also helps deliverability
        msg["List-Unsubscribe"] = f"<{unsubscribe}>"
    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=ctx) as s:
        s.login(addr, pw)
        s.sendmail(addr, [to_email], msg.as_string())
    return mid


if __name__ == "__main__":
    import sys
    to = sys.argv[1] if len(sys.argv) > 1 else os.getenv("GMAIL_ADDRESS")
    if not to:
        raise SystemExit("Usage: python send_gmail.py <your-email@example.com>")
    _mid = send(to, "Granjur pipeline - test email",
                "This is a live test from the Granjur B2B pipeline.\n\n"
                "If you received this, Gmail sending works. Nothing in the database was changed.")
    print(f"Sent a test email to {to}. Open that inbox to confirm.")
