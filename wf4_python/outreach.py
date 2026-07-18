"""
Outreach payload builder (Instantly/Smartlead shape) + CAN-SPAM/GDPR footer.

Self-contained (no DB, no config import) so the dashboard can import it to preview/queue a lead.
DRY_RUN keeps everything offline: payloads are appended to outbox_dryrun.jsonl, never sent.
"""
import html as _html
import json
import os
import re
from urllib.parse import quote

try:  # load the project-root .env so GRANJUR_* / GMAIL_* settings reach the sender (wf4 has no config)
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
                override=False)
except Exception:  # noqa: BLE001 — dotenv is optional; real env vars still work
    pass

# ---- knobs (edit before any REAL send) ----
# DRY_RUN=True logs the payload and never sends. Flip it WITHOUT editing this file by setting
# the env var GRANJUR_DRY_RUN=0 (any other value / unset keeps the safe dry-run default).
DRY_RUN = os.getenv("GRANJUR_DRY_RUN", "1") != "0"
SEND_METHOD = os.getenv("GRANJUR_SEND_METHOD", "gmail")   # 'gmail' (this build) | 'instantly'/'smartlead' (paid, not wired)
GMAIL_ADDRESS = os.getenv("GMAIL_ADDRESS", "")
PROVIDER = "instantly"               # 'instantly' | 'smartlead' (only used for the paid-provider payload shape)
COMPANY_NAME = "Granjur Technologies"
# CAN-SPAM + GDPR: a REAL physical mailing address is MANDATORY in every cold email footer.
# Set env var GRANJUR_ADDRESS or edit this before any LIVE send (the placeholder is fine for DRY RUN).
COMPANY_ADDRESS = os.getenv(
    "GRANJUR_ADDRESS", "57-S, Model Town Extension, Block S, Model Town, Lahore, Pakistan")

# Our offices — shown in the footer for CAN-SPAM/GDPR compliance AND credibility (global presence).
OFFICES = [
    ("Regional Office", "57-S, Model Town Extension, Block S, Model Town, Lahore, Pakistan"),
    ("UK Office", "15 Osprey Road, Erdington, Birmingham, UK B23 5JQ"),
    ("Canada Office", "2500 Credit Valley Rd, Mississauga, ON L5M 4G8"),
]


def address_is_placeholder():
    return "SET A REAL ADDRESS" in COMPANY_ADDRESS


def _offices_text():
    """The offices as plain text for the text/plain part of the email."""
    return "\n".join(f"{label}: {addr}" for label, addr in OFFICES)
UNSUBSCRIBE_BASE = "http://localhost:5001/webhook/unsubscribe"          # one-click opt-out
# Your Google Calendar "Appointment schedule" booking link (Calendar -> Booking pages). Google shows the
# recipient ONLY your free 15-min slots within your set hours and blocks anything already booked. Paste it
# into the project-root .env as GRANJUR_BOOKING_LINK=... . Empty -> the CTA button is hidden (no dead link).
BOOKING_LINK = os.getenv("GRANJUR_BOOKING_LINK", "").strip()
# Phase-6 telemetry: the always-on dashboard hosts the open-pixel + click-redirect endpoints. For REAL
# tracking this must be a PUBLIC URL the recipient can reach (a domain / tunnel); localhost works for
# local dry-run testing. Free — no paid analytics provider.
TRACK_BASE = os.getenv("GRANJUR_TRACK_BASE", "http://localhost:5000")
PRIVACY_URL = os.getenv("GRANJUR_PRIVACY_URL", "https://granjur.com/privacy")  # GDPR transparency
LIA_REFERENCE = "LIA-2026-B2B"          # legitimate-interest assessment ref (GDPR Art. 6(1)(f))
SENDING_DOMAINS = ["getgranjur.com", "granjurtech.com"]                 # burner domains, never granjur.com

# ---- EMAIL SIGNATURE (edit these, or set the matching env vars) ----
# A professional HTML signature makes the mail look like a real person/company, not a mass blast.
SENDER_NAME = os.getenv("GRANJUR_SENDER_NAME", "Asma Haider")  # blank = company-only signature
SENDER_TITLE = os.getenv("GRANJUR_SENDER_TITLE", "Business Development")
COMPANY_TAGLINE = os.getenv("GRANJUR_TAGLINE", "Custom Software Development · MVPs · Staff Augmentation")
WEBSITE_URL = os.getenv("GRANJUR_WEBSITE", "https://granjur.com")
PHONE = os.getenv("GRANJUR_PHONE", "")                        # e.g. "+92 300 1234567" — blank = hidden
BRAND_COLOR = "#1C9FDA"                                       # Granjur blue (matches the logo)
# Logo file: save your GT logo here (PNG/JPG). If it's missing, a styled text wordmark is used instead.
LOGO_PATH = os.getenv("GRANJUR_LOGO_PATH", os.path.join(os.path.dirname(__file__), "assets", "granjur_logo.png"))
LOGO_CID = "granjurlogo"                                      # inline-image content id referenced by the HTML

