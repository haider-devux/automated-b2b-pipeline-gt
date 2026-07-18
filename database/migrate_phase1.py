r"""
Apply the Max Plan · Phase 1 migration (timezone + tags + outreach_log).

Idempotent — run it as many times as you like. Uses the same DB env/defaults
as the rest of the stack (DB_HOST/DB_PORT/DB_NAME/DB_USER/DB_PASSWORD, with a
project-root .env auto-loaded if python-dotenv is installed).

    ..\wf3_python\.venv\Scripts\python.exe database\migrate_phase1.py
"""
import os
import sys

import psycopg2

try:                                   # match the rest of the stack: auto-load project-root .env
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
                override=True)
except Exception:                      # noqa: BLE001 — dotenv is optional; env vars still work
    pass

DB = {
    "host": os.getenv("DB_HOST", "localhost"),
    "port": int(os.getenv("DB_PORT", "5432")),
    "dbname": os.getenv("DB_NAME", "granjur_pipeline"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),   # set DB_PASSWORD in the project-root .env
}
SQL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "phase1_maxplan_migration.sql")


def main():
    with open(SQL_FILE, "r", encoding="utf-8") as f:
        sql = f.read()

    conn = psycopg2.connect(**DB)
    conn.autocommit = True             # DDL commits itself; keeps the migration atomic-per-statement
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            # quick proof the columns/table now exist
            cur.execute("""
                SELECT
                  (SELECT count(*) FROM information_schema.columns
                     WHERE table_name='companies' AND column_name='timezone') AS has_timezone,
                  (SELECT count(*) FROM information_schema.columns
                     WHERE table_name='leads' AND column_name='tags') AS has_tags,
                  (SELECT count(*) FROM information_schema.tables
                     WHERE table_name='outreach_log') AS has_outreach_log,
                  (SELECT count(*) FROM companies WHERE timezone IS NOT NULL) AS tz_filled,
                  (SELECT count(*) FROM leads WHERE tags <> '{}') AS leads_tagged;
            """)
            r = cur.fetchone()
        print("Phase 1 migration applied.")
        print(f"  companies.timezone column ... {'OK' if r[0] else 'MISSING'}")
        print(f"  leads.tags column ........... {'OK' if r[1] else 'MISSING'}")
        print(f"  outreach_log table .......... {'OK' if r[2] else 'MISSING'}")
        print(f"  companies with timezone ..... {r[3]}")
        print(f"  leads with >=1 tag .......... {r[4]}")
    except Exception as e:             # noqa: BLE001 — surface the real DB error, don't swallow it
        print(f"MIGRATION FAILED: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
