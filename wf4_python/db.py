"""
WF-4 database layer: find outreach-ready leads, queue them, simulate the send (dry-run), and apply
inbound events (reply/booking/bounce/unsubscribe). Every send-gate check lives here so nothing goes
out to a suppressed or unverified address.
"""
import os
import sys
import psycopg2
from psycopg2.extras import RealDictCursor, Json
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


# leads that passed qualification and are safe to contact (the send gate)
_READY_SQL = """
    SELECT l.id, l.email, l.first_name, l.icp_segment, l.qualify_trigger,
           l.pitch_subject, l.pitch_body, l.pitch_lang, c.legal_name, c.region
    FROM leads l JOIN companies c ON l.company_id = c.id
    WHERE l.status = %(status)s
      AND l.pitch_body IS NOT NULL
      AND l.email IS NOT NULL
      AND l.email_validation_status IN ('valid','unverified')       -- never role/invalid accounts
      AND (c.country IS NULL OR c.country NOT IN ('DE','AT'))        -- opt-in-only regions -> manual
      AND l.suppressed_at IS NULL
      AND NOT EXISTS (SELECT 1 FROM suppression_list s
                      WHERE s.target_value::text = l.email::text)
      {region_clause}
    ORDER BY l.updated_at DESC
    LIMIT %(limit)s;
"""


def _batch(conn, status, limit):
    """Shared body for fetch_ready / fetch_queued, with optional GRANJUR_REGION isolation."""
    region = _active_region()
    sql = _READY_SQL.format(region_clause="AND c.region = %(region)s::region_code" if region else "")
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, {"status": status, "limit": limit, "region": region})
        return [dict(r) for r in cur.fetchall()]


def fetch_ready(conn, limit=500):
    """QUALIFIED leads a human can review + approve for outreach (region-isolated if GRANJUR_REGION set)."""
    return _batch(conn, "QUALIFIED", limit)


def fetch_queued(conn, limit=500):
    """Leads a human already approved (QUEUED_FOR_OUTREACH) — ready for the (dry-run) sender."""
    return _batch(conn, "QUEUED_FOR_OUTREACH", limit)


def one_ready(conn, lead_id):
    # Single explicit lead: no region isolation (the clause placeholder is left empty).
    sql = _READY_SQL.format(region_clause="").replace("l.status = %(status)s", "l.id = %(id)s")
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, {"id": lead_id, "limit": 1})
        row = cur.fetchone()
        return dict(row) if row else None


def queue_lead(conn, lead, payload):
    """Human approved -> QUALIFIED → QUEUED_FOR_OUTREACH; store the routing + the payload (audit)."""
    with conn.cursor() as cur:
        cur.execute(
            """UPDATE leads SET status='QUEUED_FOR_OUTREACH', outreach_provider=%s,
                   campaign_id=%s, sending_domain=%s, updated_at=now() WHERE id=%s;""",
            (payload["provider"], payload["campaign_id"], payload["_meta"]["sending_domain"], lead["id"]))
        _event(cur, lead["id"], "QUALIFIED", "QUEUED_FOR_OUTREACH", {"campaign_id": payload["campaign_id"]})


def mark_contacted(conn, lead, payload):
    """The (dry-run) sender pushed the payload — QUEUED_FOR_OUTREACH → CONTACTED (provider 'ack')."""
    with conn.cursor() as cur:
        cur.execute("UPDATE leads SET status='CONTACTED', last_contacted_at=now(), updated_at=now() "
                    "WHERE id=%s;", (lead["id"],))
        _event(cur, lead["id"], "QUEUED_FOR_OUTREACH", "CONTACTED",
               {"campaign_id": payload["campaign_id"], "sending_account": payload["sending_account"]})


# ---- Phase 4: follow-up drip ----
_FOLLOWUP_SQL = """
    SELECT l.id, l.email, l.first_name, l.icp_segment, l.qualify_trigger,
           l.pitch_subject, l.pitch_body, l.pitch_lang, l.last_contacted_at,
           c.legal_name, c.region,
           COALESCE((SELECT min(created_at) FROM outreach_log o
                       WHERE o.lead_id=l.id AND o.step=0 AND o.outcome IN ('sent','logged')),
                    l.last_contacted_at) AS initial_at,
           (SELECT array_agg(DISTINCT o.step) FROM outreach_log o
              WHERE o.lead_id=l.id AND o.step >= 1 AND o.outcome IN ('sent','logged')) AS steps_sent
    FROM leads l JOIN companies c ON l.company_id = c.id
    WHERE l.status = 'CONTACTED'                    -- REPLIED/BOOKED/SUPPRESSED have left this status
      AND l.email IS NOT NULL
      AND l.email_validation_status IN ('valid','unverified')
      AND l.suppressed_at IS NULL
      AND (c.country IS NULL OR c.country NOT IN ('DE','AT'))
      AND NOT EXISTS (SELECT 1 FROM suppression_list s WHERE s.target_value::text = l.email::text)
      {region_clause}
    ORDER BY l.last_contacted_at ASC;
"""


