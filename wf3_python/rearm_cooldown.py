"""
Re-arm COOLDOWN leads back into the pipeline.

A lead lands in COOLDOWN when a human clicks "Skip" on its pitch (Outreach tab):
it is set aside and NOT emailed. This module gives COOLDOWN a timer + a way back:

  * Each skipped lead gets a `cooldown_until` timestamp = now + COOLDOWN_DAYS.
  * Once that time passes, the lead automatically returns to QUALIFIED (so it shows
    up on the Outreach tab again for a fresh review). Nothing is auto-sent.

This is called two ways:
  * Automatically by the dashboard on every page load (auto-heal) and by the manual
    "Re-arm" button (which sets cooldown_until = now() so it returns immediately).
  * Standalone:  python rearm_cooldown.py   (safe to run from run_pipeline / scheduler)

Idempotent: creating the column and back-filling only fill gaps; re-arming only
touches leads whose timer is up.
"""
import db

COOLDOWN_DAYS = 3  # how long a skipped lead rests before it flows back to QUALIFIED


def ensure_column():
    """Add leads.cooldown_until if missing, and give existing COOLDOWN leads a timer."""
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "ALTER TABLE leads ADD COLUMN IF NOT EXISTS cooldown_until TIMESTAMPTZ;")
            # Back-fill leads already sitting in COOLDOWN so this feature applies to them
            # too: base the timer on when they actually entered COOLDOWN (the audit event),
            # falling back to updated_at if there is no event on record.
            cur.execute(
                """UPDATE leads l
                     SET cooldown_until = e.at + make_interval(days => %s)
                    FROM (SELECT lead_id, max(at) AS at FROM lead_events
                          WHERE to_status = 'COOLDOWN' GROUP BY lead_id) e
                   WHERE l.id = e.lead_id
                     AND l.status = 'COOLDOWN' AND l.cooldown_until IS NULL;""",
                (COOLDOWN_DAYS,))
            cur.execute(
                """UPDATE leads SET cooldown_until = updated_at + make_interval(days => %s)
                    WHERE status = 'COOLDOWN' AND cooldown_until IS NULL;""",
                (COOLDOWN_DAYS,))
        conn.commit()
    finally:
        conn.close()


def rearm():
    """Move every COOLDOWN lead whose timer is up back to QUALIFIED. Returns the count."""
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """UPDATE leads
                     SET status = 'QUALIFIED', cooldown_until = NULL, updated_at = now()
                   WHERE status = 'COOLDOWN'
                     AND cooldown_until IS NOT NULL AND cooldown_until <= now()
                   RETURNING id;""")
            ids = [r[0] for r in cur.fetchall()]
            for lead_id in ids:
                cur.execute(
                    """INSERT INTO lead_events (lead_id, from_status, to_status, workflow, detail)
                       VALUES (%s, 'COOLDOWN', 'QUALIFIED', 'rearm',
                               '{"reason": "cooldown elapsed - back to review"}');""",
                    (lead_id,))
        conn.commit()
        return len(ids)
    finally:
        conn.close()


def main():
    ensure_column()
    n = rearm()
    print(f"Re-armed {n} lead(s) from COOLDOWN back to QUALIFIED.")


if __name__ == "__main__":
    main()
