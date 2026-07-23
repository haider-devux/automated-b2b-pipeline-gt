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
import os
import re
import sys
import requests

# Governor lives in scripts/ (shared across the fleet). APPEND so wf1_python's own db/config still win.
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import governor  # noqa: E402

import db
import intake

UA = {"User-Agent": "GranjurBot/0.1 (+https://granjur.com; discovery)"}
REMOTEOK = "https://remoteok.com/api"
REMOTIVE = "https://remotive.com/api/remote-jobs?category=software-dev&limit=60"

# Each feed is its own bot (bot-remoteok / bot-remotive) with its own governor bucket: a small daily
# pull cap and a rest-on-failure backoff so a cron loop never hammers a rate-limiting feed.
JOBS_MAX_PULLS_PER_DAY = int(os.getenv("JOBS_MAX_PULLS_PER_DAY", "6"))
JOBS_BACKOFF_HOURS = float(os.getenv("JOBS_BACKOFF_HOURS", "2"))

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


_SOURCES = {"remoteok": (_remoteok, "RemoteOK"), "remotive": (_remotive, "Remotive")}


def main():
    # Args:  collect_jobs.py [remoteok|remotive|both] [cap]   (both defaults for backward-compat).
    which = "both"
    cap = 30
    for a in sys.argv[1:]:
        if a.isdigit():
            cap = int(a)
        elif a.lower() in ("remoteok", "remotive", "both"):
            which = a.lower()
    sources = ["remoteok", "remotive"] if which == "both" else [which]

    conn = db.get_connection()
    tally = {"approve": 0, "reject": 0, "review": 0, "duplicate": 0}
    seen = set()
    try:
        for src in sources:
            fetch, name = _SOURCES[src]
            # Layer 2 (per-day pull cap) + Layer 3/4 (rest_until backoff): skip a feed that's capped/resting.
            g = governor.can_run(conn, src, day_cap=JOBS_MAX_PULLS_PER_DAY)
            if not g["ok"]:
                print(f"  {name}: skipped ({g['reason']}; {g['day_count']}/{JOBS_MAX_PULLS_PER_DAY} pulls today)")
                continue
            try:
                got = fetch()
                print(f"  {name}: {len(got)} dev roles")
                governor.record(conn, src, 1)          # count this successful pull
                governor.reset_fail(conn, src)
            except Exception as e:  # noqa: BLE001 — a 429/timeout should back the feed off, not crash the run
                mins = governor.back_off(conn, src, JOBS_BACKOFF_HOURS, JOBS_BACKOFF_HOURS, reason=str(e)[:120])
                print(f"  {name} error: {e} -> resting ~{mins} min")
                continue
            for c in got:
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