OUTBOX = os.path.join(os.path.dirname(__file__), "outbox_dryrun.jsonl")


def logo_path():
    """Absolute path to the logo, searching a few sensible spots so it's found wherever it was dropped:
    the env/assets default, this folder, or the project root (`logo.png`). None -> text wordmark."""
    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for p in (LOGO_PATH,
              os.path.join(os.path.dirname(__file__), "logo.png"),
              os.path.join(_root, "logo.png")):
        if p and os.path.isfile(p):
            return p
    return None


_LOGO_URI_CACHE = None


def logo_data_uri():
    """The logo as a base64 `data:` URI so it renders in ANY preview (browser/dashboard) and is
    self-contained. Cached (read once). None if no logo file is found. NOTE: for a REAL Gmail send,
    send_gmail.py swaps this for a `cid:` inline attachment (Gmail can strip data: images on receipt)."""
    global _LOGO_URI_CACHE
    if _LOGO_URI_CACHE is None:
        path = logo_path()
        if not path:
            _LOGO_URI_CACHE = ""
        else:
            import base64
            with open(path, "rb") as f:
                mime = "png" if path.lower().endswith(".png") else "jpeg"
                _LOGO_URI_CACHE = f"data:image/{mime};base64," + base64.b64encode(f.read()).decode()
    return _LOGO_URI_CACHE or None


def _seg_letter(icp_segment):
    return (icp_segment or "A")[0].upper()   # 'A_LEGACY_BRICK' -> 'A'


def campaign_id(icp_segment, region):
    return f"seg-{_seg_letter(icp_segment)}-{(region or 'xx').lower()}-2026"


def sending_domain(icp_segment):
    idx = (ord(_seg_letter(icp_segment)) - ord("A")) % len(SENDING_DOMAINS)
    return SENDING_DOMAINS[idx]


_URL_RE = re.compile(r"(https?://[^\s<]+)")


def _linkify(escaped_text):
    """Turn any http(s) URL in ALREADY-ESCAPED text into a clickable anchor (e.g. the Google
    mobile-speed report link the pitch adds). Our URLs contain no &/<>/quote chars, so they
    survive html.escape untouched and can be used directly as the href."""
    return _URL_RE.sub(
        r'<a href="\1" style="color:#2563eb" target="_blank" rel="noopener">\1</a>', escaped_text)


def _pitch_to_html(text, rtl=False):
    """Turn the plain-text pitch into safe HTML paragraphs (escaped; blank lines -> <p>, single -> <br>,
    URLs -> clickable links)."""
    d = ' dir="rtl"' if rtl else ""
    paras = re.split(r"\n\s*\n", (text or "").strip())
    return "\n".join(
        f'<p{d} style="margin:0 0 14px">{_linkify(_html.escape(p).strip().replace(chr(10), "<br>"))}</p>'
        for p in paras if p.strip())


def _logo_html():
    uri = logo_data_uri()
    if uri:
        # data: URI renders in every preview; send_gmail swaps it to a cid: attachment for real sends.
        return (f'<img src="{uri}" alt="{COMPANY_NAME}" height="42" '
                f'style="display:block;border:0;height:42px;max-height:42px">')
    # no file on disk -> a clean text wordmark so the mail still looks intentional
    return (f'<span style="font-size:19px;font-weight:800;letter-spacing:.3px;color:{BRAND_COLOR}">GRANJUR'
            f'</span><span style="font-size:19px;font-weight:300;color:#555"> technologies</span>')


