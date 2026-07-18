r"""
Minimal open/click tracking server — the ONLY thing safe to expose publicly.

The dashboard (port 5000) has these same endpoints, but it also shows all your leads and the send
buttons and has no login — so we must NOT tunnel it. This tiny server hosts ONLY the open-pixel and the
click-redirect, writing to the same `email_events` table. Point a free tunnel (cloudflared) at THIS
(port 5002), set GRANJUR_TRACK_BASE to the tunnel URL, and your dashboard stays private on localhost.

  python scripts/tracking_server.py            # serves http://localhost:5002/t/open/<id> and /t/click/<id>

Endpoints (called by the pixel + wrapped links that outreach.py builds):
  GET /t/open/<lead_id>?step=N          -> logs an 'open',  returns a 1x1 gif
  GET /t/click/<lead_id>?step=N&u=URL   -> logs a 'click', 302-redirects to URL
  GET /healthz                          -> "ok" (tunnel liveness check)
"""
import base64
import importlib.util
import re
from pathlib import Path

import psycopg2
from psycopg2.extras import Json
from flask import Flask, request, redirect, Response

ROOT = Path(__file__).resolve().parent.parent  # scripts/ -> project root
_PIXEL = base64.b64decode("R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7")
DEFAULT_REDIRECT = "https://granjur.com"

_spec = importlib.util.spec_from_file_location("wf3cfg", ROOT / "wf3_python" / "config.py")
_cfg = importlib.util.module_from_spec(_spec); _spec.loader.exec_module(_cfg)
DB = _cfg.DB

app = Flask(__name__)


def _conn():
    c = psycopg2.connect(host=DB["host"], port=DB["port"], dbname=DB["dbname"],
                         user=DB["user"], password=DB["password"])
    c.autocommit = True
    return c


def _step():
    try:
        return int(request.args.get("step", "0"))
    except ValueError:
        return 0


@app.route("/healthz")
def healthz():
    return "ok"


@app.route("/t/open/<lead_id>")
def track_open(lead_id):
    try:
        c = _conn()
        with c.cursor() as cur:
            cur.execute("INSERT INTO email_events (lead_id, step, event_type, detail) VALUES (%s,%s,'open',%s)",
                        (lead_id, _step(), Json({"ua": request.headers.get("User-Agent", "")[:200]})))
            cur.execute("UPDATE leads SET first_open_at = COALESCE(first_open_at, now()) WHERE id=%s", (lead_id,))
        c.close()
    except Exception:                       # noqa: BLE001 — a tracking miss must never error the client
        pass
    return Response(_PIXEL, mimetype="image/gif",
                    headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"})


@app.route("/t/click/<lead_id>")
def track_click(lead_id):
    url = request.args.get("u") or DEFAULT_REDIRECT
    try:
        c = _conn()
        with c.cursor() as cur:
            cur.execute("INSERT INTO email_events (lead_id, step, event_type, url, detail) "
                        "VALUES (%s,%s,'click',%s,%s)",
                        (lead_id, _step(), url, Json({"ua": request.headers.get("User-Agent", "")[:200]})))
            cur.execute("UPDATE leads SET first_click_at = COALESCE(first_click_at, now()), "
                        "first_open_at = COALESCE(first_open_at, now()) WHERE id=%s", (lead_id,))
        c.close()
    except Exception:                       # noqa: BLE001
        pass
    if not re.match(r"^https?://", url or ""):   # only ever redirect to real http(s) links
        url = DEFAULT_REDIRECT
    return redirect(url, code=302)


if __name__ == "__main__":
    print("Tracking server -> http://localhost:5002  (expose ONLY this via the tunnel)")
    app.run(host="0.0.0.0", port=5002, debug=False)
