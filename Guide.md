# Granjur B2B Pipeline — Progress & Handoff

> **Read this top-to-bottom and you have the full picture.** This is the live handoff doc.
> **Last worked: 13 July 2026.** Stack: custom **Python** + native **PostgreSQL** + local **Ollama** (`qwen2.5:3b`).
> (The old n8n build was the prototype/reference — we've since ported everything to Python.)
> **State: complete, free, 4-phase pipeline + the full 6-part "Max Plan" production upgrade, running
> end-to-end on REAL international data (dry-run), with a clean light-mode dashboard.**
> **The Max Plan (regions, scheduling, domain health, follow-up drip, tag CRM, funnel analytics) is
> documented in §1b. Post-Max-Plan polish (English-only pitch, logo + 3 offices, Google-Calendar booking
> button, per-region mass-send buttons, go-live wiring) is in §1c. Read both after §1.**

---

## 0. Big picture

**Granjur Technologies** = offshore software agency. We're building a **B2B client-acquisition pipeline**:
find companies → enrich them → qualify + write a localized pitch → outreach. Full spec in
`docs/B2B_Automation_Pipeline_Analysis.pdf` (21 pages).

**Repo layout** (cleaned up 7 Jul): `wf1_python/`..`wf4_python/` (the code), `Guide.md` + `run_pipeline.py`
+ `setup_scheduler.ps1` at root, `docs/` (this spec + `briefing.pdf`/`briefing.pptx` presentation),
`database/` (schema + mock SQL).

**Key decisions:**
- **All custom Python**, one stack. No n8n/Docker/pgAdmin needed to run it (pgAdmin is just a viewer).
- **PostgreSQL is the single source of truth.** Phases integrate ONLY through the `leads.status` column
  (a relay race — each phase reads one status, writes the next; they never call each other).
- **Code owns decisions; the LLM only writes copy** (the pitch + translation). The small model is
  unreliable at classification, so qualification/segmentation are deterministic Python.
- **100% FREE — no paid API keys.** Supervisor decided (7 Jul): no Apollo; stay free and expand
  scrapers (LinkedIn/Maps/etc.) with humans filling contact gaps from platforms they browse.

**The 4 phases + the state machine:**
```
DISCOVERED (WF-1) → ENRICHING → ENRICHED (WF-2) → QUALIFYING → QUALIFIED / DISQUALIFIED (WF-3)
   → QUEUED_FOR_OUTREACH → CONTACTED → REPLIED → BOOKED (WF-4)   Side: NEEDS_CONTACT, SUPPRESSED, ERROR, COOLDOWN
```

---

## 1. STATUS — the whole pipeline works end-to-end (free, dry-run) ✅

| Phase | Folder | State |
|---|---|---|
| **WF-1 Discovery** | `wf1_python/` | ✅ CSV import + free collectors (OSM, jobs) + auto-intake + targeting. **`--region XX` isolates discovery.** (Manual Review tab REMOVED 13 Jul — automation-only.) |
| **WF-2 Enrichment** | `wf2_python/` | ✅ `free` mode (tech + email-find + MX + homepage scrape + PageSpeed) works; `mock`/`real` modes exist. Region-isolatable. |
| **WF-3 Qualify + Pitch** | `wf3_python/` | ✅ deterministic rules + localized pitch (ar/zh/en), retry-hardened. Region-isolatable. |
| **WF-4 Outreach** | `wf4_python/` | ✅ auto-approve (`--send`) OR human-approve → DRY-RUN send → webhooks. **Now gated by local send-window + holiday + domain-warmup; emails carry open-pixel + click-wrapped links + a tracked calendar CTA.** |
| **WF-4 Follow-ups** | `wf4_python/followup.py` | ✅ multi-day drip (day 4 bump / day 7 breakup) on CONTACTED leads; **multilingual** (ar/zh/en); reuses every send gate. |
| **Dashboard** | `wf3_python/dashboard.py` | ✅ **light mode**: Dashboard · Flow · Leads (tag CRM) · Outreach · **Regions** · **Follow-ups** · **Health** · Analytics (http://localhost:5000) |
| **Compliance** | (essentially done) | ✅ DB send-gate, suppression list, unsubscribe, pre-send + staleness re-validation, role-account suppression, DE/AT routing, GDPR footer, **bounce-driven dead-email suppression** |

**The Max Plan (6 parts) is layered on top of the working 4-phase pipeline — full detail in §1b below.**

**Default is safe DRY-RUN** — WF-4 logs payloads to `wf4_python/outbox_dryrun.jsonl` and sends nothing.
**LIVE Gmail sending is now wired** (9 Jul, `wf4_python/send_gmail.py`): set `GMAIL_ADDRESS` +
`GMAIL_APP_PASSWORD` (a Google App Password; needs 2-Step Verification ON) and flip `GRANJUR_DRY_RUN=0`
to actually send from the Gmail (`granjur.tech.dev@gmail.com`). The real footer address is now the default
(Model Town, Lahore) so no env needed. `python wf4.py --test you@email.com` previews every queued pitch to
your own inbox first (DB untouched); `--limit N` caps a real send (e.g. `--limit 5`). Replies land in the
Gmail inbox = your feedback.

**Emails are HTML with a professional signature** (9 Jul): `outreach.py` builds an `html_body` (pitch as
HTML + a logo + name/title/company/tagline/website + small grey compliance line). Signature knobs at the
top of `outreach.py` (SENDER_NAME="Asma Haider", SENDER_TITLE, PHONE, WEBSITE_URL, LOGO_PATH). **Save the
company logo to `wf4_python/assets/granjur_logo.png`** — if missing, a text wordmark is used. Preview the
design any time: build a payload and write `personalization['html_body']` to an .html file and open it.

**Pitches are editable in the dashboard** (9 Jul): the **Outreach** tab now shows each pitch full (no more
300-char cut) in an editable Subject + Body form, with a FACT-CHECK box (verified trigger, detected tech,
scraped homepage description, link to their site). Buttons: **Save edits**, **Save & queue** (banks edits
then queues), **Skip**. Edits log a `human-edit` audit event. Restart the dashboard after editing its code.

**NEEDS_CONTACT — don't waste the LLM on unsendable leads** (10 Jul): WF-3 now checks `_has_sendable_contact`
BEFORE the slow LLM pitch. A qualified lead with only a **role email** (info@/sales@ → `email_validation_status`
in role/invalid) or **no email** (intent-only job-board leads) is parked in the new **NEEDS_CONTACT** status
(qualification recorded, pitch SKIPPED) instead of burning ~1–2 min of LLM on a lead that can never be
cold-emailed. These are good companies missing only a contact — the human clicks **"＋ Add contact"** on the NEEDS_CONTACT
row (Leads tab) to enter a real named email; that sets the lead back to ENRICHED so the next WF-3 run writes
its pitch. (`/lead/<id>/contact` route; email stored as `unverified`, status -> ENRICHED.) `db.ensure_status_values()` adds the enum value (idempotent,
run at WF-3 + dashboard startup). Only leads with a real (valid/unverified, non-role) email get a pitch and
reach the Outreach tab. (Existing role/no-email leads were reclassified QUALIFIED → NEEDS_CONTACT.)

**COOLDOWN now auto-returns** (10 Jul): clicking **Skip** parks a lead in COOLDOWN for 3 days
(`rearm_cooldown.COOLDOWN_DAYS`), stamped in the new `leads.cooldown_until` column. After that it flows
back to **QUALIFIED** (reappears on the Outreach tab for a fresh review — never auto-sent). Re-arming runs
(a) automatically on every dashboard page load, and (b) at the top of `run_pipeline.py` / the scheduler.
The **Leads** tab shows a **&#8635; Re-arm** button on each COOLDOWN row to send it back immediately, plus
its auto-return date. Manual: `python wf3_python/rearm_cooldown.py` (also adds/back-fills the column,
idempotent). All 14 pre-existing COOLDOWN leads were back-filled with a timer.

**WF-2 now scrapes each homepage's real description** (`companies.description`, from og/meta/title) and WF-3
feeds it to the pitch as the top "what they do" fact — kills invented industries. Run
`python wf2_python/init_db.py` once to add the column. **Pitch anti-hallucination hardening**: no `(unknown)`
greeting, KNOWN-FACTS-only prompt, and a sanitizer (`pitch.py _clean`) strips leftover `[placeholders]`,
sign-offs, and the banned "hope you're well" filler.

**Lighthouse → "Google mobile speed test" + a verifiable link in the email** (10 Jul): the score comes
from **Google PageSpeed Insights** (`wf2_python/enrich_free.py` `pagespeed()`), which needs a FREE
`PAGESPEED_API_KEY` (`wf2_python/config.py`) — with no key the score is NULL and Segment C speaks generally
(no number, no link). When a real score exists, Segment C pitches (a) call it **"Google's free mobile speed
test"**, never "Lighthouse" (jargon the recipient won't know), and (b) get a **P.S. link to the LIVE public
report** appended by Python (`pitch.psi_report_url` -> `https://pagespeed.web.dev/analysis?url=https://DOMAIN`)
so the recipient can click and see their own score — the most verifiable claim in the email. The link is made
clickable in the HTML email (`outreach._linkify`) and shown in the dashboard **Outreach** fact-check box with
the score + measured date (`companies.last_verified_at`). factcheck ignores URLs so domain digits aren't
misread as invented numbers. Get the key: Google Cloud Console -> enable "PageSpeed Insights API" -> create an
API key. **Store it in a project-root `.env`** (`PAGESPEED_API_KEY=AIza...`): the configs auto-load it via
`python-dotenv` (`load_dotenv(..., override=True)` in `wf2_python/config.py` + `wf3_python/config.py`), so ANY
terminal works — no `setx` / fresh-terminal needed. `.env` is the single secrets file (can also hold `DB_PASSWORD`,
`GMAIL_*`). Needs `pip install python-dotenv` (already in `wf3_python/.venv`). Free, generous quota.

**Pitches are RESEARCHED, professional-length + fact-checked** (10 Jul — "shrink the small model's job"): the
qwen 3B model is bad at deciding facts, so we constrain it hard. (1) **Length + shape**: prompt targets
`PITCH_MIN_WORDS`..`PITCH_MAX_WORDS` (70–140) as **three short paragraphs** — P1 "shows research" by naming the
SPECIFIC products/services from the homepage (greeting includes the company name, e.g. "Hi there at X,"); P2 =
the verified issue + why it matters + Granjur's concrete offer (custom dev, MVPs, staff augmentation); P3 = one
soft CTA. **Mobile score is LINK-ONLY (no number in the email):** Lighthouse scores fluctuate run-to-run
(Google says "values may vary" — we saw 63/65/69 on one site), so the pitch says "your store loads slowly on
phones per Google's mobile speed test" and the appended P.S. link shows the EXACT current figure. The real
number is still measured/stored + shown in the dashboard fact-check box; it just never gets baked into the copy. `pitch._enforce_length`
hard-trims overflow (keeping the CTA), too-short stubs are retried, and `_clean` strips markdown `*`, flourish,
and the "Our angle is" leak. The specificity in P1 depends on WF-2's homepage scrape, which now also captures
meaningful `<h1>-<h3>` headings (product ranges/specialties), not just the meta blurb — so pitches can name
"Genesis, Ridgeback & Frog bikes", not a generic summary. Richness MUST come from real homepage words, never
invented specifics (the fact-checker flags any that aren't grounded). (2) **Grounding**: the prompt
gets an explicit `OPENING FACT` (the homepage words / detected tech) that sentence 1 must reference, the raw
OSM category tag is no longer fed to the model (it caused invented industries), temperature dropped to 0.2,
and `_clean` also strips flourish ("as someone who…", "it's clear", "strong online presence"). (3) **Real
fact-check** — new `wf3_python/factcheck.py` scores each pitch: it flags sentences with an **invented number**,
a **tech they don't use**, **unverifiable flourish**, or a **parroted category tag**. `pitch.generate_pitch`
uses that score to keep the best-grounded of its retries (`config.PITCH_GROUNDING_MIN` = 0.6) and returns a
`pitch_grounding` value. The dashboard **Outreach** tab shows a **Grounding %** badge and highlights any
ungrounded sentence **red** (hover for why) above the editable pitch. Auto-check is English-only (ar/zh skip
the highlight). Proven on qwen: the Shopify-beauty example now returns a 17-word, 100%-grounded pitch.

---

## 1b. THE MAX PLAN — 6-part production upgrade (13 Jul 2026) ✅

A production-scale upgrade layered on the working 4-phase pipeline. **Same rules apply**: 100% free
(no paid tools), fully decoupled (phases meet only through DB columns/tables, never call each other),
`GRANJUR_DRY_RUN=1` safe default. Each part is proven end-to-end on the live DB **and** surfaced on the
dashboard. Multi-session build notes live in the assistant memory (`maxplan-progress.md`).

**One-time migrations for the Max Plan (idempotent):**
```powershell
..\wf3_python\.venv\Scripts\python.exe database\migrate_phase1.py   # region/timezone/tags + outreach_log
..\wf3_python\.venv\Scripts\python.exe database\migrate_phase6.py   # email_events + funnel timestamps
pip install tzdata                                                   # DST-accurate timezones (Windows has none)
```
(The dashboard also auto-ensures the analytics table at startup, so a fresh DB just works.)

**Phase 1 — Regional filtering & tags.** `companies.region` (enum) already existed; added
`companies.timezone`, `leads.tags TEXT[]` (+GIN), and the append-only `outreach_log`. Isolate any market
end-to-end with **`--region GCC`** (US/EU/UK/GCC/CN/AU) on `run_pipeline.py` (sets `GRANJUR_REGION` for all
children) or any phase; every `db.py` fetch filters on it. Region config + helpers in `wf1_python/targets.py`
(`VALID_REGIONS`, `REGION_TIMEZONE`, `active_region()`).

**Phase 2 — Timezone scheduling & holiday guardrails.** `wf3_python/sendwindows.py` `can_send_now(region)`
is the ENFORCED gate: WF-4 only sends inside the recipient's **local business hours** (09:00–17:00, prime
09–11 & 14–16), never on a local **weekend** (GCC = Fri/Sat, others Sat/Sun) or a local **public holiday**
(`wf3_python/holiday_calendar.py` — free, offline, editable). Out-of-window leads are HELD (stay
QUEUED, logged to `outreach_log` with the next-open time) and flush on a later run. See the **Regions** tab.

**Phase 3 — Email domain health & anti-spam.** `wf4_python/domain_health.py` (free, dnspython): SPF/DKIM/
DMARC lookups + DNSBL blacklist + **warmup** (fresh-mailbox daily-send cap that ramps 5→50/day; counts real
sends from `outreach_log`). WF-4 blocks a live send on an auth/blacklist FAIL and HOLDS anything past the
daily cap. The Gmail sender (`granjur.tech.dev@gmail.com`, consumer) has Google-managed SPF/DKIM/DMARC, so
**warmup + content are the real levers** — for real volume, move to a custom domain (this tool verifies it).
See the **Health** tab (has a "check any domain" box; run `python wf4_python/domain_health.py`).

**Phase 4 — Multi-day follow-up drip.** `wf4_python/followup.py`: nudges CONTACTED leads that never replied
(day 4 "bump" → day 7 "breakup", editable via `GRANJUR_FOLLOWUP_DAYS`). Copy is generic + **multilingual**
— one stored template set per language (`wf4_python/followup_templates.json`, en/ar/zh; Arabic RTL) chosen
by the lead's `pitch_lang`, NOT per-lead LLM. Regenerate translations with
`wf4_python/build_followup_translations.py` (uses Ollama; hardened to reject bad output). Reuses the Phase-2
+ Phase-3 gates. Logs `outreach_log` step 1..N. See the **Follow-ups** tab (`followup.py --preview`).

**Phase 5 — Tag-based CRM + light-mode redesign.** The **Leads** tab is now a CRM hub: click tag chips
(`#GCC #Shopify #SlowMobile #Hiring #Legacy/#Startup/#Ecom #Arabic/#Chinese`) to filter instantly
(client-side). Tags computed by `wf3_python/tagging.py` (live per row + a startup `retag_all`). The whole
dashboard was redesigned to a clean **light theme** (no animations) with per-page titles. **The manual
Review tab was removed** — discovery+intake are automated; human-paste leads now enter via CSV
(`wf1.py <csv>`) or collectors only.

**Phase 6 — Full funnel analytics (free telemetry).** Emails carry an **open pixel** (`/t/open`) and
**click-wrapped links** (`/t/click`, incl. a **personalized Cal.com CTA**), served by the dashboard →
`email_events`. `wf4_python/bounce_parser.py` (Gmail IMAP) extracts **dead/bounced** addresses and
auto-suppresses them (`--simulate x@y.com` to test). The **Analytics** tab shows KPIs, a Sent→Opened→
Clicked→Replied→Booked funnel, conversion **matrices by segment + region**, and a dead-email panel.
**Caveat:** opens/clicks only register when `GRANJUR_TRACK_BASE` is a PUBLIC URL (localhost is fine for
local testing); the bounce parser needs `GMAIL_APP_PASSWORD`.

---

## 1c. POST-MAX-PLAN REFINEMENTS (13 Jul 2026) — copy, branding, go-live wiring ✅

Done after the Max Plan, all proven end-to-end:

**Pitch is ENGLISH-ONLY + simpler + segment-adaptive (`wf3_python/pitch.py`).** Foreign-language
translation was tried (decoupled Google-Translate handler) then REMOVED per decision — **all regions
(GCC/UK/AU/CN) are now pitched in professional English** (`pitch_lang` is always `"en"`; follow-ups too).
The prompt is simple/jargon-light and morphs by segment via `SEGMENT_STYLE` A/B/C (A = modernize a
local/brick business incl. custom dev/MVPs/staff-aug; B = dev pods vs costly hiring; C = slow mobile store
→ protect checkouts). `_rep_name(lead)` gives the greeting a **representative name** derived from a
PERSONAL contact email (`jehad.al-atrash@x` → "Hello Jehad,"; role inboxes info@/sales@ → "Hello to the
team at {Company},"). `_clean_company_name` strips compound "A / B" names. KEPT the anti-fabrication guard
(no inventing their industry — stops the "radiotechnics company" hallucination) + `factcheck` grounding +
a foreign-script bleed stripper. See the assistant memory `pitch-tone-preference.md`.

**Email signature: embedded logo + 3 offices (`wf4_python/outreach.py`, `send_gmail.py`).** Drop a logo at
`wf4_python/assets/granjur_logo.png` (or project-root `logo.png` — `logo_path()` finds it; a 4K/4.8 MB source
was auto-resized to ~24 KB). It embeds as a **data: URI** so it renders in every preview; `send_gmail.py`
swaps it for a **cid: inline attachment** on a real send (Gmail can strip data: images). The footer now lists
**3 offices** (Regional/Lahore, UK/Birmingham, Canada/Mississauga — `OFFICES` in outreach.py) for
compliance + credibility.

**Booking CTA → real Google Calendar (`GRANJUR_BOOKING_LINK`).** The "Book a 15-min intro call" button links
to a Google Calendar **Appointment schedule** (set your Mon–Fri 9am–7pm PKT hours there; Google shows the
recipient only your FREE 15-min slots and blocks booked ones — all native/free). Set the link in `.env`
(`GRANJUR_BOOKING_LINK=https://calendar.app.google/...`). The button is **hidden if unset** (no dead link)
and goes **direct to Google** (NOT click-wrapped — a wrapped link would break for real recipients since the
tracker is on localhost). `outreach.py` now auto-loads the project-root `.env` (wf4 had no config).

**Regions tab: per-region mass-send buttons.** Each region card now has a **Send N now** button
(`POST /regions/send/<region>`) that runs WF-4 for JUST that market — sends the in-window leads, holds the
rest (respects the Phase-2 window/holiday + Phase-3 warmup gates). Green when the region is PRIME/good. A
banner reports the result; a **DRY RUN / LIVE** mode badge is shown. Respects `GRANJUR_DRY_RUN`.

**Go-live wiring done:** `GMAIL_ADDRESS` + `GMAIL_APP_PASSWORD` set as **persistent User env vars** (so any
terminal auto-sends), `GRANJUR_BOOKING_LINK` in `.env`, logo in place, offices set. A **live test email**
sent successfully via `wf4.py --test granjur.tech.dev@gmail.com`. **Still `GRANJUR_DRY_RUN` unset ⇒ DRY RUN**
— flip to `GRANJUR_DRY_RUN=0` (persistent) + restart the dashboard to send for real.

---

## 1d. ONE-COMMAND AUTOPILOT + 19/day quota + email-yield upgrade (15 Jul 2026) ✅

Goal: a single command that runs the **whole** pipeline hands-off and lines up **19 send-ready leads/day**
(only leads that actually reach outreach count — `ERROR`/`NEEDS_CONTACT` never do), then sends them.

**`run_pipeline.py` (project root) — the single command.** `python scripts/run_pipeline.py`
- Loops **discover → enrich → qualify → auto-approve** until the daily target of send-ready leads is queued,
  then **sends** (WF-4) + runs the **follow-up drip**, then **exports Excel** (a dated snapshot + the one
  central database — see below) of everything retrieved.
- **Fixed daily target = 19** (`GRANJUR_DAILY_TARGET`, or `--count N`). Only `QUEUED_FOR_OUTREACH` counts.
- **DRY RUN by default** (nothing sent) — go live with `$env:GRANJUR_DRY_RUN = "0"; python scripts/run_pipeline.py`.
- Flags: `--region GCC`, `--max-rounds 25`, `--jobs`, `--skip-discovery`, `--no-send`, `--seed CSV`.
- The dashboard's own manual buttons were **not** touched — this is a separate CLI orchestrator.
- **Deadlock fix:** the orchestrator's DB connection is `autocommit=True`. A held transaction would lock the
  `lead_status` enum type and hang any child phase running `ALTER TYPE … ADD VALUE` (wf3/rearm). Don't remove.

**`export_leads_csv.py` — retrieved-data export, `.xlsx` only** (run by the pipeline; also standalone:
`python scripts/export_leads_csv.py`). One row per lead mapped to the outreach schema: contact/firmographic,
OSM (address falls back to "city, country"; coordinates left blank — see gotchas), job-board intent, and
personalization. **Everything is Excel — no more CSV files** (opens in Excel 2010+). Three kinds of file live
in `exports/`:
- **Discovery lake** → `exports/discovered.xlsx` — one row per **company** (all of them), every raw field we
  found (domain, phone, source, gmaps rating/reviews, tech, `first_seen_at`, status, email…), **no
  segmentation**. Plus a **By Source** sheet counting each bot's contribution (osm / gmaps / remoteok /
  remotive / csv). A live DB mirror, rebuilt each run. This is "everything the bots ever found".
- **Per-run snapshot** → `exports/granjur_report_<ts>.xlsx` — a frozen picture of the whole DB at the moment
  one run finished (sheets: **Summary** status counts + **Companies** full detail). We keep the most recent
  **8** and auto-prune older ones so the folder stays tidy.
- **One central database** → `exports/granjur_central.xlsx` — the pipeline CRM that **accumulates every run's
  info together**. Three sheets: **Summary** (current counts), **Runs Log** (one appended row per run: timestamp
  + the stage breakdown that day + which snapshot file), **Latest Leads** (the full current list, refreshed live
  each export, now with the **status journey**: `discovered_at · enriched_at · qualified_at · queued_at ·
  contacted_at · replied_at · last_activity`, derived from `lead_events`). Open this for "the whole database in
  one place"; `discovered.xlsx ⊇ this` (join on `company_id`/`domain`).
- The dashboard **Analytics → "Download central Excel database"** button (`/report.xlsx`) serves this same
  central file, refreshed live from the DB on each click (a plain download does **not** add a Runs Log row —
  only a real pipeline run does).
- **Runs Log accuracy:** each run appends its row from the **same numbers** it wrote into that run's snapshot,
  so a row can never disagree with its snapshot file. If the log ever drifts (e.g. after manually deleting
  snapshots), repair it with `python scripts/export_leads_csv.py --rebuild-log` — it rebuilds the Runs Log by
  reading each `granjur_report_<ts>.xlsx`'s own Summary sheet back in, one accurate row per file present.
- **Excel-lock note:** if you have `granjur_central.xlsx` open in Excel when a run finishes, Windows locks it,
  so the export can't refresh it (the run still writes its dated snapshot and doesn't crash). Close the file in
  Excel before/after a run to let the central database update. Opt-in raw CSV is still available via `--csv`.

**Email-yield upgrade (the free-source bottleneck was email discovery):**
1. **Deep email scraping** (`wf2_python/enrich_free.py`): when the homepage has no personal email, follow the
   site's own Contact/About/Team links + try common paths, parsing **`mailto:` first** (trusted), then a
   left-anchored text scan. Role detection hardened (`contactus@`, `customerservice@`, `sales.team@` → skip).
   **Proven:** the same 24 companies that gave **0** sendable with homepage-only scraping gave **3** send-ready
   (Yeast Nashville, Nicole Bowden Optics, Biking Point) — all with valid personal emails + pitches. Now queued.
2. **Wider funnel** (`wf1_python/collect_osm.py`): `PER_CELL` 2→**5** (`GRANJUR_PER_CELL`), `FETCH_POOL`→90,
   and **Overpass retry/backoff** (2 mirror passes) so a free-server 504 doesn't waste a round.
3. **Guaranteed lane** (`--seed`): drop a contacts CSV (with emails) at `./seed_leads.csv` (template:
   `templates/seed_leads.example.csv`) or pass `--seed path.csv` — they flow straight to send-ready via `wf1.py`.

**Smart role-email relaxation** (`wf2_python/enrich_free.py`, 15 Jul 2026): the #1 yield lever. A small
local business (shop/service/office niche, NO hiring signal) with only `info@`/`contact@`/`hello@`/`admin@`
is now treated as sendable (that inbox is the owner's real mailbox, not a trap). Enterprise / job-board /
tech-SaaS leads stay STRICT (all role inboxes skipped); `noreply@`/`postmaster@` never allowed. Proven:
unlocks 3 of 6 previously-parked role leads, and makes OSM's many `info@` businesses count toward quota.

**Public-registry ingestion** (`import_public_registry.py`): parse a downloaded free bulk dataset (UK
Companies House, OpenCorporates, PDL free dump, city business-license CSVs) with pandas → wf1 schema.
Cleans names, and when a row has a website but no email, constructs `info@<domain>` **only after an MX
check** (a guessed address on a domain with no mail server is a guaranteed bounce → dropped); guesses are
marked `unverified`. Registries with NO domain (Companies House) yield company records but nothing sendable
— those rows are skipped + counted. `get_quota_leads.py` uses `./public_registry.csv` as a final top-up tier.

**Quota-filler** (`get_quota_leads.py`, auto-run by `run_pipeline.py` when the crawl is short; `--no-quota`
to disable): computes `deficit = target − queued`, then fetches that many real businesses that publish
**website + email** from OSM Overpass across high-coverage cities (EU/UK first), cleans names, maps to the
wf1 schema, caps to the deficit (never overshoots 19). Falls back to a **local `quota_fallback.csv`** you
maintain (columns: `company_name,website,email,region,city,niche`) — the reliable lane, since there is no
free open dataset of verified company emails. **Proven:** pulled 5 real Berlin businesses w/ emails in seconds. Caveats: quota
leads are lower-intent (generic first line, off-ICP categories appear) and some OSM emails are role/typo'd
(skipped or bounce-suppressed) — fine for `--test` (goes to your own inbox), **review before LIVE sending**.

**Reality check:** free crawling yields ~1 personal email per several sites, so a single run may line up
**fewer than 19** and says so honestly. To reliably hit 19/day: run more rounds (rotation reaches new cities),
add `--jobs`, widen `wf1_python/targets.py` city pools, and/or use `--seed`.

---

## 2. How to run everything

**Setup (one time):** the shared virtual environment lives in `wf3_python/.venv` — all folders use it.
```powershell
cd "c:\Users\hh\Desktop\B2B Pipeline\wf3_python"
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt          # psycopg2, requests, flask
pip install dnspython                     # for WF-2/WF-4 email checks
# set your Postgres password in wf3_python/config.py (already done)
pip install tzdata                        # Max Plan: DST-accurate timezones
# one-time DB migrations:
python ..\wf1_python\init_db.py                            # discovery_candidates table
python ..\wf4_python\init_db.py                            # outreach columns + gate fix
python ..\database\migrate_phase1.py                       # Max Plan: region/timezone/tags + outreach_log
python ..\database\migrate_phase6.py                       # Max Plan: email_events + funnel timestamps
```
Prereqs: Postgres service running (`granjur_pipeline` DB); Ollama running with `qwen2.5:3b`.
Everywhere below, run scripts with the venv Python: `..\wf3_python\.venv\Scripts\python.exe <script>`.

**The dashboard (leave it running):**
```powershell
cd wf3_python; .\.venv\Scripts\python.exe dashboard.py     # http://localhost:5000
```

**Run the whole pipeline in one command:**
```powershell
python scripts/run_pipeline.py --collect --send   # ALL stages hands-off: collect -> enrich -> qualify -> (auto-approve + dry-run send + follow-up drip)
python scripts/run_pipeline.py --collect          # stages 1-3 only (leaves outreach for human review on the dashboard)
python scripts/run_pipeline.py                     # just enrich + qualify what's waiting
python scripts/run_pipeline.py --collect --send --region GCC   # Max Plan: isolate the whole pass to ONE market
```
`--send` uses `wf4_python/auto_approve.py` to queue every outreach-ready lead (the compliance + Phase-2
send-window + Phase-3 warmup gates still apply), dry-run sends, then runs `followup.py` for due nudges.
Omit `--send` to keep the human review gate (recommended for real sends). `--region XX` isolates every
stage to one market (US/EU/UK/GCC/CN/AU).

**Or run phases individually** (each from its own folder, with the venv Python; all accept `--region XX`):
- WF-1 discovery: `wf1_python/collect_osm.py` (OSM), `collect_jobs.py [remoteok|remotive]` (job feeds), `collect_maps.py` (Google Maps scrape — needs Playwright + a proxy), `wf1.py <csv>` (CSV import for LinkedIn/Upwork/Fiverr leads). All collectors are governed by `scripts/governor.py` (daily caps + rest/backoff) and dedup by domain; OSM caches geocodes in `city_bbox`. See `DEPLOYMENT_PLAN.md` for the full bot fleet + anti-flag design.
- WF-2 enrich: `wf2_python/wf2.py` (mode set by `config.ENRICH_MODE` = `free`).
- WF-3 qualify+pitch: `wf3_python/wf3.py` (SLOW — ~1–2 min LLM pitch per qualified lead).
- WF-4 outreach: approve pitches on the dashboard **Outreach** tab → `wf4_python/wf4.py` (dry-run send, gated by send-window + warmup) → `webhook_server.py` (port 5001, inbound events).
- WF-4 follow-ups: `wf4_python/followup.py` (`--preview` to see due nudges + copy; `--ignore-window` to force).

**Max Plan scripts (all free):** `wf4_python/domain_health.py [domain]` (SPF/DKIM/DMARC/blacklist/warmup),
`wf4_python/bounce_parser.py [--days N|--simulate x@y.com]` (IMAP dead-email extraction),
`wf4_python/build_followup_translations.py` (LLM regen of follow-up translations),
`wf3_python/tagging.py` (recompute CRM tags), `database/migrate_phase1.py` / `migrate_phase6.py` (migrations).

**Scheduler (optional, off by default):**
```powershell
.\setup_scheduler.ps1                 # every 6h: collect->enrich->qualify (throttled)
.\setup_scheduler.ps1 -Remove         # disable
```
For automatic time-gated dispatch, run `wf4_python/wf4.py` + `followup.py` periodically (e.g. hourly) —
the send-window gate lets only in-window leads through, so held leads flush when their local morning opens.

**Handy helpers:** `wf3_python/status_report.py`, `show_pitches.py`; `wf2_python/check_free.py <domain>`;
`wf1_python/prune.py` (drop blocklisted chains); `wf2_python/rearm.py`, `wf1_python/seed_discovered.py`.

**Key env vars** (in a project-root `.env`, OR persistent User env vars for `GMAIL_*`): `GRANJUR_DRY_RUN`
(0=live real send), `GRANJUR_REGION`, `GMAIL_ADDRESS`/`GMAIL_APP_PASSWORD` (Gmail send/IMAP — currently set
persistently), `GRANJUR_BOOKING_LINK` (Google Calendar booking page for the CTA button),
`PAGESPEED_API_KEY`, `GRANJUR_MAILBOX_CREATED` + `GRANJUR_WARMUP_CEIL` (warmup), `GRANJUR_FOLLOWUP_DAYS`
(drip cadence), `GRANJUR_TRACK_BASE` (public URL for open/click tracking), `GRANJUR_LOGO_PATH`,
`GRANJUR_ADDRESS`. `outreach.py` auto-loads `.env`.

---

## 3. How discovery works now (hands-off; real data worldwide)

Per the PDF ("bots discover, humans act"), discovery is automatic; the human reviews the **pitch** before
outreach — NOT every company.
- **Collectors** (`collect_osm.py`, `collect_jobs.py`, `collect_maps.py`) + CSV import → write to
  `discovery_candidates`. Every collector calls **`scripts/governor.py`** first (per-source daily caps +
  a `rest_until` backoff on rate-limits/blocks), so a cron loop can never hammer a source into a ban.
- **`intake.py`** auto-evaluates each against **`targets.py`** (regions, employee range, chain blocklist)
  → auto-APPROVE (becomes DISCOVERED) / auto-REJECT / (rarely) leave PENDING for a human.
- **Free sources, all real data:**
  - **OpenStreetMap** — `collect_osm.py` **geocodes the city via Nominatim → bounding box → Overpass**
    query (works for ANY city worldwide: proven for Dubai/Riyadh, Berlin, Sydney, Austin, Manchester; CN
    can hit transient 429). `[!"brand"]` excludes chains. Geocodes are **cached in `city_bbox`** so a city
    is only ever hit once on Nominatim. Top-of-file knobs: `PER_CELL`=5, `FETCH_POOL`=90.
  - **Job boards** — `collect_jobs.py [remoteok|remotive]` pulls dev-hiring companies from **RemoteOK +
    Remotive** (Segment B intent); each feed is governed separately (≤6 pulls/day, backoff on 429).
  - **Google Maps** — `collect_maps.py` scrapes Maps directly behind a **5-layer safety trigger** (per-run
    + per-day caps, min-interval, a CAPTCHA/consent **block detector**, randomized delays). Needs Playwright
    + a residential proxy (`GMAPS_PROXIES`); stays inert without them. Highest-risk bot — see `DEPLOYMENT_PLAN.md §4.1`.
  - **LinkedIn / Fiverr / Upwork = CSV lane only** (ban risk) → drop a CSV in `inbox/` (`bot-csv`) or run
    `wf1.py <csv>`; the server never scrapes those platforms.
- **DIVERSITY (10 Jul)** — the collector no longer refetches the same top-2 each run. It pulls a big
  `FETCH_POOL`, **shuffles**, and keeps only PER_CELL companies **not already in the DB** (intake dedupes on
  domain), so each run surfaces DIFFERENT businesses until a city is exhausted. And the no-arg sweep now
  **rotates cities**: `targets.REGION_CITIES` holds a pool of ~7 cities per region and each run picks the
  next one (rotation counter persisted in `wf1_python/.discovery_rotation.json`), spreading discovery
  geographically over runs. Edit `REGION_CITIES` to add/remove cities. Target one on demand:
  `python collect_osm.py GCC Riyadh business`. The **Discovered** date now shows on the Leads + Flow tabs
  (from `companies.first_seen_at`) so you can tell which run a company came from.
- Real GCC leads work end-to-end: e.g. Dubai bike/marine shops → valid email → **Arabic pitch**.
- **Watch it live** on the dashboard **Flow** tab: pipeline nodes + a source×stage matrix + a live feed
  showing each lead's source ("Maps (OSM)" / "Job boards" / "Manual paste") and current stage.

---

## 4. Hard-won gotchas (don't re-debug these)

1. **LLM is slow** (~1–2 min/pitch on the 11.8 GB laptop). WF-3 on a big batch takes 20–40 min — run it in the background. `format:"json"` + a retry loop handle qwen dropping the pitch body.
2. **Free contact-email yield ~30–40% on REAL data.** No email + no intent (job post) → parks in `ERROR` at WF-2. A lead that qualifies but has only a role email (`info@`/`sales@`, flagged `email_validation_status='role'`) or no sendable email now parks in `NEEDS_CONTACT` (WF-3 skips its LLM pitch — see §1) instead of wasting the model. This is the free-tier ceiling — the gap is filled by the **"＋ Add contact"** button on NEEDS_CONTACT rows (Leads tab) or the Review **Add form**. We are NOT using paid enrichment (Apollo).
3. **`.example`/`.test` domains never resolve** — use REAL domains to see live tech/MX enrichment.
4. **DB trigger was broken:** `enforce_outreach_gate` referenced `email_status` / `email_hash` (wrong columns); fixed in `wf4_python/outreach_schema.sql` to use `email_validation_status` and allow `unverified`. The other trigger `enforcement_suppression_guardrail` is correct (uses `target_value`).
5. **Windows console = cp1252** — don't `print()` chars like `→`/`·` (use ASCII). Files store Unicode fine.
6. **Restart the dashboard after editing it** (`debug=False`, no auto-reload). It's started detached.
7. **PowerShell mangles inline Python** with `*`, `(`, quotes — put throwaway scripts in the scratchpad instead.
8. **Schema quirks:** email col is `email_validation_status` (not `email_status`); leads has both `icp_segment` and `segment` (WF-3 uses `icp_segment`); `suppression_list` keys on `target_value`.
9. **`--collect` ADDS to the DB — it doesn't wipe.** For a clean fresh start: `python wf3_python\reset_pipeline.py --yes` (TRUNCATEs all rows; keeps schema/triggers), then `python scripts/run_pipeline.py --collect`.
10. **Dashboard port 5000 can be held by an OLD process** serving stale code (browser shows old tabs). Before restarting, kill the port owner: `Get-NetTCPConnection -LocalPort 5000 | %{ Stop-Process -Id $_.OwningProcess -Force }`. It's started detached (`Start-Process ... -WindowStyle Hidden`).
11. **Windows has no IANA timezone DB** — `zoneinfo` needs the free `tzdata` package (`pip install tzdata`). `sendwindows.py` falls back to fixed offsets (DST-blind) + warns in the UI if it's missing.
12. **Open/click tracking needs a PUBLIC `GRANJUR_TRACK_BASE`** — recipients can't reach `localhost`, so real opens/clicks only register once it points at a public domain/tunnel. Fine as-is for local dry-run.
13. **Cross-folder imports:** `wf4` scripts reach `wf3_python/sendwindows.py` by **appending** (not inserting) `../wf3_python` to `sys.path` so `wf4`'s own `db.py`/`config.py` still win. The dashboard imports `wf4_python/*` via `importlib`. The shared-package refactor is still deliberately deferred.
14. **qwen2.5:3b is a poor translator** — `build_followup_translations.py` echoed English back, so the follow-up `followup_templates.json` ships with hand-authored ar/zh; the generator rejects output that isn't in the target script.

---

## 5. What's LEFT

Recently DONE (7 Jul, free hardening): role-account suppression (never cold-email info@/sales@),
**Analytics tab** (funnel math + yield by source/segment), GDPR footer (LIA + privacy link), DE/AT
routed out of cold email, job-collector precision + a 2nd free source (Remotive), WON/LOST feedback
capture, and a staleness re-check (`wf4_python/revalidate_stale.py`).

DONE (9 Jul): **international OSM** (geocode→bbox — GCC/AU/EU/CN cities now work; was Dubai=0),
**Flow tab** (n8n-style live view), **one-command all-4** (`run_pipeline.py --collect --send`),
`reset_pipeline.py` fresh-start wipe, `briefing.pdf`/`briefing.pptx` non-tech decks, folder cleanup,
small-batch tuning (`PER_CELL`). Verified real end-to-end on 6-region data incl. Arabic Gulf pitches.

**DONE (10 Jul) — this session (all detailed in §1/§3):**
1. **COOLDOWN auto-returns** after 3 days + manual **↻ Re-arm** button (`rearm_cooldown.py`, `leads.cooldown_until`).
2. **Pitch overhaul** — researched **3-paragraph** style (names real homepage products, company-name greeting,
   Granjur capability line), 70–140 words, `factcheck.py` grounding score + red highlights on Outreach,
   `_clean` strips markdown/flourish/leaks. Small-model job shrunk (Python owns facts; LLM only wordsmiths).
3. **Real Google mobile score** via free **PageSpeed Insights** key — `.env` + `python-dotenv` auto-loads it
   (`load_dotenv(override=True)`; no more `setx`/fresh-terminal). Segment-C pitches are **link-only** (no number,
   since Lighthouse fluctuates) with a **live P.S. report link** (`pitch.psi_report_url`); score + date shown in
   the Outreach fact-check box. Verified real (63/65/69 vs Google's 63) — not hardcoded.
4. **Discovery diversity** — collector pulls a big `FETCH_POOL`, shuffles, keeps only NEW (dedup) companies;
   no-arg sweep **rotates cities** (`targets.REGION_CITIES`, ~7/region). **Discovered** date on Leads + Flow.
   Small batches: `PER_CELL`=2/region.
5. **NEEDS_CONTACT status** — WF-3 skips the slow LLM pitch for role-email / no-email leads (can't be cold-emailed)
   and parks them for a human. **"＋ Add contact"** button on those rows re-queues them (→ENRICHED) with a real email.

**DONE (13 Jul) — the full MAX PLAN (6 parts, all detailed in §1b):**
1. **Regional filtering & tags** — `--region XX` isolation; `companies.timezone`, `leads.tags`, `outreach_log` (migrate_phase1).
2. **Timezone scheduling & holiday guardrails** — enforced local-hours/weekend/holiday send-gate (`sendwindows.py`, `holiday_calendar.py`); **Regions** tab.
3. **Email domain health & anti-spam** — SPF/DKIM/DMARC + blacklist + warmup cap (`domain_health.py`); **Health** tab.
4. **Multi-day follow-up drip** — multilingual bump/breakup nudges (`followup.py`, `followup_templates.json`); **Follow-ups** tab.
5. **Tag-based CRM + light-mode redesign** — instant tag filtering on **Leads**; whole dashboard re-themed; Review tab removed.
6. **Full funnel analytics** — open pixel + click wrapper + tracked calendar CTA + IMAP bounce parser; **Analytics** tab conversion matrix.

DONE toward go-live (§1c, 13 Jul): real 3-office footer + `GRANJUR_ADDRESS`, embedded logo, **Google-Calendar
booking button** (`GRANJUR_BOOKING_LINK`), `GMAIL_*` set persistently, a **successful live test email**, and
per-region **Send** buttons on the Regions tab.

Remaining:
1. **Flip to real sending:** set `GRANJUR_DRY_RUN=0` (persistent) + restart the dashboard. Then the Regions
   **Send** buttons (and `wf4.py`/`followup.py`) send actual emails — still warmup-capped (20/day) + window-gated.
2. **Deliverability at volume:** Gmail is fine for tiny volume but spam-prone for cold outreach — warm a
   **custom sending domain** (verify SPF/DKIM/DMARC on the Health tab). Set `GRANJUR_TRACK_BASE` to a PUBLIC
   URL so open/click tracking works for real recipients. Run `wf4.py`/`followup.py` hourly for auto time-gated dispatch.
3. **Confirm regional holiday dates** in `holiday_calendar.py` (lunar/Islamic ones are ~approx) for the year you send in.
4. **Shared-package refactor** (DEFERRED on purpose): unify duplicated `config.py`/`db.py` + the region/sendwindows helpers across wf1–4. Invasive, zero user-facing value — its own focused task.
5. **Depletion-driven rotation:** `discovery_cells` DEPLETED tracking still isn't wired to auto-skip exhausted cities.
6. **Feed outcomes back into targeting:** review WON/LOST cohorts + tune `targets.py`/qualifier (capture built; loop manual).

---

## 6. NEXT SESSION — start here 👇

1. **Start the dashboard**, killing any stale one first:
   ```powershell
   Get-NetTCPConnection -LocalPort 5000 -ErrorAction SilentlyContinue | %{ Stop-Process -Id $_.OwningProcess -Force -ErrorAction SilentlyContinue }
   cd wf3_python; .\.venv\Scripts\python.exe dashboard.py    # then open http://localhost:5000
   ```
   Tabs: **Dashboard · Flow · Leads · Outreach · Regions · Follow-ups · Health · Analytics** (light mode).
   First run on a fresh checkout: apply the Max-Plan migrations (§1b) + `pip install tzdata` once.
2. **Do a clean real run** (small). The `PAGESPEED_API_KEY` is in the project-root `.env` (auto-loaded).
   ```powershell
   python wf3_python\reset_pipeline.py --yes         # fresh slate (optional)
   python scripts/run_pipeline.py --collect --send           # collect -> enrich -> qualify -> gated dry-run send -> follow-ups
   python scripts/run_pipeline.py --collect --send --region GCC   # or isolate one market
   ```
   Watch it on **Flow**; check send timing on **Regions**; review pitches on **Outreach**; filter by tag on
   **Leads**; watch open/click/reply rates on **Analytics**; check deliverability on **Health**.
3. **The 3 ICP segments** (targeting): **A_LEGACY_BRICK** = traditional local biz, weak web → digital
   transformation; **B_FUNDED_STARTUP** = hiring developers (job posts) → offshore dev pod; **C_LOWTECH_ECOM** =
   online store (Shopify/Woo) → fix slow mobile (Google speed test + link). Logic in `wf3_python/rules.py`.
4. **Likely next work** (from Section 5): the **go-live** checklist — real footer address, a warmed **custom
   sending domain** (verify it on the Health tab), a PUBLIC `GRANJUR_TRACK_BASE`, real Cal.com, then flip
   `GRANJUR_DRY_RUN=0`. Also: better handling for **Chinese leads** (discover fine but expose no email).
5. Detailed per-phase notes live in each folder's `WF*_GUIDE.md` and in the assistant's memory files
   (esp. `maxplan-progress.md`).

Bottom line: a **complete, free, 4-phase Python pipeline + the full 6-part Max Plan** (regions, timezone/
holiday scheduling, domain health + warmup, multilingual follow-up drip, tag-based CRM, funnel analytics),
with a clean **light-mode dashboard**, proven end-to-end on REAL international data, running in safe dry-run. 🎉
