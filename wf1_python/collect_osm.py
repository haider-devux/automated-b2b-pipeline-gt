"""
FREE discovery collector: OpenStreetMap (no key, no card) — the free replacement for paid Google Maps.

Works for ANY city worldwide: it geocodes the city with Nominatim to get a bounding box, then asks
Overpass for shops/offices with a website inside that box. Each result funnels through intake.py
(auto approve / reject / review). Chains (brand-tagged) and big-box types are skipped.

  python collect_osm.py                       # rotates one city per region (targets.REGION_CITIES)
  python collect_osm.py GCC Dubai business     # one cell: region, city, niche-hint
"""
import json
import os
import random
import sys
import time
from urllib.parse import urlparse
import requests

# Governor lives in scripts/ (shared across the fleet). APPEND so wf1_python's own db/config still win.
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import governor  # noqa: E402

import db
import intake
import targets

SOURCE = "osm"
# Generous daily ceiling (OSM is free + low-risk; the real guards are the bbox cache + Overpass backoff).
OSM_MAX_PER_DAY = int(os.getenv("OSM_MAX_PER_DAY", "200"))
# How long to rest bot-osm when every Overpass mirror is failing (transient overload / soft rate-limit).
OSM_BACKOFF_HOURS = float(os.getenv("OSM_BACKOFF_HOURS", "1"))

# public Overpass mirrors (often overloaded -> try in turn)
OVERPASS_URLS = [
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass-api.de/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
]
NOMINATIM = "https://nominatim.openstreetmap.org/search"
UA = {"User-Agent": "GranjurBot/0.1 (+https://granjur.com; discovery)"}
PER_CELL = int(os.getenv("GRANJUR_PER_CELL", "5"))  # NEW leads we want per city (per region). Wider
                 # funnel (5) so the pipeline can net ~19 send-ready/day after the email-yield cut.
FETCH_POOL = 90  # how many candidates to PULL from Overpass per city, to then pick PER_CELL *new* ones
                 # from (after skipping ones already in the DB). Bigger pool = deeper into the city.

# rotation state: which city index each no-arg run uses (persisted so successive runs spread out)
_ROT_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".discovery_rotation.json")

# help Nominatim disambiguate a city by region (blank = rely on the city name alone)
_COUNTRY_HINT = {"US": "United States", "UK": "United Kingdom", "AU": "Australia", "CN": "China"}


