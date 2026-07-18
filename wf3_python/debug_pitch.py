"""One-off: show the RAW Ollama output + parsed result for one company, to diagnose pitch issues."""
import sys
import db, rules, pitch as pg
from psycopg2.extras import RealDictCursor

sys.stdout.reconfigure(encoding="utf-8")
name = sys.argv[1] if len(sys.argv) > 1 else "Souq Style"

conn = db.get_connection()
with conn.cursor(cursor_factory=RealDictCursor) as cur:
    cur.execute(
        """SELECT l.id, l.email, l.first_name AS full_name, c.legal_name AS company_name,
                  c.country, c.region, c.employee_count, c.tech_stack, c.active_job_posts,
                  c.lighthouse_mobile, c.niche AS company_desc
           FROM leads l JOIN companies c ON l.company_id = c.id
           WHERE c.legal_name = %s LIMIT 1;""",
        (name,),
    )
    lead = dict(cur.fetchone())
conn.close()

result = rules.qualify_and_segment(lead)
raw = pg._call_ollama(pg._build_prompt(lead, result))
print("=== RAW OLLAMA RESPONSE ===")
print(repr(raw))
print("\n=== PARSED ===")
print(pg._extract_first_json(raw))
