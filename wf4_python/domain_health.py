"""
Email Domain Health & Anti-Spam diagnostics (Max Plan · Phase 3).

A FREE, programmatic defense system to keep our sending identity out of the spam folder. It runs three
families of checks and produces a single authorization for the send-gate:

  1. AUTHENTICATION  — SPF, DKIM, DMARC via live DNS lookups (dnspython).
  2. BLACKLISTS      — the sending IP/domain against free DNSBLs.
  3. WARMUP          — daily-volume ramp for a fresh mailbox (the #1 lever for a new account),
                       counting real sends from outreach_log.

IMPORTANT for THIS build: the sender is a CONSUMER Gmail (granjur.tech.dev@gmail.com), so the sending
domain is gmail.com — its SPF/DKIM/DMARC are Google's and already perfect, and its IPs are Google's
shared infrastructure (not ours to fix). For a consumer mailbox the checks that actually move the needle
are WARMUP + content discipline. The DNS checks still run (they confirm Google's setup and are ready for
when you move to a CUSTOM sending domain — the recommended path for real cold outreach at volume).

No paid tools. Uses only dnspython (already a dependency). Import-safe from both WF-4 and the dashboard.

  python domain_health.py            # print a full report for the configured sender
  python domain_health.py acme.com   # check an arbitrary domain
"""
import os
import sys
from datetime import datetime, date, timezone

try:
    import dns.resolver
    _HAVE_DNS = True
except Exception:                       # noqa: BLE001 — degrade gracefully if dnspython is missing
    _HAVE_DNS = False

# ---- configuration (all overridable via env / project-root .env) ----
try:  # load project-root .env so GMAIL_* reach this script when run standalone
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
                override=False)
except Exception:  # noqa: BLE001 — dotenv is optional; real env vars still work
    pass

SENDER_EMAIL = os.getenv("GMAIL_ADDRESS", "")
MAILBOX_CREATED = os.getenv("GRANJUR_MAILBOX_CREATED", "2026-07-09")   # set to the true creation date
WARMUP_CEIL = int(os.getenv("GRANJUR_WARMUP_CEIL", "50"))              # absolute daily ceiling

# Providers whose SPF/DKIM/DMARC + IP reputation are managed for you (you can't/needn't fix them).
_SHARED_PROVIDERS = {"gmail.com", "googlemail.com", "outlook.com", "hotmail.com",
                     "live.com", "yahoo.com", "icloud.com", "me.com"}

# Free DNS blacklists to query (reverse-IP lookups). Public resolvers may rate-limit Spamhaus.
_DNSBLS = ["zen.spamhaus.org", "b.barracudacentral.org", "bl.spamcop.net", "dnsbl.sorbs.net"]

# Common DKIM selectors to probe on a custom domain (Google Workspace uses 'google').
_DKIM_SELECTORS = ["google", "default", "selector1", "selector2", "k1", "k2", "mail", "dkim", "s1", "s2"]

# Warmup ramp: (min_age_days, daily_cap). A fresh mailbox must not blast volume on day one.
_WARMUP_SCHEDULE = [(0, 5), (2, 10), (4, 20), (7, 30), (14, 40), (21, WARMUP_CEIL)]


def sending_domain(email=None):
    return (email or SENDER_EMAIL).split("@")[-1].strip().lower()


# ------------------------------------------------------------------ DNS helpers
def _resolver():
    r = dns.resolver.Resolver()
    r.lifetime = r.timeout = 5.0
    return r


def _txt(name):
    """Return the list of TXT strings at `name` (joined), or [] on any failure."""
    if not _HAVE_DNS:
        return []
    try:
        ans = _resolver().resolve(name, "TXT")
    except Exception:                   # noqa: BLE001 — NXDOMAIN / timeout / no-answer all mean "absent"
        return []
    out = []
    for rec in ans:
        try:
            out.append(b"".join(rec.strings).decode("utf-8", "ignore"))
        except Exception:               # noqa: BLE001
            out.append(str(rec).strip('"'))
    return out


def _a_records(name):
    if not _HAVE_DNS:
        return []
    try:
        return [str(x) for x in _resolver().resolve(name, "A")]
    except Exception:                   # noqa: BLE001
        return []


def _check(name, status, detail, fix=""):
    """status: 'pass' | 'warn' | 'fail' | 'na'."""
    return {"name": name, "status": status, "detail": detail, "fix": fix}


# ------------------------------------------------------------------ auth checks
def check_spf(domain):
    recs = [r for r in _txt(domain) if r.lower().startswith("v=spf1")]
    if not recs:
        return _check("SPF", "fail", f"No SPF record on {domain}.",
                      "Add a TXT record: v=spf1 include:_spf.google.com ~all")
    if len(recs) > 1:
        return _check("SPF", "warn", f"{len(recs)} SPF records (only one is valid).",
                      "Merge into a single v=spf1 record.")
    return _check("SPF", "pass", recs[0][:120])