def fetch_followup_candidates(conn):
    """Every CONTACTED lead still eligible for follow-up (not replied/suppressed), with its drip history.
    The engine (and dashboard) decide which drip STEP is actually due. Region-isolated if GRANJUR_REGION."""
    region = _active_region()
    sql = _FOLLOWUP_SQL.format(region_clause="AND c.region = %(region)s::region_code" if region else "")
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, {"region": region})
        return [dict(r) for r in cur.fetchall()]


def mark_followed_up(conn, lead_id, step, payload):
    """Record that follow-up `step` went out. Lead STAYS 'CONTACTED' (a nudge, not a new stage); we
    just bump last_contacted_at and log an audit event. outreach_log row is written separately."""
    with conn.cursor() as cur:
        cur.execute("UPDATE leads SET last_contacted_at=now(), updated_at=now() WHERE id=%s;", (lead_id,))
        _event(cur, lead_id, "CONTACTED", "CONTACTED",
               {"followup_step": step, "campaign_id": payload["campaign_id"]})


# ---- inbound events (webhook server calls these) ----
def mark_replied(conn, lead_id, sentiment):
    with conn.cursor() as cur:
        cur.execute("UPDATE leads SET status='REPLIED', replied_at=now(), reply_sentiment=%s, "
                    "updated_at=now() WHERE id=%s;", (sentiment, lead_id))
        _event(cur, lead_id, "CONTACTED", "REPLIED", {"sentiment": sentiment})
    conn.commit()


def mark_booked(conn, lead_id):
    with conn.cursor() as cur:
        cur.execute("UPDATE leads SET status='BOOKED', booked_at=now(), updated_at=now() WHERE id=%s;",
                    (lead_id,))
        _event(cur, lead_id, "REPLIED", "BOOKED", {})
    conn.commit()


def suppress(conn, lead_id, reason):
    """Bounce / unsubscribe / complaint -> global suppression + SUPPRESSED (irreversible block)."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT email FROM leads WHERE id=%s;", (lead_id,))
        row = cur.fetchone()
        email = row["email"] if row else None
        if email:
            cur.execute("SELECT 1 FROM suppression_list WHERE target_value::text = %s;", (str(email),))
            if not cur.fetchone():
                cur.execute("INSERT INTO suppression_list (target_value, reason) VALUES (%s, %s);",
                            (email, reason))
        cur.execute("UPDATE leads SET status='SUPPRESSED', suppressed_at=now(), suppression_reason=%s, "
                    "updated_at=now() WHERE id=%s;", (reason, lead_id))
        _event(cur, lead_id, None, "SUPPRESSED", {"reason": reason})
    conn.commit()


def _event(cur, lead_id, frm, to, detail):
    cur.execute("INSERT INTO lead_events (lead_id, from_status, to_status, workflow, detail) "
                "VALUES (%s, %s, %s, 'WF-4', %s);", (lead_id, frm, to, Json(detail)))


def mark_bounced(conn, email, reason="bounce"):
    """Phase-6: a hard bounce -> record it, mark the address DEAD, and suppress it (never mail again).
    Returns True if a matching lead was found. Commits."""
    with conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute("SELECT id FROM leads WHERE email=%s LIMIT 1;", (email,))
        row = cur.fetchone()
        if not row:
            return False
        lead_id = row["id"]
        cur.execute("UPDATE leads SET bounced_at=now(), bounce_count=COALESCE(bounce_count,0)+1, "
                    "updated_at=now() WHERE id=%s;", (lead_id,))
        cur.execute("INSERT INTO email_events (lead_id, event_type, detail) VALUES (%s,'bounce',%s);",
                    (lead_id, Json({"reason": reason, "email": str(email)})))
    conn.commit()
    suppress(conn, lead_id, f"bounce:{reason}"[:60])   # -> SUPPRESSED + suppression_list, commits
    return True


def log_outreach_attempt(conn, lead, outcome, step=0, dry_run=True, subject=None, body=None,
                         scheduled_for=None, sent_at=None, provider=None, sending_domain=None, error=None):
    """Append one row to outreach_log (Phase-1 follow-up execution log).

    outcome: 'sent' (or 'logged' in dry-run) when it went out; 'skipped' when the Phase-2 send-gate
    held it (store the local-window open time in scheduled_for). step 0 = initial pitch, 1..N follow-ups."""
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO outreach_log
                 (lead_id, step, region, channel, subject, body_preview, scheduled_for, sent_at,
                  dry_run, provider, sending_domain, outcome, error)
               VALUES (%(lead_id)s, %(step)s, %(region)s::region_code, 'email', %(subject)s,
                       %(body_preview)s, %(scheduled_for)s, %(sent_at)s, %(dry_run)s, %(provider)s,
                       %(sending_domain)s, %(outcome)s, %(error)s);""",
            {"lead_id": lead["id"], "step": step, "region": lead.get("region"),
             "subject": subject, "body_preview": (body or "")[:200] or None,
             "scheduled_for": scheduled_for, "sent_at": sent_at, "dry_run": dry_run,
             "provider": provider, "sending_domain": sending_domain, "outcome": outcome, "error": error})
    conn.commit()
