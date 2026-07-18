-- 1. Create custom Enumerated types
CREATE TYPE lead_status AS ENUM (
 'DISCOVERED', 'ENRICHING', 'ENRICHED', 'QUALIFYING', 'QUALIFIED',
 'DISQUALIFIED', 'TRANSLATING', 'QUEUED_FOR_OUTREACH', 'CONTACTED',
 'REPLIED', 'BOOKED', 'WON', 'LOST', 'SUPPRESSED', 'ERROR', 'COOLDOWN'
);

CREATE TYPE icp_segment AS ENUM ('A_LEGACY_BRICK', 'B_FUNDED_STARTUP', 'C_LOWTECH_ECOM');
CREATE TYPE consent_basis AS ENUM ('LEGITIMATE_INTEREST', 'CONSENT', 'NONE');
CREATE TYPE region_code AS ENUM ('US','EU','UK','GCC','CN','AU','OTHER');

-- 2. Create companies table
CREATE TABLE companies (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  legal_name TEXT NOT NULL,
  domain CITEXT UNIQUE, 
  website_url TEXT,
  phone TEXT,
  region region_code NOT NULL DEFAULT 'OTHER',
  country TEXT,
  city TEXT,
  niche TEXT, 
  employee_count INT, 
  gmaps_rating NUMERIC(2,1),
  gmaps_reviews INT,
  tech_stack TEXT[] DEFAULT '{}', 
  lighthouse_mobile INT, 
  lighthouse_lcp_ms INT,
  active_job_posts JSONB DEFAULT '[]', 
  intent_strings TEXT[] DEFAULT '{}', 
  funding_stage TEXT,
  funding_last_at DATE,
  source TEXT NOT NULL, 
  discovery_cell TEXT, 
  raw_payload JSONB, 
  first_seen_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  last_verified_at TIMESTAMPTZ,
  cooldown_until TIMESTAMPTZ, 
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 3. Create high-performance indexes for scanning companies
CREATE INDEX idx_companies_region_niche ON companies (region, niche);
CREATE INDEX idx_companies_techstack ON companies USING GIN (tech_stack);
CREATE INDEX idx_companies_jobs ON companies USING GIN (active_job_posts);


-- 1. Create leads table
CREATE TABLE leads (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  company_id UUID NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  email CITEXT UNIQUE, 
  first_name TEXT,
  last_name TEXT,
  linkedin_url TEXT,
  job_title TEXT,
  seniority TEXT,
  status lead_status NOT NULL DEFAULT 'DISCOVERED',
  icp_segment icp_segment, 
  personalized_pitch TEXT,
  email_validation_status TEXT, 
  outreach_mailbox TEXT, 
  consent_basis consent_basis NOT NULL DEFAULT 'NONE',
  consent_date DATE,
  bounce_count INT DEFAULT 0,
  last_error_message TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 2. Create high-performance indexes for scanning leads
CREATE INDEX idx_leads_company_id ON leads (company_id);
CREATE INDEX idx_leads_status ON leads (status);

-- 3. Create global suppression table
CREATE TABLE suppression_list (
  id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  target_value CITEXT UNIQUE NOT NULL, -- can be full email, domain name, or 'apple.com'
  reason TEXT NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 4. Create the anti-ban protection function
CREATE OR REPLACE FUNCTION enforcement_suppression_guardrail()
RETURNS TRIGGER AS $$
BEGIN
  -- If email domain or exact email is in suppression table, force state to SUPPRESSED
  IF EXISTS (
    SELECT 1 FROM suppression_list 
    WHERE target_value = NEW.email 
       OR target_value = split_part(NEW.email, '@', 2)
  ) THEN
    NEW.status := 'SUPPRESSED';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- 5. Attach the trigger to fire instantly before any lead gets inserted or changed
CREATE TRIGGER trg_leads_suppression_guardrail
BEFORE INSERT OR UPDATE ON leads
FOR EACH ROW EXECUTE FUNCTION enforcement_suppression_guardrail();

-- Create the global suppression list
CREATE TABLE IF NOT EXISTS suppression_list (
  email_hash   TEXT PRIMARY KEY,        -- sha256(lower(email)) - store hash, not PII
  domain       CITEXT,
  reason       TEXT NOT NULL,           -- 'optout'|'bounce'|'complaint'|'gdpr'|'trap'
  added_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Create discovery cells table for depletion management
CREATE TABLE IF NOT EXISTS discovery_cells (
  id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
  region         region_code NOT NULL,
  city           TEXT NOT NULL,
  niche          TEXT NOT NULL,
  total_seen     INT DEFAULT 0,
  new_last_run   INT DEFAULT 0,
  contacted      INT DEFAULT 0,
  saturation     NUMERIC(5,4) DEFAULT 0,   -- new_last_run / NULLIF(total_seen,0)
  depleted       BOOLEAN DEFAULT FALSE,
  last_run_at    TIMESTAMPTZ,
  cooldown_until TIMESTAMPTZ,
  UNIQUE (region, city, niche)
);

-- Create append-only audit tracking table
CREATE TABLE IF NOT EXISTS lead_events (
  id         BIGSERIAL PRIMARY KEY,
  lead_id    UUID REFERENCES leads(id) ON DELETE CASCADE,
  from_status lead_status,
  to_status   lead_status NOT NULL,
  workflow    TEXT,                       -- 'WF-2','WF-3','WF-4'
  detail      JSONB,
  at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_events_lead ON lead_events (lead_id, at);