def _geocode(city, region):
    """Return (south, west, north, east) bounding box for the city, or None."""
    q = f"{city}, {_COUNTRY_HINT[region]}" if region in _COUNTRY_HINT else city
    r = requests.get(NOMINATIM, params={"q": q, "format": "json", "limit": 1},
                     headers=UA, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data:
        return None
    s, n, w, e = data[0]["boundingbox"]          # Nominatim: [south, north, west, east]
    return float(s), float(w), float(n), float(e)


def _overpass(query, attempts=2):
    """Query Overpass, rotating mirrors. Public mirrors 504 under load, so make a couple of passes
    with a short backoff before giving up — a transient timeout shouldn't waste a whole discovery round."""
    last = None
    for attempt in range(attempts):
        for url in OVERPASS_URLS:
            try:
                r = requests.post(url, data={"data": query}, headers=UA, timeout=60)
                r.raise_for_status()
                return r.json().get("elements", [])
            except Exception as e:  # noqa: BLE001 — try the next mirror
                last = e
        if attempt + 1 < attempts:
            time.sleep(5)   # brief backoff, then another pass over the mirrors
    raise RuntimeError(f"all Overpass mirrors failed after {attempts} pass(es) ({last})")


def _query(bbox, limit):
    s, w, n, e = bbox
    # [!"brand"] excludes franchise/chain locations -> independent local businesses (our ICP)
    return f"""
[out:json][timeout:40];
(
  nwr["shop"]["website"][!"brand"]({s},{w},{n},{e});
  nwr["office"]["website"][!"brand"]({s},{w},{n},{e});
);
out center {limit};
"""


def _domain(url):
    if not url:
        return None
    host = urlparse(url if "://" in url else "http://" + url).netloc.lower()
    return host[4:] if host.startswith("www.") else (host or None)


def _to_candidate(el, region, city, niche_hint):
    t = el.get("tags", {})
    name = t.get("name")
    if not name:
        return None
    stype = t.get("shop") or t.get("office")
    if t.get("brand") or stype in targets.BLOCKLIST_SHOP_TYPES:   # skip chains / big-box
        return None
    return {
        "legal_name": name,
        "domain": _domain(t.get("website") or t.get("contact:website")),
        "website_url": t.get("website") or t.get("contact:website"),
        "region": region,
        "city": t.get("addr:city") or city,
        "niche": stype or niche_hint,
        "phone": t.get("phone") or t.get("contact:phone"),
        "email": t.get("email") or t.get("contact:email"),   # OSM sometimes has one — free contact!
        "signal": f"OSM {stype or 'business'} in {city}",
        "raw": {"osm_id": el.get("id"), "osm_type": el.get("type")},
    }


def collect_cell(conn, region, city, niche_hint):
    """Collect one city cell. Returns a status: 'ok' | 'geocode_fail' | 'overpass_fail' | 'capped'.
    Uses the bbox cache so a city is geocoded via Nominatim ONCE, then records approved leads to the
    governor's daily counter (bot-osm)."""
    print(f"\n[{region} - {city} - {niche_hint}] querying OpenStreetMap...")
    # Layer 2 (per-day cap): don't start a new cell if bot-osm has hit its daily ceiling.
    if not governor.can_run(conn, SOURCE, day_cap=OSM_MAX_PER_DAY)["ok"]:
        print("  osm daily cap reached — skipping cell (will resume tomorrow)")
        return "capped"

    # Nominatim ONCE per city: serve the bounding box from cache; only geocode on a miss.
    bbox = db.get_cached_bbox(conn, region, city)
    if not bbox:
        try:
            bbox = _geocode(city, region)
        except Exception as e:  # noqa: BLE001
            print(f"  geocode failed: {e}")
            return "geocode_fail"
        if not bbox:
            print(f"  could not locate '{city}' - skipping")
            return "geocode_fail"
        db.cache_bbox(conn, region, city, bbox)   # persist so we never re-hit Nominatim for this city
    try:
        # Pull a big pool, then keep only PER_CELL *new* ones. intake.submit() dedupes on domain
        # (already-in-DB companies come back 'duplicate'), so each run surfaces DIFFERENT businesses
        # until the city is exhausted — instead of re-fetching the same top-2 every time.
        elements = _overpass(_query(bbox, FETCH_POOL))
    except Exception as e:  # noqa: BLE001
        print(f"  Overpass error: {e}")
        return "overpass_fail"
    random.shuffle(elements)   # vary which candidates we try each run (more variety, less repetition)
    tally = {"approve": 0, "reject": 0, "review": 0, "duplicate": 0}
    scanned = 0
    for el in elements:
        if tally["approve"] >= PER_CELL:   # got our small batch of NEW companies — stop
            break
        cand = _to_candidate(el, region, city, niche_hint)
        if not cand:
            continue
        scanned += 1
        verdict = intake.submit(conn, cand, "osm")
        tally[verdict] += 1
        if verdict == "approve":
            governor.record(conn, SOURCE, 1)     # count toward the daily ceiling
    got = tally["approve"]
    depleted = " (city looks exhausted — rotation will move on)" if got < PER_CELL else ""
    print(f"  scanned {scanned}/{len(elements)} -> {got} NEW approved (target {PER_CELL}){depleted}; "
          f"skipped {tally['duplicate']} already-seen, {tally['reject']} rejected, {tally['review']} to review")
    return "ok"


def _next_rotation():
    """Read+advance the persisted run counter, so each no-arg run picks the next city per region."""
    n = 0
    try:
        with open(_ROT_FILE, encoding="utf-8") as f:
            n = int(json.load(f).get("n", 0))
    except Exception:  # noqa: BLE001 — missing/corrupt file just restarts the rotation at 0
        n = 0
    try:
        with open(_ROT_FILE, "w", encoding="utf-8") as f:
            json.dump({"n": n + 1}, f)
    except Exception:  # noqa: BLE001 — if we can't persist, we simply don't advance (still works)
        pass
    return n


def _rotated_cells(only_region=None):
    """One city per region for this run, rotating across targets.REGION_CITIES on each run.

    If only_region is given, discovery is isolated to that single market's city pool."""
    n = _next_rotation()
    niche = targets.DISCOVERY_NICHE
    cells = [(region, cities[n % len(cities)], niche)
             for region, cities in targets.REGION_CITIES.items()
             if cities and (only_region is None or region == only_region)]
    print(f"Rotation #{n} -> " + ", ".join(f"{r}:{c}" for r, c, _ in cells))
    return cells


def main():
    # --region XX isolates discovery to one market; also honours GRANJUR_REGION (set by run_pipeline).
    only_region = targets.active_region()
    # Drop the "--region XX" pair, then read any positional  <REGION> <city> <niche>  override.
    args = sys.argv[1:]
    positional = []
    i = 0
    while i < len(args):
        if args[i] == "--region":
            i += 2                      # skip the flag and its value
            continue
        positional.append(args[i])
        i += 1

    if len(positional) >= 3:
        cells = [(positional[0].upper(), positional[1], positional[2])]
    else:
        if only_region:
            print(f"REGION ISOLATION: discovering in {only_region} only.")
        cells = _rotated_cells(only_region)
    conn = db.get_connection()
    try:
        # Layers 2 + 3: if bot-osm is resting (Overpass backoff) or capped for the day, no-op and exit.
        g = governor.can_run(conn, SOURCE, day_cap=OSM_MAX_PER_DAY)
        if not g["ok"]:
            print(f"[osm] not running ({g['reason']}); day {g['day_count']}/{OSM_MAX_PER_DAY}. Exiting clean.")
            return
        governor.start_run(conn, SOURCE)

        overpass_failed = False
        for i, (region, city, niche) in enumerate(cells):
            if i:
                time.sleep(3)   # be polite to the free Nominatim + Overpass servers
            if collect_cell(conn, region, city, niche) == "overpass_fail":
                overpass_failed = True

        # Layer 4: every mirror failing looks like an overload/soft rate-limit — rest before retrying.
        if overpass_failed:
            mins = governor.back_off(conn, SOURCE, OSM_BACKOFF_HOURS, OSM_BACKOFF_HOURS,
                                     reason="overpass mirrors failing")
            print(f"[osm] Overpass unavailable -> resting ~{mins} min before the next run.")
        else:
            governor.reset_fail(conn, SOURCE)   # clean run — clear any prior backoff streak
    finally:
        conn.close()
    print("\nDone. Approved candidates are now DISCOVERED - run WF-2 to enrich them.")


if __name__ == "__main__":
    main()
