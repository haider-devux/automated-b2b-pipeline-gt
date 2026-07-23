"""
bot-gmaps — Google Maps discovery collector (DIRECT SCRAPE). HIGHEST-RISK bot in the fleet.

This is the paid-Google-Maps replacement done by scraping maps.google.com with a headless browser,
wrapped in the 5-layer safety trigger so a cron loop can't get the server IP flagged/blacklisted:

  Layer 1  PER-RUN cap      GMAPS_MAX_PER_RUN (20)   — stop THIS session
  Layer 2  PER-DAY cap      GMAPS_MAX_PER_DAY (30)   — governor, persists ACROSS cron runs
  Layer 3  MIN-INTERVAL     GMAPS_MIN_INTERVAL_H (2) — governor no-ops if last run was too recent
  Layer 4  BLOCK DETECTOR   classify_page() ★        — CAPTCHA/consent/ /sorry/ → STOP + rest 6-12h
  Layer 5  RANDOM DELAYS    GMAPS_DELAY_MIN/MAX      — sleep between cards + jittered session

Every knob is env-overridable (see the constants below). Extraction selectors target live Google Maps
and MAY need re-tuning if Google changes its markup — that's expected for any Maps scraper. The safety
spine (governor + block detector) is the durable part.

PREREQUISITES (unique to this bot, see DEPLOYMENT_PLAN.md §4.1):
  - pip install playwright  &&  playwright install chromium
  - rotating RESIDENTIAL proxies via GMAPS_PROXY / GMAPS_PROXIES (datacenter IPs get CAPTCHA'd fast)
  - run its egress ISOLATED from SMTP so a burned scraper IP never touches sending

  python collect_maps.py                       # rotate one city per targeted region (like collect_osm)
  python collect_maps.py US Austin restaurant  # one cell: region, city, niche
"""
import os
import random
import sys
import time
from urllib.parse import quote_plus, urlparse

# Governor lives in scripts/ (shared across the fleet). APPEND so wf1_python's own db/config still win.
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import governor  # noqa: E402

import db          # noqa: E402
import intake      # noqa: E402
import targets     # noqa: E402

SOURCE = "gmaps"

# ---- 5-layer safety knobs (all env-overridable; DEPLOYMENT_PLAN.md §4.1) ----
MAX_PER_RUN      = int(os.getenv("GMAPS_MAX_PER_RUN", "20"))     # Layer 1: session cap
MAX_PER_DAY      = int(os.getenv("GMAPS_MAX_PER_DAY", "30"))     # Layer 2: real daily ceiling
MIN_INTERVAL_H   = float(os.getenv("GMAPS_MIN_INTERVAL_H", "2")) # Layer 3: min gap between runs
DELAY_MIN        = float(os.getenv("GMAPS_DELAY_MIN", "5"))      # Layer 5: min inter-card delay (s)
DELAY_MAX        = float(os.getenv("GMAPS_DELAY_MAX", "12"))     # Layer 5: max inter-card delay (s)
BLOCK_REST_LOW_H = float(os.getenv("GMAPS_BLOCK_REST_LOW_H", "6"))
BLOCK_REST_HIGH_H = float(os.getenv("GMAPS_BLOCK_REST_HIGH_H", "12"))
BREAKER          = int(os.getenv("GMAPS_BLOCK_BREAKER", "3"))    # blocks/day -> park 24h
PARK_HOURS       = float(os.getenv("GMAPS_PARK_HOURS", "24"))
NAV_TIMEOUT_MS   = int(os.getenv("GMAPS_NAV_TIMEOUT_MS", "45000"))
MAX_SCROLLS      = int(os.getenv("GMAPS_MAX_SCROLLS", "8"))      # politeness cap on feed scrolls


class BlockedError(Exception):
    """Google served a CAPTCHA / consent wall / block page — Layer 4 stop."""


class PlaywrightMissing(Exception):
    """Playwright (or its browser) isn't installed — the bot can't scrape without it."""


# ---------------------------------------------------------------- Layer 4: block detector
# Pure + unit-testable (no browser needed): given the final URL + page HTML, decide the page's nature.
# See DEPLOYMENT_PLAN.md §5 for the reasoning behind each signal.
_BLOCK_URL_MARKERS = ("/sorry/", "consent.google.", "accounts.google.com")
_BLOCK_HTML_MARKERS = ("recaptcha", "unusual traffic", "detected unusual traffic",
                       "are you a robot", "not a robot", "our systems have detected")


