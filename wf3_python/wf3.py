"""
WF-3: AI Qualification & Translation (Python).

Reads every ENRICHED lead, qualifies + segments it deterministically (code has final say),
writes a localized pitch with the local Ollama LLM, and saves the result back to Postgres.
This mirrors the working n8n workflow. See WF3_GUIDE.md.

Run:  python wf3.py
"""
import db
import rules
import pitch as pitch_gen


def _has_sendable_contact(lead):
    """Can we actually cold-email this lead? Needs a real, non-role email. Role accounts (info@/sales@)
    and no-email leads are never cold-emailed, so pitching them wastes the (slow) LLM."""
    return bool(lead.get("email")) and lead.get("email_validation_status") in ("valid", "unverified")


def process_lead(conn, lead):
    # 1) qualify + segment in code (the "JSON extractor" logic)
    result = rules.qualify_and_segment(lead)

    # 2) route: disqualified -> just record it and move on (no LLM call)
    if not result["qualified"]:
        db.write_disqualified(conn, lead, result)
        db.log_event(conn, lead["id"], "DISQUALIFIED", {"reason": result["reason"]})
        return f"DISQUALIFIED  {lead['company_name']}"

    # 2b) qualified BUT no cold-emailable contact -> skip the slow LLM pitch; park for a human contact
    if not _has_sendable_contact(lead):
        db.write_needs_contact(conn, lead, result)
        db.log_event(conn, lead["id"], "NEEDS_CONTACT",
                     {"reason": "no cold-emailable contact (role/no email) - LLM pitch skipped",
                      "email_validation": lead.get("email_validation_status")})
        return f"NEEDS_CONTACT {lead['company_name']}  (no sendable email - pitch skipped)"

    # 3) qualified + contactable -> generate a localized pitch, then save
    pitch = pitch_gen.generate_pitch(lead, result)
    db.write_qualified(conn, lead, result, pitch)
    db.log_event(
        conn, lead["id"], "QUALIFIED",
        {"segment": result["segment"], "trigger": result["trigger"], "pitch_lang": pitch["pitch_lang"]},
    )
    return f"QUALIFIED     {lead['company_name']}  ->  segment {result['segment']}  ({pitch['pitch_lang']})"


def main():
    db.ensure_status_values()          # make sure the NEEDS_CONTACT status exists (idempotent)
    conn = db.get_connection()
    conn.autocommit = False
    try:
        leads = db.fetch_enriched_leads(conn)
        if not leads:
            print("No ENRICHED leads to process. (Re-arm the batch — see WF3_GUIDE.md.)")
            return

        print(f"Processing {len(leads)} ENRICHED lead(s)...\n")
        for lead in leads:
            try:
                summary = process_lead(conn, lead)
                conn.commit()        # one lead = one transaction, so a failure can't undo the others
                print("  " + summary)
            except Exception as e:   # noqa: BLE001 — keep going even if one lead fails
                conn.rollback()
                print(f"  ERROR  {lead.get('company_name')}: {e}")
        print("\nDone.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
