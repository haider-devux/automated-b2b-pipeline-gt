"""
Auto-approve: queue EVERY outreach-ready QUALIFIED lead for outreach, hands-off (no dashboard).

This is the fully-automated alternative to reviewing pitches on the dashboard Outreach tab. The send
gate still fully applies (db.fetch_ready only returns leads with a valid/unverified email, not
suppressed, not on the suppression list, not an opt-in-only country) - so nothing unsafe slips through.

  python auto_approve.py
"""
import db
import outreach


def main():
    conn = db.get_connection()
    try:
        leads = db.fetch_ready(conn)
        if not leads:
            print("No QUALIFIED leads ready to queue.")
            return
        n = 0
        for l in leads:
            db.queue_lead(conn, l, outreach.build_payload(l))
            conn.commit()
            n += 1
            print(f"  QUEUED  {l['legal_name']}")
        print(f"\nAuto-approved {n} lead(s) -> QUEUED_FOR_OUTREACH. Run wf4.py to (dry-run) send them.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
