-- ============ WF-3 STRESS-TEST BATCH (10 varied leads) ============
-- 1) Insert 10 real-shaped companies
INSERT INTO companies
  (legal_name, domain, region, country, city, niche,
   employee_count, gmaps_rating, gmaps_reviews, tech_stack,
   lighthouse_mobile, active_job_posts, funding_stage, source)
VALUES
 ('Bright Smile Dental Group','brightsmile-dental-test.com','US','US','Austin','dental clinic',
   45, 4.9, 320, '{}'::text[], 41, '[]'::jsonb, NULL, 'mock'),

 ('Falcon Pay','falconpay-test.sa','GCC','SA','Riyadh','fintech',
   55, NULL, NULL, ARRAY['React','Node','AWS']::text[], 80,
   '[{"title":"Senior React Developer","url":"https://falconpay-test.sa/jobs/1","seen_at":"2026-06-25","source":"linkedin"}]'::jsonb,
   'Series A', 'mock'),

 ('BlueWhale Tech','bluewhale-test.cn','CN','CN','Shenzhen','mobile saas',
   40, NULL, NULL, ARRAY['React Native','Python']::text[], 72,
   '[{"title":"iOS Engineer","url":"https://bluewhale-test.cn/jobs/1","seen_at":"2026-06-24","source":"linkedin"}]'::jsonb,
   'Seed', 'mock'),

 ('Coastal Threads','coastalthreads-test.com','US','US','San Diego','d2c apparel ecommerce',
   12, 4.2, 45, ARRAY['Shopify']::text[], 34, '[]'::jsonb, NULL, 'mock'),

 ('Boulangerie Laurent','boulangerie-laurent-test.fr','EU','FR','Lyon','bakery chain',
   28, 4.7, 210, '{}'::text[], 38, '[]'::jsonb, NULL, 'mock'),

 ('Jane Doe Consulting','janedoe-consulting-test.com','US','US','Denver','independent consulting',
   1, NULL, NULL, '{}'::text[], 66, '[]'::jsonb, NULL, 'mock'),

 ('MegaCorp Systems','megacorp-test.com','US','US','Seattle','enterprise software',
   4200, NULL, NULL, ARRAY['Java','Oracle','SAP']::text[], 88, '[]'::jsonb, 'Public', 'mock'),

 ('Unknown Ventures','unknown-ventures-test.com','OTHER',NULL,NULL,NULL,
   NULL, NULL, NULL, '{}'::text[], NULL, '[]'::jsonb, NULL, 'mock'),

 ('Nimbus AI Labs','nimbus-ai-test.co.uk','UK','GB','London','ai saas',
   35, NULL, NULL, ARRAY['React','Python','GCP']::text[], 79,
   '[{"title":"React Developer","url":"https://nimbus-ai-test.co.uk/jobs/1","seen_at":"2026-06-23","source":"linkedin"},{"title":"ML Engineer","url":"https://nimbus-ai-test.co.uk/jobs/2","seen_at":"2026-06-22","source":"linkedin"}]'::jsonb,
   'Series A', 'mock'),

 ('Souq Style','souq-style-test.ae','GCC','AE','Dubai','d2c ecommerce',
   18, 4.1, 30, ARRAY['WooCommerce']::text[], 29, '[]'::jsonb, NULL, 'mock');

-- 2) Insert one ENRICHED lead per company (matched by domain)
INSERT INTO leads
  (company_id, email, first_name, last_name, job_title, seniority,
   status, consent_basis, email_validation_status)
SELECT c.id, v.email, v.first_name, v.last_name, v.job_title, v.seniority,
       'ENRICHED', 'LEGITIMATE_INTEREST', 'valid'
FROM (VALUES
  ('brightsmile-dental-test.com','dr.emily@brightsmile-dental-test.com','Emily','Carter','Practice Owner','Owner'),
  ('falconpay-test.sa','khalid.f@falconpay-test.sa','Khalid','Rahman','Co-Founder & CTO','C-Level'),
  ('bluewhale-test.cn','wei@bluewhale-test.cn','Wei','Zhang','Founder','C-Level'),
  ('coastalthreads-test.com','mia@coastalthreads-test.com','Mia','Nguyen','Founder','Owner'),
  ('boulangerie-laurent-test.fr','laurent@boulangerie-laurent-test.fr','Laurent','Dubois','Owner','Owner'),
  ('janedoe-consulting-test.com','jane@janedoe-consulting-test.com','Jane','Doe','Principal Consultant','Owner'),
  ('megacorp-test.com','robert.king@megacorp-test.com','Robert','King','CTO','C-Level'),
  ('unknown-ventures-test.com','contact@unknown-ventures-test.com',NULL,NULL,NULL,NULL),
  ('nimbus-ai-test.co.uk','sara@nimbus-ai-test.co.uk','Sara','Patel','VP Engineering','VP'),
  ('souq-style-test.ae','noura@souq-style-test.ae','Noura','Al-Mansoori','Founder','C-Level')
) AS v(domain, email, first_name, last_name, job_title, seniority)
JOIN companies c ON c.domain = v.domain;

SELECT l.status, c.region, c.legal_name, c.employee_count, l.email
FROM leads l
JOIN companies c ON l.company_id = c.id
WHERE c.domain LIKE '%-test%'
ORDER BY c.legal_name;

One heads-up so it's not a surprise: if you ever need to re-run this batch, it'll error on duplicate emails/domains 
(they must be unique). 
When that time comes I'll give you a one-line reset. For now, just run it once.

WF-1 (Python)  →  inserts leads          →  status = DISCOVERED
WF-2 (Python)  →  reads DISCOVERED, enriches →  status = ENRICHED
WF-3 (n8n)     →  reads ENRICHED, qualifies  →  status = QUALIFIED / DISQUALIFIED
WF-4 (?)       →  reads QUALIFIED, sends      →  status = QUEUED_FOR_OUTREACH ...
