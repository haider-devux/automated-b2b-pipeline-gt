# WF-4 (Python) — Outreach Engine (DRY RUN)

> Final phase. Takes QUALIFIED leads a human approves, builds the provider payload, and (in DRY RUN)
> logs it instead of sending. Integrates with the pipeline only through `leads.status`.

## The flow
```
QUALIFIED
  → [dashboard "Outreach" page: you review the pitch + Approve]   ← the human-in-the-loop gate
     → QUEUED_FOR_OUTREACH   (payload built, routing stored)
  → wf4.py (DRY RUN)  → logs payload to outbox_dryrun.jsonl → CONTACTED
  → webhook_server.py ← reply / booking / bounce / unsubscribe events update the lead
```

## Files
| File | Does |
|---|---|
| `outreach_schema.sql` / `init_db.py` | add outreach + compliance columns to `leads` |
| `config.py` | reuse WF-3's DB password |
| `outreach.py` | build the Instantly/Smartlead payload + CAN-SPAM footer; `DRY_RUN` flag; dry-run log |
| `db.py` | send-gate query, queue lead, mark contacted, inbound event handlers |
| `wf4.py` | the sender (QUEUED_FOR_OUTREACH → CONTACTED); dry-run unless `GRANJUR_DRY_RUN=0` |
| `followup.py` | threaded follow-up drip (shares the send day-budget) |
| `domain_health.py` | SPF/DKIM/DMARC/blacklist + warmup cap + `bounce_stats()` breaker math |
| `webhook_server.py` | always-on Flask service (port 5001) for reply/booking/bounce/unsubscribe |

> **Server send-safety (Phase 7).** As bot-send, `wf4.py` and `followup.py` share these anti-flag guards:
> the fresh-mailbox **warmup cap** (5→50/day by age) + per-recipient **local-time windows**; an **anti-burst
> drip** — real sends are spaced a randomized **45–120s** apart (`GRANJUR_SEND_JITTER_MIN/MAX`); a **bounce
> circuit-breaker** — if the trailing-window bounce rate crosses `GRANJUR_BOUNCE_RATE_CEIL` (3%) they park
> the `send` governor bucket and refuse to send; and they honor any `send` rest set by the governor (e.g. a
> health-driven pause). Under cron, `bot-send` runs a small **drip of ~5 emails/tick** (`GRANJUR_SEND_DRIP`).
> See `DEPLOYMENT_PLAN.md §4.7` and `scripts/governor.py`.

## Setup + run
```powershell
# once: add the columns
..\wf3_python\.venv\Scripts\python.exe init_db.py
# approve leads on the dashboard Outreach page (http://localhost:5000/outreach), then:
..\wf3_python\.venv\Scripts\python.exe wf4.py          # dry-run "send" -> CONTACTED
..\wf3_python\.venv\Scripts\python.exe webhook_server.py   # (separate terminal) inbound events
```

## The send gate (safety)
`db.fetch_ready` only returns leads that are QUALIFIED, have a pitch + email, aren't `email_validation_status='invalid'`, aren't suppressed, and aren't on the global `suppression_list`. Nothing else can be queued.

## Going live later (NOT yet)
Everything is `DRY_RUN=True` — payloads are logged to `outbox_dryrun.jsonl`, never sent. Before flipping to live: warm burner sending domains (2–3 weeks), add real Instantly/Smartlead + Cal.com, set a real `COMPANY_ADDRESS`, and finish the compliance layer (suppression is started here; unsubscribe endpoint is live).