def check_dmarc(domain):
    recs = [r for r in _txt("_dmarc." + domain) if r.lower().startswith("v=dmarc1")]
    if not recs:
        return _check("DMARC", "fail", f"No DMARC record at _dmarc.{domain}.",
                      "Add TXT _dmarc: v=DMARC1; p=quarantine; rua=mailto:you@domain")
    policy = ""
    for part in recs[0].split(";"):
        k, _, v = part.strip().partition("=")
        if k.strip().lower() == "p":
            policy = v.strip().lower()
    if policy in ("quarantine", "reject"):
        return _check("DMARC", "pass", f"policy p={policy}")
    return _check("DMARC", "warn", f"policy p={policy or 'none'} (monitoring only, not enforced).",
                  "Move to p=quarantine once SPF/DKIM are solid.")


def check_dkim(domain):
    for sel in _DKIM_SELECTORS:
        recs = _txt(f"{sel}._domainkey.{domain}")
        if any(("v=dkim1" in r.lower()) or ("k=rsa" in r.lower()) or ("p=" in r.lower()) for r in recs):
            return _check("DKIM", "pass", f"selector '{sel}' present")
    return _check("DKIM", "warn", f"No DKIM found on common selectors for {domain}.",
                  "In Google Workspace: Apps > Gmail > Authenticate email; publish the 'google' selector.")


# ------------------------------------------------------------------ blacklist check
def _reverse_ip(ip):
    return ".".join(reversed(ip.split(".")))


def check_blacklists(domain):
    ips = _a_records(domain)
    if not ips:
        return _check("Blacklists", "na", f"No A record on {domain} to check.")
    listed = []
    for ip in ips:
        if ip.count(".") != 3:          # skip IPv6 for this simple check
            continue
        for bl in _DNSBLS:
            try:
                _resolver().resolve(f"{_reverse_ip(ip)}.{bl}", "A")
                listed.append(f"{ip} on {bl}")
            except Exception:           # noqa: BLE001 — not listed / query blocked -> treat as clean
                pass
    if listed:
        return _check("Blacklists", "fail", "; ".join(listed[:4]),
                      "Request delisting; pause sending until reputation recovers.")
    return _check("Blacklists", "pass", f"{len(ips)} IP(s) clean on {len(_DNSBLS)} DNSBLs")


# ------------------------------------------------------------------ warmup
def account_age_days(today=None):
    today = today or datetime.now(timezone.utc).date()
    try:
        created = date.fromisoformat(MAILBOX_CREATED)
    except ValueError:
        return 999                      # bad config -> treat as fully warmed
    return max(0, (today - created).days)


def daily_cap(age_days):
    cap = _WARMUP_SCHEDULE[0][1]
    for min_age, c in _WARMUP_SCHEDULE:
        if age_days >= min_age:
            cap = c
    return min(cap, WARMUP_CEIL)


def sent_today(conn):
    """Real sends counted from outreach_log for the current server day (sent + dry-run logged)."""
    with conn.cursor() as cur:
        cur.execute("""SELECT COUNT(*) FROM outreach_log
                       WHERE outcome IN ('sent','logged')
                         AND created_at >= date_trunc('day', now());""")
        return cur.fetchone()[0]


# ------------------------------------------------------------------ bounce circuit breaker
# The #1 signal that a list/identity is going bad. If the trailing-window bounce rate crosses the
# ceiling (on a big enough sample), bot-send parks itself for the day (governor) rather than keep
# torching reputation. Complaint tracking isn't available on consumer Gmail, so this is bounce-based.
BOUNCE_RATE_CEIL   = float(os.getenv("GRANJUR_BOUNCE_RATE_CEIL", "0.03"))    # 3%
BOUNCE_MIN_SAMPLE  = int(os.getenv("GRANJUR_BOUNCE_MIN_SAMPLE", "20"))       # don't trip on tiny samples
BOUNCE_WINDOW_DAYS = int(os.getenv("GRANJUR_BOUNCE_WINDOW_DAYS", "7"))


