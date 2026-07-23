r"""
Granjur B2B pipeline — ONE command to run the whole thing.

    python scripts/run_pipeline.py

That's it. No "launch dashboard / send test / kill cache" dance. This orchestrates the four
decoupled phases end-to-end and stops when today's send budget is filled:

    0. RE-ARM     (wf3_python/rearm_cooldown.py) elapsed COOLDOWN leads flow back to QUALIFIED
    1. DISCOVER   (wf1_python/collect_osm.py)   free OpenStreetMap businesses, all regions
    2. ENRICH     (wf2_python/wf2.py)           find a public email + tech from each site
    3. QUALIFY    (wf3_python/wf3.py)           segment + write the AI pitch (local Ollama)
    4. AUTO-APPROVE (wf4_python/auto_approve.py) queue every send-safe QUALIFIED lead
       ... loop 1-4 until TARGET unique, send-ready leads are queued ...
    4b. QUOTA-FILL (get_quota_leads.py)          if the crawl fell short, top up the balance to TARGET
                                                 from free public sources (OSM website+email + fallback CSV)
    5. SEND       (wf4_python/wf4.py)            send them (dry-run unless GRANJUR_DRY_RUN=0)
    6. FOLLOW-UP  (wf4_python/followup.py)       send any drip nudges that are due
    7. EXPORT     (export_leads_csv.py)          write exports/discovered.xlsx (raw discovery lake +
                                                 By-Source sheet), a dated snapshot, and update the central
                                                 CRM (exports/granjur_central.xlsx, journey on Latest Leads)

TARGET is a FIXED daily quota of send-ready leads (default 19 — override with GRANJUR_DAILY_TARGET or
--count). Only leads that actually reach outreach count toward it; ERROR / NEEDS_CONTACT never do.
Free sources yield many leads with no personal email (they park as NEEDS_CONTACT), so the loop keeps
discovering fresh, UNIQUE companies (dedup is by domain in the DB) until it nets the 19 or the
discovery pool runs dry. wf4's own warmup cap is the final safety authority on how many actually leave
today, so you can never overshoot and burn the mailbox.

SAFE BY DEFAULT: this is a DRY RUN unless you set GRANJUR_DRY_RUN=0. Dry-run marks leads CONTACTED
and logs the exact payload, but sends nothing. To actually send:

    $env:GRANJUR_DRY_RUN = "0"; python scripts/run_pipeline.py

Common flags:
    --count N          override today's target (default = warmup remaining)
    --region GCC       isolate the whole run to one market (US/EU/UK/GCC/CN/AU)
    --max-rounds R     safety cap on discovery loops (default 12)
    --jobs             also pull hiring-intent leads from free job boards each round
    --skip-discovery   don't discover; just enrich/qualify/send whatever's already in the DB
    --no-send          build & queue the leads + CSV, but stop before the send step
    --test EMAIL       build the queue, then send every pitch to EMAIL as a preview (real send to your OWN
                       inbox; DB untouched, no follow-ups). Bare --test uses GMAIL_ADDRESS.
    --seed CSV         import a contacts CSV first (guaranteed send-ready lane; auto-detects
                       ./seed_leads.csv). CSV columns: legal_name, domain, region, city, niche,
                       website_url, email, first_name, last_name, job_title  (see wf1_python/wf1.py)
    --collect/--send   accepted for backward-compatibility (both are the default now)
"""
import argparse
import importlib.util
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import psycopg2

# Windows consoles default to cp1252; force UTF-8 so the banner's dashes render cleanly, not as mojibake.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:  # noqa: BLE001 — older Python / non-reconfigurable stream
    pass

ROOT = Path(__file__).resolve().parent.parent  # scripts/ -> project root
PY = ROOT / "wf3_python" / ".venv" / "Scripts" / "python.exe"
if not PY.exists():                       # non-Windows / different venv layout fallback
    PY = Path(sys.executable)

VALID_REGIONS = {"US", "EU", "UK", "GCC", "CN", "AU", "OTHER"}


def _daily_target():
    """Fixed number of send-ready leads to line up + send every day (only leads that actually reach
    outreach count — ERROR / NEEDS_CONTACT never do). Override with env GRANJUR_DAILY_TARGET or --count."""
    try:
        return max(0, int(os.getenv("GRANJUR_DAILY_TARGET", "19")))
    except ValueError:
        return 19


DAILY_TARGET = _daily_target()


# ----------------------------------------------------------------- infra helpers
def _load_module(rel_path, name):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def db_config():
    return _load_module("wf3_python/config.py", "wf3_config_for_pipe").DB


