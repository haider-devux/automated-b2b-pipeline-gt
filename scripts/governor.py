"""
Rate Governor — the shared rate-limit + rest/backoff spine for the parallel bot fleet.

Every bot (gmaps, osm, remoteok, remotive, enrich, send, ...) calls this BEFORE doing work and
AFTER each unit of work. It is the single place all limits and rests live, so behaviour is
consistent and tunable without touching bot code. State persists in the `rate_state` table
(see database/phase7_governor_migration.sql) — crucially, ACROSS cron runs, because each bot
is fired many times a day in separate processes and an in-process counter would reset every tick.

Concept (DEPLOYMENT_PLAN.md §2 / §3):
  Layer 2  PER-DAY cap    day_count  >= day_cap    -> can_run() = False (rest)
           PER-HOUR cap   hour_count >= hour_cap   -> can_run() = False (rest)
  Layer 3  MIN-INTERVAL   now() < rest_until        -> can_run() = False (rest)
  Layer 4  BLOCK          back_off() sets rest_until = now + rand(hours) * 2^fail_streak (capped)
           BREAKER        blocks_today >= breaker   -> can_run() = False (park the bot)

Windows roll forward lazily: an expired hour/day window is zeroed on the next _roll() (called by
can_run/record). Each mutating call commits, so governor state is durable regardless of what the
caller's transaction does. Pass your own psycopg2 connection (bots already have one), or omit it
and the governor opens its own from the shared DB config.

Usage:
    import governor
    g = governor.can_run(conn, "gmaps", hour_cap=None, day_cap=30)
    if not g["ok"]:
        print(f"[governor] resting: {g['reason']}"); return
    ... do one unit ...
    governor.record(conn, "gmaps", 1)
    ... on a CAPTCHA/block ...
    governor.back_off(conn, "gmaps", 6, 12, reason="captcha")   # rest 6-12h, exponential
    ... on a fully clean run ...
    governor.reset_fail(conn, "gmaps")
"""
import importlib.util
import os
import random
from pathlib import Path

# Cap the exponential-backoff multiplier so a long fail streak can't schedule an absurd rest
# (2^3 = 8x by default: e.g. a 6h base block rest tops out at ~48h, not weeks).
_BACKOFF_FACTOR_CAP = int(os.getenv("GRANJUR_BACKOFF_FACTOR_CAP", "8"))
# Consecutive blocks in one rolling day that trip the circuit breaker (park the bot ~24h).
_DEFAULT_BREAKER = int(os.getenv("GRANJUR_BLOCK_BREAKER", "3"))
_PARK_HOURS = float(os.getenv("GRANJUR_BREAKER_PARK_HOURS", "24"))

_ROOT = Path(__file__).resolve().parent.parent


