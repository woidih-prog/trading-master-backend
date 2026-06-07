from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import os
import sqlite3
from datetime import datetime
import pytz
import threading
import time

app = Flask(__name__)
CORS(app, origins="*")
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ANTHROPIC_KEY    = os.environ.get("ANTHROPIC_KEY")
DB_PATH          = "/tmp/journal.db"

mt4_prices      = {}
mt4_candles     = {}
mt4_m15         = {}
mt4_daily       = {}
mt4_screenshots = {}

# Etat temporaire des trades en attente de feedback
# {trade_id: {step: 'resultat'|'contexte'|'difficulte', resultat, contexte, chat_id, message_id}}
pending_feedback = {}

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS journal (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT, pair TEXT, tf TEXT, session TEXT,
        score INTEGER, decision TEXT, bias TEXT,
        entry TEXT, sl TEXT, tp TEXT, rr TEXT,
        resultat TEXT, contexte_marche TEXT, difficulte TEXT,
        pnl REAL, commentaire TEXT, created_at TEXT
    )''')
    # Migration si colonne manquante
    try:
        c.execute("ALTER TABLE journal ADD COLUMN contexte_marche TEXT")
    except: pass
    try:
        c.execute("ALTER TABLE journal ADD COLUMN difficulte TEXT")
    except: pass
    conn.commit()
    conn.close()

init_db()

def send_tg(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload)
    return r.json()

def edit_tg_markup(chat_id, message_id, reply_markup):
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageReplyMarkup",
        json={"chat_id": chat_id, "message_id": message_id, "reply_markup": reply_markup})

def answer_callback(callback_id, text="OK"):
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
        json={"callback_query_id": callback_id, "text": text})

@app.route("/")
def home():
    return "Trading Master V5 Backend OK"

# ── TELEGRAM SIGNAL + BOUTONS ─────────────────────────────────
@app.route("/telegram", methods=["POST"])
def telegram():
    data = request.json
    text = data.get("text", "")
    trade_id = data.get("trade_id")

    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}

    if trade_id:
        payload["reply_markup"] = {
            "inline_keyboard": [[
                {"text": "✅ WIN",  "callback_data": f"r_{trade_id}_win"},
                {"text": "❌ LOSS", "callback_data": f"r_{trade_id}_loss"},
                {"text": "➖ BE",   "callback_data": f"r_{trade_id}_be"}
            ]]
        }

    r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload)
    resp = r.json()

    if trade_id and resp.get("ok"):
        pending_feedback[str(trade_id)] = {
            "step": "resultat",
            "chat_id": TELEGRAM_CHAT_ID,
            "message_id": resp["result"]["message_id"]
        }

    return jsonify(resp)

# ── WEBHOOK TELEGRAM ──────────────────────────────────────────
@app.route("/webhook/telegram", methods=["POST"])
def telegram_webhook():
    data = request.json
    if not data:
        return jsonify({"ok": True})

    callback = data.get("callback_query")
    if not callback:
        return jsonify({"ok": True})

    callback_id  = callback["id"]
    callback_data = callback.get("data", "")
    chat_id      = callback["message"]["chat"]["id"]
    message_id   = callback["message"]["message_id"]

    # ── ETAPE 1 : Résultat (win/loss/be) ──────────────────────
    if callback_data.startswith("r_"):
        parts = callback_data.split("_")
        if len(parts) == 3:
            trade_id = parts[1]
            resultat = parts[2]
            label = {"win":"✅ WIN","loss":"❌ LOSS","be":"➖ BE"}.get(resultat, resultat)

            pending_feedback[trade_id] = {
                "step": "contexte",
                "resultat": resultat,
                "chat_id": chat_id,
                "message_id": message_id
            }

            answer_callback(callback_id, f"{label} noté !")
            edit_tg_markup(chat_id, message_id, {"inline_keyboard": []})

            # Demander le contexte marché
            send_tg(chat_id, f"{label} enregistré.\n\n<b>Type de marché ?</b>", {
                "inline_keyboard": [[
                    {"text": "📈 TREND",        "callback_data": f"c_{trade_id}_trend"},
                    {"text": "📦 RANGE",        "callback_data": f"c_{trade_id}_range"},
                    {"text": "🪤 MANIPULATION", "callback_data": f"c_{trade_id}_manipulation"}
                ]]
            })

    # ── ETAPE 2 : Contexte marché ─────────────────────────────
    elif callback_data.startswith("c_"):
        parts = callback_data.split("_")
        if len(parts) == 3:
            trade_id = parts[1]
            contexte = parts[2]
            label_c  = {"trend":"📈 TREND","range":"📦 RANGE","manipulation":"🪤 MANIPULATION"}.get(contexte, contexte)
            fb = pending_feedback.get(trade_id, {})
            fb["contexte"] = contexte
            fb["step"] = "difficulte"
            pending_feedback[trade_id] = fb

            answer_callback(callback_id, f"{label_c} noté !")
            edit_tg_markup(chat_id, message_id, {"inline_keyboard": []})

            # Demander la difficulté
            send_tg(chat_id, f"{label_c} noté.\n\n<b>Difficulté du setup ?</b>", {
                "inline_keyboard": [[
                    {"text": "🟢 EASY",   "callback_data": f"d_{trade_id}_easy"},
                    {"text": "🟡 MEDIUM", "callback_data": f"d_{trade_id}_medium"},
                    {"text": "🔴 HARD",   "callback_data": f"d_{trade_id}_hard"}
                ]]
            })

    # ── ETAPE 3 : Difficulté ──────────────────────────────────
    elif callback_data.startswith("d_"):
        parts = callback_data.split("_")
        if len(parts) == 3:
            trade_id   = parts[1]
            difficulte = parts[2]
            label_d    = {"easy":"🟢 EASY","medium":"🟡 MEDIUM","hard":"🔴 HARD"}.get(difficulte, difficulte)
            fb = pending_feedback.get(trade_id, {})

            resultat = fb.get("resultat","")
            contexte = fb.get("contexte","")
            label_r  = {"win":"✅ WIN","loss":"❌ LOSS","be":"➖ BE"}.get(resultat, resultat)
            label_c  = {"trend":"📈 TREND","range":"📦 RANGE","manipulation":"🪤 MANIPULATION"}.get(contexte, contexte)

            # Sauvegarder dans le journal
            try:
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                c.execute("UPDATE journal SET resultat=?, contexte_marche=?, difficulte=? WHERE id=?",
                    (resultat, contexte, difficulte, int(trade_id)))
                conn.commit()
                conn.close()
                print(f"Journal mis a jour: trade #{trade_id} = {resultat} / {contexte} / {difficulte}")
            except Exception as e:
                print(f"Erreur update journal: {e}")

            answer_callback(callback_id, "Journal mis à jour !")
            edit_tg_markup(chat_id, message_id, {"inline_keyboard": []})

            # Confirmation finale
            send_tg(chat_id,
                f"<b>Trade #{trade_id} journalise</b>\n"
                f"Resultat  : {label_r}\n"
                f"Marche    : {label_c}\n"
                f"Difficulte: {label_d}\n\n"
                f"Journal mis a jour automatiquement.")

            # Nettoyer
            pending_feedback.pop(trade_id, None)

    return jsonify({"ok": True})

# ── SETUP WEBHOOK ─────────────────────────────────────────────
@app.route("/setup/webhook", methods=["GET"])
def setup_webhook():
    webhook_url = "https://trading-master-backend.onrender.com/webhook/telegram"
    r = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook",
        json={"url": webhook_url})
    return jsonify(r.json())

# ── ANTHROPIC ─────────────────────────────────────────────────
@app.route("/anthropic", methods=["POST"])
def anthropic():
    data = request.json
    r = requests.post("https://api.anthropic.com/v1/messages",
        headers={"Content-Type":"application/json","x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01"},
        json=data)
    return jsonify(r.json())

# ── PRIX ──────────────────────────────────────────────────────
@app.route("/price", methods=["POST"])
def receive_price():
    data = request.json
    symbol = data.get("symbol","").upper().replace("/","")
    mt4_prices[symbol] = data
    return jsonify({"success": True})

@app.route("/price/<symbol>", methods=["GET"])
def get_price(symbol):
    key = symbol.upper().replace("/","")
    price = mt4_prices.get(key)
    if price: return jsonify(price)
    return jsonify({"error": "Prix non disponible"}), 404

# ── BOUGIES H1 ────────────────────────────────────────────────
@app.route("/candles", methods=["POST"])
def receive_candles():
    data = request.json
    symbol = data.get("symbol","").upper().replace("/","")
    mt4_candles[symbol] = data
    print(f"Bougies H1: {symbol} — {len(data.get('candles',[]))} bougies")
    return jsonify({"success": True})

@app.route("/candles/<symbol>", methods=["GET"])
def get_candles(symbol):
    key = symbol.upper().replace("/","")
    candles = mt4_candles.get(key)
    if candles: return jsonify(candles)
    return jsonify({"error": "Bougies non disponibles"}), 404

# ── BOUGIES M15 ───────────────────────────────────────────────
@app.route("/m15", methods=["POST"])
def receive_m15():
    data = request.json
    symbol = data.get("symbol","").upper().replace("/","")
    mt4_m15[symbol] = data
    print(f"Bougies M15: {symbol} — {len(data.get('candles',[]))} bougies")
    return jsonify({"success": True})

@app.route("/m15/<symbol>", methods=["GET"])
def get_m15(symbol):
    key = symbol.upper().replace("/","")
    candles = mt4_m15.get(key)
    if candles: return jsonify(candles)
    return jsonify({"error": "Bougies M15 non disponibles"}), 404

# ── BOUGIES DAILY ─────────────────────────────────────────────
@app.route("/daily", methods=["POST"])
def receive_daily():
    data = request.json
    symbol = data.get("symbol","").upper().replace("/","")
    mt4_daily[symbol] = data
    print(f"Daily: {symbol} — {len(data.get('candles',[]))} bougies")
    return jsonify({"success": True})

@app.route("/daily/<symbol>", methods=["GET"])
def get_daily(symbol):
    key = symbol.upper().replace("/","")
    daily = mt4_daily.get(key)
    if daily: return jsonify(daily)
    return jsonify({"error": "Daily non disponible"}), 404

# ── SCREENSHOT ────────────────────────────────────────────────
@app.route("/screenshot", methods=["POST"])
def receive_screenshot():
    data = request.json
    symbol = data.get("symbol","").upper().replace("/","")
    mt4_screenshots[symbol] = data
    return jsonify({"success": True})

@app.route("/screenshot/<symbol>", methods=["GET"])
def get_screenshot(symbol):
    key = symbol.upper().replace("/","")
    shot = mt4_screenshots.get(key)
    if shot: return jsonify(shot)
    return jsonify({"error": "Screenshot non disponible"}), 404

# ── JOURNAL ───────────────────────────────────────────────────
@app.route("/journal", methods=["POST"])
def add_trade():
    data = request.json
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''INSERT INTO journal
        (date,pair,tf,session,score,decision,bias,entry,sl,tp,rr,resultat,contexte_marche,difficulte,pnl,commentaire,created_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (data.get("date"),data.get("pair"),data.get("tf"),data.get("session"),data.get("score"),
         data.get("decision"),data.get("bias"),data.get("entry"),data.get("sl"),data.get("tp"),
         data.get("rr"),data.get("resultat"),data.get("contexte_marche"),data.get("difficulte"),
         data.get("pnl"),data.get("commentaire"),datetime.now().isoformat()))
    conn.commit()
    trade_id = c.lastrowid
    conn.close()
    return jsonify({"success": True, "id": trade_id})

@app.route("/journal", methods=["GET"])
def get_trades():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT * FROM journal ORDER BY id DESC LIMIT 100")
    rows = c.fetchall()
    cols = [d[0] for d in c.description]
    conn.close()
    return jsonify([dict(zip(cols,r)) for r in rows])

@app.route("/journal/<int:trade_id>", methods=["PUT"])
def update_trade(trade_id):
    data = request.json
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("UPDATE journal SET resultat=?,contexte_marche=?,difficulte=?,pnl=?,commentaire=? WHERE id=?",
        (data.get("resultat"),data.get("contexte_marche"),data.get("difficulte"),
         data.get("pnl"),data.get("commentaire"),trade_id))
    conn.commit()
    conn.close()
    return jsonify({"success": True})

@app.route("/journal/stats", methods=["GET"])
def get_stats():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    stats = {}
    c.execute("SELECT COUNT(*) FROM journal"); stats["total"] = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM journal WHERE resultat='win'"); stats["wins"] = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM journal WHERE resultat='loss'"); stats["losses"] = c.fetchone()[0]
    c.execute("SELECT COUNT(*) FROM journal WHERE resultat='be'"); stats["be"] = c.fetchone()[0]
    c.execute("SELECT SUM(pnl) FROM journal WHERE pnl IS NOT NULL"); stats["total_pnl"] = round(c.fetchone()[0] or 0, 2)
    c.execute("SELECT COUNT(*) FROM journal WHERE resultat IS NOT NULL AND resultat!=''")
    done = c.fetchone()[0]
    stats["winrate"] = round(stats["wins"]/done*100) if done > 0 else 0

    # Stats par contexte marché
    for ctx in ["trend","range","manipulation"]:
        c.execute("SELECT COUNT(*) FROM journal WHERE contexte_marche=?", (ctx,)); total_ctx = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM journal WHERE contexte_marche=? AND resultat='win'", (ctx,)); wins_ctx = c.fetchone()[0]
        stats[f"ctx_{ctx}"] = {"total": total_ctx, "wins": wins_ctx, "winrate": round(wins_ctx/total_ctx*100) if total_ctx > 0 else 0}

    # Stats par difficulté
    for diff in ["easy","medium","hard"]:
        c.execute("SELECT COUNT(*) FROM journal WHERE difficulte=?", (diff,)); total_d = c.fetchone()[0]
        c.execute("SELECT COUNT(*) FROM journal WHERE difficulte=? AND resultat='win'", (diff,)); wins_d = c.fetchone()[0]
        stats[f"diff_{diff}"] = {"total": total_d, "wins": wins_d, "winrate": round(wins_d/total_d*100) if total_d > 0 else 0}

    conn.close()
    return jsonify(stats)

# ── SCHEDULER ─────────────────────────────────────────────────
def scheduler_job():
    paris_tz = pytz.timezone('Europe/Paris')
    analyzed_today = {'london': None, 'ny': None}
    while True:
        try:
            now = datetime.now(paris_tz)
            h, m, day = now.hour, now.minute, now.weekday()
            today = now.strftime('%Y-%m-%d')
            if day < 5:
                if h == 7 and m == 0 and analyzed_today['london'] != today:
                    analyzed_today['london'] = today
                    trigger_analysis('London Open')
                if h == 13 and m == 30 and analyzed_today['ny'] != today:
                    analyzed_today['ny'] = today
                    trigger_analysis('NY Open')
        except Exception as e:
            print(f"Scheduler error: {e}")
        time.sleep(30)

def trigger_analysis(session):
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID,
                  "text": f"Trading Master V5 — {session}\nAnalyse automatique declenchee.",
                  "parse_mode": "HTML"})
        print(f"Scheduler: {session} declenche")
    except Exception as e:
        print(f"Trigger error: {e}")

threading.Thread(target=scheduler_job, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    def keep_alive():
        while True:
            time.sleep(840)
            try: requests.get("https://trading-master-backend.onrender.com/")
            except: pass
    threading.Thread(target=keep_alive, daemon=True).start()
    app.run(host="0.0.0.0", port=port)
