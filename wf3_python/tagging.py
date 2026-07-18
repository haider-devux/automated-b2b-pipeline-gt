"""
Lead tagging (Max Plan · Phase 5 — tag-based CRM hub).

Derives a lead's CRM tags from its firmographics/signals — the same families backfilled in Phase 1:
region (#GCC…), segment (#Legacy/#Startup/#Ecom), tech (#Shopify/#Woo), signals (#SlowMobile/#Hiring),
and language (#Arabic/#Chinese). Pure + deterministic so the dashboard can compute tags live (always
fresh) and a startup pass keeps the stored leads.tags column in sync for fast server-side filtering.
"""
import db

_SEG_TAG = {"A": "#Legacy", "B": "#Startup", "C": "#Ecom"}
_LANG_TAG = {"ar": "#Arabic", "zh": "#Chinese"}


def compute_tags(row):
    """row keys: region, icp_segment, pitch_lang, tech_stack(list), lighthouse_mobile, active_job_posts(list)."""
    tags = []
    region = (row.get("region") or "").upper()
    if region and region != "OTHER":
        tags.append(f"#{region}")

    seg = (row.get("icp_segment") or "")[:1].upper()
    if seg in _SEG_TAG:
        tags.append(_SEG_TAG[seg])

    tech = [str(t).lower() for t in (row.get("tech_stack") or [])]
    if any(t.startswith("shopify") for t in tech):
        tags.append("#Shopify")
    if any(t.startswith("woo") for t in tech):
        tags.append("#Woo")

    lh = row.get("lighthouse_mobile")
    if lh is not None and lh < 50:
        tags.append("#SlowMobile")

    jobs = row.get("active_job_posts")
    if isinstance(jobs, (list, tuple)) and len(jobs) > 0:
        tags.append("#Hiring")

    lang = (row.get("pitch_lang") or "").lower()
    if lang in _LANG_TAG:
        tags.append(_LANG_TAG[lang])

    # de-dup, preserve order
    seen, out = set(), []
    for t in tags:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def retag_all():
    """Recompute and store leads.tags for every lead (idempotent). Safe to run at startup."""
    conn = db.get_connection()
    try:
        from psycopg2.extras import RealDictCursor
        with conn.cursor(cursor_factory=RealDictCursor) as cur:
            cur.execute("""SELECT l.id, l.icp_segment, l.pitch_lang, c.region, c.tech_stack,
                                  c.lighthouse_mobile, c.active_job_posts
                           FROM leads l JOIN companies c ON c.id = l.company_id""")
            rows = cur.fetchall()
        with conn.cursor() as cur:
            for r in rows:
                cur.execute("UPDATE leads SET tags = %s WHERE id = %s", (compute_tags(r), r["id"]))
        conn.commit()
        return len(rows)
    finally:
        conn.close()


if __name__ == "__main__":
    print(f"retagged {retag_all()} lead(s).")
