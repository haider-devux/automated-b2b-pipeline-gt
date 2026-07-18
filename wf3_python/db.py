"""
Database layer — the ONLY place that talks to Postgres.
Mirrors the n8n nodes: "Execute a SQL query", "DB - Qualified", "Update DB - Disqualified".
Every write is parameterized (%(name)s) so quotes/newlines in AI text can't break or inject SQL.
"""
import os
import sys
import psycopg2
from psycopg2.extras import Json, RealDictCursor
import config

# Mirrors targets.VALID_REGIONS / the Postgres region_code enum. Duplicated here (not imported from
# wf1_python/targets.py) because the phases run as separate processes in separate folders — the
# shared-package refactor that would unify this is deliberately deferred. See Guide.md §5.
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
        host=config.DB["host"],
        port=config.DB["port"],
        dbname=config.DB["dbname"],
        user=config.DB["user"],
        password=config.DB["password"],
    )


def ensure_status_values():
    """Add the NEEDS_CONTACT lead status if missing (idempotent). Must run in autocommit — Postgres
    won't let a new enum value be added and used in the same transaction."""
    conn = get_connection()
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute("ALTER TYPE lead_status ADD VALUE IF NOT EXISTS 'NEEDS_CONTACT';")
    finally:
        conn.close()


FETCH_SQL = """
    SELECT l.id, l.email, l.email_validation_status, l.first_name AS full_name,
           c.legal_name AS company_name, c.country, c.region,
           c.employee_count, c.tech_stack, c.active_job_posts,
           c.lighthouse_mobile, c.niche AS company_desc, c.description AS site_description,
           c.city, c.domain, c.website_url
    FROM leads l
    JOIN companies c ON l.company_id = c.id
    WHERE l.status = 'ENRICHED'
    {region_clause}
    ORDER BY l.created_at ASC;
"""


def fetch_enriched_leads(conn):
    """Return every ENRICHED lead as a dict (tech_stack -> list, active_job_posts -> list).

    If GRANJUR_REGION is set to a valid region, only that region's leads are returned (regional
    isolation); otherwise every ENRICHED lead is returned exactly as before."""
    region = _active_region()
    sql = FETCH_SQL.format(region_clause="AND c.region = %(region)s::region_code" if region else "")
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, {"region": region})
        return [dict(row) for row in cur.fetchall()]


def write_qualified(conn, lead, result, pitch):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE leads SET
                status             = 'QUALIFIED',
                icp_segment        = %(icp_segment)s,
                qualify_score      = %(score)s,
                qualify_reason     = %(reason)s,
                qualify_trigger    = %(trigger)s,
                pitch_subject      = %(pitch_subject)s,
                pitch_body         = %(pitch_body)s,
                personalized_pitch = %(pitch_body)s,
                pitch_lang         = %(pitch_lang)s,
                pitch_localized    = %(pitch_localized)s,
                updated_at         = now()
            WHERE id = %(id)s;
            """,
            {
                "icp_segment": config.SEGMENT_DB.get(result["segment"]),
                "score": result["score"],
                "reason": result["reason"],
                "trigger": result["trigger"],
                "pitch_subject": pitch["pitch_subject"],
                "pitch_body": pitch["pitch_body"],
                "pitch_lang": pitch["pitch_lang"],
                "pitch_localized": pitch["pitch_localized"],
                "id": lead["id"],
            },
        )


def write_needs_contact(conn, lead, result):
    """Qualified as a good target, but has no cold-emailable contact (role/no email) — record the
    qualification but SKIP the expensive LLM pitch. Parks in NEEDS_CONTACT for a human to add a real
    named contact (Review -> Add form), which re-enters it as a sendable lead."""
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE leads SET
                status          = 'NEEDS_CONTACT',
                icp_segment     = %(icp_segment)s,
                qualify_score   = %(score)s,
                qualify_reason  = %(reason)s,
                qualify_trigger = %(trigger)s,
                updated_at      = now()
            WHERE id = %(id)s;
            """,
            {"icp_segment": config.SEGMENT_DB.get(result["segment"]),
             "score": result["score"], "reason": result["reason"],
             "trigger": result["trigger"], "id": lead["id"]},
        )


def write_disqualified(conn, lead, result):
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE leads SET
                status            = 'DISQUALIFIED',
                disqualify_reason = %(reason)s,
                updated_at        = now()
            WHERE id = %(id)s;
            """,
            {"reason": result["reason"], "id": lead["id"]},
        )


def log_event(conn, lead_id, to_status, detail):
    """Append-only audit row for the lead state machine (ENRICHED -> QUALIFIED/DISQUALIFIED)."""
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO lead_events (lead_id, from_status, to_status, workflow, detail)
            VALUES (%s, 'ENRICHED', %s, 'WF-3', %s);
            """,
            (lead_id, to_status, Json(detail)),
        )
