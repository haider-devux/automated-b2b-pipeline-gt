-- ============================================================
-- Phase 3 (WF-3) schema migration
-- Adds the columns the AI Qualification & Translation workflow writes.
-- Safe to run multiple times (IF NOT EXISTS / additive only).
-- ============================================================

-- 1. Qualification + pitch output fields on leads -------------
ALTER TABLE leads
  ADD COLUMN IF NOT EXISTS qualify_score     NUMERIC(4,2),  -- 0.00-1.00 LLM fit score
  ADD COLUMN IF NOT EXISTS qualify_reason    TEXT,          -- 1-sentence rationale (feedback loop)
  ADD COLUMN IF NOT EXISTS qualify_trigger   TEXT,          -- the observed signal the pitch opens on
  ADD COLUMN IF NOT EXISTS disqualify_reason TEXT,          -- why a lead was archived
  ADD COLUMN IF NOT EXISTS pitch_subject     TEXT,          -- localized subject line
  ADD COLUMN IF NOT EXISTS pitch_body        TEXT,          -- localized body (final outreach copy)
  ADD COLUMN IF NOT EXISTS pitch_lang        TEXT DEFAULT 'en',   -- 'en' | 'ar' | 'zh'
  ADD COLUMN IF NOT EXISTS pitch_localized   BOOLEAN DEFAULT FALSE;

-- 2. Fast claim index: WF-3 polls WHERE status='ENRICHED' ----
CREATE INDEX IF NOT EXISTS idx_leads_enriched
  ON leads (created_at)
  WHERE status = 'ENRICHED';

-- 3. Optional convenience index for QUALIFYING in-flight rows -
CREATE INDEX IF NOT EXISTS idx_leads_qualifying
  ON leads (updated_at)
  WHERE status = 'QUALIFYING';

-- ------------------------------------------------------------
-- NOTE (not auto-applied): DB.sql defines suppression_list TWICE
-- (lines ~77 and ~106) with conflicting shapes. The first wins on
-- CREATE; the second's CREATE ... IF NOT EXISTS silently no-ops, so
-- your live table is the (id, target_value, reason, created_at) one
-- and the suppression guardrail trigger matches that shape. The
-- email_hash variant from the blueprint is never created. Reconcile
-- before Phase 4, which expects the hashed-email suppression model.
-- ------------------------------------------------------------
