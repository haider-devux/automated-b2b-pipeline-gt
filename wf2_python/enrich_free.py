"""
FREE enrichment (config.ENRICH_MODE = "free") — real data, no paid keys.

  - tech_stack     : fetch the site's HTML and match known signatures (Shopify/WordPress/React...)
  - email check    : syntax + DNS MX lookup (dnspython) — no sending, no paid verifier
  - Lighthouse     : optional, only if a free PageSpeed Insights key is set (else skipped)
  - firmographics  : trusted from the CSV/WF-1 data already on the row (free manual enrichment)

Philosophy: best-effort + honest. What we can confirm is marked 'valid'; what we can't is marked
'unverified' (kept, flagged for a future paid verify) — only clearly malformed emails are 'invalid'.
Every step is wrapped so one failure never halts the lead (partial success).
"""
import html as _htmllib
import re
import config
import requests

try:
    import dns.resolver
    _HAS_DNS = True
except Exception:  # dnspython not installed
    _HAS_DNS = False


class FreeEnrichError(Exception):
    """A single free-enrichment step failed (caught per-step, never fatal)."""


_EMAIL_RE = re.compile(r"^[^@\s]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})$")

# Signatures are asset URLs / code markers (NOT brand names), so a page that merely *mentions* a
# competitor isn't misread as using it. Free heuristic detection — paid BuiltWith is more thorough.
_TECH_SIGNATURES = {
    "Shopify": ("cdn.shopify.com", "myshopify.com", "x-shopify", "shopify-section"),
    "WooCommerce": ("woocommerce/assets", "wc-ajax", "class=\"woocommerce"),
    "WordPress": ("wp-content", "wp-includes", "wp-json"),
    "Magento": ("/static/version", "mage-init", "/mage/", "magento_"),
    "Wix": ("wixstatic.com", "_wix", "static.parastorage.com"),
    "Squarespace": ("squarespace.com/universal", "static1.squarespace"),
    "Webflow": ("assets.website-files.com", "webflow.js"),
    "BigCommerce": ("cdn11.bigcommerce.com", "bigcommerce.com/s-"),
    "React": ("data-reactroot", "react.production.min", "__reactcontainer"),
    "Next.js": ("/_next/static",),
    "Vue.js": ("data-v-", "vue.runtime", "vue.min.js"),
    "Angular": ("ng-version=", "angular.min.js"),
    "jQuery": ("jquery.min.js", "jquery.js"),
    "Bootstrap": ("bootstrap.min.css", "bootstrap.min.js"),
    "Google Analytics": ("google-analytics.com/analytics.js", "googletagmanager.com/gtag"),
}


def _fetch_html(domain):
    """Return the homepage HTML with ORIGINAL casing (callers lowercase where they need to).
    Original case is kept so the scraped description reads naturally, not all-lowercase."""
    last = None
    for scheme in ("https", "http"):
        try:
            r = requests.get(f"{scheme}://{domain}", timeout=config.FREE_HTTP_TIMEOUT,
                             headers={"User-Agent": config.FREE_USER_AGENT})
            if r.status_code < 400 and r.text:
                return r.text
            last = f"HTTP {r.status_code}"
        except requests.RequestException as e:
            last = e
    raise FreeEnrichError(f"could not fetch site ({last})")


def _meta_content(html, attr, value):
    """Grab the `content` of a <meta> whose `attr` equals `value` (attribute order-agnostic)."""
    for m in re.finditer(r"<meta\b[^>]*>", html, re.I):
        tag = m.group(0)
        if re.search(rf'{attr}\s*=\s*["\']{re.escape(value)}["\']', tag, re.I):
            c = re.search(r'content\s*=\s*["\']([^"\']*)["\']', tag, re.I)
            if c:
                return c.group(1)
    return None


# nav/boilerplate headings to ignore so we keep only meaningful product/service text
_NAV_JUNK = {
    "home", "shop", "cart", "menu", "account", "search", "contact", "contact us", "about",
    "about us", "login", "log in", "register", "sign in", "sign up", "checkout", "my account",
    "basket", "wishlist", "blog", "news", "faq", "faqs", "reviews", "gallery", "services",
    "products", "welcome", "newsletter", "follow us", "categories",
}


def _headings(html):
    """Meaningful <h1>-<h3> text (product ranges, specialties) — the specifics that make a pitch feel
    researched. Skips nav/boilerplate so we keep 'Genesis, Ridgeback & Frog bikes', not 'Home / Cart'."""
    out, seen = [], set()
    for h in re.findall(r"<h[1-3][^>]*>(.*?)</h[1-3]>", html, re.I | re.S):
        t = re.sub(r"\s+", " ", _htmllib.unescape(re.sub(r"<[^>]+>", " ", h))).strip()
        low = t.lower()
        if not t or low in _NAV_JUNK or len(t) < 4 or len(t) > 90 or low in seen:
            continue
        seen.add(low)
        out.append(t)
        if len(out) >= 6:
            break
    return out


