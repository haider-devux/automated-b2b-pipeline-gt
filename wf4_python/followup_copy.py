"""
Follow-up drip: 4-step copy + scheduling logic (Max Plan · Phase 4).

PURE module — no DB, no network, no sending. Two jobs:
  1. generate_followup(lead, kind) -> (subject, body, "en")  : the nudge text for a given step.
  2. next_due_step(steps_sent, initial_at, now)              : which step a CONTACTED lead is due for.

The pipeline pitches every region in ENGLISH, so follow-ups are English too. The sequence is 4 distinct,
low-friction, non-spammy emails (nudge -> issue -> check -> breakup) stored in followup_templates.json.
Cadence is cumulative from the ORIGINAL pitch: day 3, 10, 20, 34 (override via GRANJUR_FOLLOWUP_DAYS).
Bodies carry NO sign-off — outreach.py appends the signature.
"""
import json
import os
from datetime import timedelta

SENDER_NAME = os.getenv("GRANJUR_SENDER_NAME", "Asma Haider")

_TEMPLATES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "followup_templates.json")


def _load_templates():
    try:
        with open(_TEMPLATES_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:                   # noqa: BLE001 — a missing/corrupt store must not crash sending
        return {}


TEMPLATES = _load_templates()
_EN = TEMPLATES.get("en", {})

# The 4 steps, in order. after_days is measured from the INITIAL pitch (step 0). Override the day
# thresholds via env GRANJUR_FOLLOWUP_DAYS="3,10,20,34" (comma-separated, one per step).
_STEP_KINDS = ["nudge", "issue", "check", "breakup"]   # extra steps (if any) repeat the last kind
_DEFAULT_DAYS = [3, 10, 20, 34]


def _build_steps():
    days = _DEFAULT_DAYS
    env = os.getenv("GRANJUR_FOLLOWUP_DAYS")
    if env:
        try:
            days = [int(x) for x in env.split(",") if x.strip() != ""]
        except ValueError:
            days = _DEFAULT_DAYS
    return [{"step": i + 1, "after_days": d, "kind": _STEP_KINDS[min(i, len(_STEP_KINDS) - 1)]}
            for i, d in enumerate(days)]


FOLLOWUP_STEPS = _build_steps()
MAX_STEP = max((s["step"] for s in FOLLOWUP_STEPS), default=0)


def followup_lang(lead):
    return "en"                          # English-only pipeline


def is_rtl(lang):
    return False


def generate_followup(lead, kind, sender_name=None):
    """Return (subject, body, "en") for a follow-up. `lead` needs first_name, legal_name, pitch_subject.
    `kind` is one of nudge|issue|check|breakup."""
    sender_name = sender_name or SENDER_NAME
    fn = (lead.get("first_name") or "").strip()
    greeting = (_EN.get("greeting_named", "Hi {name},").format(name=fn) if fn
                else _EN.get("greeting_generic", "Hi there,"))
    company = (lead.get("legal_name") or "your team").strip()

    body_tpl = _EN.get(kind) or _EN.get("nudge", "{greeting}")
    body = body_tpl.format(greeting=greeting, company=company, sender=sender_name)

    original = (lead.get("pitch_subject") or "").strip()
    prefix = _EN.get("subject_prefix", "Re:")
    subject = f"{prefix} {original}".strip() if original else prefix.rstrip(":")
    return subject, body, "en"


def next_due_step(steps_sent, initial_at, now):
    """Given the follow-up steps already sent (iterable of ints), the initial-contact datetime, and
    'now', decide the drip state. Returns a dict:
       {complete: bool, step, kind, after_days, due_at, is_due, days_until}."""
    sent = {int(s) for s in (steps_sent or []) if s is not None and int(s) >= 1}
    for cfg in FOLLOWUP_STEPS:
        if cfg["step"] in sent:
            continue
        due_at = (initial_at + timedelta(days=cfg["after_days"])) if initial_at else None
        is_due = bool(due_at and now >= due_at)
        days_until = None if not due_at else max(0, (due_at - now).days + (0 if now >= due_at else 1))
        return {"complete": False, "step": cfg["step"], "kind": cfg["kind"],
                "after_days": cfg["after_days"], "due_at": due_at,
                "is_due": is_due, "days_until": 0 if is_due else days_until}
    return {"complete": True, "step": None, "kind": None, "after_days": None,
            "due_at": None, "is_due": False, "days_until": None}