def connect():
    d = db_config()
    conn = psycopg2.connect(host=d["host"], port=d["port"], dbname=d["dbname"],
                            user=d["user"], password=d["password"])
    # Autocommit so this read-only orchestrator connection never holds a transaction open. A held
    # transaction would keep a lock on the lead_status enum type and DEADLOCK any child phase that
    # runs `ALTER TYPE lead_status ADD VALUE` (wf3/rearm ensure_status_values). Reads only here.
    conn.autocommit = True
    return conn


def count_status(conn, status, region=None):
    sql = ("SELECT count(*) FROM leads l JOIN companies c ON c.id=l.company_id "
           "WHERE l.status=%s::lead_status")
    params = [status]
    if region:
        sql += " AND c.region=%s::region_code"
        params.append(region)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchone()[0]


def count_companies(conn):
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM companies;")
        return cur.fetchone()[0]


def warmup_remaining(conn):
    """Today's remaining send budget straight from the Email-Health engine (single source of truth)."""
    try:
        dh = _load_module("wf4_python/domain_health.py", "domain_health_for_pipe")
        return int(dh.report(conn=conn)["warmup"]["remaining"])
    except Exception as e:  # noqa: BLE001 — never let a health hiccup stop the run
        print(f"  (couldn't read warmup budget: {e} — defaulting target to 19)")
        return 19


def ollama_up():
    try:
        import urllib.request
        cfg = _load_module("wf3_python/config.py", "wf3_config_for_ollama")
        base = cfg.OLLAMA_URL.rsplit("/api/", 1)[0]
        urllib.request.urlopen(base, timeout=3)
        return True
    except Exception:  # noqa: BLE001
        return False


# ----------------------------------------------------------------- phase runner
def run_phase(title, cwd, script, extra_args=None, region=None):
    """Run one phase as its own process (the decoupled design), streaming its output live."""
    print(f"\n{'='*72}\n  {title}\n{'='*72}")
    env = os.environ.copy()
    if region:
        env["GRANJUR_REGION"] = region
    else:
        env.pop("GRANJUR_REGION", None)
    cmd = [str(PY), script] + [str(a) for a in (extra_args or [])]
    try:
        r = subprocess.run(cmd, cwd=str(ROOT / cwd), env=env)
        return r.returncode == 0
    except Exception as e:  # noqa: BLE001 — one phase failing must not crash the orchestrator
        print(f"  !! {title} could not run: {e}")
        return False


