"""
Seed skeletal DISCOVERED leads for WF-2 to enrich.

In production, WF-1 (discovery) creates these — a company shell with a domain and little else,
and a bare lead row. WF-2's job is to fill in the firmographics, decision-maker, and email.
This script stands in for WF-1 so you can watch the full flow: DISCOVERED -> ENRICHED -> QUALIFIED.

Idempotent: it clears the previous source='mock_wf2' set first, so you can re-run it any time.

  python seed_discovered.py
"""
import db

# Only identity-level fields (what a discovery scraper would find). Everything else is left
# NULL on purpose — WF-2 fills employee_count, tech_stack, lighthouse, contact, and email.
COMPANIES = [
    {"legal_name": "Verdant Landscaping", "domain": "verdant-landscaping.test",
     "region": "US", "country": "US", "city": "Austin", "niche": "landscaping"},
    {"legal_name": "PixelForge Studio", "domain": "pixelforge.test",
     "region": "UK", "country": "GB", "city": "London", "niche": "saas"},
    {"legal_name": "Bazaar Online", "domain": "bazaar-online.test",
     "region": "GCC", "country": "SA", "city": "Jeddah", "niche": "ecommerce"},
    {"legal_name": "Dragon Mart", "domain": "dragon-mart.test",
     "region": "CN", "country": "CN", "city": "Shanghai", "niche": "ecommerce"},
    {"legal_name": "Titan Industries", "domain": "titan-industries.test",
     "region": "US", "country": "US", "city": "Detroit", "niche": "manufacturing"},
    {"legal_name": "Glitch Corp", "domain": "glitch-corp.test",
     "region": "US", "country": "US", "city": "Denver", "niche": "saas"},
    {"legal_name": "Solo Freelance", "domain": "solo-freelance.test",
     "region": "EU", "country": "DE", "city": "Berlin", "niche": "consulting"},
]


def main():
    conn = db.get_connection()
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            # clear the previous WF-2 demo set (lead_events cascade off leads)
            cur.execute("""DELETE FROM leads WHERE company_id IN
                           (SELECT id FROM companies WHERE source='mock_wf2');""")
            cur.execute("DELETE FROM companies WHERE source='mock_wf2';")

            for c in COMPANIES:
                cur.execute(
                    """INSERT INTO companies (legal_name, domain, region, country, city, niche, source)
                       VALUES (%(legal_name)s, %(domain)s, %(region)s, %(country)s,
                               %(city)s, %(niche)s, 'mock_wf2')
                       RETURNING id;""",
                    c,
                )
                company_id = cur.fetchone()[0]
                cur.execute(
                    """INSERT INTO leads (company_id, status, consent_basis)
                       VALUES (%s, 'DISCOVERED', 'NONE');""",
                    (company_id,),
                )
        conn.commit()
        print(f"Seeded {len(COMPANIES)} DISCOVERED leads (source='mock_wf2'). Run:  python wf2.py")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
