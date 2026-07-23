"""
bot-health (part 2) — domain-health gate that PARKS bot-send when authentication/blacklist fails.

Runs after bounce_parser.py each morning (granjur-health.timer). It asks domain_health for the full
report; if any AUTHENTICATION or BLACKLIST check hard-fails (only possible on a CUSTOM sending domain —
consumer Gmail returns 'na'/provider-managed), it parks the 'send' governor bucket for 24h so no email
goes out from a failing identity. On consumer Gmail this is a clean no-op.

The bounce circuit breaker is enforced live inside wf4.py/followup.py; this covers the DNS/auth side.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(ROOT / "scripts"))       # governor
sys.path.insert(0, str(ROOT / "wf4_python"))    # domain_health

import governor         # noqa: E402
import domain_health    # noqa: E402

PARK_HOURS = 24


def main():
    conn = governor.connect()
    try:
        rep = domain_health.report(conn=conn)
        fails = [c for c in rep["checks"] if c["status"] == "fail"]
        if fails:
            reason = "domain health fail: " + "; ".join(f"{c['name']}={c['detail']}" for c in fails)
            governor.park(conn, "send", hours=PARK_HOURS, reason=reason)
            print(f"[health] {rep['domain']} -> FAIL. Parked bot-send {PARK_HOURS}h.\n  " +
                  "\n  ".join(f"{c['name']}: {c['detail']} (fix: {c['fix']})" for c in fails))
        else:
            print(f"[health] {rep['domain']} -> {rep['overall'].upper()} — send identity clear.")
        # Surface the bounce picture too (informational; the live breaker enforces it at send time).
        bs = domain_health.bounce_stats(conn)
        if bs["sample_ok"]:
            flag = "OVER CEILING" if bs["tripped"] else "ok"
            print(f"[health] bounce rate {bs['rate']:.1%} over {bs['window_days']}d "
                  f"({bs['bounces']}/{bs['sends']}) — {flag}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
