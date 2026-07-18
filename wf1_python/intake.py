"""
Auto-intake: evaluate a discovery candidate against targets.py and act on it automatically.

This is what makes discovery hands-off — collectors (OSM, jobs) and the manual Add form all funnel
through here. A candidate that meets the requirements is auto-APPROVED straight into the pipeline
(a DISCOVERED lead); one that clearly fails is auto-REJECTED; only a genuinely ambiguous one is left
PENDING for a human. Every outcome is recorded in discovery_candidates for the /review page.
"""
import db
import targets


def evaluate(c):
    """Return (verdict, reason) where verdict is 'approve' | 'reject' | 'review'."""
    if targets.is_blocked(c.get("legal_name")):
        return "reject", "known mega-brand / national chain (blocklist)"

    region = (c.get("region") or "OTHER").upper()
    if targets.TARGET_REGIONS and region not in targets.TARGET_REGIONS:
        return "reject", f"region {region} not targeted"

    emp = c.get("employee_count")
    if emp is not None and emp < targets.EMPLOYEE_MIN:
        return "reject", f"{emp} employees (too small)"
    if emp is not None and emp > targets.EMPLOYEE_MAX:
        return "reject", f"{emp} employees (enterprise — likely internal IT)"

    niche = (c.get("niche") or "").lower()
    if targets.TARGET_NICHES and niche and niche not in targets.TARGET_NICHES:
        return "reject", f"niche '{niche}' not targeted"

    contactable = c.get("domain") or c.get("website_url") or c.get("email")
    intent = c.get("active_job_posts")
    if targets.REQUIRE_CONTACTABLE and not contactable and not intent:
        return "review", "no domain/website/email and no intent — needs a human"

    return "approve", "meets requirements"


def _company_row(c, source):
    return {
        "legal_name": c.get("legal_name"), "domain": c.get("domain"),
        "region": (c.get("region") or "OTHER").upper(), "country": c.get("country"),
        "city": c.get("city"), "niche": c.get("niche"),
        "website_url": c.get("website_url"), "phone": c.get("phone"),
        "employee_count": c.get("employee_count"), "tech_stack": c.get("tech_stack"),
        "active_job_posts": c.get("active_job_posts"),
        "source": source, "cell": f"{c.get('city') or '?'}/{c.get('niche') or '?'}",
    }


def _lead_row(c):
    return {"first_name": c.get("first_name"), "last_name": c.get("last_name"),
            "job_title": c.get("job_title"), "email": c.get("email")}


def submit(conn, cand, source):
    """Push one candidate through intake. Returns the verdict; records the outcome. Commits per candidate."""
    domain = cand.get("domain")
    if domain and db.company_id_by_domain(conn, domain):
        db.record_candidate(conn, cand, source, "REJECTED", "duplicate (already in pipeline)")
        conn.commit()
        return "duplicate"

    verdict, reason = evaluate(cand)
    if verdict == "approve":
        company_id = db.insert_company(conn, _company_row(cand, source))
        lead_id = db.insert_lead(conn, company_id, _lead_row(cand))
        db.log_event(conn, lead_id)
        db.record_candidate(conn, cand, source, "APPROVED", reason, lead_id)
    elif verdict == "reject":
        db.record_candidate(conn, cand, source, "REJECTED", reason)
    else:
        db.record_candidate(conn, cand, source, "PENDING", reason)
    conn.commit()
    return verdict
