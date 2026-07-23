"""
WF-4 · Follow-up drip engine (Max Plan · Phase 4).

Finds CONTACTED leads that never replied, and sends the next GENERIC nudge in the sequence once its
cooling window has elapsed (4 days -> bump, 7 days -> polite breakup). Fully decoupled: it reads
leads.status='CONTACTED' + outreach_log history, and integrates through the DB only.

Every send passes the SAME gates as the initial email:
  * Phase 2 — recipient-local business hours + weekend + public-holiday guardrails
  * Phase 3 — fresh-mailbox warmup daily cap (follow-ups count toward the same cap)
  * compliance — re-validate address, suppression list, opt-in-only regions

  python followup.py                 # DRY RUN (logs, marks, nothing sent) unless GRANJUR_DRY_RUN=0
  python followup.py --preview       # show which follow-ups are DUE + their copy; no send, no DB writes
  python followup.py --test you@x.com  # send every due nudge to YOUR inbox (DB untouched)
  python followup.py --ignore-window   # bypass the time/holiday + warmup gate (manual override)
"""
import argparse
import os
import random
import sys
import time
from datetime import datetime, timezone

sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "wf3_python"))
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))
import sendwindows  # noqa: E402
import governor     # noqa: E402

import db
import domain_health
import followup_copy
import outreach
import revalidate
import send_gmail

# Follow-ups are real sends too — same anti-burst drip + bounce breaker as wf4.py (shared knobs).
SEND_JITTER_MIN = float(os.getenv("GRANJUR_SEND_JITTER_MIN", "45"))
SEND_JITTER_MAX = float(os.getenv("GRANJUR_SEND_JITTER_MAX", "120"))
SEND_BOUNCE_PARK_HOURS = float(os.getenv("GRANJUR_SEND_BOUNCE_PARK_HOURS", "18"))


def _p(s):
    """Print safely on any console — Windows cp1252 can't encode Arabic/Chinese, so replace on failure."""
    try:
        print(s)
    except UnicodeEncodeError:
        enc = sys.stdout.encoding or "utf-8"
        print(str(s).encode(enc, "replace").decode(enc))


def _send_live(payload, to_override=None, subject_override=None):
    p = payload["personalization"]
    h = payload.get("_headers") or {}
    send_gmail.send(to_email=to_override or payload["email"],
                    subject=subject_override or p["subject"], body=p["body"], html=p.get("html_body"),
                    logo_path=payload["_meta"].get("logo_path"), from_name=outreach.COMPANY_NAME,
                    unsubscribe=payload["_meta"]["unsubscribe"],
                    message_id=h.get("message_id"), in_reply_to=h.get("in_reply_to"),
                    references=h.get("references"))
    return True


def _due_leads(conn, now):
    """(lead, plan) pairs whose next drip step is due right now."""
    out = []
    for l in db.fetch_followup_candidates(conn):
        plan = followup_copy.next_due_step(l["steps_sent"], l["initial_at"], now)
        if not plan["complete"] and plan["is_due"]:
            out.append((l, plan))
    return out