def classify_page(url, html):
    """Return 'BLOCKED' | 'OK' | 'AMBIGUOUS'. Strongest signal first (URL host/path), then DOM markers,
    then presence of the results feed. AMBIGUOUS = right host but no feed (silent throttle / slow proxy /
    zero-result city) — the caller treats a repeat AMBIGUOUS as a block."""
    u = (url or "").lower()
    if any(m in u for m in _BLOCK_URL_MARKERS):
        return "BLOCKED"
    h = (html or "").lower()
    if any(m in h for m in _BLOCK_HTML_MARKERS):
        return "BLOCKED"
    if "/maps/place/" in h:          # real results feed contains place links
        return "OK"
    return "AMBIGUOUS"


# ---------------------------------------------------------------- helpers
def _domain(url):
    if not url:
        return None
    host = urlparse(url if "://" in url else "http://" + url).netloc.lower()
    return host[4:] if host.startswith("www.") else (host or None)


def _pick_proxy():
    """One proxy per session. GMAPS_PROXIES = comma-separated pool (rotated), or GMAPS_PROXY = single."""
    pool = [p.strip() for p in os.getenv("GMAPS_PROXIES", "").split(",") if p.strip()]
    if pool:
        return random.choice(pool)
    return os.getenv("GMAPS_PROXY", "").strip() or None


def _import_playwright():
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except Exception as e:  # noqa: BLE001
        raise PlaywrightMissing(
            "Playwright not available. Install it for the Google Maps bot:\n"
            "    pip install playwright && playwright install chromium\n"
            f"(import error: {e})")


def _to_candidate(name, website, phone, region, city, niche):
    if not name:
        return None
    return {
        "legal_name": name.strip(),
        "domain": _domain(website),
        "website_url": website,
        "region": region,
        "city": city,
        "niche": niche,
        "phone": phone,
        "signal": f"Google Maps {niche} in {city}",
        "raw": {"source": "gmaps", "city": city},
    }


def _scrape_cell(region, city, niche, want):
    """Open ONE headless session, scrape up to `want` businesses from a Maps search, return candidates.
    Raises BlockedError on a CAPTCHA/consent/block page (Layer 4), PlaywrightMissing if it can't run.
    Applies the Layer-5 random delay between cards. Selectors target live Maps and may need re-tuning."""
    sync_playwright = _import_playwright()
    proxy = _pick_proxy()
    if not proxy:
        print("  [gmaps] WARNING: no GMAPS_PROXY/GMAPS_PROXIES set — scraping from the raw server IP is\n"
              "          very likely to be CAPTCHA'd fast. Set a residential proxy before real use.")
    query = f"{niche} in {city}"
    url = "https://www.google.com/maps/search/" + quote_plus(query) + "?hl=en"

    launch = {"headless": True, "args": ["--disable-blink-features=AutomationControlled"]}
    if proxy:
        launch["proxy"] = {"server": proxy}

    out = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(**launch)
        ctx = browser.new_context(
            locale="en-US",
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"),
            viewport={"width": 1280, "height": 900},
        )
        page = ctx.new_page()
        try:
            page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
            time.sleep(random.uniform(2.0, 4.0))   # let the feed render / any consent redirect settle

            verdict = classify_page(page.url, page.content())
            if verdict == "BLOCKED":
                raise BlockedError("CAPTCHA/consent/block page on search")
            if verdict == "AMBIGUOUS":
                # right host but no feed — treat as a soft block so we don't hammer a throttling proxy
                raise BlockedError("no results feed (silent throttle / slow proxy / empty city)")

            feed = page.query_selector('div[role="feed"]')
            # scroll the feed to lazy-load more cards, up to what we need (politeness-capped)
            for _ in range(MAX_SCROLLS):
                cards = page.query_selector_all('div[role="feed"] a[href*="/maps/place/"]')
                if len(cards) >= want:
                    break
                if feed:
                    page.evaluate("(el) => el.scrollTo(0, el.scrollHeight)", feed)
                time.sleep(random.uniform(1.5, 3.0))

            cards = page.query_selector_all('div[role="feed"] a[href*="/maps/place/"]')
            for a in cards[:want]:
                name = (a.get_attribute("aria-label") or "").strip() or None
                website = phone = None
                # the card's sibling container usually holds a Website button + phone text
                container = a.evaluate_handle("(el) => el.closest('div[role=\"feed\"] > div')")
                if container:
                    el = container.as_element()
                    if el:
                        wb = el.query_selector('a[data-value="Website"]') or el.query_selector('a[aria-label^="Visit"]')
                        if wb:
                            website = wb.get_attribute("href")
                cand = _to_candidate(name, website, phone, region, city, niche)
                if cand:
                    out.append(cand)
                time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))   # Layer 5: human-like pacing
        finally:
            ctx.close()
            browser.close()
    return out


