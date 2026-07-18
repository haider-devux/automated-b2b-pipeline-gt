-- =====================================================================
--  MAX PLAN · PHASE 1  —  Database preparation & regional filtering
--  Fully IDEMPOTENT: safe to run any number of times.
--  Adds:  companies.timezone, leads.tags, and the append-only
--         outreach_log (follow-up execution logging infrastructure).
--  Note:  companies.region ALREADY EXISTS (region_code enum) — we do
--         NOT re-add it; we build around it.
-- =====================================================================

-- ---------------------------------------------------------------------
-- 1. RECIPIENT-LOCAL TIMEZONE (substrate for Phase-2 send scheduling).
--    IANA name (e.g. 'Asia/Dubai'). Region-level default now; per-country
--    refinement happens in Phase 2. Nothing reads this yet.
-- ---------------------------------------------------------------------
ALTER TABLE companies ADD COLUMN IF NOT EXISTS timezone TEXT;

-- Backfill a sensible region-level default ONLY where still empty.
UPDATE companies
   SET timezone = CASE region
        WHEN 'GCC' THEN 'Asia/Dubai'
        WHEN 'US'  THEN 'America/New_York'
        WHEN 'UK'  THEN 'Europe/London'
        WHEN 'EU'  THEN 'Europe/Berlin'
        WHEN 'CN'  THEN 'Asia/Shanghai'
        WHEN 'AU'  THEN 'Australia/Sydney'
        ELSE 'UTC'
   END
 WHERE timezone IS NULL;

-- ---------------------------------------------------------------------
-- 2. TAGS  (substrate for the Phase-5 tag-based CRM hub).
--    A per-lead free-form label array, e.g. {'#GCC','#Shopify','#Hiring'}.
--    GIN index makes  WHERE tags @> '{#GCC}'  instant.
-- ---------------------------------------------------------------------
ALTER TABLE leads ADD COLUMN IF NOT EXISTS tags TEXT[] NOT NULL DEFAULT '{}';
CREATE INDEX IF NOT EXISTS idx_leads_tags ON leads USING GIN (tags);

-- One-time backfill: derive tags from data already on the row
-- (region + ICP segment + tech + mobile score + hiring intent).
-- Going forward, WF-3 will stamp these at qualify time (wired in Phase 5).
UPDATE leads l
   SET tags = sub.tags
  FROM (
    SELECT l2.id AS lead_id,
           ARRAY(SELECT DISTINCT x FROM unnest(
                ARRAY['#' || c.region::text]
             || CASE l2.icp_segment
                    WHEN 'A_LEGACY_BRICK'   THEN ARRAY['#Legacy']
                    WHEN 'B_FUNDED_STARTUP' THEN ARRAY['#Startup']
                    WHEN 'C_LOWTECH_ECOM'   THEN ARRAY['#Ecom']
                    ELSE ARRAY[]::text[] END
             || CASE WHEN EXISTS (SELECT 1 FROM unnest(c.tech_stack) t
                                   WHERE lower(t) LIKE 'shopify%')
                     THEN ARRAY['#Shopify'] ELSE ARRAY[]::text[] END
             || CASE WHEN EXISTS (SELECT 1 FROM unnest(c.tech_stack) t
                                   WHERE lower(t) LIKE 'woo%')
                     THEN ARRAY['#Woo'] ELSE ARRAY[]::text[] END
             || CASE WHEN c.lighthouse_mobile IS NOT NULL
                       AND c.lighthouse_mobile < 50
                     THEN ARRAY['#SlowMobile'] ELSE ARRAY[]::text[] END
             || CASE WHEN jsonb_typeof(c.active_job_posts) = 'array'
                       AND jsonb_array_length(c.active_job_posts) > 0
                     THEN ARRAY['#Hiring'] ELSE ARRAY[]::text[] END
           ) AS x) AS tags
    FROM leads l2
    JOIN companies c ON c.id = l2.company_id
  ) sub
 WHERE l.id = sub.lead_id
   AND l.tags = '{}';                 -- only rows never tagged yet

-- ---------------------------------------------------------------------
-- 3. OUTREACH_LOG  —  append-only follow-up EXECUTION logging.
--    One row per send ATTEMPT (initial pitch = step 0, follow-ups 1..N).
--    Phase-4 drip engine WRITES here; Phase-6 analytics READS here.
--    Decoupled: no module reads another; they meet in this table + status.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS outreach_log (
  id             BIGSERIAL PRIMARY KEY,
  lead_id        UUID NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  step           SMALLINT     NOT NULL DEFAULT 0,     -- 0 = initial pitch, 1..N = follow-ups
  region         region_code,                         -- denormalized for fast regional analytics
  channel        TEXT         NOT NULL DEFAULT 'email',
  subject        TEXT,
  body_preview   TEXT,                                -- first ~200 chars, for the audit trail
  scheduled_for  TIMESTAMPTZ,                         -- when the guardrails permitted/planned the send
  sent_at        TIMESTAMPTZ,                         -- when it actually left (NULL while dry-run/pending)
  dry_run        BOOLEAN      NOT NULL DEFAULT TRUE,   -- honours GRANJUR_DRY_RUN
  provider       TEXT,                                -- 'gmail' | 'instantly' | ...
  sending_domain TEXT,
  outcome        TEXT         NOT NULL DEFAULT 'logged', -- 'logged'|'sent'|'skipped'|'error'
  error          TEXT,
  created_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_outreach_log_lead   ON outreach_log (lead_id, step);
CREATE INDEX IF NOT EXISTS idx_outreach_log_sched  ON outreach_log (scheduled_for);
CREATE INDEX IF NOT EXISTS idx_outreach_log_region ON outreach_log (region);
