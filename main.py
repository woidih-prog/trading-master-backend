from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import os
import sqlite3
import json
from datetime import datetime

app = Flask(__name__)
CORS(app, origins="*")

TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ANTHROPIC_KEY   = os.environ.get("ANTHROPIC_KEY")
DB_PATH         = "/tmp/journal.db"

# ── Base de données ──────────────────────────────────────────
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS journal (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        date      TEXT,
        pair      TEXT,
        tf        TEXT,
        session   TEXT,
        score     INTEGER,
        decision  TEXT,
        bias      TEXT,
        entry     TEXT,
        sl        TEXT,
        tp        TEXT,
        rr        TEXT,
        resultat  TEXT,
        pnl       REAL,
        commentaire TEXT,
        created_at TEXT
    )''')
    conn.commit()
    conn.close()

init_db()

# ── Routes ───────────────────────────────────────────────────
@app.route("/")
def home():
    return "Trading Master V5 Backend OK"

@app.route("/telegram", methods=["POST"])
def telegram():
    data = request.json
    msg  = data.get("text", "")
    r = requests.post(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
        json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "HTML"}
    )
    return jsonify(r.json())

@app.route("/anthropic", methods=["POST"])
def anthropic():
    data = request.json
    r = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "Content-Type": "application/json",
            "x-api-key": ANTHROPIC_KEY,
            "anthropic-version": "2023-06-01"
        },
        json=data
    )
    return jsonify(r.json())

# ── Journal ──────────────────────────────────────────────────
@app.route("/journal", methods=["POST"])
def add_trade():
    data = request.json
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()
    c.execute('''INSERT INTO journal
        (date, pair, tf, session, score, decision, bias, entry, sl, tp, rr, resultat, pnl, commentaire, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', (
        data.get("date"), data.get("pair"), data.get("tf"),
        data.get("session"), data.get("score"), data.get("decision"),
        data.get("bias"), data.get("entry"), data.get("sl"),
        data.get("tp"), data.get("rr"), data.get("resultat"),
        data.get("pnl"), data.get("commentaire"),
        datetime.now().isoformat()
    ))
    conn.commit()
    trade_id = c.lastrowid
    conn.close()
    return jsonify({"success": True, "id": trade_id})

@app.route("/journal", methods=["GET"])
def get_trades():
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()
    c.execute("SELECT * FROM journal ORDER BY id DESC LIMIT 100")
    rows = c.fetchall()
    cols = [d[0] for d in c.description]
    conn.close()
    return jsonify([dict(zip(cols, r)) for r in rows])

@app.route("/journal/<int:trade_id>", methods=["PUT"])
def update_trade(trade_id):
    data = request.json
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()
    c.execute('''UPDATE journal SET
        resultat=?, pnl=?, commentaire=?
        WHERE id=?''', (
        data.get("resultat"), data.get("pnl"),
        data.get("commentaire"), trade_id
    ))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route("/journal/stats", methods=["GET"])
def get_stats():
    conn = sqlite3.connect(DB_PATH)
    c    = conn.cursor()
    c.execute("SELECT COUNT(*) FROM journal")
    total = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM journal WHERE resultat='win'")
    wins = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM journal WHERE resultat='loss'")
    losses = c.fetchone()[0]
    c.execute("SELECT SUM(pnl) FROM journal WHERE pnl IS NOT NULL")
    total_pnl = c.fetchone()[0] or 0
    c.execute("SELECT COUNT(*) FROM journal WHERE resultat IS NOT NULL AND resultat != ''")
    done = c.fetchone()[0]
    conn.close()
    winrate = round(wins / done * 100) if done > 0 else 0
    return jsonify({
        "total": total, "wins": wins, "losses": losses,
        "winrate": winrate, "total_pnl": round(total_pnl, 2)
    })
@app.route("/price", methods=["POST"])
def receive_price():
    data = request.json
    print(f"Prix MT4 reçu: {data}")
    return jsonify({"success": True, "received": data})
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