def _html_signature(unsubscribe):
    """A tidy, table-based HTML signature (email-client safe): logo, name/title, company, contact,
    then small greyed compliance line (address + LIA + privacy + one-click unsubscribe)."""
    sep = ' &nbsp;<span style="color:#ccc">|</span>&nbsp; '
    contact = [f'<a href="{WEBSITE_URL}" style="color:{BRAND_COLOR};text-decoration:none">'
               f'{_html.escape(WEBSITE_URL.split("//")[-1])}</a>',
               f'<a href="mailto:{GMAIL_ADDRESS}" style="color:{BRAND_COLOR};text-decoration:none">{GMAIL_ADDRESS}</a>']
    if PHONE:
        contact.append(_html.escape(PHONE))
    person = ""
    if SENDER_NAME:
        person = (f'<div style="font-weight:700;color:#222;font-size:14px">{_html.escape(SENDER_NAME)}</div>'
                  f'<div style="color:#888;font-size:12px">{_html.escape(SENDER_TITLE)}</div>')
    offices = "".join(
        f'<div><span style="color:#888;font-weight:600">{_html.escape(label)}</span> &nbsp;'
        f'{_html.escape(addr)}</div>' for label, addr in OFFICES)
    return f"""
    <table cellpadding="0" cellspacing="0" border="0" style="margin-top:22px;font-family:Arial,Helvetica,sans-serif">
      <tr><td style="padding:0 0 8px">{_logo_html()}</td></tr>
      <tr><td style="border-top:2px solid {BRAND_COLOR};padding-top:8px">
        {person}
        <div style="color:{BRAND_COLOR};font-weight:700;font-size:14px">{COMPANY_NAME}</div>
        <div style="color:#888;font-size:12px;padding-bottom:4px">{_html.escape(COMPANY_TAGLINE)}</div>
        <div style="color:#555;font-size:12px">{sep.join(contact)}</div>
      </td></tr>
    </table>
    <div style="margin-top:14px;color:#aaa;font-size:11px;line-height:1.6;font-family:Arial,Helvetica,sans-serif">
      {offices}
      <div style="margin-top:6px">You're receiving this on a legitimate business-interest basis ({LIA_REFERENCE}).
      <a href="{PRIVACY_URL}" style="color:#aaa">Privacy</a> &middot;
      <a href="{unsubscribe}" style="color:#aaa">Unsubscribe</a></div>
    </div>"""


# --------------------------------------------------------------------- Phase-6 tracking helpers
def tracking_pixel(lead_id, step=0):
    """A 1x1 open-tracking pixel. When the recipient's client loads it, /t/open logs a VIEW."""
    return (f'<img src="{TRACK_BASE}/t/open/{lead_id}?step={step}" width="1" height="1" '
            f'alt="" style="display:none;border:0;width:1px;height:1px">')


def wrap_links(html_body, lead_id, step=0):
    """Link wrapper: route every http(s) link through /t/click so clicks are logged, then redirected.
    mailto: (unsubscribe) and already-tracked links are left alone."""
    def _sub(m):
        url = m.group(1)
        # Leave the tracker's own links alone, AND never wrap the BOOKING link: the click-redirect runs
        # on the (local) dashboard, so a wrapped booking link would break for a real recipient. The
        # booking button must always reach Google directly. (Other links are still click-tracked.)
        if url.startswith(TRACK_BASE) or (BOOKING_LINK and url == BOOKING_LINK):
            return m.group(0)
        return f'href="{TRACK_BASE}/t/click/{lead_id}?step={step}&u={quote(url, safe="")}"'
    return re.sub(r'href="(https?://[^"]+)"', _sub, html_body)


def calendar_link(lead):
    """The Google Calendar booking page (GRANJUR_BOOKING_LINK). Google handles availability + free-slot
    logic on its own page, so we pass the link through as-is (no URL prefill)."""
    return BOOKING_LINK


def calendar_cta_html(lead):
    """The 'Book a 15-min intro call' button — rendered ONLY when a real booking link is configured, so we
    never send a recipient to a dead link. Wrapped for click-tracking by _finalize_html."""
    if not BOOKING_LINK.startswith("http"):
        return ""   # no booking link set yet -> omit the button (the pitch still has its text CTA)
    return (f'<div style="margin:20px 0"><a href="{BOOKING_LINK}" '
            f'style="display:inline-block;background:{BRAND_COLOR};color:#fff;text-decoration:none;'
            f'padding:10px 18px;border-radius:8px;font-weight:600;font-size:14px">'
            f'Book a 15-min intro call</a></div>')


def _finalize_html(inner_html, lead, step):
    """Wrap the body div, add the calendar CTA + signature already inside `inner_html`, then link-wrap
    every href and append the open pixel. One place so initial + follow-up emails track identically."""
    html_body = (f'<div style="font-family:Arial,Helvetica,sans-serif;font-size:14px;color:#222;'
                 f'line-height:1.6;max-width:600px">{inner_html}{tracking_pixel(lead["id"], step)}</div>')
    return wrap_links(html_body, lead["id"], step)


def thread_headers(lead_id, step):
    """Deterministic Message-ID + In-Reply-To/References so every follow-up threads under the previous
    step (and the original) into ONE Gmail conversation. IDs are derived from lead_id + step (no DB
    lookup), so a follow-up can reference the prior step's ID for real AND test/preview sends alike."""
    def _mid(s):
        return f"<granjur.{lead_id}.s{s}@granjur.tech.dev>"
    msgid = _mid(step)
    if not step or step <= 0:
        return {"message_id": msgid, "in_reply_to": None, "references": None}
    return {"message_id": msgid, "in_reply_to": _mid(step - 1),
            "references": " ".join(_mid(s) for s in range(step))}