def connect():
    """Open a governor connection from the shared DB config (same source run_pipeline.py uses)."""
    import psycopg2
    spec = importlib.util.spec_from_file_location("wf3_config_for_gov", _ROOT / "wf3_python" / "config.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    d = mod.DB
    return psycopg2.connect(host=d["host"], port=d["port"], dbname=d["dbname"],
                            user=d["user"], password=d["password"])


def _ensure(cur, source):
    """Make sure a row exists for this bot (first ever call)."""
    cur.execute("INSERT INTO rate_state (source) VALUES (%s) ON CONFLICT (source) DO NOTHING;", (source,))


def _roll(cur, source):
    """Zero any window whose start is older than its span (hour / day / blocks-day)."""
    cur.execute(
        """
        UPDATE rate_state SET
            hour_count       = CASE WHEN now() - hour_start       >= interval '1 hour' THEN 0 ELSE hour_count END,
            hour_start       = CASE WHEN now() - hour_start       >= interval '1 hour' THEN now() ELSE hour_start END,
            day_count        = CASE WHEN now() - day_start        >= interval '1 day'  THEN 0 ELSE day_count END,
            day_start        = CASE WHEN now() - day_start        >= interval '1 day'  THEN now() ELSE day_start END,
            blocks_today     = CASE WHEN now() - blocks_day_start >= interval '1 day'  THEN 0 ELSE blocks_today END,
            blocks_day_start = CASE WHEN now() - blocks_day_start >= interval '1 day'  THEN now() ELSE blocks_day_start END
        WHERE source = %s;
        """,
        (source,),
    )


def _own_or(conn):
    """Return (conn, opened_here) — open a throwaway connection if the caller didn't pass one."""
    if conn is not None:
        return conn, False
    return connect(), True


def can_run(conn, source, *, hour_cap=None, day_cap=None, min_interval_min=None,
            breaker=_DEFAULT_BREAKER):
    """Decide whether `source` may work right now. Returns a dict:
        ok        True to proceed; False means the bot should log the reason and exit 0 (this IS the rest)
        reason    None if ok, else 'resting' | 'too soon' | 'day cap' | 'hour cap' | 'circuit breaker'
        rest_until / hour_count / day_count / blocks_today   current state (for logging)
    `min_interval_min` enforces Layer 3 (min-interval): False if the last run was < that many minutes ago,
    even outside a backoff rest. Rolls expired windows first; commits the (idempotent) roll."""
    c, opened = _own_or(conn)
    try:
        with c.cursor() as cur:
            _ensure(cur, source)
            _roll(cur, source)
            cur.execute(
                """SELECT rest_until, hour_count, day_count, blocks_today,
                          (rest_until IS NOT NULL AND now() < rest_until) AS resting,
                          (last_run_at IS NOT NULL AND %s IS NOT NULL
                             AND now() < last_run_at + make_interval(mins => %s)) AS too_soon
                     FROM rate_state WHERE source = %s;""",
                (min_interval_min, min_interval_min or 0, source))
            rest_until, hour_count, day_count, blocks_today, resting, too_soon = cur.fetchone()
        c.commit()
    finally:
        if opened:
            c.close()

    reason = None
    if resting:
        reason = "resting"
    elif breaker is not None and blocks_today >= breaker:
        reason = "circuit breaker"
    elif day_cap is not None and day_count >= day_cap:
        reason = "day cap"
    elif hour_cap is not None and hour_count >= hour_cap:
        reason = "hour cap"
    elif too_soon:
        reason = "too soon"
    return {
        "ok": reason is None, "reason": reason, "source": source,
        "rest_until": rest_until, "hour_count": hour_count,
        "day_count": day_count, "day_cap": day_cap,
        "hour_cap": hour_cap, "blocks_today": blocks_today,
    }


def record(conn, source, n=1):
    """Count `n` completed units toward this bot's run/hour/day windows. Commits."""
    c, opened = _own_or(conn)
    try:
        with c.cursor() as cur:
            _ensure(cur, source)
            _roll(cur, source)
            cur.execute(
                """UPDATE rate_state
                      SET run_count = run_count + %s, hour_count = hour_count + %s,
                          day_count = day_count + %s, last_run_at = now(), updated_at = now()
                    WHERE source = %s;""",
                (n, n, n, source))
        c.commit()
    finally:
        if opened:
            c.close()


def start_run(conn, source):
    """Reset the per-run counter at the top of a tick (Layer-1 window is in-process; this is for telemetry)."""
    c, opened = _own_or(conn)
    try:
        with c.cursor() as cur:
            _ensure(cur, source)
            cur.execute("UPDATE rate_state SET run_count = 0, updated_at = now() WHERE source = %s;", (source,))
        c.commit()
    finally:
        if opened:
            c.close()


def back_off(conn, source, low_hours, high_hours, reason="blocked"):
    """A block was detected (CAPTCHA / consent wall / 429). Set an exponential rest and count the block.
    rest = rand(low_hours, high_hours) * 2^fail_streak, capped by _BACKOFF_FACTOR_CAP. Commits.
    Returns the number of minutes the bot will rest (for logging)."""
    c, opened = _own_or(conn)
    try:
        with c.cursor() as cur:
            _ensure(cur, source)
            _roll(cur, source)
            cur.execute("SELECT fail_streak FROM rate_state WHERE source = %s;", (source,))
            fail_streak = cur.fetchone()[0]
            factor = min(2 ** fail_streak, _BACKOFF_FACTOR_CAP)
            minutes = int(random.uniform(low_hours, high_hours) * 60 * factor)
            cur.execute(
                """UPDATE rate_state
                      SET rest_until = now() + make_interval(mins => %s),
                          fail_streak = fail_streak + 1,
                          blocks_today = blocks_today + 1,
                          last_block_at = now(), last_block_reason = %s, updated_at = now()
                    WHERE source = %s;""",
                (minutes, reason[:200], source))
        c.commit()
    finally:
        if opened:
            c.close()
    return minutes


def park(conn, source, hours=_PARK_HOURS, reason="parked"):
    """Force a fixed rest (e.g. circuit breaker tripped, or a health/bounce failure pausing sends). Commits."""
    c, opened = _own_or(conn)
    try:
        with c.cursor() as cur:
            _ensure(cur, source)
            cur.execute(
                """UPDATE rate_state
                      SET rest_until = now() + make_interval(mins => %s),
                          last_block_at = now(), last_block_reason = %s, updated_at = now()
                    WHERE source = %s;""",
                (int(hours * 60), reason[:200], source))
        c.commit()
    finally:
        if opened:
            c.close()


def reset_fail(conn, source):
    """A fully clean run — clear the consecutive-block streak (backoff returns to its base). Commits."""
    c, opened = _own_or(conn)
    try:
        with c.cursor() as cur:
            _ensure(cur, source)
            cur.execute("UPDATE rate_state SET fail_streak = 0, updated_at = now() WHERE source = %s;", (source,))
        c.commit()
    finally:
        if opened:
            c.close()


def status(conn=None, source=None):
    """Return current governor rows (all bots, or one) — for a dashboard panel / debugging."""
    from psycopg2.extras import RealDictCursor
    c, opened = _own_or(conn)
    try:
        with c.cursor(cursor_factory=RealDictCursor) as cur:
            if source:
                cur.execute("SELECT * FROM rate_state WHERE source = %s;", (source,))
                return cur.fetchone()
            cur.execute("SELECT * FROM rate_state ORDER BY source;")
            return cur.fetchall()
    finally:
        if opened:
            c.close()


if __name__ == "__main__":
    # Quick CLI: print the current state of every bot's governor row.
    import json
    rows = status() or []
    if isinstance(rows, dict):
        rows = [rows]
    for r in rows:
        print(json.dumps({k: str(v) for k, v in r.items()}, indent=2))
    if not rows:
        print("(no rate_state rows yet — bots populate them on first run)")