def bounce_stats(conn, window_days=None):
    """Trailing-window bounce rate. Returns dict: sends, bounces, rate, tripped, ceil, sample_ok.
    `tripped` is True only when the sample is large enough AND the rate exceeds the ceiling."""
    window_days = window_days or BOUNCE_WINDOW_DAYS
    if conn is None:
        return {"sends": 0, "bounces": 0, "rate": 0.0, "tripped": False,
                "ceil": BOUNCE_RATE_CEIL, "sample_ok": False, "window_days": window_days}
    with conn.cursor() as cur:
        cur.execute("""SELECT COUNT(*) FROM outreach_log
                        WHERE outcome IN ('sent','logged')
                          AND created_at >= now() - make_interval(days => %s);""", (window_days,))
        sends = cur.fetchone()[0]
        cur.execute("""SELECT COUNT(*) FROM email_events
                        WHERE event_type = 'bounce'
                          AND at >= now() - make_interval(days => %s);""", (window_days,))
        bounces = cur.fetchone()[0]
    rate = (bounces / sends) if sends else 0.0
    sample_ok = sends >= BOUNCE_MIN_SAMPLE
    return {"sends": sends, "bounces": bounces, "rate": rate,
            "tripped": sample_ok and rate > BOUNCE_RATE_CEIL,
            "ceil": BOUNCE_RATE_CEIL, "sample_ok": sample_ok, "window_days": window_days}


def warmup_status(conn=None, sent=None, today=None):
    age = account_age_days(today)
    cap = daily_cap(age)
    used = sent if sent is not None else (sent_today(conn) if conn is not None else 0)
    remaining = max(0, cap - used)
    return {"age_days": age, "cap": cap, "sent_today": used, "remaining": remaining,
            "created": MAILBOX_CREATED, "ceiling": WARMUP_CEIL,
            "schedule": _WARMUP_SCHEDULE}


# ------------------------------------------------------------------ full report + gate
def report(conn=None, email=None, sent=None, today=None):
    """Assemble the full health report + an overall send authorization."""
    email = email or SENDER_EMAIL
    domain = sending_domain(email)
    shared = domain in _SHARED_PROVIDERS

    if shared:
        checks = [
            _check("SPF", "pass", f"{domain} SPF is provider-managed by Google."),
            _check("DKIM", "pass", f"{domain} DKIM is provider-managed by Google."),
            _check("DMARC", "pass", f"{domain} DMARC is provider-managed by Google."),
            _check("Blacklists", "na", f"You send via {domain}'s shared IPs — reputation isn't yours "
                   "to control. Warmup + content are what protect you here.",
                   "For real cold outreach at volume, use a CUSTOM domain you own + warm it."),
        ]
    else:
        checks = [check_spf(domain), check_dkim(domain), check_dmarc(domain), check_blacklists(domain)]

    warm = warmup_status(conn=conn, sent=sent, today=today)

    hard_fail = [c for c in checks if c["status"] == "fail"]
    # Blocking = a real authentication/blacklist failure (only possible on a custom domain), OR warmup
    # exhausted for the day. Consumer-Gmail 'na' items never block.
    blocking = []
    if hard_fail:
        blocking.append("; ".join(f"{c['name']}: {c['detail']}" for c in hard_fail))
    if warm["remaining"] <= 0:
        blocking.append(f"warmup cap reached ({warm['sent_today']}/{warm['cap']} sent today)")

    overall = "fail" if hard_fail else ("warn" if any(c["status"] == "warn" for c in checks) else "pass")
    return {
        "email": email, "domain": domain, "shared_provider": shared,
        "checks": checks, "warmup": warm,
        "overall": overall, "blocking": blocking,
        "dns_available": _HAVE_DNS,
    }


def _print_report(rep):
    icon = {"pass": "[ OK ]", "warn": "[WARN]", "fail": "[FAIL]", "na": "[ NA ]"}
    print(f"\nEmail Domain Health — {rep['email']}  (domain: {rep['domain']})")
    print("=" * 66)
    for c in rep["checks"]:
        print(f"  {icon[c['status']]} {c['name']:<11} {c['detail']}")
        if c["fix"]:
            print(f"           fix: {c['fix']}")
    w = rep["warmup"]
    print(f"\n  WARMUP  mailbox age {w['age_days']}d (created {w['created']}) -> today's cap {w['cap']}, "
          f"sent {w['sent_today']}, remaining {w['remaining']}")
    print("=" * 66)
    print(f"  OVERALL: {rep['overall'].upper()}"
          + (f"  | BLOCKING: {'; '.join(rep['blocking'])}" if rep["blocking"] else "  | clear to send"))


if __name__ == "__main__":
    dom_email = sys.argv[1] if len(sys.argv) > 1 else None
    conn = None
    try:
        import db                       # optional: count today's sends if the DB is reachable
        conn = db.get_connection()
    except Exception:                   # noqa: BLE001 — DNS checks still run without a DB
        conn = None
    if dom_email and "@" not in dom_email:
        dom_email = "user@" + dom_email  # allow a bare domain arg
    _print_report(report(conn=conn, email=dom_email))
    if conn:
        conn.close()