def main():
    ap = argparse.ArgumentParser(description="WF-4 follow-up drip engine")
    ap.add_argument("--test", metavar="EMAIL", help="Send every due nudge to THIS address (DB untouched).")
    ap.add_argument("--preview", action="store_true", help="List due follow-ups + copy; no send, no writes.")
    ap.add_argument("--limit", type=int, metavar="N", help="Process at most N due leads.")
    ap.add_argument("--ignore-window", action="store_true", help="Bypass the time/holiday + warmup gate.")
    ap.add_argument("--region", metavar="XX", help="Only one region (also via GRANJUR_REGION).")
    args = ap.parse_args()

    conn = db.get_connection()
    try:
        now = datetime.now(timezone.utc)
        due = _due_leads(conn, now)
        if args.limit is not None:
            due = due[:max(0, args.limit)]
        if not due:
            print("No follow-ups are due right now. (Contacted leads are still inside their cooling window,"
                  " have replied, or the sequence is complete.)")
            return

        # --preview: just show what would go out, no send, no DB writes
        if args.preview:
            _p(f"{len(due)} follow-up(s) DUE:\n")
            for l, plan in due:
                subject, body, lang = followup_copy.generate_followup(l, plan["kind"])
                _p(f"  [{l['region']}/{lang}] {l['legal_name']}  ->  step {plan['step']} ({plan['kind']})")
                _p(f"     Subject: {subject}")
                _p("     " + body.replace("\n", "\n     ") + "\n")
            return

        health = domain_health.report(conn=conn)
        w = health["warmup"]
        auth_fail = [c for c in health["checks"] if c["status"] == "fail"]
        mode = (f"TEST -> {args.test} (DB untouched)" if args.test
                else ("DRY RUN - logging only" if outreach.DRY_RUN else f"LIVE via Gmail ({outreach.GMAIL_ADDRESS})"))
        gate = "OFF (--ignore-window)" if args.ignore_window else "ON (hours+holiday+warmup)"
        print(f"{len(due)} follow-up(s) due. Mode: {mode}. Gate: {gate}. "
              f"Warmup cap {w['cap']}/day, sent today {w['sent_today']}, remaining {w['remaining']}\n")
        if auth_fail and not outreach.DRY_RUN and not args.test and not args.ignore_window:
            raise SystemExit("Refusing to LIVE send — domain auth failing: "
                             + "; ".join(f"{c['name']}: {c['detail']}" for c in auth_fail))
        cap, used0 = w["cap"], w["sent_today"]

        # Same Phase-7 guard as the initial send: honor a governor 'send' rest + the bounce breaker.
        if not outreach.DRY_RUN and not args.test:
            gs = governor.can_run(conn, "send")
            if not gs["ok"] and not args.ignore_window:
                raise SystemExit(f"bot-send is resting ({gs['reason']}) until {gs['rest_until']}. "
                                 "Fix the cause, then pass --ignore-window to override once.")
            bs = domain_health.bounce_stats(conn)
            if bs["tripped"]:
                governor.park(conn, "send", hours=SEND_BOUNCE_PARK_HOURS, reason="bounce breaker")
                raise SystemExit(
                    f"Refusing to send follow-ups: bounce rate {bs['rate']:.1%} over {bs['window_days']}d "
                    f"({bs['bounces']}/{bs['sends']}) exceeds the {bs['ceil']:.0%} ceiling. Parked "
                    f"{SEND_BOUNCE_PARK_HOURS:.0f}h.")

        sent = held = suppressed = failed = 0
        for i, (l, plan) in enumerate(due):
            step = plan["step"]
            if revalidate.check(l["email"]) == "invalid":
                if not args.test:
                    db.suppress(conn, l["id"], "invalid-at-send")
                suppressed += 1
                _p(f"  SKIPPED    {l['legal_name']:<26} -> email invalid at send-time")
                continue

            if not args.test and not args.ignore_window:
                g = sendwindows.can_send_now(l.get("region"))
                if not g["ok"]:
                    db.log_outreach_attempt(conn, l, outcome="skipped", step=step, dry_run=outreach.DRY_RUN,
                                            subject=l.get("pitch_subject"), scheduled_for=g["next_open_utc"],
                                            error=g["reason"])
                    held += 1
                    _p(f"  HELD       {l['legal_name']:<26} -> f{step}: {g['reason']} [{l['region']}]; "
                          f"opens {g['next_open_local']}")
                    continue
                if used0 + sent >= cap:
                    db.log_outreach_attempt(conn, l, outcome="skipped", step=step, dry_run=outreach.DRY_RUN,
                                            subject=l.get("pitch_subject"), error="warmup daily cap reached")
                    held += 1
                    _p(f"  HELD       {l['legal_name']:<26} -> f{step}: warmup cap "
                          f"({used0 + sent}/{cap}); resumes tomorrow")
                    continue

            subject, body, lang = followup_copy.generate_followup(l, plan["kind"])
            payload = outreach.build_followup_payload(l, subject, body, step=step,
                                                      rtl=followup_copy.is_rtl(lang))

            if args.test:
                try:
                    _send_live(payload, to_override=args.test,
                               subject_override=f"[FUP f{step}->{l['email']}] {subject}")
                    sent += 1
                    _p(f"  PREVIEW    {l['legal_name']:<26} -> f{step}/{lang} to {args.test}")
                except Exception as e:                       # noqa: BLE001
                    failed += 1
                    _p(f"  FAILED     {l['legal_name']:<26} -> {e}")
                continue

            if not outreach.DRY_RUN:
                try:
                    _send_live(payload)
                except Exception as e:                       # noqa: BLE001
                    failed += 1
                    db.log_outreach_attempt(conn, l, outcome="error", step=step, dry_run=False,
                                            subject=subject, body=body, error=str(e)[:300])
                    _p(f"  FAILED     {l['legal_name']:<26} -> {e}")
                    continue

            outreach.log_dry_run(payload)
            db.mark_followed_up(conn, l["id"], step, payload)
            db.log_outreach_attempt(conn, l, outcome=("logged" if outreach.DRY_RUN else "sent"), step=step,
                                    dry_run=outreach.DRY_RUN, subject=subject, body=body,
                                    sent_at=None if outreach.DRY_RUN else datetime.now(timezone.utc),
                                    provider=payload["provider"], sending_domain=payload["_meta"]["sending_domain"])
            conn.commit()
            sent += 1
            tag = "logged [DRY RUN]" if outreach.DRY_RUN else f"SENT -> {l['email']}"
            _p(f"  FOLLOW-UP  {l['legal_name']:<26} -> f{step}/{lang} ({plan['kind']}) {tag}")

            # Anti-burst drip on real follow-up sends (never after the last, never in dry-run/test).
            if not outreach.DRY_RUN and not args.test and i < len(due) - 1:
                delay = random.uniform(SEND_JITTER_MIN, SEND_JITTER_MAX)
                _p(f"             ...cooling down {delay:.0f}s before next send (anti-burst)")
                time.sleep(delay)

        verb = "previewed" if args.test else ("logged (DRY RUN)" if outreach.DRY_RUN else "SENT")
        print(f"\nDone. {sent} follow-up(s) {verb}, {held} held (window/warmup), "
              f"{suppressed} suppressed, {failed} failed.")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
