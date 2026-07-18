"""
Free, offline public-holiday calendar (Max Plan · Phase 2).

No paid holiday API — just an editable table of national public holidays per region. The send-gate
(sendwindows.can_send_now) reads this so cold email never lands on a local public holiday.

EDIT FREELY: add/adjust dates for your target countries and future years. Dates are the RECIPIENT's
LOCAL calendar date (YYYY-MM-DD). Lunar / Islamic holidays (marked ~approx) shift year to year and are
best-effort — confirm them for the year you send in. Region keys mirror the region_code enum; each
region uses ONE representative country (the same one its timezone points at):
GCC->UAE(Asia/Dubai), US, UK(England), EU->Germany(Europe/Berlin), CN, AU(national).
"""
from datetime import date, timedelta

# region -> { "YYYY-MM-DD": "Holiday name" }
HOLIDAYS = {
    "US": {
        "2026-01-01": "New Year's Day", "2026-01-19": "Martin Luther King Jr. Day",
        "2026-02-16": "Presidents' Day", "2026-05-25": "Memorial Day",
        "2026-06-19": "Juneteenth", "2026-07-03": "Independence Day (observed)",
        "2026-09-07": "Labor Day", "2026-11-11": "Veterans Day",
        "2026-11-26": "Thanksgiving", "2026-12-25": "Christmas Day",
        "2027-01-01": "New Year's Day",
    },
    "UK": {
        "2026-01-01": "New Year's Day", "2026-04-03": "Good Friday",
        "2026-04-06": "Easter Monday", "2026-05-04": "Early May Bank Holiday",
        "2026-05-25": "Spring Bank Holiday", "2026-08-31": "Summer Bank Holiday",
        "2026-12-25": "Christmas Day", "2026-12-28": "Boxing Day (observed)",
        "2027-01-01": "New Year's Day",
    },
    "EU": {  # Germany (Europe/Berlin)
        "2026-01-01": "Neujahr", "2026-04-03": "Karfreitag", "2026-04-06": "Ostermontag",
        "2026-05-01": "Tag der Arbeit", "2026-05-14": "Christi Himmelfahrt",
        "2026-05-25": "Pfingstmontag", "2026-10-03": "Tag der Deutschen Einheit",
        "2026-12-25": "1. Weihnachtstag", "2026-12-26": "2. Weihnachtstag",
        "2027-01-01": "Neujahr",
    },
    "GCC": {  # UAE (Asia/Dubai) — Islamic dates ~approx, confirm yearly
        "2026-01-01": "New Year's Day", "2026-03-20": "Eid al-Fitr (~approx)",
        "2026-03-21": "Eid al-Fitr (~approx)", "2026-05-27": "Arafat Day (~approx)",
        "2026-05-28": "Eid al-Adha (~approx)", "2026-05-29": "Eid al-Adha (~approx)",
        "2026-06-17": "Islamic New Year (~approx)", "2026-08-26": "Prophet's Birthday (~approx)",
        "2026-12-01": "Commemoration Day", "2026-12-02": "UAE National Day",
        "2026-12-03": "UAE National Day", "2027-01-01": "New Year's Day",
    },
    "CN": {
        "2026-01-01": "New Year's Day", "2026-02-17": "Spring Festival (Chinese New Year)",
        "2026-02-18": "Spring Festival", "2026-02-19": "Spring Festival",
        "2026-02-20": "Spring Festival", "2026-04-05": "Qingming Festival",
        "2026-05-01": "Labour Day", "2026-06-19": "Dragon Boat Festival (~approx)",
        "2026-09-25": "Mid-Autumn Festival (~approx)", "2026-10-01": "National Day",
        "2026-10-02": "National Day", "2026-10-03": "National Day",
        "2027-01-01": "New Year's Day",
    },
    "AU": {  # national public holidays
        "2026-01-01": "New Year's Day", "2026-01-26": "Australia Day",
        "2026-04-03": "Good Friday", "2026-04-06": "Easter Monday",
        "2026-04-25": "Anzac Day", "2026-06-08": "King's Birthday",
        "2026-12-25": "Christmas Day", "2026-12-28": "Boxing Day (observed)",
        "2027-01-01": "New Year's Day",
    },
    "OTHER": {  # conservative: only the near-universal ones
        "2026-01-01": "New Year's Day", "2026-12-25": "Christmas Day",
        "2027-01-01": "New Year's Day",
    },
}


def is_holiday(region, local_date):
    """Return the holiday NAME if local_date is a public holiday in region, else None."""
    key = local_date.isoformat() if isinstance(local_date, (date,)) else str(local_date)
    return HOLIDAYS.get(region, {}).get(key)


def upcoming_holidays(region, from_date=None, days=45):
    """List of (date, name) in the next `days` days for region, soonest first (for the dashboard)."""
    from_date = from_date or date.today()
    out = []
    for k, name in HOLIDAYS.get(region, {}).items():
        try:
            d = date.fromisoformat(k)
        except ValueError:
            continue
        if from_date <= d <= from_date + timedelta(days=days):
            out.append((d, name))
    out.sort(key=lambda x: x[0])
    return out
