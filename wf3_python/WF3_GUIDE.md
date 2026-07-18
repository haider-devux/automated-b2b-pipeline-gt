# WF-3 (Python) — AI Qualification & Translation

> Ported from the working n8n WF-3 on **2 July 2026**. This is the first piece of the
> all-Python Granjur B2B pipeline. Read this top-to-bottom and you have the full context.

---

## What WF-3 does
Takes `ENRICHED` leads → decides **fit + segment** (in code) → writes a **localized sales pitch**
(with the local LLM) → saves the result, moving each lead to `QUALIFIED` or `DISQUALIFIED`.

## Where it fits — the relay race
The pipeline phases **never call each other**. They pass leads through the `leads.status` column:

```
DISCOVERED (WF-1) → ENRICHED (WF-2) → QUALIFIED / DISQUALIFIED (WF-3 = this) → QUEUED_FOR_OUTREACH (WF-4)
```

The **database is the single source of truth.** WF-3 only needs to know: *read `status='ENRICHED'`,
write the next status.* That's why we can build each phase separately — they integrate through the DB.

## The golden rule we learned the hard way
**Code decides; the LLM only writes copy.** The small local model (`qwen2.5:3b`) is unreliable at
classification — it hallucinated triggers and copied prompt examples. So qualification + segmentation
are **100% deterministic Python** (`rules.py`); the LLM is used **only** to write + translate the
pitch (`pitch.py`). This matches the blueprint's "firm business rules the model can't override."

## Files (each ≈ one n8n node)
| File | Does | n8n equivalent |
|---|---|---|
| `config.py` | DB + Ollama settings, mappings | credentials / settings |
| `db.py` | read ENRICHED, write QUALIFIED/DISQUALIFIED, audit row | "Execute a SQL query" + DB nodes |
| `rules.py` | disqualify + segment (deterministic) | "JSON extractor" code node |
| `pitch.py` | LLM pitch + localization + robust JSON parse | "Basic LLM Chain1" + "Pitch parser" |
| `wf3.py` | orchestrator: loop leads → rules → route → pitch → write | the canvas wiring + Switch |

## Setup (one time)
1. Check Python is installed: `python --version` (need 3.9+).
2. In this folder, create a virtual environment and install dependencies:
   ```powershell
   python -m venv .venv
   .\.venv\Scripts\Activate.ps1
   pip install -r requirements.txt
   ```
3. **Set your Postgres password:** open `config.py` and replace `PUT_YOUR_POSTGRES_PASSWORD_HERE`
   with the password you use in pgAdmin for user `postgres`. (Or set env var `DB_PASSWORD`.)
4. Make sure **Ollama** is running and has the model: `ollama list` should show `qwen2.5:3b`.
5. Make sure **Postgres** is running. It runs natively on your PC — you do **NOT** need Docker or
   pgAdmin open. (pgAdmin is just a viewer; the pipeline talks to Postgres directly.)

## Run it
```powershell
python wf3.py
```
It processes **all** `ENRICHED` leads in one go — no clicking 11 times like n8n. Qualified leads
take ~1–2 min each (local LLM); disqualified leads are instant (no LLM call).

## Re-arm the test batch (to run again)
```sql
UPDATE leads SET status='ENRICHED'
WHERE company_id IN (SELECT id FROM companies WHERE source='mock');
```

## Check the results
```sql
SELECT c.legal_name, c.employee_count, l.status, l.icp_segment, l.qualify_score, l.pitch_lang
FROM leads l JOIN companies c ON l.company_id = c.id
WHERE c.source='mock'
ORDER BY l.icp_segment NULLS FIRST, c.legal_name;
```
Expected: dental/bakery → **A**, Coastal/Souq → **C**, Falcon/BlueWhale/Nimbus → **B**,
Jane Doe/MegaCorp/Unknown Ventures → **DISQUALIFIED**; GCC → `ar`, CN → `zh`, else `en`.

## The rules (rules.py)
- **Disqualify** if `employee_count <= 1` (single-person / unknown) **or** `> 150` (enterprise w/ internal IT).
- Else pick the segment **in order** (stop at first match):
  1. **B** — a developer/engineer role in `active_job_posts` (React/iOS/etc.)
  2. **C** — Shopify/WooCommerce in tech stack, or an e-commerce description
  3. **A** — everything else (mid-size local/service business)

## Localization (pitch.py)
`region` GCC → Arabic, CN → Chinese, else English. Sets `pitch_lang` + `pitch_localized`.

## Lessons carried over from n8n
- **Parameterized SQL** (never concat AI text into SQL): `psycopg2` `%(name)s` handles quotes/newlines safely.
- **Parse only the first `{...}`** the model returns; `json.loads(strict=False)` tolerates raw newlines.
- **The model can't be trusted with decisions** — that's why they live in `rules.py`.

## Next (tomorrow / later)
1. Run this and confirm results match the n8n run.
2. Build **WF-2** (enrichment) as a sibling Python module: reads `DISCOVERED` → API waterfalls → writes `ENRICHED`. Needs real API keys (Apollo/Hunter/etc.) for live data.
3. Build **WF-1** (discovery), then **WF-4** (outreach — needs a tiny always-on webhook server for reply/booking events; that's the one place n8n was easier).
4. Refactor `config.py`/`db.py` into a shared package used by all phases.


Tomorrow — start here (3 quick things, all in WF3_GUIDE.md)
Set your Postgres password in wf3_python/config.py (replace PUT_YOUR_POSTGRES_PASSWORD_HERE).
pip install -r requirements.txt (ideally in a venv).
Re-arm the batch, then python wf3.py — and we compare its output to your n8n run to confirm they match.