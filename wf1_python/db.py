"""
WF-1 database layer. Inserts DISCOVERED companies + leads, and keeps discovery_cells
depletion accounting up to date. Dedups on company identity (domain) so re-runs never
create duplicate leads — the blueprint's idempotency rule.
"""
import psycopg2
from psycopg2.extras import RealDictCursor, Json
import config


def _tech_text(tech):
    """Normalize a tech_stack value to the TEXT the candidates table stores."""
    if not tech:
        return None
    return ";".join(tech) if isinstance(tech, (list, tuple)) else str(tech)


def get_connection():
    return psycopg2.connect(
        host=config.DB["host"], port=config.DB["port"], dbname=config.DB["dbname"],
        user=config.DB["user"], password=config.DB["password"],
    )


def company_id_by_domain(conn, domain):
    """Return an existing company id for this domain, or None. (Dedup key = domain.)"""
    if not domain:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT id FROM companies WHERE domain = %s LIMIT 1;", (domain,))
        row = cur.fetchone()
        return row[0] if row else None


def insert_company(conn, c):
    """Insert a discovered company (identity + whatever firmographics discovery already had)."""
    jobs = c.get("active_job_posts")
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO companies
                (legal_name, domain, region, country, city, niche,
                 website_url, phone, employee_count, tech_stack, active_job_posts, source, discovery_cell)
            VALUES
                (%(legal_name)s, %(domain)s, %(region)s, %(country)s, %(city)s, %(niche)s,
                 %(website_url)s, %(phone)s, %(employee_count)s, %(tech_stack)s, %(active_job_posts)s,
                 %(source)s, %(cell)s)
            RETURNING id;
            """,
            {**c, "active_job_posts": Json(jobs) if jobs else None},
        )
        return cur.fetchone()[0]


def insert_lead(conn, company_id, lead):
    """Insert a DISCOVERED lead (contact fields optional — WF-2 fills the rest)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO leads
                (company_id, status, consent_basis, first_name, last_name, job_title, email)
            VALUES
                (%(company_id)s, 'DISCOVERED', 'NONE',
                 %(first_name)s, %(last_name)s, %(job_title)s, %(email)s)
            RETURNING id;
            """,
            {"company_id": company_id, **lead},
        )
        return cur.fetchone()[0]


def get_cached_bbox(conn, region, city):
    """Return a cached (south, west, north, east) bbox for this city, or None on a miss.
    Lets bot-osm geocode each city with Nominatim ONCE instead of every run (the real long-run
    Nominatim-ban risk). Table created by database/phase7_governor_migration.sql."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT south, west, north, east FROM city_bbox WHERE region=%s AND city=%s;",
            (region, city))
        row = cur.fetchone()
    if not row or any(v is None for v in row):
        return None
    return (float(row[0]), float(row[1]), float(row[2]), float(row[3]))


def cache_bbox(conn, region, city, bbox):
    """Persist a freshly geocoded (south, west, north, east) bbox so we never re-geocode this city.
    Idempotent upsert; commits so the cache survives regardless of the caller's transaction."""
    s, w, n, e = bbox
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO city_bbox (region, city, south, west, north, east)
                   VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (region, city)
                   DO UPDATE SET south=EXCLUDED.south, west=EXCLUDED.west,
                                 north=EXCLUDED.north, east=EXCLUDED.east, geocoded_at=now();""",
            (region, city, s, w, n, e))
    conn.commit()


def upsert_cell(conn, region, city, niche, seen, new):
    """Update depletion accounting for a (region, city, niche) cell for this run."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            "SELECT id, total_seen FROM discovery_cells WHERE region=%s AND city=%s AND niche=%s;",
            (region, city, niche))
        row = cur.fetchone()
        if row:
            total = (row["total_seen"] or 0) + seen
            saturation = (new / total) if total else 0
            cur.execute(
                """UPDATE discovery_cells SET total_seen=%s, new_last_run=%s, saturation=%s,
                       depleted=%s, last_run_at=now() WHERE id=%s;""",
                (total, new, round(saturation, 4), saturation < config.DEPLETION_THRESHOLD, row["id"]))
        else:
            saturation = (new / seen) if seen else 0
            cur.execute(
                """INSERT INTO discovery_cells (region, city, niche, total_seen, new_last_run,
                       saturation, depleted, last_run_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s, now());""",
                (region, city, niche, seen, new, round(saturation, 4),
                 saturation < config.DEPLETION_THRESHOLD))


def log_event(conn, lead_id):
    """Birth of a lead: (no prior status) -> DISCOVERED, workflow WF-1."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO lead_events (lead_id, from_status, to_status, workflow) "
            "VALUES (%s, NULL, 'DISCOVERED', 'WF-1');",
            (lead_id,))


def record_candidate(conn, cand, source, status, reason=None, lead_id=None):
    """Log a discovery candidate + its intake verdict (drives the /review page)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO discovery_candidates
                (legal_name, domain, region, country, city, niche, website_url, phone,
                 employee_count, tech_stack, first_name, last_name, job_title, email,
                 source, signal, raw, status, review_note, lead_id, reviewed_at)
            VALUES
                (%(legal_name)s, %(domain)s, %(region)s, %(country)s, %(city)s, %(niche)s,
                 %(website_url)s, %(phone)s, %(employee_count)s, %(tech_stack)s,
                 %(first_name)s, %(last_name)s, %(job_title)s, %(email)s,
                 %(source)s, %(signal)s, %(raw)s, %(status)s, %(reason)s, %(lead_id)s,
                 CASE WHEN %(status)s = 'PENDING' THEN NULL ELSE now() END);
            """,
            {
                "legal_name": cand.get("legal_name"), "domain": cand.get("domain"),
                "region": (cand.get("region") or "OTHER"), "country": cand.get("country"),
                "city": cand.get("city"), "niche": cand.get("niche"),
                "website_url": cand.get("website_url"), "phone": cand.get("phone"),
                "employee_count": cand.get("employee_count"), "tech_stack": _tech_text(cand.get("tech_stack")),
                "first_name": cand.get("first_name"), "last_name": cand.get("last_name"),
                "job_title": cand.get("job_title"), "email": cand.get("email"),
                "source": source, "signal": cand.get("signal"), "raw": Json(cand.get("raw") or {}),
                "status": status, "reason": reason, "lead_id": lead_id,
            },
        )
