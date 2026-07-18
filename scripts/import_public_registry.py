r"""
import_public_registry.py — turn a FREE bulk public business-registry CSV into send-ready seed leads.

Reads a downloaded open dataset (UK Companies House bulk data, OpenCorporates, People Data Labs free
company dump, a city "active business licenses" export, etc.), cleans it, and maps it into the schema
wf1.py imports — so it can feed the daily quota alongside OpenStreetMap.

  python scripts/import_public_registry.py --in companies_house.csv --out registry_leads.csv --limit 16
  python scripts/import_public_registry.py --in pdl_companies.csv --out registry_leads.csv --region UK

HONEST DATA REALITY (read this):
  * Registries with a DOMAIN column (People Data Labs, some city license exports) work well — we can build
    a contact address from the domain.
  * Registries WITHOUT a website/domain (UK Companies House bulk data is name + number + address + SIC only)
    give you company records but NO way to email them. Rows with no domain are skipped and reported — the
    registry alone can't make them send-ready.

THE info@ GUESS (Part 2 requirement) — done SAFELY:
  When a row has a website but no email, we construct `info@<domain>` under the relaxed small-local rule.
  BUT we FIRST check the domain actually accepts mail (an MX DNS lookup). A guessed address on a domain
  with no MX is a guaranteed bounce, and hard bounces wreck a fresh mailbox's reputation — so we drop it.
  Guessed addresses are marked `unverified` (email_status column) so you can see which are guesses, and the
  pipeline's bounce-suppression removes any that fail on the first send. Use `--no-guess` to disable.

Requires:  pandas  (pip install pandas)  +  dnspython (already installed).  Falls back to the stdlib csv
module if pandas is missing.
"""
import argparse
import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent  # scripts/ -> project root
sys.path.insert(0, str(Path(__file__).resolve().parent))   # scripts/ — where get_quota_leads.py lives
# Reuse the quota-filler's helpers so cleaning / region mapping / schema stay identical across both tools.
from get_quota_leads import CITY_REGION, OUT_COLUMNS, clean_name, domain_of, first_line_for  # noqa: E402

try:
    import dns.resolver
    _HAVE_DNS = True
except Exception:  # noqa: BLE001
    _HAVE_DNS = False

_mx_cache = {}

# Flexible header matching — open datasets name these columns a dozen different ways.
_NAME_KEYS = ["company_name", "companyname", "name", "legal_name", "organisation_name", "organization",
              "business_name", "dba_name", "entity_name"]
_CITY_KEYS = ["city", "town", "reg_address.posttown", "regaddress.posttown", "locality",
              "addr_city", "address_city", "post_town"]
_WEB_KEYS = ["website", "website_url", "url", "domain", "homepage", "web"]
_EMAIL_KEYS = ["email", "contact_email", "e-mail", "email_address"]


def _get(row, keys):
    low = {str(k).strip().lower(): v for k, v in row.items()}
    for k in keys:
        v = low.get(k)
        if v is not None and str(v).strip() not in ("", "nan", "None"):
            return str(v).strip()
    return ""


def has_mx(domain):
    """True if the domain publishes an MX record (can receive mail). Cached. Fail-closed if DNS is down."""
    if not domain:
        return False
    if domain in _mx_cache:
        return _mx_cache[domain]
    ok = False
    if _HAVE_DNS:
        try:
            ans = dns.resolver.resolve(domain, "MX", lifetime=5.0)
            ok = len(ans) > 0
        except Exception:  # noqa: BLE001 — NXDOMAIN / no MX / timeout -> treat as "can't receive"
            ok = False
    _mx_cache[domain] = ok
    return ok


def _region_for(city, default_region):
    return CITY_REGION.get((city or "").strip().title(), default_region)


def load_registry(path, limit=None, guess_email=True, require_mx=True, default_region="OTHER",
                  skip_domains=None, verbose=True):
    """Parse a registry CSV -> list of rows in the wf1/quota schema. Skips rows we can't email."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"registry file not found: {path}")
    try:
        import pandas as pd
        records = pd.read_csv(p, dtype=str, keep_default_na=False).to_dict("records")
    except Exception:  # noqa: BLE001 — no pandas / parse issue -> stdlib csv
        with open(p, newline="", encoding="utf-8-sig") as f:
            records = list(csv.DictReader(f))

    skip_domains = {d.lower() for d in (skip_domains or set())}
    out, seen = [], set()
    n_nodomain = n_nomx = n_dupe = 0
    for r in records:
        if limit is not None and len(out) >= limit:
            break
        name = clean_name(_get(r, _NAME_KEYS))
        if not name:
            continue
        city = _get(r, _CITY_KEYS)
        website = _get(r, _WEB_KEYS)
        email = _get(r, _EMAIL_KEYS)
        domain = domain_of(website) if website else (domain_of(email.split("@")[-1]) if "@" in email else None)

        status = "provided"
        if not email:
            if not (guess_email and domain):
                n_nodomain += 1
                continue                                    # no email + no domain -> can't contact; skip
            if require_mx and not has_mx(domain):
                n_nomx += 1
                continue                                    # domain can't receive mail -> would bounce; skip
            email = f"info@{domain}"                        # relaxed small-local guess, MX-checked
            status = "guessed"

        key = (domain or email).lower()
        if not key or key in seen or key in skip_domains:
            n_dupe += 1
            continue
        seen.add(key)
        region = _region_for(city, default_region)
        cat = "local_business"
        out.append({
            "company_name": name, "legal_name": name, "website": website or (f"https://{domain}" if domain else ""),
            "website_url": website or (f"https://{domain}" if domain else ""), "domain": domain,
            "email": email, "region": region, "country": "", "city": city,
            "niche": cat, "osm_category": cat, "first_line": first_line_for(name, cat, city),
            "email_status": status,
        })
    if verbose:
        print(f"  registry: kept {len(out)} | skipped {n_nodomain} no-domain, {n_nomx} no-MX, {n_dupe} dupes")
    return out


def main():
    ap = argparse.ArgumentParser(description="Import a free public business-registry CSV into seed leads.")
    ap.add_argument("--in", dest="infile", required=True, help="downloaded registry CSV")
    ap.add_argument("--out", default=str(ROOT / "registry_leads.csv"), help="output seed CSV")
    ap.add_argument("--limit", type=int, help="cap number of rows (e.g. today's deficit)")
    ap.add_argument("--region", default="OTHER", help="default region for rows whose city we can't map")
    ap.add_argument("--no-guess", action="store_true", help="do NOT construct info@domain when email missing")
    ap.add_argument("--no-mx", action="store_true", help="skip the MX check on guessed emails (NOT advised)")
    args = ap.parse_args()

    rows = load_registry(args.infile, limit=args.limit, guess_email=not args.no_guess,
                         require_mx=not args.no_mx, default_region=args.region.upper())
    cols = OUT_COLUMNS + (["email_status"] if rows and "email_status" in rows[0] else [])
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in cols})
    guessed = sum(1 for r in rows if r.get("email_status") == "guessed")
    print(f"Wrote {len(rows)} lead(s) -> {args.out}  ({guessed} guessed info@ addresses, MX-checked).")
    if guessed:
        print("NOTE: guessed addresses are 'unverified' — keep daily volume modest and let bounce-suppression "
              "prune failures. Review before a big LIVE send.")


if __name__ == "__main__":
    main()
