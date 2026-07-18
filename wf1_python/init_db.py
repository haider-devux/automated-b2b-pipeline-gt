"""
Apply WF-1 schema additions (idempotent). Run once:

  python init_db.py
"""
import os
import db


def main():
    sql_path = os.path.join(os.path.dirname(__file__), "candidates_schema.sql")
    with open(sql_path, encoding="utf-8") as f:
        sql = f.read()
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
        conn.commit()
        print("Applied candidates_schema.sql (discovery_candidates ready).")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
