r"""
Apply the Max Plan · Phase 6 migration (email_events + funnel timestamps). Idempotent.

    ..\wf3_python\.venv\Scripts\python.exe database\migrate_phase6.py
"""
import os
import sys

import psycopg2

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"),
                override=True)
except Exception:                      # noqa: BLE001
    pass

DB = {"host": os.getenv("DB_HOST", "localhost"), "port": int(os.getenv("DB_PORT", "5432")),
      "dbname": os.getenv("DB_NAME", "granjur_pipeline"), "user": os.getenv("DB_USER", "postgres"),
      "password": os.getenv("DB_PASSWORD", "")}   # set DB_PASSWORD in the project-root .env
SQL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "phase6_analytics_migration.sql")


def main():
    sql = open(SQL_FILE, "r", encoding="utf-8").read()
    conn = psycopg2.connect(**DB)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            cur.execute("""SELECT
                (SELECT count(*) FROM information_schema.tables WHERE table_name='email_events'),
                (SELECT count(*) FROM information_schema.columns
                   WHERE table_name='leads' AND column_name IN ('first_open_at','first_click_at','bounced_at'));""")
            r = cur.fetchone()
        print("Phase 6 migration applied.")
        print(f"  email_events table ......... {'OK' if r[0] else 'MISSING'}")
        print(f"  leads funnel columns (3) ... {r[1]}/3")
    except Exception as e:             # noqa: BLE001
        print(f"MIGRATION FAILED: {e}", file=sys.stderr); sys.exit(1)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