# ----------------------------------------------------------------- the pipeline
def main():
    ap = argparse.ArgumentParser(description="Run the whole Granjur pipeline with one command.")
    ap.add_argument("--count", type=int, help=f"send-ready leads to line up today (default {DAILY_TARGET})")
    ap.add_argument("--region", help="isolate the whole run to one market (US/EU/UK/GCC/CN/AU)")
    ap.add_argument("--max-rounds", type=int, default=25, help="safety cap on discovery loops (default 25)")
    ap.add_argument("--jobs", action="store_true", help="also pull free job-board hiring-intent leads each round")
    ap.add_argument("--skip-discovery", action="store_true", help="skip discovery; process existing DB leads only")
    ap.add_argument("--no-send", action="store_true", help="queue leads + write CSV but don't run the sender")
    ap.add_argument("--seed", metavar="CSV", help="import a CSV of companies (with emails) first — the "
                    "guaranteed path to send-ready leads (auto-detects ./seed_leads.csv if present)")
    ap.add_argument("--test", nargs="?", const="__self__", metavar="EMAIL",
                    help="build the queue as usual, then send ALL queued pitches to THIS address as a preview "
                         "(a real send to your OWN inbox; the DB is untouched and no follow-ups fire). "
                         "Bare --test uses GMAIL_ADDRESS.")
    ap.add_argument("--no-quota", action="store_true",
                    help="don't top up with the quota-filler (get_quota_leads.py) when discovery falls short")
    # accepted for backward-compatibility with the old two-flag interface (both are the default now):
    ap.add_argument("--collect", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--send", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    region = (args.region or "").strip().upper() or None
    if region and region not in VALID_REGIONS:
        raise SystemExit(f"--region {args.region!r} invalid. Use one of: {', '.join(sorted(VALID_REGIONS))}")

    test_to = None
    if args.test is not None:
        test_to = (os.getenv("GMAIL_ADDRESS", "") if args.test == "__self__" else args.test).strip()
        if not test_to:
            raise SystemExit("--test needs an email address (or set GMAIL_ADDRESS). e.g. --test you@gmail.com")

    dry = os.getenv("GRANJUR_DRY_RUN", "1") != "0"
    if test_to:
        mode_str = f"TEST — every pitch goes to {test_to} only (DB untouched)"
    elif dry:
        mode_str = "DRY RUN (nothing sent — set GRANJUR_DRY_RUN=0 to go live)"
    else:
        mode_str = "LIVE SEND via Gmail"
    print("\n" + "#"*72)
    print("#  GRANJUR B2B PIPELINE  —  single-command run")
    print(f"#  started {datetime.now():%Y-%m-%d %H:%M:%S}  |  mode: " + mode_str)
    if region:
        print(f"#  REGION ISOLATION: this pass is limited to {region}.")
    print("#"*72)

    conn = connect()
    try:
        target = max(0, args.count if args.count is not None else DAILY_TARGET)
        queued0 = count_status(conn, "QUEUED_FOR_OUTREACH", region)
        wr = warmup_remaining(conn)
        print(f"\nToday's target: {target} send-ready lead(s)"
              + (f"  [region: {region}]" if region else "  [all regions]")
              + f"  |  already queued: {queued0}")
        if not dry and target > wr:
            print(f"  Note: the mailbox warmup safety allows {wr} more real send(s) today, so this run will\n"
                  f"        actually email {wr} now and hold the rest for the next run (protects your Gmail).\n"
                  f"        Raise the ceiling only if you must:  $env:GRANJUR_WARMUP_CEIL = \"50\"")
        if not ollama_up():
            print("  NOTE: Ollama doesn't look reachable — pitch writing (WF-3) may skip leads.\n"
                  "        Start it with:  ollama serve   (model qwen2.5:3b)")

        # Step 0: any COOLDOWN lead whose rest period elapsed flows back to QUALIFIED.
        run_phase("RE-ARM elapsed COOLDOWN leads (WF-3)", "wf3_python", "rearm_cooldown.py", region=region)

        # Close the loop FIRST: scan the inbox for replies so we never nudge someone who already answered.
        # (Inbox-wide, region-independent. A real reply -> REPLIED stops that lead's drip; OOO is ignored.)
        run_phase("SCAN REPLIES (WF-4: IMAP -> mark REPLIED, stop drip)", "wf4_python", "reply_parser.py",
                  extra_args=["--days", "14"])

        # Optional guaranteed lane: import a CSV of companies (with emails). Auto-detect ./seed_leads.csv.
        seed = args.seed
        if not seed and (ROOT / "seed_leads.csv").exists():
            seed = str(ROOT / "seed_leads.csv")
        if seed:
            if Path(seed).exists():
                run_phase("SEED IMPORT (WF-1: CSV -> DISCOVERED)", "wf1_python", "wf1.py",
                          extra_args=[str(Path(seed).resolve())], region=region)
            else:
                print(f"\n  (--seed: file not found: {seed} — skipping seed import.)")

        # ---- discovery loop: keep finding UNIQUE companies until the queue hits target ----
        if args.skip_discovery:
            print("\n(--skip-discovery: not discovering; enriching/qualifying existing DB leads once.)")
            run_phase("ENRICH  (WF-2: find email + tech)", "wf2_python", "wf2.py", region=region)
            run_phase("QUALIFY (WF-3: segment + AI pitch)", "wf3_python", "wf3.py", region=region)
            run_phase("AUTO-APPROVE (queue send-safe leads)", "wf4_python", "auto_approve.py", region=region)
        else:
            stale_rounds = 0
            for rnd in range(1, args.max_rounds + 1):
                queued = count_status(conn, "QUEUED_FOR_OUTREACH", region)
                if queued >= target:
                    print(f"\nTarget met: {queued} lead(s) queued (>= {target}). Stopping discovery.")
                    break
                print(f"\n----- discovery round {rnd}/{args.max_rounds}  (queued {queued}/{target}) -----")
                before = count_companies(conn)

                run_phase(f"DISCOVER (WF-1: OpenStreetMap, round {rnd})", "wf1_python", "collect_osm.py",
                          region=region)
                if args.jobs:
                    run_phase(f"DISCOVER (WF-1: free job boards, round {rnd})", "wf1_python", "collect_jobs.py")
                run_phase("ENRICH  (WF-2: find email + tech)", "wf2_python", "wf2.py", region=region)
                run_phase("QUALIFY (WF-3: segment + AI pitch)", "wf3_python", "wf3.py", region=region)
                run_phase("AUTO-APPROVE (queue send-safe leads)", "wf4_python", "auto_approve.py", region=region)

                after = count_companies(conn)
                gained = after - before
                print(f"\n  round {rnd}: +{gained} new compan(ies) discovered (total {after}); "
                      f"queued now {count_status(conn, 'QUEUED_FOR_OUTREACH', region)}")
                if gained == 0:
                    stale_rounds += 1
                    if stale_rounds >= 3:
                        print("\n  Discovery pool looks exhausted (3 rounds, no new companies). Stopping early — "
                              "free sources are thin right now.")
                        break
                else:
                    stale_rounds = 0

        queued_final = count_status(conn, "QUEUED_FOR_OUTREACH", region)

        # ---- QUOTA TOP-UP: if the high-intent crawl fell short, fill the balance from free public
        # sources (OSM website+email businesses, then a local fallback CSV) so the batch reaches target. ----
        if queued_final < target and not args.no_quota:
            deficit = target - queued_final
            print(f"\n{'='*72}\n  QUOTA TOP-UP: high-intent crawl gave {queued_final}/{target} — "
                  f"filling {deficit} more from public sources\n{'='*72}")
            quota_csv = ROOT / "quota_leads.csv"
            qargs = ["--have", queued_final, "--target", target, "--out", str(quota_csv)]
            if region:
                qargs += ["--region", region]
            run_phase("QUOTA FETCH (get_quota_leads.py: OSM + fallback)", "scripts", "get_quota_leads.py",
                      extra_args=qargs, region=region)
            # import + process the top-up leads (only if the fetch actually wrote some rows)
            rows = 0
            if quota_csv.exists():
                with open(quota_csv, encoding="utf-8-sig") as f:
                    rows = max(0, sum(1 for _ in f) - 1)   # minus header
            if rows:
                run_phase("QUOTA IMPORT (WF-1: CSV -> DISCOVERED)", "wf1_python", "wf1.py",
                          extra_args=[str(quota_csv)], region=region)
                run_phase("ENRICH  (WF-2: quota leads)", "wf2_python", "wf2.py", region=region)
                run_phase("QUALIFY (WF-3: quota leads)", "wf3_python", "wf3.py", region=region)
                run_phase("AUTO-APPROVE (queue quota leads)", "wf4_python", "auto_approve.py", region=region)
                queued_final = count_status(conn, "QUEUED_FOR_OUTREACH", region)
            else:
                print("  Quota-filler produced no rows (thin OSM email coverage + empty fallback CSV).")

        print(f"\n{'='*72}\n  Lined up for outreach: {queued_final} send-ready lead(s) "
              f"(target was {target})\n{'='*72}")
        if queued_final < target:
            print(f"  Heads-up: only {queued_final} of {target} could be lined up this run.\n"
                  f"  Free OSM email coverage is finite and many listings are role inboxes (info@) we skip.\n"
                  f"  GUARANTEED fix: create quota_fallback.csv with columns "
                  f"company_name,website,email,region,city,niche\n"
                  f"  — the quota-filler draws from it to top up to {target} every day.")

        # ---- send (dry-run unless GRANJUR_DRY_RUN=0). wf4's warmup cap is the final send authority. ----
        if args.no_send:
            print("\n(--no-send: stopping before the sender. Nothing was contacted.)")
        elif queued_final == 0:
            print("\nNothing queued — skipping the send step.")
        elif test_to:
            # Preview: send every queued pitch to your own inbox. wf4 --test does NOT touch the DB
            # (leads stay QUEUED for a later real send) and we skip the follow-up drip in test mode.
            run_phase(f"TEST SEND -> {test_to} (WF-4: preview to your inbox, DB untouched)",
                      "wf4_python", "wf4.py", extra_args=["--test", test_to, "--limit", target], region=region)
        else:
            run_phase("SEND (WF-4: outreach)" + ("  [DRY RUN]" if dry else "  [LIVE]"),
                      "wf4_python", "wf4.py", extra_args=["--limit", target], region=region)
            run_phase("FOLLOW-UP drip (WF-4: due nudges)", "wf4_python", "followup.py", region=region)

        # ---- export: dated .xlsx snapshot for this run + fold it into the central database ----
        print(f"\n{'='*72}\n  EXPORT (Excel snapshot of this run + central database)\n{'='*72}")
        try:
            exporter = _load_module("scripts/export_leads_csv.py", "export_leads_csv_mod")
            path, n = exporter.export_xlsx(conn=connect())
            print(f"  Wrote snapshot: {n} compan(ies) -> {path}")
            print(f"  Updated central database -> {ROOT / 'exports' / 'granjur_central.xlsx'}")
        except Exception as e:  # noqa: BLE001
            print(f"  Excel export failed: {e}")

        # ---- final summary ----
        contacted = count_status(conn, "CONTACTED", region)
        print("\n" + "#"*72)
        print("#  DONE")
        print(f"#  queued (send-ready): {queued_final}   |   CONTACTED total: {contacted}")
        if test_to:
            print(f"#  mode: TEST — up to {target} pitch(es) emailed to {test_to} only.")
            print(f"#  The DB is untouched — those {queued_final} lead(s) are still QUEUED for a real send.")
            print("#  Happy with how they look? Send to the real companies:")
            print('#      $env:GRANJUR_DRY_RUN = "0"; python scripts/run_pipeline.py')
        elif dry:
            print("#  mode: DRY RUN — nothing was actually emailed.")
            print("#  To send for real next time, one command:")
            print('#      $env:GRANJUR_DRY_RUN = "0"; python scripts/run_pipeline.py')
        else:
            print("#  mode: LIVE — real emails were sent via Gmail.")
        print("#"*72 + "\n")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
