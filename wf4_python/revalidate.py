"""
Pre-send email re-validation (blueprint Gate 2: re-check in the window immediately before sending).

A lead can sit in QUEUED_FOR_OUTREACH for a while; email data decays. So right before the (dry-run)
send, we re-run a live syntax + DNS MX check. 'invalid' -> do NOT send (suppress instead).
Free and fast (dnspython) — no paid verifier.
"""
import re

try:
    import dns.resolver
    _HAS_DNS = True
except Exception:  # dnspython not installed
    _HAS_DNS = False

_EMAIL_RE = re.compile(r"^[^@\s]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})$")


def check(email, dns_timeout=5):
    """'valid' (MX exists) | 'invalid' (bad syntax) | 'unverified' (couldn't confirm — allowed)."""
    m = _EMAIL_RE.match((email or "").strip())
    if not m:
        return "invalid"
    if not _HAS_DNS:
        return "unverified"
    try:
        answers = dns.resolver.resolve(m.group(1), "MX", lifetime=dns_timeout)
        return "valid" if len(answers) else "unverified"
    except Exception:
        return "unverified"
