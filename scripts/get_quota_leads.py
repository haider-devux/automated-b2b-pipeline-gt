r"""
get_quota_leads.py — the daily "quota-filler".

Your high-intent scrapers (OSM homepage + job boards) find ~3-8 send-ready leads/day. This tops the
list up to a STRICT daily target (default 19) from free, bot-friendly public sources, so the pipeline
always has a full batch to work with.

WHAT IT DOES
  1. Deficit  : target (19) minus how many send-ready leads you already have (arg / queued_leads.json / DB).
  2. Overpass : queries OpenStreetMap for businesses in target cities that publish BOTH a `website`
                AND an email tag (`email` or `contact:email`) — because a lead with no email can't be
                emailed. Rotates cities until the deficit is filled or the pool is exhausted.
  3. Fallback : if OSM comes up short, tops up the remainder from a LOCAL public-registry CSV you supply
                (quota_fallback.csv). There is NO free, legal, open dataset of *verified* company emails
                (PDL / CompanyData are paid + gated), so the honest free equivalent is a local list you
                maintain — from directories, chambers of commerce, your own network, past exports, etc.
  4. Output   : writes exactly `deficit` rows to a CSV in the schema wf1.py imports (so run_pipeline can
                pick them straight up). Names are cleaned (LLC / Ltd / GmbH stripped). Each row gets a
                generic `first_line` (these are lower-intent than a job-board trigger).

HONEST NOTE ON "EXACTLY 19": this script never emits MORE than the deficit (never overshoots 19). Whether
it REACHES 19 depends on real data — OSM email coverage varies by city, and some OSM emails are role
inboxes (info@) that the send-gate correctly refuses. The `quota_fallback.csv` lane is what makes 19
guaranteed on any given day: keep ~30 real contacts in it and the filler always has enough to draw from.

    python scripts/get_quota_leads.py --have 3                       # fill 19-3=16 from OSM (+fallback)
    python scripts/get_quota_leads.py --have 3 --target 19 --region GCC
    python scripts/get_quota_leads.py --have 3 --out quota_leads.csv --fallback quota_fallback.csv

Requires:  requests   (already installed).  pandas is OPTIONAL (used for the fallback CSV if present;
falls back to the stdlib csv module otherwise).  ->  pip install requests   (and optionally pandas)
"""
import argparse
import csv
import json
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests

ROOT = Path(__file__).resolve().parent.parent  # scripts/ -> project root
DEFAULT_TARGET = int(os.getenv("GRANJUR_DAILY_TARGET", "19"))

# Overpass mirrors — the user-requested endpoint first, then fallbacks (public mirrors 504 under load).
OVERPASS_URLS = [
    "http://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://lz4.overpass-api.de/api/interpreter",
]
NOMINATIM = "https://nominatim.openstreetmap.org/search"
UA = {"User-Agent": "GranjurBot/0.1 (+https://granjur.com; quota-filler)"}

# City -> region (region_code enum). intake.py REJECTS region 'OTHER', so only use targeted-region cities.
# OSM email coverage is best in EU/UK — those cities are listed first so we fill from them preferentially.
CITY_REGION = {
    # EU (strong OSM email coverage)
    "Berlin": "EU", "Munich": "EU", "Hamburg": "EU", "Amsterdam": "EU", "Vienna": "EU",
    "Barcelona": "EU", "Milan": "EU", "Dublin": "EU", "Copenhagen": "EU", "Lisbon": "EU",
    "Zurich": "EU", "Rotterdam": "EU", "Cologne": "EU", "Frankfurt": "EU",
    # UK
    "London": "UK", "Manchester": "UK", "Birmingham": "UK", "Bristol": "UK", "Leeds": "UK",
    "Glasgow": "UK", "Edinburgh": "UK", "Brighton": "UK",
    # US
    "Austin": "US", "Denver": "US", "Portland": "US", "Seattle": "US", "Chicago": "US", "Boston": "US",
    # GCC
    "Dubai": "GCC", "Abu Dhabi": "GCC", "Riyadh": "GCC", "Doha": "GCC", "Manama": "GCC",
    # AU
    "Sydney": "AU", "Melbourne": "AU", "Brisbane": "AU", "Perth": "AU",
}
_COUNTRY_HINT = {"US": "United States", "UK": "United Kingdom", "AU": "Australia",
                 "GCC": "United Arab Emirates", "EU": ""}

