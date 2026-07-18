"""
Deterministic qualification + segmentation — the code has final say.
This is the Python version of the n8n "JSON extractor" node.

WHY code and not the LLM: a small local model (qwen2.5:3b) proved unreliable at this
(it hallucinated triggers and copied prompt examples). These rules are simple and
mechanical, so code does them: instant, free, 100% consistent, easy to debug.
"""
import re

# job titles that signal "hiring developers" -> Segment B
DEV_ROLE_RE = re.compile(
    r"react|ios|android|mobile|software|engineer|developer|backend|frontend|full.?stack|\bml\b|devops|data",
    re.I,
)
# descriptions that signal a low-tech online store -> Segment C
ECOM_RE = re.compile(r"ecommerce|e-commerce|online store|d2c|retail|apparel|\bshop\b", re.I)


def qualify_and_segment(lead):
    """Return {qualified, segment, score, trigger, reason} decided purely from the data."""
    emp_raw = lead.get("employee_count")
    emp = int(emp_raw) if emp_raw not in (None, "") else None   # None = genuinely unknown (real discovery)
    jobs = lead.get("active_job_posts") or []
    job_text = str(jobs).lower()
    tech = [str(t).lower() for t in (lead.get("tech_stack") or [])]
    desc = str(lead.get("company_desc") or "").lower()
    lh = lead.get("lighthouse_mobile")

    # ---- HARD RULE 1: disqualify only on a KNOWN out-of-range size ----
    # Unknown size (None) is NOT a disqualifier — real discovery data often lacks it; the qualifier
    # handles nulls (blueprint) and lets the segment rules + later enrichment/human decide.
    if emp is not None and emp <= 1:
        return {"qualified": False, "segment": None, "score": 0.0,
                "trigger": "employee_count <= 1",
                "reason": f"Auto-disqualified: single-person firm (employee_count = {emp})."}
    if emp is not None and emp > 150:
        return {"qualified": False, "segment": None, "score": 0.0,
                "trigger": "employee_count > 150",
                "reason": f"Auto-disqualified: {emp} employees (over 150 - likely has internal IT)."}

    # ---- HARD RULE 2: assign segment from real data, in priority order ----
    hiring_devs = bool(DEV_ROLE_RE.search(job_text))
    is_ecom = any("shopify" in t or "woo" in t for t in tech) or bool(ECOM_RE.search(desc))

    if hiring_devs:
        first_job = ""
        if jobs and isinstance(jobs[0], dict):
            first_job = jobs[0].get("title", "")
        first_job = first_job or "a developer role"
        return {"qualified": True, "segment": "B", "score": 0.85,
                "trigger": f"hiring {first_job}",
                "reason": "Actively hiring developers - fit for a dedicated dev pod (segment B)."}

    if is_ecom:
        if lh is None:
            # no measured score (no PageSpeed key, or PSI failed) -> speak generally, cite NO number
            trigger = "online store, mobile experience"
            reason = "Low-tech e-commerce - improve the mobile store experience to recover revenue (segment C)."
        else:
            # real Google mobile-speed score exists (kept in `reason`/DB for our records + the dashboard),
            # but the NUMBER is deliberately kept out of the trigger/pitch — Lighthouse scores fluctuate, so
            # the email speaks generally and links the LIVE report for the exact current figure.
            trigger = "online store, slow on mobile (Google speed test)"
            reason = f"Low-tech e-commerce (Google mobile speed {lh}/100 at last check) - fix mobile performance to recover revenue (segment C)."
        return {"qualified": True, "segment": "C", "score": 0.85, "trigger": trigger, "reason": reason}

    size_txt = f"{emp}-person" if emp is not None else "local"
    return {"qualified": True, "segment": "A", "score": 0.85,
            "trigger": f"{size_txt} local/service business, weak web",
            "reason": "Mid-size local/service business with a weak web presence - digital transformation (segment A)."}
