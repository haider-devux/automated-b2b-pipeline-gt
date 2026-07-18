"""
Send-window advisor (Max Plan · Phase 1 — visual layer).

Given the current instant, tells you — per region — the recipient's LOCAL time, whether it's a good
moment to cold-email them, and when the next good window opens. This is ADVISORY only: it powers the
dashboard "Regions" tab so a human can see "GCC is prime right now, run outreach for it." The actual
automated time-gated send-gate (holding a queued email until its local window) is Phase 2.

Timezone math uses the stdlib `zoneinfo` (DST-accurate) backed by the free, offline `tzdata` package.
If tzdata is somehow missing it degrades to fixed UTC offsets (DST-blind) so the dashboard never crashes.
"""
from datetime import datetime, timedelta, timezone

import holiday_calendar

try:
    from zoneinfo import ZoneInfo
    _HAVE_TZ = True
except Exception:                       # noqa: BLE001 — pre-3.9 or no tzdata -> fixed-offset fallback
    _HAVE_TZ = False

# Mirrors targets.REGION_TIMEZONE / companies.timezone. Kept local to avoid a cross-folder import
# (phases run as separate processes; the shared-package refactor is deferred — Guide.md §5).
REGION_TZ = {
    "GCC": "Asia/Dubai",    "US": "America/New_York", "UK": "Europe/London",
    "EU":  "Europe/Berlin", "CN": "Asia/Shanghai",    "AU": "Australia/Sydney",
    "OTHER": "UTC",
}
# DST-blind fallback offsets (hours from UTC), only used if tzdata is unavailable.
_FIXED_OFFSET = {"GCC": 4, "US": -5, "UK": 0, "EU": 1, "CN": 8, "AU": 10, "OTHER": 0}

# Local weekday indices (Mon=0 .. Sun=6) that count as the WEEKEND per region.
# GCC business week is Sun–Thu, so Fri(4)/Sat(5) are off; most others are Sat(5)/Sun(6).
_WEEKEND = {"GCC": {4, 5}, "US": {5, 6}, "UK": {5, 6}, "EU": {5, 6}, "CN": {5, 6},
            "AU": {5, 6}, "OTHER": {5, 6}}

BUSINESS_START, BUSINESS_END = 9, 17     # 09:00–17:00 local = acceptable to send
PRIME_WINDOWS = ((9, 11), (14, 16))      # best open/reply rates (mid-morning, early-afternoon)

# State ranking for "who should I send to right now" (lower = send sooner).
STATE_RANK = {"PRIME": 0, "GOOD": 1, "OFF": 2, "WEEKEND": 3, "HOLIDAY": 4}


def _local_now(region, now_utc):
    """Recipient-local aware datetime for a region."""
    if _HAVE_TZ:
        try:
            return now_utc.astimezone(ZoneInfo(REGION_TZ.get(region, "UTC")))
        except Exception:                # noqa: BLE001 — unknown key -> fall through to fixed offset
            pass
    off = _FIXED_OFFSET.get(region, 0)
    return now_utc.astimezone(timezone(timedelta(hours=off)))


def _is_weekend(region, dt):
    return dt.weekday() in _WEEKEND.get(region, {5, 6})


def _is_workday(region, dt):
    """A regular business day for this region: not a weekend AND not a public holiday."""
    return (not _is_weekend(region, dt)) and (holiday_calendar.is_holiday(region, dt.date()) is None)


def _in_prime(hour):
    return any(a <= hour < b for a, b in PRIME_WINDOWS)


def _blocked_reason(region, local_dt):
    """Why sending is NOT allowed at this recipient-local instant, or None if it IS allowed."""
    if _is_weekend(region, local_dt):
        return "weekend"
    hol = holiday_calendar.is_holiday(region, local_dt.date())
    if hol:
        return f"holiday: {hol}"
    hour = local_dt.hour + local_dt.minute / 60.0
    if hour < BUSINESS_START:
        return "before business hours"
    if hour >= BUSINESS_END:
        return "after business hours"
    return None


