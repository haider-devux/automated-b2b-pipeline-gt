"""
FREE discovery collector: companies actively HIRING DEVELOPERS (strong Segment-B intent).
Pulls from two free, no-key sources and funnels each through intake.py:
  - RemoteOK  (https://remoteok.com/api)
  - Remotive  (https://remotive.com/api/remote-jobs?category=software-dev)

Precision: we match the JOB TITLE (not the noisy tags) and skip recruiting/staffing firms (they post
lots of roles but aren't the hiring company / our ICP). Job feeds give intent + company but no domain,
so these leads reach ENRICHED via intent and need a human to add the contact later.

  python collect_jobs.py           # pull recent dev roles from both sources
  python collect_jobs.py 40        # cap total
"""
import re
import sys
import requests
import db
import intake

UA = {"User-Agent": "GranjurBot/0.1 (+https://granjur.com; discovery)"}
REMOTEOK = "https://remoteok.com/api"
REMOTIVE = "https://remotive.com/api/remote-jobs?category=software-dev&limit=60"

DEV_TITLE_RE = re.compile(
    r"react|ios|android|mobile|software|engineer|developer|backend|back-end|frontend|front-end|"
    r"full.?stack|\bml\b|machine learning|devops|golang|\bgo\b|python|node|java|rust|data engineer", re.I)
# recruiters / staffing firms post jobs but aren't the hiring company -> not our ICP
RECRUITER_RE = re.compile(r"recruit|staffing|talent|headhunt|hiring solution|outsourc|consultanc", re.I)


def _region(location):
    l = (location or "").lower()
    if any(k in l for k in ("united states", "usa", "u.s", "america", "canada")):
        return "US"
    if "united kingdom" in l or "britain" in l or l.strip() == "uk":
        return "UK"
    if any(k in l for k in ("europe", "germany", "france", "spain", "netherlands", "poland", "sweden")):
        return "EU"
    if any(k in l for k in ("saudi", "uae", "emirates", "qatar", "dubai", "riyadh", "gcc", "gulf")):
        return "GCC"
    if "china" in l:
        return "CN"
    return "OTHER"


def _cand(company, title, location, url, seen_at, source):
    if not company or RECRUITER_RE.search(company):
        return None
    if not DEV_TITLE_RE.search(title or ""):     # match the TITLE, not the tags
        return None
    return {
        "legal_name": company.strip(),
        "region": _region(location),
        "niche": "tech/saas",
        "job_title": title,
        "signal": f"hiring {title}",
        "active_job_posts": [{"title": title, "url": url, "seen_at": str(seen_at or "")[:10], "source": source}],
        "raw": {"source": source},
    }


def _remoteok():
    r = requests.get(REMOTEOK, headers=UA, timeout=30)
    r.raise_for_status()
    out = []
    for j in r.json():
        if isinstance(j, dict) and j.get("company"):
            c = _cand(j.get("company"), j.get("position") or j.get("title"), j.get("location"),
                      j.get("url") or j.get("apply_url"), j.get("date"), "remoteok")
            if c:
                out.append(c)
    return out


def _remotive():
    r = requests.get(REMOTIVE, headers=UA, timeout=30)
    r.raise_for_status()
    out = []
    for j in r.json().get("jobs", []):
        c = _cand(j.get("company_name"), j.get("title"), j.get("candidate_required_location"),
                  j.get("url"), j.get("publication_date"), "remotive")
        if c:
            out.append(c)
    return out


def main():
    cap = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 30
    cands = []
    for fetch, name in ((_remoteok, "RemoteOK"), (_remotive, "Remotive")):
        try:
            got = fetch()
            print(f"  {name}: {len(got)} dev roles")
            cands += got
        except Exception as e:  # noqa: BLE001
            print(f"  {name} error: {e}")

    conn = db.get_connection()
    tally = {"approve": 0, "reject": 0, "review": 0, "duplicate": 0}
    seen = set()
    try:
        for c in cands:
            key = c["legal_name"].lower()
            if key in seen:
                continue
            seen.add(key)
            tally[intake.submit(conn, c, "jobfeed")] += 1
            if sum(tally.values()) >= cap:
                break
    finally:
        conn.close()
    print(f"Processed: approved {tally['approve']}, rejected {tally['reject']}, "
          f"review {tally['review']}, duplicate {tally['duplicate']}  (region-gated by targets.py)")


if __name__ == "__main__":
    main()
