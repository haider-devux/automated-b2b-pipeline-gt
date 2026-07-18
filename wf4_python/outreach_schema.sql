-- WF-4 outreach linkage + compliance columns on leads (idempotent).
ALTER TABLE leads ADD COLUMN IF NOT EXISTS outreach_provider  TEXT;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS campaign_id        TEXT;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS sending_domain     TEXT;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS last_contacted_at  TIMESTAMPTZ;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS replied_at         TIMESTAMPTZ;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS reply_sentiment    TEXT;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS booked_at          TIMESTAMPTZ;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS suppressed_at      TIMESTAMPTZ;
ALTER TABLE leads ADD COLUMN IF NOT EXISTS suppression_reason TEXT;

-- Fix the outreach-gate guardrail. The original (from the blueprint DDL) referenced NEW.email_status
-- and a suppression_list.email_hash column that don't exist in this DB, so it errored on every queue.
-- Corrected: use email_validation_status; block only suppressed OR invalid/missing email (free mode
-- produces 'unverified', which is allowed pre-send). The separate suppression guardrail handles the
-- global suppression_list match. This is the compliance send-gate at the database layer.
CREATE OR REPLACE FUNCTION public.enforce_outreach_gate()
RETURNS trigger LANGUAGE plpgsql AS $fn$
BEGIN
  IF NEW.status = 'QUEUED_FOR_OUTREACH' THEN
    IF NEW.suppressed_at IS NOT NULL THEN
      RAISE EXCEPTION 'Lead % is suppressed; cannot queue', NEW.id;
    END IF;
    IF NEW.email IS NULL OR NEW.email_validation_status = 'invalid' THEN
      RAISE EXCEPTION 'Lead % email missing or invalid; cannot queue', NEW.id;
    END IF;
  END IF;
  NEW.updated_at := now();
  RETURN NEW;
END;
$fn$;
