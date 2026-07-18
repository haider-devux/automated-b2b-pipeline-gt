# WF-2 (Python) — Enrichment Waterfall

> Second phase of the all-Python Granjur B2B pipeline. Built after WF-3.
> Reads `DISCOVERED` leads → fills firmographics + decision-maker + verified email →
> writes `ENRICHED` (or parks in `ERROR`). Integrates with the rest of the pipeline **only**
> through the `leads.status` column.

---

## Where it fits — the relay race
```
DISCOVERED (WF-1)  →  ENRICHING → ENRICHED (WF-2 = this)  →  QUALIFIED / DISQUALIFIED (WF-3)  →  outreach (WF-4)
```
WF-2 never calls WF-1 or WF-3. It just reads `status='DISCOVERED'` and writes the next status.

## The golden rules it follows (from the blueprint, Section 04 Phase 2)
1. **Atomic claim.** It flips leads `DISCOVERED → ENRICHING` with `FOR UPDATE SKIP LOCKED` so two
   runs never grab the same lead and a crash leaves a recoverable state, not a lost lead.
2. **Fault tolerance.** Every API step (Apollo → Hunter → BuiltWith → PageSpeed → verify) is wrapped
   in its own try/except. One failing/slow API never halts the lead or its siblings — the failure is
   recorded and the waterfall continues (**partial success**).
3. **Keep-or-park rule.** A lead becomes `ENRICHED` if it has a **verified email OR a strong intent
   signal** (a job post). Otherwise it parks in `ERROR` for a later retry.

## Files (each ≈ one blueprint concern)
| File | Does |
|---|---|
| `config.py` | reuses WF-3's DB password; `ENRICH_MODE` (mock/free/real); batch size |
| `db.py` | atomic claim, write-enriched, park-error, audit log |
| `enrich.py` | dispatcher + the mock waterfall (`_run_mock`) |
| `enrich_free.py` | the FREE waterfall: site-HTML tech detection + DNS MX email check + optional Lighthouse |
| `wf2.py` | orchestrator: claim → waterfall → keep-or-park |
| `check_free.py` | quick tool to test the free enrichers on any real domain (no DB) |
| `seed_discovered.py` | mock demo: inserts skeletal `DISCOVERED` leads (use WF-1's CSV importer for real data) |

## Run it (uses the venv you already made for WF-3)
From this folder (`wf2_python`):
```powershell
# 1) create some DISCOVERED leads to work on (stand-in for WF-1)
..\wf3_python\.venv\Scripts\python.exe seed_discovered.py
# 2) enrich them
..\wf3_python\.venv\Scripts\python.exe wf2.py
```
Then **watch the dashboard** (http://localhost:5000) — the leads move `DISCOVERED → ENRICHED`,
and the ones WF-2 enriches become visible to WF-3.

## What the demo set shows (source = `mock_wf2`)
| Company | Outcome | Demonstrates |
|---|---|---|
| Verdant Landscaping | ENRICHED → WF-3 **A** | clean happy path |
| PixelForge Studio | ENRICHED → WF-3 **B** | **Apollo has no email → Hunter fallback** fills it |
| Bazaar Online (GCC) | ENRICHED → WF-3 **C** (ar) | e-commerce, low Lighthouse |
| Dragon Mart (CN) | ENRICHED → WF-3 **C** (zh) | e-commerce, Chinese |
| Titan Industries | ENRICHED → WF-3 **DISQUALIFIED** | enrichment succeeds, qualifier rejects (>150 staff) |
| Glitch Corp | ENRICHED (tech empty) | **BuiltWith fails but lead still enriches** (partial success) |
| Solo Freelance | **ERROR** | no email anywhere + no intent → parked, doesn't block others |

## Enrichment modes (`config.ENRICH_MODE`)
- **`"mock"`** — canned demo data keyed by domain (the built-in `mock_wf2` demo set). No network.
- **`"free"`** (default) — REAL but free, no paid keys (`enrich_free.py`):
  - **tech_stack** — fetch the site's HTML, match asset/code signatures (Shopify/WordPress/React…)
  - **email** — syntax + DNS **MX** check via `dnspython` (`valid` / `invalid` / `unverified`)
  - **Lighthouse** — optional, only if a free `PAGESPEED_API_KEY` is set (else skipped)
  - **firmographics** — trusted from the CSV/WF-1 row (free manual enrichment)
  - Degrades gracefully: a lead is kept unless its email is *syntactically* invalid AND it has no
    intent — free mode never drops a human-curated lead just because DNS couldn't confirm it.
- **`"real"`** — paid API waterfall (Apollo/Hunter/BuiltWith/ZeroBounce). Not wired: replace the
  `_mock_*` bodies in `enrich.py` and set `ENRICH_MODE="real"`. Claim, keep-or-park, and DB writes
  are identical across all three modes.

Try the free enrichers on any real domain (no DB needed):
```powershell
..\wf3_python\.venv\Scripts\python.exe check_free.py shopify.com
```

## Re-run from scratch
`seed_discovered.py` clears the previous `mock_wf2` set first, so just run it again then `wf2.py`.