def extract_description(html):
    """A REAL blurb about the company from its own homepage: og:description / meta description / <title>,
    ENRICHED with a few meaningful page headings (the specific products/services they name). This is what
    the site itself says it does — the raw material for a researched, grounded pitch (far better than a tag)."""
    if not html:
        return None
    desc = (_meta_content(html, "property", "og:description")
            or _meta_content(html, "name", "description"))
    if not desc:
        t = re.search(r"<title[^>]*>([^<]+)</title>", html, re.I)
        desc = t.group(1) if t else None
    blurb = re.sub(r"\s+", " ", _htmllib.unescape(desc)).strip() if desc else ""
    # append headings that add NEW words (specific products/services not already in the blurb)
    low_blurb = blurb.lower()
    extras = [h for h in _headings(html) if h.lower() not in low_blurb]
    if extras:
        blurb = (blurb + " — " if blurb else "") + "; ".join(extras)
    blurb = re.sub(r"\s+", " ", blurb).strip()
    return blurb[:450] or None


_EMAIL_FIND_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
# Left-anchored so a stray preceding character can't be glued onto the address (e.g. text that renders
# "...n info@x.com" must not become "ninfo@x.com", which would bounce). mailto: links are trusted first.
_BOUNDED_EMAIL_RE = re.compile(r"(?<![A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_IMG_EXT = (".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp")
_JUNK_PREFIX = ("noreply", "no-reply", "donotreply", "example", "sentry", "wixpress")
# role accounts are prime spam-trap territory (blueprint 1.3) — never auto cold-email them
_ROLE_PREFIXES = {
    "info", "sales", "admin", "support", "contact", "hello", "hi", "office", "postmaster",
    "webmaster", "enquiries", "enquiry", "mail", "team", "help", "service", "services",
    "marketing", "hr", "careers", "jobs", "billing", "accounts", "accounting", "press", "media",
    "general", "reception", "bookings", "orders", "customerservice", "care",
    # compound role inboxes (no separator) the deep scraper commonly surfaces on Contact/About pages:
    "contactus", "aboutus", "customercare", "customerservices", "customerservice", "salesteam",
    "infodesk", "infoline", "getintouch", "hellothere", "helpdesk", "noreply", "enquiriesuk",
}


def is_role_account(email):
    """True if this is a role/shared inbox (info@, contactus@, sales.team@ ...) we must never cold-email.
    Errs toward 'role' on ambiguous compounds — skipping a maybe-personal address is far safer than
    landing a cold pitch in a monitored role box (spam-complaint risk)."""
    local = (email or "").lower().split("@")[0]
    if local in _ROLE_PREFIXES or re.split(r"[._+-]", local)[0] in _ROLE_PREFIXES:
        return True
    # compound with no separator, e.g. 'contactus', 'infodesk' — role word + a short suffix
    return any(local.startswith(p) and (len(local) - len(p)) <= 4 for p in _ROLE_PREFIXES)


# --------------------------------------------------------------------------- smart role relaxation
# A small local business (bakery, dentist, salon, local shop/office) with NO active hiring signal usually
# has ONLY a role inbox like info@ — and it's the owner's real, monitored mailbox, not a spam trap. For
# THAT case we relax the rule and allow a short list of reachable role prefixes. Enterprise / job-board
# leads (a hiring signal, or a tech/SaaS niche) stay STRICT — those role boxes ARE spam-trap territory.
_RELAXED_ROLE_PREFIXES = {"info", "contact", "hello", "hallo", "admin", "hi", "office",
                          "enquiries", "enquiry", "mail", "kontakt", "reception", "hey"}
# never acceptable, even for a local business (system mailboxes / guaranteed traps)
_NEVER_OK_PREFIXES = {"noreply", "no-reply", "donotreply", "postmaster", "abuse", "webmaster",
                      "mailer-daemon", "bounce", "spam"}
# niches that are NOT small-local (job-board / startup / tech) — keep strict role filtering for these
_NON_LOCAL_NICHES = {"tech/saas", "saas", "software", "technology", "it", "fintech", "startup",
                     "enterprise", "corporate"}


def is_small_local(niche, has_hiring):
    """A small local business (shop / service / office) with no active hiring signal — the case where a
    role inbox like info@ is the owner's real mailbox rather than a spam trap."""
    if has_hiring:
        return False
    n = (niche or "").strip().lower()
    return bool(n) and n not in _NON_LOCAL_NICHES


def role_email_acceptable(email, niche, has_hiring):
    """True if this role inbox is safe to cold-email under the relaxed small-local rule (Part 1).
    System mailboxes (noreply@, postmaster@) are never acceptable; enterprise/hiring leads stay strict."""
    local = (email or "").lower().split("@")[0]
    first = re.split(r"[._+-]", local)[0]
    if first in _NEVER_OK_PREFIXES or local in _NEVER_OK_PREFIXES:
        return False
    if not is_small_local(niche, has_hiring):
        return False
    return first in _RELAXED_ROLE_PREFIXES or local in _RELAXED_ROLE_PREFIXES


def _detect_from_html(html):
    h = html.lower()   # signatures are lowercase; the fetched HTML now keeps original case
    return [tech for tech, sigs in _TECH_SIGNATURES.items() if any(s in h for s in sigs)]


def detect_tech(domain):
    """Convenience for check_free.py — fetches then detects."""
    return _detect_from_html(_fetch_html(domain))


def find_email_in_html(html, domain):
    """Return (email, is_role). Only trusts emails on the company's OWN domain (skips third-party
    font/CDN/analytics emails), and prefers a personal address over a role account."""
    dom = (domain or "").lower()
    if dom.startswith("www."):
        dom = dom[4:]
    hits = [e for e in _EMAIL_FIND_RE.findall(html)
            if not e.lower().endswith(_IMG_EXT)
            and not any(e.lower().startswith(p) for p in _JUNK_PREFIX)]
    own = [e for e in hits if dom and e.lower().split("@")[-1].endswith(dom)]
    if not own:
        return None, False
    personal = [e for e in own if not is_role_account(e)]
    if personal:
        return personal[0], False
    return own[0], True   # only a role account is available — capture it but flag it


# --------------------------------------------------------------------------- deep email finding
# Most small businesses DON'T put an address on the homepage — it lives on a Contact/About/Team page,
# usually behind a mailto: link. So when the homepage yields no *personal* email, follow the site's own
# contact-ish links (and try a few conventional paths) and scan those too. Free; just a few more GETs,
# and only when needed (a homepage that already exposes a personal address short-circuits before this).
_CONTACT_PATHS = ["contact", "contact-us", "contactus", "about", "about-us",
                  "team", "our-team", "company", "people", "staff", "get-in-touch"]
_MAILTO_RE = re.compile(r'mailto:([^"\'?>\s]+)', re.I)
_MAX_CONTACT_PAGES = 6           # politeness cap on extra fetches per lead


def _own_domain_emails(candidates, dom):
    out = []
    for e in candidates:
        el = e.lower()
        if el.endswith(_IMG_EXT) or any(el.startswith(p) for p in _JUNK_PREFIX):
            continue
        if dom and el.split("@")[-1].endswith(dom) and e not in out:
            out.append(e)
    return out


def _emails_from_html(html, domain):
    """Own-domain emails on a page. Trusts mailto: links FIRST (clean, unambiguous); only if there are
    none does it fall back to left-anchored text scraping (noisier). De-duped, in document order."""
    dom = (domain or "").lower()
    if dom.startswith("www."):
        dom = dom[4:]
    mailtos = _own_domain_emails(
        [_htmllib.unescape(m).strip() for m in _MAILTO_RE.findall(html or "")], dom)
    if mailtos:
        return mailtos
    return _own_domain_emails(_BOUNDED_EMAIL_RE.findall(html or ""), dom)


def _pick_email(emails):
    """(email, is_role) preferring a personal address; (None, False) if the list is empty."""
    if not emails:
        return None, False
    personal = [e for e in emails if not is_role_account(e)]
    if personal:
        return personal[0], False
    return emails[0], True


def _contact_links(html, domain):
    """Same-site Contact/About/Team hrefs found ON the homepage — the site's own way to its contact page."""
    dom = (domain or "").lower()
    if dom.startswith("www."):
        dom = dom[4:]
    links = []
    for m in re.finditer(r'href\s*=\s*["\']([^"\']+)["\']', html or "", re.I):
        href = _htmllib.unescape(m.group(1)).strip()
        low = href.lower()
        if low.startswith(("mailto:", "tel:", "#", "javascript:")):
            continue
        if any(k in low for k in ("contact", "about", "team", "people", "staff", "get-in-touch")):
            links.append(href)
    return links


def _abs_url(href, domain):
    if href.lower().startswith("http"):
        return href
    return f"https://{domain}/{href.lstrip('/')}"


def find_email_deep(domain, homepage_html):
    """Homepage first, then a few Contact/About/Team pages. Returns (email, is_role) or (None, False).
    Prefers a personal address; keeps a role address only as a last-resort fallback (flagged)."""
    dom = (domain or "").strip().lower()
    if dom.startswith("www."):
        dom = dom[4:]
    if not dom:
        return None, False

    email, is_role = _pick_email(_emails_from_html(homepage_html, dom))
    if email and not is_role:
        return email, False                 # homepage already has a personal address — done
    best = (email, is_role)                  # a homepage role email is our fallback for now

    # candidate pages: the homepage's own contact-ish links first, then conventional paths
    seen, candidates = set(), []
    for href in _contact_links(homepage_html or "", dom):
        url = _abs_url(href, dom)
        if dom in url.lower() and url not in seen:
            seen.add(url)
            candidates.append(url)
    for p in _CONTACT_PATHS:
        url = f"https://{dom}/{p}"
        if url not in seen:
            seen.add(url)
            candidates.append(url)

    for url in candidates[:_MAX_CONTACT_PAGES]:
        try:
            r = requests.get(url, timeout=config.FREE_HTTP_TIMEOUT,
                             headers={"User-Agent": config.FREE_USER_AGENT})
            if r.status_code >= 400 or not r.text:
                continue
        except requests.RequestException:
            continue
        email, is_role = _pick_email(_emails_from_html(r.text, dom))
        if email and not is_role:
            return email, False              # found a personal address on a contact page — done
        if email and not best[0]:
            best = (email, is_role)          # remember first role email if we never find a personal one
    return best


def verify_email(email):
    """'valid' (MX exists) | 'invalid' (bad syntax) | 'unverified' (couldn't confirm)."""
    m = _EMAIL_RE.match(email or "")
    if not m:
        return "invalid"
    if not _HAS_DNS:
        return "unverified"
    domain = m.group(1)
    try:
        answers = dns.resolver.resolve(domain, "MX", lifetime=config.FREE_DNS_TIMEOUT)
        return "valid" if len(answers) else "unverified"
    except Exception:
        # NXDOMAIN / no MX / DNS timeout — free mode won't drop a curated lead over this
        return "unverified"


def pagespeed(domain):
    """Lighthouse mobile score + LCP via free PageSpeed Insights (only if a key is configured)."""
    if not config.PAGESPEED_API_KEY:
        return None
    r = requests.get(
        "https://www.googleapis.com/pagespeedonline/v5/runPagespeed",
        params={"url": f"https://{domain}", "strategy": "mobile",
                "category": "performance", "key": config.PAGESPEED_API_KEY},
        timeout=config.FREE_HTTP_TIMEOUT + 20)
    r.raise_for_status()
    j = r.json()
    score = j["lighthouseResult"]["categories"]["performance"]["score"]
    lcp = j["lighthouseResult"]["audits"]["largest-contentful-paint"]["numericValue"]
    return {"mobile": int(round(score * 100)), "lcp": int(lcp)}


def run(company, lead):
    """Return (data, errors). Firmographics stay whatever the CSV gave; we fill tech/email/lighthouse."""
    domain = str(company.get("domain") or "").strip()
    data, errors = {}, []

    # fetch the site ONCE, reuse for tech detection + email finding
    html = None
    if domain:
        try:
            html = _fetch_html(domain)
        except FreeEnrichError as e:
            errors.append(f"fetch: {e}")

    # 1) tech stack + a real description from the page
    if html:
        tech = _detect_from_html(html)
        if tech:
            data["tech_stack"] = tech
        desc = extract_description(html)
        if desc:
            data["description"] = desc

    # 2) Lighthouse (best-effort; only if a free PageSpeed key is set)
    try:
        lh = pagespeed(domain)
        if lh:
            data["lighthouse_mobile"], data["lighthouse_lcp_ms"] = lh["mobile"], lh["lcp"]
    except Exception as e:  # noqa: BLE001
        errors.append(f"pagespeed: {e}")

    # 3) email — use the CSV/WF-1 one if present, else try to find a public one on the site
    email = (lead.get("email") or "").strip() or None
    email_source = "provided" if email else None
    if not email and html:
        # Deep scan: homepage first, then the site's own Contact/About/Team pages + mailto: links.
        found, is_role = find_email_deep(domain, html)
        if found:
            email, email_source = found, ("site-role" if is_role else "site")
    relaxed = False
    if email:
        data["email"] = email
        role = is_role_account(email)
        has_hiring = bool((company or {}).get("active_job_posts") or (lead or {}).get("active_job_posts"))
        relaxed = role and role_email_acceptable(email, (company or {}).get("niche"), has_hiring)
        if role and not relaxed:
            # role account on an enterprise / job-board lead — prime trap territory; never auto-send it
            data["email_validation_status"] = "role"
        else:
            # a personal address, OR a small-local role inbox we now accept (Part 1) — verify + keep.
            try:
                data["email_validation_status"] = verify_email(email)
            except Exception as e:  # noqa: BLE001
                errors.append(f"verify: {e}")

    data["raw_payload"] = {"source": "free",
                           "email_source": (email_source + "-role-relaxed") if relaxed else email_source,
                           "errors": errors}
    return data, errors
