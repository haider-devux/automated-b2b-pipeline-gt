"""
WF-4 webhook server — the pipeline's one always-on service (the DB stays the system of record).

Providers (Instantly/Smartlead) and Cal.com POST inbound events here; each updates the lead's state.
Runs on port 5001 (the dashboard is on 5000).

  python webhook_server.py

Endpoints:
  POST /webhook/reply      {lead_id, sentiment}   -> REPLIED
  POST /webhook/booking    {lead_id}              -> BOOKED
  POST /webhook/bounce     {lead_id}              -> SUPPRESSED (+ suppression_list)
  GET  /webhook/unsubscribe?lead=<id>             -> SUPPRESSED (one-click opt-out from the footer)
"""
from flask import Flask, request, jsonify
import db

app = Flask(__name__)


def _lead_id(default_json_key="lead_id"):
    d = request.get_json(force=True, silent=True) or request.form
    return d.get(default_json_key)


@app.post("/webhook/reply")
def reply():
    lead_id = _lead_id()
    sentiment = ((request.get_json(silent=True) or request.form).get("sentiment")) or "neutral"
    if not lead_id:
        return jsonify(error="lead_id required"), 400
    conn = db.get_connection()
    try:
        db.mark_replied(conn, lead_id, sentiment)
    finally:
        conn.close()
    return jsonify(ok=True, lead_id=lead_id, status="REPLIED", sentiment=sentiment)


@app.post("/webhook/booking")
def booking():
    lead_id = _lead_id()
    if not lead_id:
        return jsonify(error="lead_id required"), 400
    conn = db.get_connection()
    try:
        db.mark_booked(conn, lead_id)
    finally:
        conn.close()
    return jsonify(ok=True, lead_id=lead_id, status="BOOKED")


@app.post("/webhook/bounce")
def bounce():
    lead_id = _lead_id()
    if not lead_id:
        return jsonify(error="lead_id required"), 400
    conn = db.get_connection()
    try:
        db.suppress(conn, lead_id, "bounce")
    finally:
        conn.close()
    return jsonify(ok=True, lead_id=lead_id, status="SUPPRESSED")


@app.route("/webhook/unsubscribe", methods=["GET", "POST"])
def unsubscribe():
    lead_id = request.args.get("lead") or (request.get_json(silent=True) or {}).get("lead_id")
    if not lead_id:
        return "Missing lead id", 400
    conn = db.get_connection()
    try:
        db.suppress(conn, lead_id, "optout")
    finally:
        conn.close()
    return ("<html><body style='font-family:system-ui;padding:48px;max-width:520px'>"
            "<h2>You've been unsubscribed.</h2>"
            "<p>You will not receive further emails from Granjur Technologies. "
            "Your address has been added to our permanent suppression list.</p></body></html>")


if __name__ == "__main__":
    print("WF-4 webhook server -> http://localhost:5001")
    app.run(host="127.0.0.1", port=5001, debug=False)
