"""Apply WF-4 schema additions (idempotent). Run once:  python init_db.py"""
import os
import db


def main():
    with open(os.path.join(os.path.dirname(__file__), "outreach_schema.sql"), encoding="utf-8") as f:
        sql = f.read()
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        print("Applied outreach_schema.sql (outreach columns ready).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
