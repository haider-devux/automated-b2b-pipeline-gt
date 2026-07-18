-- =====================================================================
--  MAX PLAN · PHASE 6  —  Full-funnel analytics & telemetry
--  IDEMPOTENT. Adds the email_events telemetry log (opens / clicks /
--  bounces) + first-touch timestamps on leads for the funnel matrix.
-- =====================================================================

-- Append-only telemetry: one row per recipient interaction.
CREATE TABLE IF NOT EXISTS email_events (
  id          BIGSERIAL PRIMARY KEY,
  lead_id     UUID REFERENCES leads(id) ON DELETE CASCADE,
  step        SMALLINT,                       -- which send (0 initial, 1..N follow-ups)
  event_type  TEXT NOT NULL,                  -- 'open' | 'click' | 'bounce'
  url         TEXT,                           -- destination for 'click'
  detail      JSONB,                          -- user-agent, ip, bounce reason, etc.
  at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_email_events_lead ON email_events (lead_id);
CREATE INDEX IF NOT EXISTS idx_email_events_type ON email_events (event_type, at);

-- First-touch timestamps on the lead for the funnel (bounce_count already exists).
ALTER TABLE leads ADD COLUMN IF NOT EXISTS first_open_at  TIMESTAMPTZ;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS first_click_at TIMESTAMPTZ;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS bounced_at     TIMESTAMPTZ;