# Output columns: wf1.py imports legal_name/domain/region/city/niche/website_url/email; the rest are
# friendly aliases (wf1 ignores unknown columns). Kept in the schema so the file is human-readable too.
OUT_COLUMNS = ["company_name", "legal_name", "website", "website_url", "domain", "email",
               "region", "country", "city", "niche", "osm_category", "first_line"]

# legal suffixes to strip from a business name (requirement 3)
_SUFFIX_RE = re.compile(
    r"[\s,]+(l\.?l\.?c|inc|corp(oration)?|co|company|ltd|limited|llp|lp|plc|gmbh|ag|s\.?a|s\.?l|"
    r"b\.?v|pty|pvt|bhd|sdn|srl|oy|ab|as)\.?$", re.I)


# --------------------------------------------------------------------------- deficit
def current_have(args):
    """How many send-ready leads we already have: --have wins, then queued_leads.json, then the DB, then 0."""
    if args.have is not None:
        return max(0, args.have)
    qj = ROOT / "queued_leads.json"
    if qj.exists():
        try:
            data = json.loads(qj.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return len(data)
            if isinstance(data, dict):
                return int(data.get("count", data.get("have", 0)))
        except Exception:  # noqa: BLE001 — a malformed file just falls through to the DB / 0
            pass
    try:  # optional: count QUEUED_FOR_OUTREACH straight from Postgres if reachable
        import importlib.util
        import psycopg2
        spec = importlib.util.spec_from_file_location("wf3cfg", ROOT / "wf3_python" / "config.py")
        cfg = importlib.util.module_from_spec(spec); spec.loader.exec_module(cfg)
        conn = psycopg2.connect(host=cfg.DB["host"], port=cfg.DB["port"], dbname=cfg.DB["dbname"],
                                user=cfg.DB["user"], password=cfg.DB["password"]); conn.autocommit = True
        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FROM leads WHERE status='QUEUED_FOR_OUTREACH';")
            n = cur.fetchone()[0]
        conn.close()
        return int(n)
    except Exception:  # noqa: BLE001 — no DB? assume 0 already have
        return 0


# --------------------------------------------------------------------------- name / url helpers
def clean_name(name):
    n = re.sub(r"\s+", " ", (name or "").strip())
    n = n.split(" / ")[0].split(" | ")[0].strip()      # drop "Brand A / Brand B" tails
    prev = None
    while n and n != prev:                              # strip stacked suffixes: "Foo Co Ltd"
        prev = n
        n = _SUFFIX_RE.sub("", n).strip(" ,.-")
    return n or (name or "").strip()


def domain_of(url):
    if not url:
        return None
    host = urlparse(url if "://" in url else "http://" + url).netloc.lower()
    return host[4:] if host.startswith("www.") else (host or None)


def first_line_for(company, category, city):
    cat = (category or "business").replace("_", " ")
    where = f" in {city}" if city else ""
    return (f"I came across {company} while looking at {cat} businesses{where} and had a quick, "
            f"specific idea for making your website work harder — mind if I share it in a sentence?")


# --------------------------------------------------------------------------- Overpass
def geocode(city, region):
    q = f"{city}, {_COUNTRY_HINT.get(region, '')}".strip(", ")
    r = requests.get(NOMINATIM, params={"q": q, "format": "json", "limit": 1}, headers=UA, timeout=30)
    r.raise_for_status()
    data = r.json()
    if not data:
        return None
    s, n, w, e = data[0]["boundingbox"]
    return float(s), float(w), float(n), float(e)


def overpass(query, attempts=2):
    last = None
    for _ in range(attempts):
        for url in OVERPASS_URLS:
            try:
                r = requests.post(url, data={"data": query}, headers=UA, timeout=60)
                r.raise_for_status()
                return r.json().get("elements", [])
            except Exception as e:  # noqa: BLE001 — try the next mirror
                last = e
        time.sleep(5)
    raise RuntimeError(f"all Overpass mirrors failed ({last})")


def _query(bbox, limit=200):
    s, w, n, e = bbox
    # businesses that publish a website AND an email — the only ones we can actually contact.
    return f"""
[out:json][timeout:60];
(
  nwr["website"]["email"][!"brand"]({s},{w},{n},{e});
  nwr["website"]["contact:email"][!"brand"]({s},{w},{n},{e});
);
out center {limit};
"""


def _to_row(el, city, region):
    t = el.get("tags", {})
    name = t.get("name")
    email = (t.get("email") or t.get("contact:email") or "").split(";")[0].strip()
    website = t.get("website") or t.get("contact:website")
    if not name or not email or not website:
        return None
    if "@" not in email:
        return None
    company = clean_name(name)
    category = t.get("shop") or t.get("office") or t.get("amenity") or "business"
    dom = domain_of(website)
    return {
        "company_name": company, "legal_name": company,
        "website": website, "website_url": website, "domain": dom,
        "email": email, "region": region, "country": "",
        "city": t.get("addr:city") or city, "niche": category, "osm_category": category,
        "first_line": first_line_for(company, category, t.get("addr:city") or city),
    }


def collect_from_overpass(deficit, only_region=None, cities=None, verbose=True):
    """Gather up to `deficit` unique (by domain/email) website+email businesses from OSM."""
    rows, seen = [], set()
    pool = cities or [c for c in CITY_REGION if (only_region is None or CITY_REGION[c] == only_region)]
    for city in pool:
        if len(rows) >= deficit:
            break
        region = CITY_REGION.get(city, "EU")
        try:
            bbox = geocode(city, region)
            time.sleep(1)
            if not bbox:
                continue
            elements = overpass(_query(bbox))
        except Exception as e:  # noqa: BLE001 — skip a failing city, keep going
            if verbose:
                print(f"  [{city}] skipped: {str(e)[:60]}")
            continue
        got = 0
        for el in elements:
            if len(rows) >= deficit:
                break
            row = _to_row(el, city, region)
            if not row:
                continue
            key = (row["domain"] or "").lower() or row["email"].lower()
            if not key or key in seen:
                continue
            seen.add(key)
            rows.append(row)
            got += 1
        if verbose:
            print(f"  [{city} · {region}] +{got} (running total {len(rows)}/{deficit})")
        time.sleep(1)                       # be polite to the free servers
    return rows


# --------------------------------------------------------------------------- fallback CSV
def collect_from_fallback(deficit, path, skip_domains):
    """Top up from a LOCAL registry CSV (real contacts you maintain). Accepts flexible headers."""
    p = Path(path)
    if not p.exists():
        return []
    try:
        import pandas as pd                 # optional, per the spec
        records = pd.read_csv(p).to_dict("records")
    except Exception:                        # noqa: BLE001 — no pandas / read issue -> stdlib csv
        with open(p, newline="", encoding="utf-8-sig") as f:
            records = list(csv.DictReader(f))
    out = []
    for r in records:
        if len(out) >= deficit:
            break
        g = lambda *keys: next((str(r[k]).strip() for k in keys if r.get(k) not in (None, "")), "")  # noqa: E731
        name = clean_name(g("company_name", "legal_name", "name"))
        email = g("email", "contact_email")
        website = g("website", "website_url", "url")
        if not name or "@" not in email:
            continue
        dom = domain_of(website) or email.split("@")[-1]
        if dom and dom.lower() in skip_domains:
            continue
        region = (g("region") or "EU").upper()
        city = g("city")
        cat = g("niche", "osm_category", "category") or "business"
        out.append({
            "company_name": name, "legal_name": name, "website": website, "website_url": website,
            "domain": dom, "email": email, "region": region, "country": g("country"),
            "city": city, "niche": cat, "osm_category": cat,
            "first_line": g("first_line") or first_line_for(name, cat, city),
        })
    return out


# --------------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser(description="Fill the daily lead quota from free public sources.")
    ap.add_argument("--have", type=int, help="send-ready leads you already have (else reads "
                    "queued_leads.json, then the DB, then assumes 0)")
    ap.add_argument("--target", type=int, default=DEFAULT_TARGET, help=f"daily target (default {DEFAULT_TARGET})")
    ap.add_argument("--out", default=str(ROOT / "quota_leads.csv"), help="output CSV path")
    ap.add_argument("--region", help="restrict OSM cities to one region (US/EU/UK/GCC/AU)")
    ap.add_argument("--cities", help="comma-separated city list to override the built-in pool")
    ap.add_argument("--fallback", default=str(ROOT / "quota_fallback.csv"),
                    help="local curated contacts CSV to top up from if OSM is short (default ./quota_fallback.csv)")
    ap.add_argument("--registry", default=str(ROOT / "public_registry.csv"),
                    help="a downloaded bulk public-registry CSV to top up from last "
                         "(see import_public_registry.py; default ./public_registry.csv)")
    args = ap.parse_args()

    have = current_have(args)
    deficit = max(0, args.target - have)
    print(f"Have {have} send-ready lead(s); target {args.target} -> need {deficit} more.")
    if deficit == 0:
        Path(args.out).write_text(",".join(OUT_COLUMNS) + "\n", encoding="utf-8")
        print("Quota already met — wrote an empty top-up file.")
        return

    region = (args.region or "").strip().upper() or None
    cities = [c.strip() for c in args.cities.split(",")] if args.cities else None

    print(f"\n1) OpenStreetMap (website + email businesses)...")
    rows = collect_from_overpass(deficit, only_region=region, cities=cities)

    if len(rows) < deficit:
        need = deficit - len(rows)
        skip = {(r["domain"] or "").lower() for r in rows if r["domain"]}
        print(f"\n2) OSM gave {len(rows)}/{deficit}. Topping up {need} from curated fallback: {args.fallback}")
        rows += collect_from_fallback(need, args.fallback, skip)

    if len(rows) < deficit and Path(args.registry).exists():
        need = deficit - len(rows)
        skip = {(r["domain"] or "").lower() for r in rows if r.get("domain")}
        print(f"\n3) Still {len(rows)}/{deficit}. Topping up {need} from public registry: {args.registry}")
        try:
            from import_public_registry import load_registry
            rows += load_registry(args.registry, limit=need, skip_domains=skip)
        except Exception as e:  # noqa: BLE001 — registry issues must not crash the quota run
            print(f"  registry top-up failed: {e}")

    rows = rows[:deficit]                    # never overshoot the target
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=OUT_COLUMNS)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in OUT_COLUMNS})

    print(f"\nWrote {len(rows)} quota lead(s) -> {args.out}")
    if len(rows) < deficit:
        print(f"STILL SHORT by {deficit - len(rows)}: free OSM email coverage was thin and the fallback "
              f"CSV\n  ({args.fallback}) didn't have enough rows. Add real contacts to that file to "
              f"guarantee the\n  full {args.target} — the reliable lane. Columns: "
              f"company_name,website,email,region,city,niche.")
    else:
        print(f"Quota filled: {have} high-intent + {len(rows)} top-up = {args.target}. "
              f"Import with wf1.py (run_pipeline does this automatically).")


if __name__ == "__main__":
    main()
