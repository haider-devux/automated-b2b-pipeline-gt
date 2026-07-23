"""
WF-2 configuration.

The DB credentials are reused from wf3_python/config.py so there is ONE place to set your
Postgres password (the one you already set for WF-3). In the week-2 refactor we'll extract a
proper shared package; for now WF-2 loads WF-3's DB dict by file path (no re-entry needed).
"""
import importlib.util
import pathlib

# Load secrets from the project-root .env (PAGESPEED_API_KEY lives here). override=True so .env wins
# over any stale OS env value. Safe no-op if python-dotenv isn't installed.
try:
    from dotenv import load_dotenv
    load_dotenv(pathlib.Path(__file__).resolve().parent.parent / ".env", override=True)
except ImportError:
    pass

# ---- reuse the DB settings (and password) you already set in wf3_python/config.py ----
_wf3_config = pathlib.Path(__file__).resolve().parent.parent / "wf3_python" / "config.py"
_spec = importlib.util.spec_from_file_location("wf3_config", _wf3_config)
_wf3 = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_wf3)
DB = _wf3.DB

import os

# ---- WF-2 specific settings ----
# How enrichment runs:
#   "mock" — canned demo data keyed by domain (no network, for the built-in demo set)
#   "free" — REAL but free: site-HTML tech detection + DNS MX email check + optional Lighthouse
#   "real" — paid API waterfall (Apollo/Hunter/BuiltWith/ZeroBounce) — not wired yet
ENRICH_MODE = os.getenv("ENRICH_MODE", "free")
ENRICH_BATCH_SIZE = 25   # how many DISCOVERED leads to claim per run (blueprint suggests ~25)

# ---- free-mode settings (no paid keys) ----
FREE_HTTP_TIMEOUT = 10          # seconds to wait when fetching a site's HTML
FREE_DNS_TIMEOUT = 5            # seconds for the MX lookup
FREE_USER_AGENT = "GranjurBot/0.1 (+https://granjur.com; enrichment)"
# Politeness pause between site fetches (between a lead's Contact/About pages, and between leads) so
# bot-enrich never hammers a single host and stays a courteous crawler. Env: ENRICH_FETCH_DELAY.
FREE_FETCH_DELAY = float(os.getenv("ENRICH_FETCH_DELAY", "0.5"))
# Optional: a FREE Google PageSpeed Insights key enables Lighthouse scoring. Leave blank to skip.
PAGESPEED_API_KEY = os.getenv("PAGESPEED_API_KEY", "")

# A lead becomes ENRICHED if it has a usable email (verified OR unverified-but-present) OR a strong
# intent signal (a job post). Only a syntactically INVALID email with no intent parks in ERROR —
# free mode never drops a human-curated lead just because DNS couldn't confirm it. (Blueprint Phase 2.)