def _next_open(region, local_dt):
    """Datetime (local) of the next moment sending becomes acceptable — skips weekends AND holidays."""
    d = local_dt
    # If it's a working day and we're simply before today's business start, it opens today.
    if _is_workday(region, d) and d.hour < BUSINESS_START:
        return d.replace(hour=BUSINESS_START, minute=0, second=0, microsecond=0)
    # Otherwise roll forward to the next WORKING day's business start (skip weekends + holidays).
    d = (d + timedelta(days=1)).replace(hour=BUSINESS_START, minute=0, second=0, microsecond=0)
    for _ in range(20):                  # covers long holiday stretches (e.g. CN Golden Week)
        if _is_workday(region, d):
            return d
        d += timedelta(days=1)
    return d


def _fmt_delta(delta):
    mins = max(0, int(delta.total_seconds() // 60))
    h, m = divmod(mins, 60)
    if h and m:
        return f"{h}h {m}m"
    return f"{h}h" if h else f"{m}m"


def region_status(region, now_utc=None):
    """Return an advisory dict for one region at the given instant (defaults to real 'now', UTC)."""
    now_utc = now_utc or datetime.now(timezone.utc)
    local = _local_now(region, now_utc)
    reason = _blocked_reason(region, local)

    if reason is None:
        state = "PRIME" if _in_prime(local.hour) else "GOOD"
        holiday = None
    elif reason.startswith("holiday:"):
        state, holiday = "HOLIDAY", reason.split("holiday:", 1)[1].strip()
    elif reason == "weekend":
        state, holiday = "WEEKEND", None
    else:
        state, holiday = "OFF", None

    if state in ("PRIME", "GOOD"):
        opens_in, opens_label = timedelta(0), "sending now"
    else:
        nxt = _next_open(region, local)
        opens_in = nxt - local
        # Human "when": today / tomorrow / weekday name
        if nxt.date() == local.date():
            opens_label = f"opens {nxt:%H:%M} today"
        elif nxt.date() == (local + timedelta(days=1)).date():
            opens_label = f"opens {nxt:%H:%M} tomorrow"
        else:
            opens_label = f"opens {nxt:%a %d %b %H:%M}"

    return {
        "region": region,
        "tz": REGION_TZ.get(region, "UTC"),
        "local_time": local.strftime("%H:%M"),
        "local_day": local.strftime("%a"),
        "state": state,
        "holiday": holiday,
        "rank": (STATE_RANK[state], opens_in),   # sort key: best-to-send-now first
        "window": f"{BUSINESS_START:02d}:00–{BUSINESS_END:02d}:00 local",
        "prime": "09:00–11:00 & 14:00–16:00",
        "opens_label": opens_label,
        "opens_in": "" if opens_in.total_seconds() <= 0 else _fmt_delta(opens_in),
        "accurate": _HAVE_TZ,
    }


def can_send_now(region, now_utc=None):
    """THE ENFORCED SEND-GATE (Phase 2). Decides if a cold email to `region` may go out right now.

    Returns a dict:
      ok            True only during local business hours on a working day (no weekend / holiday)
      reason        None if ok, else why it's blocked ('weekend' | 'holiday: X' | 'before/after business hours')
      next_open_utc timezone-aware UTC datetime when the window next opens (store as scheduled_for)
      next_open_local / local_time  human-friendly strings for logs + the dashboard
    Guarantees: never True at 2:00 AM local, on a local weekend, or on a local public holiday.
    """
    now_utc = now_utc or datetime.now(timezone.utc)
    region = (region or "OTHER")
    local = _local_now(region, now_utc)
    reason = _blocked_reason(region, local)
    ok = reason is None
    nxt_local = local if ok else _next_open(region, local)
    return {
        "ok": ok,
        "reason": reason,
        "region": region,
        "local_time": local.strftime("%a %H:%M %Z"),
        "next_open_local": nxt_local.strftime("%a %d %b %H:%M %Z"),
        "next_open_utc": nxt_local.astimezone(timezone.utc),
    }


def all_regions(regions=None, now_utc=None):
    """Advisory rows for every region, sorted best-to-send-now first."""
    regions = regions or [r for r in REGION_TZ if r != "OTHER"]
    rows = [region_status(r, now_utc) for r in regions]
    rows.sort(key=lambda x: x["rank"])
    return rows