def build_payload(lead):
    """lead: dict with id, email, first_name, icp_segment, region, legal_name, qualify_trigger,
       pitch_subject, pitch_body, pitch_lang. Returns the provider payload with a plain-text body
       (fallback) AND an html_body with a professional signature + compliant footer."""
    seg, region = lead.get("icp_segment"), lead.get("region")
    domain = sending_domain(seg)
    if SEND_METHOD == "gmail":
        # We send FROM the Gmail address, so the burner domains don't apply. The unsubscribe must be
        # something a recipient can actually use with no public server running -> a mailto opt-out.
        from_account = GMAIL_ADDRESS
        unsubscribe = f"mailto:{GMAIL_ADDRESS}?subject=unsubscribe"
    else:
        from_account = f"outreach@{domain}"
        unsubscribe = f"{UNSUBSCRIBE_BASE}?lead={lead['id']}"
    footer = (f"\n\n--\n{COMPANY_NAME}\n{_offices_text()}"
              f"\nYou're receiving this on a legitimate business-interest basis ({LIA_REFERENCE}). "
              f"Privacy: {PRIVACY_URL}"
              f"\nUnsubscribe (one click): {unsubscribe}")
    rtl = lead.get("pitch_lang") == "ar"
    inner = (_pitch_to_html(lead.get("pitch_body"), rtl=rtl)
             + calendar_cta_html(lead) + _html_signature(unsubscribe))
    html_body = _finalize_html(inner, lead, step=0)
    return {
        "provider": PROVIDER,
        "campaign_id": campaign_id(seg, region),
        "email": lead.get("email"),
        "first_name": lead.get("first_name"),
        "company_name": lead.get("legal_name"),
        "sending_account": from_account,
        "personalization": {
            "subject": lead.get("pitch_subject"),
            "body": (lead.get("pitch_body") or "") + footer,     # plain-text fallback part
            "html_body": html_body,                              # rich part with logo + signature
            "trigger": lead.get("qualify_trigger"),
            "booking_link": BOOKING_LINK,
        },
        "custom_vars": {"lead_id": str(lead["id"]), "segment": seg, "region": region},
        "_headers": thread_headers(lead["id"], 0),
        "_meta": {"sending_domain": domain, "unsubscribe": unsubscribe, "dry_run": DRY_RUN,
                  "logo_path": logo_path()},
    }


def build_followup_payload(lead, subject, body, step=1, rtl=False):
    """Like build_payload, but for a FOLLOW-UP nudge (Phase 4): same compliant signature + footer +
    one-click unsubscribe, with the generic follow-up subject/body passed in (already in the recipient's
    language). `rtl=True` for Arabic. `step` (1..N) is recorded in the campaign id."""
    seg, region = lead.get("icp_segment"), lead.get("region")
    domain = sending_domain(seg)
    if SEND_METHOD == "gmail":
        from_account = GMAIL_ADDRESS
        unsubscribe = f"mailto:{GMAIL_ADDRESS}?subject=unsubscribe"
    else:
        from_account = f"outreach@{domain}"
        unsubscribe = f"{UNSUBSCRIBE_BASE}?lead={lead['id']}"
    footer = (f"\n\n--\n{COMPANY_NAME}\n{_offices_text()}"
              f"\nYou're receiving this on a legitimate business-interest basis ({LIA_REFERENCE}). "
              f"Privacy: {PRIVACY_URL}"
              f"\nUnsubscribe (one click): {unsubscribe}")
    # All four follow-up steps now end with a calendar CTA, so each gets the "Book a call" button.
    inner = _pitch_to_html(body, rtl=rtl) + calendar_cta_html(lead) + _html_signature(unsubscribe)
    html_body = _finalize_html(inner, lead, step=step)
    return {
        "provider": PROVIDER,
        "campaign_id": f"{campaign_id(seg, region)}-f{step}",
        "email": lead.get("email"),
        "first_name": lead.get("first_name"),
        "company_name": lead.get("legal_name"),
        "sending_account": from_account,
        "personalization": {
            "subject": subject,
            "body": (body or "") + footer,
            "html_body": html_body,
            "trigger": lead.get("qualify_trigger"),
            "booking_link": BOOKING_LINK,
        },
        "custom_vars": {"lead_id": str(lead["id"]), "segment": seg, "region": region,
                        "followup_step": step},
        "_headers": thread_headers(lead["id"], step),
        "_meta": {"sending_domain": domain, "unsubscribe": unsubscribe, "dry_run": DRY_RUN,
                  "logo_path": logo_path(), "followup_step": step},
    }


def log_dry_run(payload):
    """Append the payload to the dry-run outbox (what WOULD have been sent)."""
    with open(OUTBOX, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")
