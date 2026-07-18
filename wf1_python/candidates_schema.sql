-- Staging table for human-in-the-loop discovery review.
-- Every find (manual paste OR a free bot collector) lands here as PENDING. A human approves or
-- rejects it on the website; only APPROVED candidates become DISCOVERED leads in the real pipeline.
CREATE TABLE IF NOT EXISTS discovery_candidates (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    legal_name     TEXT NOT NULL,
    domain         TEXT,
    region         TEXT DEFAULT 'OTHER',          -- validated to region_code enum on approve
    country        TEXT,
    city           TEXT,
    niche          TEXT,
    website_url    TEXT,
    phone          TEXT,
    employee_count INT,
    tech_stack     TEXT,                           -- free text (semicolon/comma), parsed on approve
    first_name     TEXT,
    last_name      TEXT,
    job_title      TEXT,
    email          TEXT,
    source         TEXT NOT NULL DEFAULT 'manual', -- 'manual' | 'osm' | 'jobfeed' | ...
    signal         TEXT,                           -- why it's interesting (e.g. "hiring React dev")
    raw            JSONB,
    status         TEXT NOT NULL DEFAULT 'PENDING',-- PENDING | APPROVED | REJECTED
    review_note    TEXT,
    lead_id        UUID,                           -- the lead created on approval
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    reviewed_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_candidates_status ON discovery_candidates (status, created_at);
