"""
WF-1 targeting requirements — "who we want."

These rules run at INTAKE (every collected/added candidate), so discovery is hands-off: a candidate
that meets them is auto-approved into the pipeline; one that clearly fails is auto-rejected; only a
genuinely ambiguous one waits for a human. Edit these to change who gets in automatically.
"""
import os
import sys

# ---------------------------------------------------------------------------
# REGION as a first-class dimension (Max Plan · Phase 1).
# VALID_REGIONS mirrors the Postgres `region_code` enum — the DB is the real
# authority; this set just lets Python validate a --region flag before it ever
# reaches SQL. REGION_TIMEZONE is the default IANA zone per region (feeds the
# companies.timezone backfill and the Phase-2 send scheduler).
# ---------------------------------------------------------------------------
VALID_REGIONS = {"US", "EU", "UK", "GCC", "CN", "AU", "OTHER"}
REGION_TIMEZONE = {
    "GCC": "Asia/Dubai",   "US": "America/New_York", "UK": "Europe/London",
    "EU":  "Europe/Berlin", "CN": "Asia/Shanghai",   "AU": "Australia/Sydney",
    "OTHER": "UTC",
}


def normalize_region(value):
    """Uppercase + validate a region string. Returns a VALID_REGIONS member, or None if unknown/blank."""
    r = (value or "").strip().upper()
    return r if r in VALID_REGIONS else None


def active_region(argv=None):
    """The region this run is isolated to, or None for 'all regions' (today's default behaviour).

    Resolution order (first hit wins):
      1. a  --region XX  flag in argv (per-run override)
      2. the GRANJUR_REGION env var (set by run_pipeline.py so every phase inherits it)
    An unknown value is ignored (falls back to all-regions) so a typo never silently drops the pipeline.
    """
    argv = sys.argv if argv is None else argv
    if "--region" in argv:
        i = argv.index("--region")
        if i + 1 < len(argv):
            return normalize_region(argv[i + 1])
    return normalize_region(os.getenv("GRANJUR_REGION"))


TARGET_REGIONS = {"US", "EU", "UK", "GCC", "CN", "AU"}   # empty set() = accept ANY region
TARGET_NICHES = set()          # empty = accept any niche; else e.g. {"logistics", "ecommerce", "saas"}
EMPLOYEE_MIN = 2               # a KNOWN size below this -> reject (single-person)
EMPLOYEE_MAX = 150            # a KNOWN size above this -> reject (enterprise w/ internal IT)
REQUIRE_CONTACTABLE = True    # must have a domain / website / email / intent, else -> human review

# Free sources lack headcount, so the >150 rule can't catch national chains / mega-brands by size.
# Reject them by name instead (matched as a substring of the company name). Edit freely.
BLOCKLIST_TOKENS = {
    "whole foods", "best buy", "cvs", "costco", "petco", "petsmart", "walmart", "target corp",
    "home depot", "lowe's", "kroger", "safeway", "sprouts", "ross ", "michaels", "total wine",
    "pga tour", "scrubs & beyond", "fiesta market", "fresh plus", "floor & decor", "floor decor",
    "copenhagen", "palm beach tan", "usps", "postal service", "fedex", "ups store", "starbucks",
    "mcdonald", "subway ", "7-eleven", "walgreens", "microsoft", "linkedin", "google", "amazon",
    "apple inc", "meta ", "oracle", "salesforce", "ibm ",
}
# OSM shop/office types that are inherently too big or off-ICP for a dev-agency pitch.
BLOCKLIST_SHOP_TYPES = {
    "supermarket", "department_store", "chemist", "pharmacy", "wholesale", "hypermarket",
    "mall", "convenience", "variety_store", "chain", "fuel", "car",
}


def is_blocked(name):
    n = (name or "").lower()
    return any(tok in n for tok in BLOCKLIST_TOKENS)

# Per region, a POOL of cities to rotate through. Each no-arg run of collect_osm picks ONE city
# per region (rotating on every run), so over successive runs discovery spreads geographically
# far and wide instead of hammering the same city. Still ~PER_CELL leads per region per run.
# Geocoded per city, so any city name you add here works worldwide — edit freely.
REGION_CITIES = {
    "GCC": ["Dubai", "Abu Dhabi", "Riyadh", "Doha", "Kuwait City", "Manama", "Sharjah"],
    "US":  ["Austin", "Denver", "Portland", "Nashville", "Seattle", "Miami", "Minneapolis"],
    "UK":  ["Manchester", "Bristol", "Leeds", "Glasgow", "Birmingham", "Brighton", "Edinburgh"],
    "EU":  ["Berlin", "Amsterdam", "Barcelona", "Lisbon", "Munich", "Milan", "Copenhagen"],
    "CN":  ["Shanghai", "Shenzhen", "Guangzhou", "Hangzhou", "Chengdu", "Beijing", "Suzhou"],
    "AU":  ["Sydney", "Melbourne", "Brisbane", "Perth", "Adelaide", "Gold Coast", "Canberra"],
}
DISCOVERY_NICHE = "business"   # niche hint passed to the geocode/Overpass sweep
