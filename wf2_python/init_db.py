"""
WF-2 one-time migration: add companies.description — a short, REAL blurb scraped from the company's
own homepage (meta/og description or <title>). WF-3's pitch uses it so the LLM describes the prospect
from their own words instead of guessing from a one-word category tag.

  python init_db.py     (idempotent — safe to re-run)
"""
import db


def main():
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("ALTER TABLE companies ADD COLUMN IF NOT EXISTS description text;")
        conn.commit()
        print("OK: companies.description ensured.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
