"""
WF-2: Enrichment Waterfall (Python).

Claims DISCOVERED leads, runs a fault-tolerant enrichment waterfall on each, and flips it to
ENRICHED (if it has a verified email OR a strong intent signal) or parks it in ERROR otherwise.
Integrates with the rest of the pipeline ONLY through the leads.status column. See WF2_GUIDE.md.

Run:  python wf2.py
"""
import time

import config
import db
import enrich


def _decide(data, company):
    """Blueprint partial-success rule: keep a lead if it's actually reachable/interesting.

    A usable email is one that's 'valid' (confirmed) OR 'unverified' (present but not confirmable
    for free). Intent counts whether it was just enriched OR already on the company (e.g. a job
    post from the WF-1 job collector). Only a lead with neither gets parked.
    """
    has_email = data.get("email_validation_status") in ("valid", "unverified", "role")
    has_intent = bool(data.get("active_job_posts") or (company or {}).get("active_job_posts"))
    return has_email or has_intent, has_email, has_intent


def process_lead(conn, lead):
    company = db.get_company(conn, lead["company_id"])
    if not company:
        db.park_error(conn, lead, "No company row for lead")
        db.log_event(conn, lead["id"], "ENRICHING", "ERROR", {"reason": "missing company"})
        return f"ERROR      (missing company)  {lead['id']}"

    data, errors = enrich.run_waterfall(company, lead)
    keep, has_email, has_intent = _decide(data, company)
    name = company["legal_name"]

    def _short(errs):  # keep console output readable — full detail lives in raw_payload
        return "; ".join((e[:55] + "...") if len(e) > 55 else e for e in errs)

    if keep:
        db.write_enriched(conn, lead, company["id"], data)
        db.log_event(conn, lead["id"], "ENRICHING", "ENRICHED",
                     {"email": has_email, "intent": has_intent, "errors": errors})
        added = [k.split("_")[0] for k in ("tech_stack", "lighthouse_mobile") if data.get(k) is not None]
        note = f"  [caught: {_short(errors)}]" if errors else ""
        return (f"ENRICHED   {name}  (email={data.get('email_validation_status') or '-'}, "
                f"added={'+'.join(added) or 'none, kept CSV data'}){note}")

    reason = "No usable email and no intent signal."
    db.park_error(conn, lead, f"{reason} {_short(errors)}".strip())
    db.log_event(conn, lead["id"], "ENRICHING", "ERROR", {"errors": errors, "reason": reason})
    return f"ERROR      {name}  ({reason})"


def main():
    conn = db.get_connection()
    conn.autocommit = False
    try:
        leads = db.claim_discovered(conn, config.ENRICH_BATCH_SIZE)
        conn.commit()  # commit the claim so the ENRICHING state is durable before we work
        if not leads:
            print("No DISCOVERED leads to enrich. (Seed some — see WF2_GUIDE.md.)")
            return

        print(f"Claimed {len(leads)} lead(s) -> ENRICHING. Enriching...\n")
        for i, lead in enumerate(leads):
            if i:
                time.sleep(config.FREE_FETCH_DELAY)   # polite pacing between leads (different hosts)
            try:
                summary = process_lead(conn, lead)
                conn.commit()   # one lead = one transaction; a failure can't undo the others
                print("  " + summary)
            except Exception as e:  # noqa: BLE001 — never let one lead kill the batch
                conn.rollback()
                try:
                    db.park_error(conn, lead, f"Unexpected: {e}")
                    conn.commit()
                except Exception:
                    conn.rollback()
                print(f"  ERROR      {lead.get('id')}: {e}")
        print("\nDone.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
