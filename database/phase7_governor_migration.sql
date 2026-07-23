-- =====================================================================
--  MAX PLAN · PHASE 7  —  Rate Governor & anti-flag state
--  IDEMPOTENT. Adds:
--    1. rate_state  — per-bot token buckets + rest/backoff/circuit-breaker
--                     state, the shared safety spine for the parallel bot fleet.
--    2. city_bbox   — cache of geocoded city bounding boxes so bot-osm stops
--                     re-hitting Nominatim for the same city every run (the
--                     real long-run Nominatim-ban risk).
--  See DEPLOYMENT_PLAN.md §2 and §4.2.
-- =====================================================================

-- One row per bot. All windows roll forward in-place; the governor module owns
-- the reset logic (an expired window is zeroed on the next can_run/record call).
CREATE TABLE IF NOT EXISTS rate_state (
  source            TEXT PRIMARY KEY,           -- bot name: 'gmaps' | 'osm' | 'send' | ...
  run_count         INTEGER     NOT NULL DEFAULT 0,   -- units in the current run (advisory)
  hour_count        INTEGER     NOT NULL DEFAULT 0,   -- units this rolling hour
  hour_start        TIMESTAMPTZ NOT NULL DEFAULT now(),
  day_count         INTEGER     NOT NULL DEFAULT 0,   -- units this rolling day (the real ceiling)
  day_start         TIMESTAMPTZ NOT NULL DEFAULT now(),
  rest_until        TIMESTAMPTZ,                       -- while now() < rest_until the bot no-ops + exits
  fail_streak       INTEGER     NOT NULL DEFAULT 0,   -- consecutive blocks -> exponential backoff
  blocks_today      INTEGER     NOT NULL DEFAULT 0,   -- blocks this rolling day (circuit breaker)
  blocks_day_start  TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_run_at       TIMESTAMPTZ,
  last_block_at     TIMESTAMPTZ,
  last_block_reason TEXT,
  updated_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Cache of city -> bounding box so bot-osm geocodes each city ONCE, not every run.
CREATE TABLE IF NOT EXISTS city_bbox (
  region      TEXT NOT NULL,
  city        TEXT NOT NULL,
  south       DOUBLE PRECISION,
  west        DOUBLE PRECISION,
  north       DOUBLE PRECISION,
  east        DOUBLE PRECISION,
  geocoded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  PRIMARY KEY (region, city)
);