# ---------------------------------------------------------------- cell selection (mirrors collect_osm)
_ROT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".gmaps_rotation.json")


def _pick_cell(only_region):
    """One (region, city, niche) for this run, rotating cities across runs so we spread geographically."""
    import json
    n = 0
    try:
        with open(_ROT_FILE, encoding="utf-8") as f:
            n = int(json.load(f).get("n", 0))
    except Exception:  # noqa: BLE001
        n = 0
    try:
        with open(_ROT_FILE, "w", encoding="utf-8") as f:
            json.dump({"n": n + 1}, f)
    except Exception:  # noqa: BLE001
        pass
    regions = [r for r in targets.REGION_CITIES
               if targets.REGION_CITIES[r] and (only_region is None or r == only_region)]
    if not regions:
        return None
    region = regions[n % len(regions)]
    cities = targets.REGION_CITIES[region]
    city = cities[n % len(cities)]
    return region, city, targets.DISCOVERY_NICHE


# ---------------------------------------------------------------- main (the 5-layer flow)
def main():
    only_region = targets.active_region()
    # allow  collect_maps.py <REGION> <city> <niche>  ; drop any "--region XX" pair first.
    argv = sys.argv[1:]
    positional, i = [], 0
    while i < len(argv):
        if argv[i] == "--region":
            i += 2                      # skip the flag and its value
            continue
        positional.append(argv[i])
        i += 1

    conn = db.get_connection()
    try:
        # Layers 2 + 3: don't even open a browser if we're resting / capped / too-soon.
        g = governor.can_run(conn, SOURCE, day_cap=MAX_PER_DAY,
                             min_interval_min=int(MIN_INTERVAL_H * 60), breaker=BREAKER)
        if not g["ok"]:
            print(f"[gmaps] not running ({g['reason']}); day {g['day_count']}/{MAX_PER_DAY}, "
                  f"blocks today {g['blocks_today']}. Exiting clean.")
            return
        governor.start_run(conn, SOURCE)

        if len(positional) >= 3:
            region, city, niche = positional[0].upper(), positional[1], positional[2]
        else:
            cell = _pick_cell(only_region)
            if not cell:
                print("[gmaps] no targeted region/city to scrape. Check targets.REGION_CITIES.")
                return
            region, city, niche = cell
        print(f"[gmaps] scraping {region} / {city} / {niche}  "
              f"(run cap {MAX_PER_RUN}, day {g['day_count']}/{MAX_PER_DAY})")

        scraped = 0
        try:
            cands = _scrape_cell(region, city, niche, want=MAX_PER_RUN)   # Layer 1 bound via want
        except PlaywrightMissing as e:
            print(f"  {e}")
            return
        except BlockedError as e:
            mins = governor.back_off(conn, SOURCE, BLOCK_REST_LOW_H, BLOCK_REST_HIGH_H, reason=str(e))
            st = governor.status(conn, SOURCE) or {}
            print(f"  [SAFETY · Layer 4] BLOCK: {e} -> resting ~{mins} min "
                  f"(blocks today {st.get('blocks_today')})")
            if (st.get("blocks_today") or 0) >= BREAKER:
                governor.park(conn, SOURCE, hours=PARK_HOURS, reason="circuit breaker")
                print(f"  [SAFETY] circuit breaker tripped -> parked {PARK_HOURS:.0f}h")
            return

        for cand in cands:
            if scraped >= MAX_PER_RUN:                       # Layer 1
                print(f"  [SAFETY · Layer 1] per-run cap {MAX_PER_RUN} reached — stopping.")
                break
            g2 = governor.can_run(conn, SOURCE, day_cap=MAX_PER_DAY, breaker=BREAKER)  # Layer 2 live re-check
            if not g2["ok"]:
                print(f"  [SAFETY · Layer 2] {g2['reason']} ({g2['day_count']}/{MAX_PER_DAY}) — stopping.")
                break
            verdict = intake.submit(conn, cand, SOURCE)      # dedupes by domain, commits per candidate
            if verdict == "approve":
                governor.record(conn, SOURCE, 1)
                scraped += 1

        governor.reset_fail(conn, SOURCE)                    # clean run -> clear the backoff streak
        print(f"[gmaps] done: {scraped} NEW approved from {region}/{city}. "
              f"Run WF-2 (bot-enrich) to find their emails.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
