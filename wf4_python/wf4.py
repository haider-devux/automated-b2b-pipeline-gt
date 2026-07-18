"""
WF-4: Outreach sender.

Takes leads a human already APPROVED on the dashboard (status QUEUED_FOR_OUTREACH), builds the
email, and either logs it (DRY RUN) or actually sends it via Gmail, then marks each CONTACTED.

  python wf4.py                       # DRY RUN unless GRANJUR_DRY_RUN=0 (logs, nothing sent)
  python wf4.py --test you@gmail.com  # send every queued pitch to YOUR inbox to preview (DB untouched)

LIVE send (real companies): set GRANJUR_DRY_RUN=0 plus GMAIL_ADDRESS / GMAIL_APP_PASSWORD, and a real
GRANJUR_ADDRESS (physical mailing address required by law in the footer).
"""
import argparse
import os
import sys
from datetime import datetime, timezone

# Phase-2 scheduling gate lives in wf3_python (single source of truth, shared with the dashboard
# advisor). Phases run as separate processes; the shared-package refactor is deferred (Guide.md §5).
# APPEND (not insert-0) so wf4_python's own db.py/config.py still win — we only want `sendwindows`.
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "wf3_python"))
import sendwindows  # noqa: E402

import db
import domain_health
import outreach
import revalidate
import send_gmail


def _send_live(payload, to_override=None, subject_override=None):
    """Actually send one email via Gmail (HTML with logo + signature). Returns True on success."""
    p = payload["personalization"]
    h = payload.get("_headers") or {}
    send_gmail.send(
        to_email=to_override or payload["email"],
        subject=subject_override or p["subject"],
        body=p["body"],
        html=p.get("html_body"),
        logo_path=payload["_meta"].get("logo_path"),
        from_name=outreach.COMPANY_NAME,
        unsubscribe=payload["_meta"]["unsubscribe"],
        message_id=h.get("message_id"), in_reply_to=h.get("in_reply_to"), references=h.get("references"),
    )
    return True


