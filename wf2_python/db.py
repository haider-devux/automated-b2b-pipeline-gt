"""
WF-2 database layer — the only place that talks to Postgres.

Mirrors the blueprint's Phase-2 nodes:
  - atomic claim (DISCOVERED -> ENRICHING) with FOR UPDATE SKIP LOCKED (queue-safe, no double-processing)
  - write enriched firmographics + contact + email validation, flip to ENRICHED
  - park a lead in ERROR (with a message) when it can't be enriched — never blocks siblings
Every write is parameterized (%(name)s) so scraped/AI text can't break or inject SQL.
"""
import os
import sys
import psycopg2
from psycopg2.extras import Json, RealDictCursor
import config

# Mirrors targets.VALID_REGIONS / the Postgres region_code enum. Duplicated per phase on purpose —
# the shared-package refactor that would unify config/db is deferred (Guide.md §5).
_VALID_REGIONS = {"US", "EU", "UK", "GCC", "CN", "AU", "OTHER"}


def _active_region():
    """Region this run is limited to, or None for all regions (today's default). Unknown value -> None.

    Resolution: a  --region XX  flag (standalone run) wins, else the GRANJUR_REGION env var
    (set by run_pipeline.py so every phase inherits the same isolation)."""
    val = None
    if "--region" in sys.argv:
        i = sys.argv.index("--region")
        if i + 1 < len(sys.argv):
            val = sys.argv[i + 1]
    r = (val or os.getenv("GRANJUR_REGION") or "").strip().upper()
    return r if r in _VALID_REGIONS else None


def get_connection():
    return psycopg2.connect(
        host=config.DB["host"], port=config.DB["port"], dbname=config.DB["dbname"],
        user=config.DB["user"], password=config.DB["password"],
    )


def claim_discovered(conn, limit):
    """Atomically move up to `limit` DISCOVERED leads to ENRICHING and return them.

    FOR UPDATE SKIP LOCKED means two workers never grab the same lead, and a crash mid-run
    leaves a lead in ENRICHING (recoverable) rather than lost. If GRANJUR_REGION is set, only that
    region's leads are claimed (the join locks ONLY the leads rows via FOR UPDATE OF l).
    """
    region = _active_region()
    region_clause = "AND c.region = %(region)s::region_code" if region else ""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(
            f"""
            WITH claimed AS (
                SELECT l.id FROM leads l
                JOIN companies c ON c.id = l.company_id
                WHERE l.status = 'DISCOVERED'
                  {region_clause}
                ORDER BY l.created_at
                FOR UPDATE OF l SKIP LOCKED
                LIMIT %(limit)s
            )
            UPDATE leads l
               SET status = 'ENRICHING', updated_at = now()
              FROM claimed
             WHERE l.id = claimed.id
            RETURNING l.id, l.company_id, l.email;
            """,
            {"limit": limit, "region": region},
        )
        return [dict(r) for r in cur.fetchall()]


def get_company(conn, company_id):
    """Fetch the company row (context the enrichers need: domain, niche, region...)."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT * FROM companies WHERE id = %s;", (company_id,))
        row = cur.fetchone()
        return dict(row) if row else None


def write_enriched(conn, lead, company_id, data):
    """Persist enrichment: firmographics on the company, contact + validation on the lead, -> ENRICHED."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE companies SET
                employee_count    = COALESCE(%(employee_count)s, employee_count),
                description       = COALESCE(%(description)s, description),
                tech_stack        = COALESCE(%(tech_stack)s, tech_stack),
                active_job_posts  = COALESCE(%(active_job_posts)s, active_job_posts),
                lighthouse_mobile = COALESCE(%(lighthouse_mobile)s, lighthouse_mobile),
                lighthouse_lcp_ms = COALESCE(%(lighthouse_lcp_ms)s, lighthouse_lcp_ms),
                intent_strings    = COALESCE(%(intent_strings)s, intent_strings),
                raw_payload       = %(raw_payload)s,
                last_verified_at  = now(),
                updated_at        = now()
            WHERE id = %(company_id)s;
            """,
            {
                "employee_count": data.get("employee_count"),
                "description": data.get("description"),
                "tech_stack": data.get("tech_stack"),
                "active_job_posts": Json(data["active_job_posts"]) if data.get("active_job_posts") is not None else None,
                "lighthouse_mobile": data.get("lighthouse_mobile"),
                "lighthouse_lcp_ms": data.get("lighthouse_lcp_ms"),
                "intent_strings": data.get("intent_strings"),
                "raw_payload": Json(data.get("raw_payload") or {}),
                "company_id": company_id,
            },
        )
        cur.execute(
            """
            UPDATE leads SET
                first_name              = COALESCE(%(first_name)s, first_name),
                last_name               = COALESCE(%(last_name)s, last_name),
                job_title               = COALESCE(%(job_title)s, job_title),
                seniority               = COALESCE(%(seniority)s, seniority),
                email                   = COALESCE(%(email)s, email),
                email_validation_status = %(email_validation_status)s,
                consent_basis           = 'LEGITIMATE_INTEREST',
                last_error_message      = NULL,
                status                  = 'ENRICHED',
                updated_at              = now()
            WHERE id = %(id)s;
            """,
            {
                "first_name": data.get("first_name"),
                "last_name": data.get("last_name"),
                "job_title": data.get("job_title"),
                "seniority": data.get("seniority"),
                "email": data.get("email"),
                "email_validation_status": data.get("email_validation_status"),
                "id": lead["id"],
            },
        )


def park_error(conn, lead, message):
    """Enrichment couldn't produce a usable lead — park it in ERROR with a reason (retry later)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE leads SET status = 'ERROR', last_error_message = %(msg)s, updated_at = now()
            WHERE id = %(id)s;
            """,
            {"msg": message[:500], "id": lead["id"]},
        )


def log_event(conn, lead_id, from_status, to_status, detail):
    """Append-only audit row (drives the dashboard activity feed)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO lead_events (lead_id, from_status, to_status, workflow, detail)
            VALUES (%s, %s, %s, 'WF-2', %s);
            """,
            (lead_id, from_status, to_status, Json(detail)),
        )
