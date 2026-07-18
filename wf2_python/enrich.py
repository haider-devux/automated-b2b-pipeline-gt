"""
The enrichment waterfall (Blueprint Phase 2): Apollo -> Hunter (fallback) -> BuiltWith ->
PageSpeed -> Jobs/intent -> Email verification.

The defining rule: one slow or failing API must NEVER halt the pipeline. Each step is wrapped
in its own try/except; a failure is recorded and the waterfall continues (partial success).

RIGHT NOW this runs in MOCK mode (config.USE_MOCK): the "API calls" return canned data keyed by
domain, and a few are rigged to fail so you can see the fault-tolerance + ERROR path on the
dashboard. To go live later, replace each _mock_* body with a real API call — the structure stays.
"""
import config
import enrich_free


class EnrichError(Exception):
    """A single enrichment source failed (caught per-step, never fatal)."""


# --------------------------------------------------------------------------- mock dataset
# Each domain rigs a scenario. `apollo.email=None` forces the Hunter fallback; `fail=True`
# simulates a 429/500 that must be caught. (These are the ONLY fake bits — everything else is real.)
MOCK = {
    "verdant-landscaping.test": {  # -> WF-3 Segment A (mid-size local service business)
        "apollo": {"first_name": "Greg", "last_name": "Palmer", "job_title": "Owner",
                   "seniority": "Owner", "employee_count": 35, "email": "greg@verdant-landscaping.test"},
        "builtwith": ["WordPress", "PHP", "jQuery"], "pagespeed": (58, 3200),
        "jobs": [], "verify": "valid"},
    "pixelforge.test": {  # -> Segment B. Apollo returns NO email -> Hunter fallback fills it.
        "apollo": {"first_name": "Aisha", "last_name": "Khan", "job_title": "CTO",
                   "seniority": "C-Level", "employee_count": 48, "email": None},
        "hunter": "aisha@pixelforge.test",
        "builtwith": ["React", "Node.js", "AWS"], "pagespeed": (74, 2100),
        "jobs": [{"title": "Senior React Engineer", "url": "https://ex.test/job/1",
                  "seen_at": "2026-07-02", "source": "linkedin"}], "verify": "valid"},
    "bazaar-online.test": {  # -> Segment C (low-tech e-commerce), GCC -> Arabic pitch
        "apollo": {"first_name": "Omar", "last_name": "Farouk", "job_title": "Founder",
                   "seniority": "C-Level", "employee_count": 20, "email": "omar@bazaar-online.test"},
        "builtwith": ["Shopify", "Liquid"], "pagespeed": (29, 5400),
        "jobs": [], "verify": "valid"},
    "dragon-mart.test": {  # -> Segment C, CN -> Chinese pitch
        "apollo": {"first_name": "Li", "last_name": "Wei", "job_title": "Founder",
                   "seniority": "C-Level", "employee_count": 26, "email": "li@dragon-mart.test"},
        "builtwith": ["Magento", "PHP"], "pagespeed": (41, 4300),
        "jobs": [], "verify": "valid"},
    "titan-industries.test": {  # enriches fine, but WF-3 will DISQUALIFY (>150 staff)
        "apollo": {"first_name": "Robert", "last_name": "Stone", "job_title": "VP Engineering",
                   "seniority": "VP", "employee_count": 430, "email": "robert@titan-industries.test"},
        "builtwith": ["Java", "Oracle", "SAP"], "pagespeed": (66, 2800),
        "jobs": [], "verify": "valid"},
    "glitch-corp.test": {  # BuiltWith fails (caught) -> still ENRICHED with empty tech_stack (partial success)
        "apollo": {"first_name": "Dana", "last_name": "Lee", "job_title": "Head of Product",
                   "seniority": "Director", "employee_count": 60, "email": "dana@glitch-corp.test"},
        "builtwith_fail": True, "pagespeed": (62, 3000),
        "jobs": [], "verify": "valid"},
    "solo-freelance.test": {  # no email anywhere + no intent -> parks in ERROR
        "apollo": {"first_name": "Sam", "last_name": "Rivera", "job_title": "Consultant",
                   "seniority": "IC", "employee_count": 1, "email": None},
        "hunter_fail": True,
        "builtwith": ["Wix"], "pagespeed": (70, 2500),
        "jobs": [], "verify": "valid"},
}


