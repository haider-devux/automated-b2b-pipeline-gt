"""
WF-1: Discovery Ingestion (Python) — CSV importer.

Reads a CSV of businesses and inserts each as a DISCOVERED company + lead, updating the
discovery_cells depletion accounting. This is the always-free discovery path; free *real*
sources (OpenStreetMap Overpass, job-board feeds) can be added later as extra importers that
call the same db.insert_* functions. Integrates with the pipeline ONLY through leads.status.

Run:  python wf1.py                 (uses sample_leads.csv)
      python wf1.py path/to/my.csv  (your own file)

CSV columns (only legal_name + region + city + niche are required; the rest are optional):
  legal_name, domain, region, country, city, niche,
  website_url, phone, employee_count, tech_stack, first_name, last_name, job_title, email
tech_stack is semicolon-separated, e.g.  React;Node;AWS
"""
import csv
import os
import sys
import config
import db


def _clean(v):
    v = (v or "").strip()
    return v or None


def _region(v):
    r = (v or "").strip().upper()
    return r if r in config.VALID_REGIONS else "OTHER"


def _int(v):
    v = (v or "").strip()
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _tech(v):
    v = (v or "").strip()
    return [t.strip() for t in v.split(";") if t.strip()] if v else None


def load_rows(path):
    with open(path, newline="", encoding="utf-8-sig") as f:
        return list(csv.DictReader(f))


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else os.path.join(os.path.dirname(__file__), "sample_leads.csv")
    if not os.path.exists(path):
        print(f"CSV not found: {path}")
        return
    rows = load_rows(path)
    if not rows:
        print("CSV has no rows.")
        return

    conn = db.get_connection()
    conn.autocommit = False
    imported, skipped = 0, 0
    cells = {}   # (region, city, niche) -> {"seen": n, "new": n}
    try:
        for row in rows:
            legal_name = _clean(row.get("legal_name"))
            if not legal_name:
                continue
            region = _region(row.get("region"))
            city = _clean(row.get("city")) or "unknown"
            niche = _clean(row.get("niche")) or "unknown"
            domain = _clean(row.get("domain"))

            key = (region, city, niche)
            cell = cells.setdefault(key, {"seen": 0, "new": 0})
            cell["seen"] += 1

            # dedup on company identity (domain) — never create a duplicate lead
            if domain and db.company_id_by_domain(conn, domain):
                skipped += 1
                continue

            company = {
                "legal_name": legal_name, "domain": domain, "region": region,
                "country": _clean(row.get("country")), "city": city, "niche": niche,
                "website_url": _clean(row.get("website_url")), "phone": _clean(row.get("phone")),
                "employee_count": _int(row.get("employee_count")), "tech_stack": _tech(row.get("tech_stack")),
                "source": "csv", "cell": f"{city}/{niche}",
            }
            company_id = db.insert_company(conn, company)
            lead_id = db.insert_lead(conn, company_id, {
                "first_name": _clean(row.get("first_name")), "last_name": _clean(row.get("last_name")),
                "job_title": _clean(row.get("job_title")), "email": _clean(row.get("email")),
            })
            db.log_event(conn, lead_id)
            imported += 1
            cell["new"] += 1
            print(f"  DISCOVERED  {legal_name}  ({region} - {city} - {niche})")

        # update depletion accounting per cell
        for (region, city, niche), c in cells.items():
            db.upsert_cell(conn, region, city, niche, c["seen"], c["new"])

        conn.commit()
        print(f"\nImported {imported} new, skipped {skipped} duplicate(s), across {len(cells)} cell(s). "
              f"Next:  run WF-2 to enrich them.")
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        print(f"ERROR — rolled back, nothing imported: {e}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