def main():
    ap = argparse.ArgumentParser(description="WF-4 outreach sender")
    ap.add_argument("--test", metavar="EMAIL",
                    help="Send every queued pitch to THIS address (a safe preview to your own inbox). "
                         "Really sends via Gmail but does NOT touch the database.")
    ap.add_argument("--limit", type=int, metavar="N",
                    help="Only process the first N approved leads (safety cap, e.g. --limit 5).")
    ap.add_argument("--ignore-window", action="store_true",
                    help="Manual override: bypass the Phase-2 local-time/holiday send-gate and send now.")
    ap.add_argument("--region", metavar="XX",
                    help="Only process one region (US/EU/UK/GCC/CN/AU). Also honoured via GRANJUR_REGION.")
    args = ap.parse_args()

    # Compliance guard: never mail real companies with a placeholder footer address.
    if (not outreach.DRY_RUN) and (not args.test) and outreach.address_is_placeholder():
        raise SystemExit(
            "Refusing to send: the footer address is still a placeholder.\n"
            "Set a real physical address first, e.g.  $env:GRANJUR_ADDRESS = 'Granjur Technologies, <street>, <city>, <country>'")

    conn = db.get_connection()
    try:
        leads = db.fetch_queued(conn)
        if not leads:
            print("No QUEUED_FOR_OUTREACH leads. (Approve some on the dashboard Outreach page first.)")
            return
        if args.limit is not None:
            if args.limit < 0:
                raise SystemExit("--limit must be 0 or more.")
            if len(leads) > args.limit:
                print(f"(--limit {args.limit}: capping {len(leads)} approved lead(s) down to {args.limit})\n")
                leads = leads[:args.limit]

        if args.test:
            mode = f"TEST - sending all pitches to {args.test} (DB untouched)"
        elif outreach.DRY_RUN:
            mode = "DRY RUN - logging only, nothing sent"
        else:
            mode = f"LIVE SEND via Gmail ({outreach.GMAIL_ADDRESS})"
        gate = "OFF (--ignore-window)" if args.ignore_window else "ON (local business hours + holidays)"
        print(f"{len(leads)} approved lead(s). Mode: {mode}. Schedule-gate: {gate}")

        # Gate 4 (Phase 3): domain health + warmup. Never live-send from a failing/blacklisted domain,
        # and never exceed the fresh-mailbox daily warmup cap (the #1 anti-spam lever for a new account).
        health = domain_health.report(conn=conn)
        w = health["warmup"]
        auth_fail = [c for c in health["checks"] if c["status"] == "fail"]
        print(f"Domain health: {health['domain']} -> {health['overall'].upper()} | "
              f"warmup age {w['age_days']}d, cap {w['cap']}/day, sent today {w['sent_today']}, "
              f"remaining {w['remaining']}\n")
        if auth_fail and not outreach.DRY_RUN and not args.test and not args.ignore_window:
            raise SystemExit("Refusing to LIVE send — domain authentication/blacklist failing:\n  "
                             + "\n  ".join(f"{c['name']}: {c['detail']} (fix: {c['fix']})" for c in auth_fail)
                             + "\nFix DNS or pass --ignore-window to override.")
        warmup_cap, warmup_used0 = w["cap"], w["sent_today"]

        sent = suppressed = failed = held = 0
        held_by_region = {}
        for l in leads:
            # Gate 2 (compliance): re-validate the email at send-time; never mail a dead address
            if revalidate.check(l["email"]) == "invalid":
                if not args.test:
                    db.suppress(conn, l["id"], "invalid-at-send")   # commits
                suppressed += 1
                print(f"  SKIPPED    {l['legal_name']:<28} -> email invalid at send-time")
                continue

            # Gate 3 (Phase 2 scheduling): only send inside the recipient's LOCAL business window
            # (never at 2 AM local, on a local weekend, or on a local public holiday). A held lead
            # stays QUEUED_FOR_OUTREACH and goes out on a later run once its window opens.
            if not args.test and not args.ignore_window:
                g = sendwindows.can_send_now(l.get("region"))
                if not g["ok"]:
                    db.log_outreach_attempt(conn, l, outcome="skipped", dry_run=outreach.DRY_RUN,
                                            subject=l.get("pitch_subject"), body=l.get("pitch_body"),
                                            scheduled_for=g["next_open_utc"], error=g["reason"])
                    held += 1
                    held_by_region[l.get("region")] = held_by_region.get(l.get("region"), 0) + 1
                    print(f"  HELD       {l['legal_name']:<28} -> {g['reason']} "
                          f"[{l.get('region')}]; opens {g['next_open_local']}")
                    continue

                # Gate 4b: stop once the fresh-mailbox daily warmup cap is hit (protects reputation).
                if warmup_used0 + sent >= warmup_cap:
                    db.log_outreach_attempt(conn, l, outcome="skipped", dry_run=outreach.DRY_RUN,
                                            subject=l.get("pitch_subject"), body=l.get("pitch_body"),
                                            error="warmup daily cap reached")
                    held += 1
                    held_by_region[l.get("region")] = held_by_region.get(l.get("region"), 0) + 1
                    print(f"  HELD       {l['legal_name']:<28} -> warmup cap "
                          f"({warmup_used0 + sent}/{warmup_cap} today); resumes tomorrow")
                    continue

            payload = outreach.build_payload(l)

            # --- TEST preview: send the real email to yourself, don't change the DB ---
            if args.test:
                try:
                    _send_live(payload, to_override=args.test,
                               subject_override=f"[TEST->{l['email']}] {payload['personalization']['subject']}")
                    sent += 1
                    print(f"  PREVIEW    {l['legal_name']:<28} -> {args.test}")
                except Exception as e:
                    failed += 1
                    print(f"  FAILED     {l['legal_name']:<28} -> {e}")
                continue

            p = payload["personalization"]

            # --- DRY RUN: log the payload only ---
            if outreach.DRY_RUN:
                outreach.log_dry_run(payload)
                db.mark_contacted(conn, l, payload)
                db.log_outreach_attempt(conn, l, outcome="logged", dry_run=True,
                                        subject=p["subject"], body=p["body"],
                                        provider=payload["provider"],
                                        sending_domain=payload["_meta"]["sending_domain"])
                conn.commit()
                sent += 1
                print(f"  CONTACTED  {l['legal_name']:<28} -> {payload['campaign_id']} [DRY RUN, nothing sent]")
                continue

            # --- LIVE SEND ---
            try:
                _send_live(payload)
            except Exception as e:
                failed += 1
                db.log_outreach_attempt(conn, l, outcome="error", dry_run=False,
                                        subject=p["subject"], body=p["body"], error=str(e)[:300])
                print(f"  FAILED     {l['legal_name']:<28} -> {e}")
                continue
            outreach.log_dry_run(payload)              # keep an audit copy of what was actually sent
            db.mark_contacted(conn, l, payload)
            db.log_outreach_attempt(conn, l, outcome="sent", dry_run=False,
                                    subject=p["subject"], body=p["body"],
                                    sent_at=datetime.now(timezone.utc),
                                    provider=payload["provider"],
                                    sending_domain=payload["_meta"]["sending_domain"])
            conn.commit()
            sent += 1
            print(f"  SENT       {l['legal_name']:<28} -> {payload['email']}")

        verb = 'previewed' if args.test else ('logged (DRY RUN)' if outreach.DRY_RUN else 'SENT')
        print(f"\nDone. {sent} email(s) {verb}, {held} held (outside local window), "
              f"{suppressed} suppressed, {failed} failed.")
        if held_by_region:
            summary = ", ".join(f"{rg or 'OTHER'}:{n}" for rg, n in sorted(held_by_region.items()))
            print(f"  Held by region (will send when their local window opens): {summary}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