# --------------------------------------------------------------------------- mock "API" steps
def _mock_apollo(domain):
    m = MOCK.get(domain, {})
    if m.get("apollo_fail"):
        raise EnrichError("Apollo 429 (rate limited)")
    a = m.get("apollo")
    if not a:
        raise EnrichError("Apollo: no record for domain")
    return dict(a)  # name, title, seniority, employee_count, email (may be None)


def _mock_hunter(domain):
    m = MOCK.get(domain, {})
    if m.get("hunter_fail"):
        raise EnrichError("Hunter: no pattern-matched address found")
    return m.get("hunter")  # email string or None


def _mock_builtwith(domain):
    m = MOCK.get(domain, {})
    if m.get("builtwith_fail"):
        raise EnrichError("BuiltWith 500 (upstream error)")
    return list(m.get("builtwith") or [])


def _mock_pagespeed(domain):
    m = MOCK.get(domain, {})
    mobile, lcp = m.get("pagespeed", (None, None))
    return {"mobile": mobile, "lcp": lcp}


def _mock_jobs(domain):
    return list(MOCK.get(domain, {}).get("jobs") or [])


def _mock_verify(email, domain):
    return MOCK.get(domain, {}).get("verify", "valid")


# --------------------------------------------------------------------------- the waterfall
def run_waterfall(company, lead):
    """Dispatch to the configured enrichment mode; return (data, errors)."""
    if config.ENRICH_MODE == "free":
        return enrich_free.run(company, lead)
    if config.ENRICH_MODE == "mock":
        return _run_mock(company)
    raise NotImplementedError(
        "ENRICH_MODE='real' needs paid API keys. Use 'mock' or 'free' in config.py.")


def _run_mock(company):
    """Return (data, errors) from the canned demo dataset (no network)."""
    domain = str(company.get("domain") or "").strip()
    data, errors = {}, []

    # 1) Apollo (primary): decision-maker + firmographics + maybe email
    try:
        a = _mock_apollo(domain)
        for k, v in a.items():
            if v is not None:
                data[k] = v
    except EnrichError as e:
        errors.append(f"apollo: {e}")

    # 2) Hunter (fallback): only if we still have no email
    if not data.get("email"):
        try:
            email = _mock_hunter(domain)
            if email:
                data["email"] = email
        except EnrichError as e:
            errors.append(f"hunter: {e}")

    # 3) BuiltWith: tech stack (independent of the email path)
    try:
        data["tech_stack"] = _mock_builtwith(domain)
    except EnrichError as e:
        errors.append(f"builtwith: {e}")

    # 4) PageSpeed: mobile Lighthouse + LCP
    try:
        lh = _mock_pagespeed(domain)
        data["lighthouse_mobile"], data["lighthouse_lcp_ms"] = lh["mobile"], lh["lcp"]
    except EnrichError as e:
        errors.append(f"pagespeed: {e}")

    # 5) Jobs / intent signals
    try:
        jobs = _mock_jobs(domain)
        if jobs:
            data["active_job_posts"] = jobs
            data["intent_strings"] = [j.get("title", "") for j in jobs]
    except EnrichError as e:
        errors.append(f"jobs: {e}")

    # 6) Email verification (trap/catch-all/invalid detection)
    if data.get("email"):
        try:
            data["email_validation_status"] = _mock_verify(data["email"], domain)
        except EnrichError as e:
            errors.append(f"verify: {e}")

    data["raw_payload"] = {"source": "mock", "errors": errors}
    return data, errors
