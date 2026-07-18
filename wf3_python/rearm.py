"""
Re-arm mock leads back to ENRICHED so WF-3 can process them again.

  python rearm.py                              # re-arm ALL mock leads
  python rearm.py "Souq Style" "Nimbus AI Labs"  # re-arm only these companies
"""
import sys
import db


def main():
    names = sys.argv[1:]
    conn = db.get_connection()
    try:
        with conn.cursor() as cur:
            if names:
                cur.execute(
                    """UPDATE leads SET status = 'ENRICHED', updated_at = now()
                       WHERE company_id IN (
                           SELECT id FROM companies WHERE source = 'mock' AND legal_name = ANY(%s)
                       );""",
                    (names,),
                )
            else:
                cur.execute(
                    """UPDATE leads SET status = 'ENRICHED', updated_at = now()
                       WHERE company_id IN (SELECT id FROM companies WHERE source = 'mock');"""
                )
            print(f"Re-armed {cur.rowcount} lead(s) to ENRICHED.")
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    main()
