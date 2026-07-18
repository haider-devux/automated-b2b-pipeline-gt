"""
DANGER: wipe ALL pipeline data for a fresh start. Keeps the schema, enums, and triggers intact -
only the rows go. Deletes companies, leads, lead_events, discovery_candidates, suppression_list,
discovery_cells, outreach_log and email_events (the Max Plan telemetry tables).

  python reset_pipeline.py --yes
"""
import sys
import db


def main():
    if "--yes" not in sys.argv:
        print("This DELETES every lead / company / candidate / event / telemetry row. "
              "Re-run with --yes to confirm.")
        return
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            # outreach_log + email_events FK->leads (ON DELETE CASCADE), so CASCADE clears them too;
            # listed explicitly for clarity. suppression_list/discovery_cells are standalone.
            cur.execute("""TRUNCATE companies, leads, lead_events, discovery_candidates,
                           suppression_list, discovery_cells, outreach_log, email_events
                           RESTART IDENTITY CASCADE;""")
        conn.commit()
        print("Wiped. Fresh slate - schema, enums and triggers are untouched.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
