from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime
import pytz
import threading
import time
import redis
import json

app = Flask(__name__)
CORS(app, origins="*")
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ANTHROPIC_KEY    = os.environ.get("ANTHROPIC_KEY")
REDIS_URL        = os.environ.get("REDIS_URL", "redis://red-d8j855mq1p3s73ff62ig:6379")
DATABASE_URL     = os.environ.get("DATABASE_URL")

# ── REDIS ─────────────────────────────────────────────────────
try:
    r = redis.from_url(REDIS_URL, decode_responses=True)
    r.ping()
    print("Redis connecte OK")
except Exception as e:
    print(f"Redis erreur: {e}")
    r = None

mt4_prices_ram  = {}
mt4_candles_ram = {}
mt4_m15_ram     = {}
mt4_daily_ram   = {}
mt4_screenshots_ram = {}
pending_feedback = {}

def redis_set(key, data):
    if r:
        try:
            r.set(key, json.dumps(data), ex=86400)
            return True
        except: pass
    return False

def redis_get(key):
    if r:
        try:
            val = r.get(key)
            if val: return json.loads(val)
        except: pass
    return None

# ── POSTGRESQL ────────────────────────────────────────────────
def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)

def init_db():
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS journal (
            id SERIAL PRIMARY KEY,
            date TEXT, pair TEXT, tf TEXT, session TEXT,
            score INTEGER, decision TEXT, bias TEXT,
            entry TEXT, sl TEXT, tp TEXT, rr TEXT,
            resultat TEXT, contexte_marche TEXT, difficulte TEXT,
            pnl REAL, commentaire TEXT, created_at TEXT,
            direction_ok TEXT, entree_ok TEXT, sortie_ok TEXT,
            raison_sortie TEXT
        )''')
        # Ajouter colonnes si elles n'existent pas (migration)
        for col in ['direction_ok','entree_ok','sortie_ok','raison_sortie']:
            try:
                c.execute(f"ALTER TABLE journal ADD COLUMN {col} TEXT")
            except: pass
        conn.commit()
        conn.close()
        print("PostgreSQL connecte OK")
    except Exception as e:
        print(f"PostgreSQL erreur: {e}")

init_db()

# ── HELPERS TELEGRAM ──────────────────────────────────────────
def send_tg(chat_id, text, reply_markup=None):
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    r2 = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload)
    return r2.json()

def edit_tg_markup(chat_id, message_id, reply_markup):
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/editMessageReplyMarkup",
        json={"chat_id": chat_id, "message_id": message_id, "reply_markup": reply_markup})

def answer_callback(callback_id, text="OK"):
    requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
        json={"callback_query_id": callback_id, "text": text})

@app.route("/")
def home():
    redis_status = "OK" if r else "RAM fallback"
    return f"Trading Master V5 Backend OK — Redis: {redis_status} — DB: PostgreSQL"

# ── TELEGRAM ──────────────────────────────────────────────────
@app.route("/telegram", methods=["POST"])
def telegram():
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "JSON invalide"}), 400
    text = data.get("text", "")
    trade_id = data.get("trade_id")

    ftmo_override = data.get("ftmo_override", False)
    wait_override = data.get("wait_override", False)
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"}

    if trade_id and wait_override:
        # Signal ATTENDRE — boutons Je prends / Je passe
        payload["reply_markup"] = {
            "inline_keyboard": [[
                {"text": "⚡ Je prends", "callback_data": f"wait_{trade_id}_take"},
                {"text": "❌ Je passe",  "callback_data": f"wait_{trade_id}_pass"}
            ]]
        }
    elif trade_id and ftmo_override:
        # Signal bloqué FTMO
        payload["reply_markup"] = {
            "inline_keyboard": [[
                {"text": "⚠️ Ignorer FTMO — Je prends", "callback_data": f"ftmo_{trade_id}_take"},
                {"text": "❌ Ne pas prendre",            "callback_data": f"ftmo_{trade_id}_skip"}
            ]]
        }
    elif trade_id:
        payload["reply_markup"] = {
            "inline_keyboard": [[
                {"text": "✅ WIN",  "callback_data": f"r_{trade_id}_win"},
                {"text": "❌ LOSS", "callback_data": f"r_{trade_id}_loss"},
                {"text": "➖ BE",   "callback_data": f"r_{trade_id}_be"}
            ]]
        }

    resp = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage", json=payload).json()

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
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"ok": True})

    # ── MESSAGE TEXTE LIBRE (commentaire journal) ─────────────
    message = data.get("message")
    if message and not data.get("callback_query"):
        chat_id_msg = message.get("chat", {}).get("id")
        text_msg = message.get("text", "").strip()
        if text_msg and chat_id_msg and str(chat_id_msg) == str(TELEGRAM_CHAT_ID):
            # Chercher si un trade attend un commentaire
            waiting_trade = None
            for tid, fb in list(pending_feedback.items()):
                if fb.get("step") == "commentaire":
                    waiting_trade = tid
                    break
            if waiting_trade:
                fb = pending_feedback[waiting_trade]
                resultat   = fb.get("resultat","")
                contexte   = fb.get("contexte","")
                difficulte = fb.get("difficulte","")
                label_r = {"win":"✅ WIN","loss":"❌ LOSS","be":"➖ BE"}.get(resultat, resultat)
                label_c = {"trend":"📈 TREND","range":"📦 RANGE","manipulation":"🪤 MANIPULATION"}.get(contexte, contexte)
                label_d = {"easy":"🟢 EASY","medium":"🟡 MEDIUM","hard":"🔴 HARD"}.get(difficulte, difficulte)
                try:
                    conn = get_db()
                    c = conn.cursor()
                    c.execute("UPDATE journal SET commentaire=%s WHERE id=%s",
                        (text_msg, int(waiting_trade)))
                    conn.commit()
                    conn.close()
                    print(f"Commentaire trade #{waiting_trade}: {text_msg[:50]}")
                except Exception as e:
                    print(f"Erreur commentaire: {e}")
                send_tg(chat_id_msg, f"<b>✅ Trade #{waiting_trade} journalisé</b>\n{label_r} · {label_c} · {label_d}\n💬 <i>{text_msg[:100]}</i>")
                pending_feedback.pop(waiting_trade, None)
        return jsonify({"ok": True})

    callback = data.get("callback_query")
    if not callback:
        return jsonify({"ok": True})

    callback_id   = callback["id"]
    callback_data = callback.get("data", "")
    chat_id       = callback["message"]["chat"]["id"]
    message_id    = callback["message"]["message_id"]

    # ── ATTENDRE OVERRIDE ────────────────────────────────────
    if callback_data.startswith("wait_"):
        parts = callback_data.split("_")
        if len(parts) == 3:
            trade_id = parts[1]
            action   = parts[2]

            if action == "pass":
                answer_callback(callback_id, "Trade ignoré.")
                edit_tg_markup(chat_id, message_id, {"inline_keyboard": []})
                try:
                    conn = get_db()
                    c = conn.cursor()
                    c.execute("UPDATE journal SET resultat=%s, commentaire=%s WHERE id=%s",
                        ('passe', 'Signal ATTENDRE — trade non pris', int(trade_id)))
                    conn.commit()
                    conn.close()
                except: pass
                send_tg(chat_id, "❌ Trade non pris — Signal ATTENDRE respecté.")

            elif action == "take":
                answer_callback(callback_id, "Override signal ATTENDRE !")
                edit_tg_markup(chat_id, message_id, {"inline_keyboard": []})
                pending_feedback[trade_id] = {"step":"resultat","chat_id":chat_id,"message_id":message_id}
                send_tg(chat_id, "⚡ Override ATTENDRE — trade pris.\n\nRésultat du trade ?", {
                    "inline_keyboard": [[
                        {"text": "✅ WIN",  "callback_data": f"r_{trade_id}_win"},
                        {"text": "❌ LOSS", "callback_data": f"r_{trade_id}_loss"},
                        {"text": "➖ BE",   "callback_data": f"r_{trade_id}_be"}
                    ]]
                })

    # ── FTMO OVERRIDE ────────────────────────────────────────
    elif callback_data.startswith("ftmo_"):
        parts = callback_data.split("_")
        if len(parts) == 3:
            trade_id = parts[1]
            action   = parts[2]

            if action == "skip":
                answer_callback(callback_id, "Trade ignoré.")
                edit_tg_markup(chat_id, message_id, {"inline_keyboard": []})
                send_tg(chat_id, "❌ Trade non pris — FTMO respecté.")

            elif action == "take":
                answer_callback(callback_id, "Trade pris malgré FTMO !")
                edit_tg_markup(chat_id, message_id, {"inline_keyboard": []})
                pending_feedback[trade_id] = {"step":"resultat","chat_id":chat_id,"message_id":message_id}
                send_tg(chat_id, "⚠️ FTMO ignoré — trade pris.\n\nRésultat du trade ?", {
                    "inline_keyboard": [[
                        {"text": "✅ WIN",  "callback_data": f"r_{trade_id}_win"},
                        {"text": "❌ LOSS", "callback_data": f"r_{trade_id}_loss"},
                        {"text": "➖ BE",   "callback_data": f"r_{trade_id}_be"}
                    ]]
                })

    # ── RESULTAT ─────────────────────────────────────────────
    elif callback_data.startswith("r_"):
        parts = callback_data.split("_")
        if len(parts) == 3:
            trade_id = parts[1]
            resultat = parts[2]
            label = {"win":"✅ WIN","loss":"❌ LOSS","be":"➖ BE"}.get(resultat, resultat)
            pending_feedback[trade_id] = {"step":"contexte","resultat":resultat,"chat_id":chat_id,"message_id":message_id}
            answer_callback(callback_id, f"{label} noté !")
            edit_tg_markup(chat_id, message_id, {"inline_keyboard": []})
            send_tg(chat_id, f"{label} enregistré.\n\n<b>Type de marché ?</b>", {
                "inline_keyboard": [[
                    {"text": "📈 TREND",        "callback_data": f"c_{trade_id}_trend"},
                    {"text": "📦 RANGE",        "callback_data": f"c_{trade_id}_range"},
                    {"text": "🪤 MANIPULATION", "callback_data": f"c_{trade_id}_manipulation"}
                ]]
            })

    # ── CONTEXTE MARCHE ──────────────────────────────────────
    elif callback_data.startswith("c_"):
        parts = callback_data.split("_")
        if len(parts) == 3:
            trade_id = parts[1]
            contexte = parts[2]
            label_c = {"trend":"📈 TREND","range":"📦 RANGE","manipulation":"🪤 MANIPULATION"}.get(contexte, contexte)
            fb = pending_feedback.get(trade_id, {})
            fb["contexte"] = contexte
            fb["step"] = "difficulte"
            pending_feedback[trade_id] = fb
            answer_callback(callback_id, f"{label_c} noté !")
            edit_tg_markup(chat_id, message_id, {"inline_keyboard": []})
            send_tg(chat_id, f"{label_c} noté.\n\n<b>Difficulté du setup ?</b>", {
                "inline_keyboard": [[
                    {"text": "🟢 EASY",   "callback_data": f"d_{trade_id}_easy"},
                    {"text": "🟡 MEDIUM", "callback_data": f"d_{trade_id}_medium"},
                    {"text": "🔴 HARD",   "callback_data": f"d_{trade_id}_hard"}
                ]]
            })

    # ── DIFFICULTE ───────────────────────────────────────────
    elif callback_data.startswith("d_"):
        parts = callback_data.split("_")
        if len(parts) == 3:
            trade_id   = parts[1]
            difficulte = parts[2]
            label_d = {"easy":"🟢 EASY","medium":"🟡 MEDIUM","hard":"🔴 HARD"}.get(difficulte, difficulte)
            fb = pending_feedback.get(trade_id, {})
            resultat = fb.get("resultat","")
            contexte = fb.get("contexte","")
            label_r = {"win":"✅ WIN","loss":"❌ LOSS","be":"➖ BE"}.get(resultat, resultat)
            label_c = {"trend":"📈 TREND","range":"📦 RANGE","manipulation":"🪤 MANIPULATION"}.get(contexte, contexte)
            try:
                conn = get_db()
                c = conn.cursor()
                c.execute("UPDATE journal SET resultat=%s, contexte_marche=%s, difficulte=%s WHERE id=%s",
                    (resultat, contexte, difficulte, int(trade_id)))
                conn.commit()
                conn.close()
                print(f"Journal mis a jour: trade #{trade_id} = {resultat} / {contexte} / {difficulte}")
            except Exception as e:
                print(f"Erreur update journal: {e}")
            answer_callback(callback_id, "Presque fini !")
            edit_tg_markup(chat_id, message_id, {"inline_keyboard": []})
            # Passer à l'étape commentaire
            fb["resultat"] = resultat
            fb["contexte"] = contexte
            fb["difficulte"] = difficulte
            fb["step"] = "commentaire"
            pending_feedback[trade_id] = fb
            send_tg(chat_id,
                f"{label_d} noté.\n\n<b>💬 Commentaire sur ce trade ?</b>\nTape ton analyse, ce que tu as vu, pourquoi tu as pris ou raté ce trade.\n\nOu appuie sur Passer pour terminer.",
                {"inline_keyboard": [[{"text": "⏭️ Passer", "callback_data": f"skip_comment_{trade_id}"}]]})


    # ── SKIP COMMENTAIRE ─────────────────────────────────────
    elif callback_data.startswith("skip_comment_"):
        trade_id = callback_data.replace("skip_comment_", "")
        fb = pending_feedback.get(trade_id, {})
        resultat   = fb.get("resultat","")
        contexte   = fb.get("contexte","")
        difficulte = fb.get("difficulte","")
        label_r = {"win":"✅ WIN","loss":"❌ LOSS","be":"➖ BE"}.get(resultat, resultat)
        label_c = {"trend":"📈 TREND","range":"📦 RANGE","manipulation":"🪤 MANIPULATION"}.get(contexte, contexte)
        label_d = {"easy":"🟢 EASY","medium":"🟡 MEDIUM","hard":"🔴 HARD"}.get(difficulte, difficulte)
        send_tg(chat_id, f"<b>✅ Trade #{trade_id} journalisé</b>\n{label_r} · {label_c} · {label_d}\n<i>Sans commentaire</i>")
        answer_callback(callback_id, "Journalisé !")
        edit_tg_markup(chat_id, message_id, {"inline_keyboard": []})
        pending_feedback.pop(trade_id, None)

    return jsonify({"ok": True})

# ── MESSAGES TEXTE LIBRES (commentaires journal) ──────────────
# Le webhook reçoit aussi les messages texte (pas seulement les callbacks)
# On gère ça dans la même route en vérifiant "message" au lieu de "callback_query"
# Note: la route /webhook/telegram gère les deux cas via le même endpoint

@app.route("/admin/reset-journal", methods=["GET"])
def reset_journal():
    secret = request.args.get("key","")
    if secret != "RENARD2026":
        return jsonify({"error": "Non autorise"}), 403
    try:
        conn = get_db()
        c = conn.cursor()
        # Garder uniquement les trades d'aujourd'hui (19/06/2026)
        c.execute("DELETE FROM journal WHERE created_at NOT LIKE '2026-06-19%'")
        deleted = c.rowcount
        conn.commit()
        conn.close()
        return jsonify({"success": True, "message": f"{deleted} anciens trades supprimes. Trades du 19/06 conserves."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def setup_webhook():
    webhook_url = "https://trading-master-backend.onrender.com/webhook/telegram"
    resp = requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook", json={"url": webhook_url})
    return jsonify(resp.json())

# ── ANTHROPIC ─────────────────────────────────────────────────
@app.route("/anthropic", methods=["POST"])
def anthropic():
    data = request.get_json(force=True, silent=True)
    if not data:
        return jsonify({"error": "JSON invalide"}), 400
    resp = requests.post("https://api.anthropic.com/v1/messages",
        headers={"Content-Type":"application/json","x-api-key":ANTHROPIC_KEY,"anthropic-version":"2023-06-01"},
        json=data)
    return jsonify(resp.json())

# ── PRIX ──────────────────────────────────────────────────────
@app.route("/price", methods=["POST"])
def receive_price():
    data = request.get_json(force=True, silent=True)
    if not data: return jsonify({"error": "JSON invalide"}), 400
    symbol = data.get("symbol","").upper().replace("/","")
    if not redis_set(f"price:{symbol}", data): mt4_prices_ram[symbol] = data
    return jsonify({"success": True})

@app.route("/price/<symbol>", methods=["GET"])
def get_price(symbol):
    key = symbol.upper().replace("/","")
    data = redis_get(f"price:{key}") or mt4_prices_ram.get(key)
    if data: return jsonify(data)
    return jsonify({"error": "Prix non disponible"}), 404

# ── BOUGIES H1 ────────────────────────────────────────────────
@app.route("/candles", methods=["POST"])
def receive_candles():
    data = request.get_json(force=True, silent=True)
    if not data: return jsonify({"error": "JSON invalide"}), 400
    symbol = data.get("symbol","").upper().replace("/","")
    if not redis_set(f"candles:{symbol}", data): mt4_candles_ram[symbol] = data
    print(f"Bougies H1: {symbol} — {len(data.get('candles',[]))} bougies")
    return jsonify({"success": True})

@app.route("/candles/<symbol>", methods=["GET"])
def get_candles(symbol):
    key = symbol.upper().replace("/","")
    data = redis_get(f"candles:{key}") or mt4_candles_ram.get(key)
    if data: return jsonify(data)
    return jsonify({"error": "Bougies non disponibles"}), 404

# ── BOUGIES M15 ───────────────────────────────────────────────
@app.route("/m15", methods=["POST"])
def receive_m15():
    raw = request.get_data(as_text=True)
    data = request.get_json(force=True, silent=True)
    if not data:
        print(f"M15 JSON echec: {raw[:200]}")
        return jsonify({"error": "JSON invalide"}), 400
    symbol = data.get("symbol","").upper().replace("/","")
    if not redis_set(f"m15:{symbol}", data): mt4_m15_ram[symbol] = data
    print(f"Bougies M15: {symbol} — {len(data.get('candles',[]))} bougies")
    return jsonify({"success": True})

@app.route("/m15/<symbol>", methods=["GET"])
def get_m15(symbol):
    key = symbol.upper().replace("/","")
    data = redis_get(f"m15:{key}") or mt4_m15_ram.get(key)
    if data: return jsonify(data)
    return jsonify({"error": "Bougies M15 non disponibles"}), 404

# ── BOUGIES DAILY ─────────────────────────────────────────────
@app.route("/daily", methods=["POST"])
def receive_daily():
    data = request.get_json(force=True, silent=True)
    if not data: return jsonify({"error": "JSON invalide"}), 400
    symbol = data.get("symbol","").upper().replace("/","")
    if not redis_set(f"daily:{symbol}", data): mt4_daily_ram[symbol] = data
    print(f"Daily: {symbol} — {len(data.get('candles',[]))} bougies")
    return jsonify({"success": True})

@app.route("/daily/<symbol>", methods=["GET"])
def get_daily(symbol):
    key = symbol.upper().replace("/","")
    data = redis_get(f"daily:{key}") or mt4_daily_ram.get(key)
    if data: return jsonify(data)
    return jsonify({"error": "Daily non disponible"}), 404

# ── SCREENSHOT ────────────────────────────────────────────────
@app.route("/screenshot", methods=["POST"])
def receive_screenshot():
    data = request.get_json(force=True, silent=True)
    if not data: return jsonify({"error": "JSON invalide"}), 400
    symbol = data.get("symbol","").upper().replace("/","")
    mt4_screenshots_ram[symbol] = data
    return jsonify({"success": True})

@app.route("/screenshot/<symbol>", methods=["GET"])
def get_screenshot(symbol):
    key = symbol.upper().replace("/","")
    shot = mt4_screenshots_ram.get(key)
    if shot: return jsonify(shot)
    return jsonify({"error": "Screenshot non disponible"}), 404

# ── JOURNAL ───────────────────────────────────────────────────
@app.route("/journal", methods=["POST"])
def add_trade():
    data = request.get_json(force=True, silent=True)
    if not data: return jsonify({"error": "JSON invalide"}), 400
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute('''INSERT INTO journal
            (date,pair,tf,session,score,decision,bias,entry,sl,tp,rr,
             resultat,contexte_marche,difficulte,pnl,commentaire,created_at,
             direction_ok,entree_ok,sortie_ok,raison_sortie)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id''',
            (data.get("date"),data.get("pair"),data.get("tf"),data.get("session"),
             data.get("score"),data.get("decision"),data.get("bias"),
             data.get("entry"),data.get("sl"),data.get("tp"),data.get("rr"),
             data.get("resultat"),data.get("contexte_marche"),data.get("difficulte"),
             data.get("pnl"),data.get("commentaire"),datetime.now().isoformat(),
             data.get("direction_ok"),data.get("entree_ok"),
             data.get("sortie_ok"),data.get("raison_sortie")))
        conn.commit()
        trade_id = c.fetchone()["id"]
        conn.close()
        return jsonify({"success": True, "id": trade_id})
    except Exception as e:
        print(f"Erreur add_trade: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/journal", methods=["GET"])
def get_trades():
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM journal ORDER BY id DESC LIMIT 100")
        rows = c.fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/journal/<int:trade_id>", methods=["PUT"])
def update_trade(trade_id):
    data = request.get_json(force=True, silent=True)
    if not data: return jsonify({"error": "JSON invalide"}), 400
    try:
        conn = get_db()
        c = conn.cursor()
        c.execute("UPDATE journal SET resultat=%s,contexte_marche=%s,difficulte=%s,pnl=%s,commentaire=%s WHERE id=%s",
            (data.get("resultat"),data.get("contexte_marche"),data.get("difficulte"),
             data.get("pnl"),data.get("commentaire"),trade_id))
        conn.commit()
        conn.close()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/journal/stats", methods=["GET"])
def get_stats():
    try:
        conn = get_db()
        c = conn.cursor()
        stats = {}
        c.execute("SELECT COUNT(*) as n FROM journal"); stats["total"] = c.fetchone()["n"]
        c.execute("SELECT COUNT(*) as n FROM journal WHERE resultat='win'"); stats["wins"] = c.fetchone()["n"]
        c.execute("SELECT COUNT(*) as n FROM journal WHERE resultat='loss'"); stats["losses"] = c.fetchone()["n"]
        c.execute("SELECT COUNT(*) as n FROM journal WHERE resultat='be'"); stats["be"] = c.fetchone()["n"]
        c.execute("SELECT SUM(pnl) as s FROM journal WHERE pnl IS NOT NULL"); stats["total_pnl"] = round(c.fetchone()["s"] or 0, 2)
        c.execute("SELECT COUNT(*) as n FROM journal WHERE resultat IS NOT NULL AND resultat!=''")
        done = c.fetchone()["n"]
        stats["winrate"] = round(stats["wins"]/done*100) if done > 0 else 0
        for ctx in ["trend","range","manipulation"]:
            c.execute("SELECT COUNT(*) as n FROM journal WHERE contexte_marche=%s", (ctx,)); total_ctx = c.fetchone()["n"]
            c.execute("SELECT COUNT(*) as n FROM journal WHERE contexte_marche=%s AND resultat='win'", (ctx,)); wins_ctx = c.fetchone()["n"]
            stats[f"ctx_{ctx}"] = {"total":total_ctx,"wins":wins_ctx,"winrate":round(wins_ctx/total_ctx*100) if total_ctx>0 else 0}
        for diff in ["easy","medium","hard"]:
            c.execute("SELECT COUNT(*) as n FROM journal WHERE difficulte=%s", (diff,)); total_d = c.fetchone()["n"]
            c.execute("SELECT COUNT(*) as n FROM journal WHERE difficulte=%s AND resultat='win'", (diff,)); wins_d = c.fetchone()["n"]
            stats[f"diff_{diff}"] = {"total":total_d,"wins":wins_d,"winrate":round(wins_d/total_d*100) if total_d>0 else 0}
        conn.close()
        return jsonify(stats)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ── JOURNAL CONTEXT POUR LES AGENTS ──────────────────────────
@app.route("/journal/context", methods=["GET"])
def get_journal_context():
    try:
        conn = get_db()
        c = conn.cursor()

        # FILTRE STRICT : uniquement les trades réellement pris
        # Exclure : décision ATTENDRE auto, trades sans résultat, opportunités manquées
        FILTRE = """
            resultat IN ('win','loss','be')
            AND (commentaire NOT LIKE '%%opportunite_manquee%%' OR commentaire IS NULL)
            AND (commentaire NOT LIKE '%%TRADE NON PRIS%%' OR commentaire IS NULL)
        """

        c.execute(f"SELECT COUNT(*) as n FROM journal WHERE {FILTRE}")
        total_done = c.fetchone()["n"]
        if total_done == 0:
            conn.close()
            return jsonify({"context": "", "has_data": False})

        c.execute(f"SELECT COUNT(*) as n FROM journal WHERE {FILTRE} AND resultat='win'")
        wins = c.fetchone()["n"]
        c.execute(f"SELECT COUNT(*) as n FROM journal WHERE {FILTRE} AND resultat='loss'")
        losses = c.fetchone()["n"]
        winrate = round(wins/total_done*100) if total_done > 0 else 0

        c.execute(f"""SELECT pair, COUNT(*) as total,
            SUM(CASE WHEN resultat='win' THEN 1 ELSE 0 END) as wins
            FROM journal WHERE {FILTRE} AND pair IS NOT NULL
            GROUP BY pair ORDER BY total DESC LIMIT 8""")
        pairs_stats = c.fetchall()

        c.execute(f"""SELECT contexte_marche, COUNT(*) as total,
            SUM(CASE WHEN resultat='win' THEN 1 ELSE 0 END) as wins
            FROM journal WHERE {FILTRE} AND contexte_marche IS NOT NULL
            GROUP BY contexte_marche""")
        marche_stats = c.fetchall()

        c.execute(f"""SELECT session, COUNT(*) as total,
            SUM(CASE WHEN resultat='win' THEN 1 ELSE 0 END) as wins
            FROM journal WHERE {FILTRE} AND session IS NOT NULL
            GROUP BY session ORDER BY total DESC""")
        session_stats = c.fetchall()

        ctx = f"HISTORIQUE TRADES REELS ({total_done} trades pris) :\n"
        ctx += f"- Winrate global : {winrate}% ({wins} wins / {losses} pertes)\n"

        if pairs_stats:
            ctx += "- Performance par paire :\n"
            for p in pairs_stats:
                wr = round(p['wins']/p['total']*100) if p['total'] > 0 else 0
                adj = "" if p['total'] >= 5 else " (N<5 — donnee insuffisante)"
                ctx += f"  {p['pair']} : {wr}% sur {p['total']} trades{adj}\n"

        if marche_stats:
            ctx += "- Performance par marche :\n"
            for m in marche_stats:
                wr = round(m['wins']/m['total']*100) if m['total'] > 0 else 0
                ctx += f"  {m['contexte_marche'].upper()} : {wr}% sur {m['total']} trades\n"

        if session_stats:
            ctx += "- Performance par session :\n"
            for s in session_stats:
                wr = round(s['wins']/s['total']*100) if s['total'] > 0 else 0
                adj = "" if s['total'] >= 5 else " (N<5 — donnee insuffisante)"
                ctx += f"  {s['session']} : {wr}% sur {s['total']} trades{adj}\n"

        ctx += "\nINSTRUCTION : "
        ctx += "Ajuster le score UNIQUEMENT si N>=5 trades sur ce contexte. "
        ctx += "N<5 = donnee insuffisante = ne pas ajuster. "
        ctx += "Winrate < 40% et N>=5 = reduire score -2pts. "
        ctx += "Winrate > 65% et N>=5 = bonus +2pts. "

        # Stats qualité
        c.execute(f"""SELECT
            SUM(CASE WHEN direction_ok='oui' THEN 1 ELSE 0 END) as dir_ok,
            SUM(CASE WHEN sortie_ok='be_force' THEN 1 ELSE 0 END) as be_force,
            SUM(CASE WHEN raison_sortie='ny_trap' THEN 1 ELSE 0 END) as ny_trap,
            SUM(CASE WHEN raison_sortie='sl_manuel' THEN 1 ELSE 0 END) as sl_manuel
            FROM journal WHERE {FILTRE}""")
        qualite = c.fetchone()
        if qualite and qualite['dir_ok']:
            ctx += f"\nQUALITE : Direction correcte={qualite['dir_ok']} fois. "
            if qualite['be_force']: ctx += f"BE force (bon trade)={qualite['be_force']} fois. "
            if qualite['ny_trap']: ctx += f"NY Trap={qualite['ny_trap']} fois. "
            ctx += "BE force = bon trade mal gere, ne pas penaliser la direction."

        # Ajouter les news macro si disponibles
        if redis_client:
            try:
                news = redis_client.get("macro_news")
                if news:
                    news_txt = news.decode('utf-8') if isinstance(news, bytes) else news
                    ctx += f"\n{news_txt}"
            except:
                pass

        conn.close()
        return jsonify({"context": ctx, "has_data": True, "total_trades": total_done})
    except Exception as e:
        print(f"Erreur journal/context: {e}")
        return jsonify({"context": "", "has_data": False})

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

def fetch_macro_news():
    """Récupère les news macro depuis Alpha Vantage"""
    try:
        url = f"https://www.alphavantage.co/query?function=NEWS_SENTIMENT&topics=forex,economy_macro,financial_markets&sort=LATEST&limit=10&apikey=UCP44WUC4UHAJ2I8"
        r = requests.get(url, timeout=10)
        data = r.json()
        feed = data.get("feed", [])
        if not feed:
            return ""
        summary = "NEWS MACRO DU JOUR:\n"
        for item in feed[:5]:
            title = item.get("title","")
            sentiment = item.get("overall_sentiment_label","neutral")
            summary += f"- {title} [{sentiment}]\n"
        if redis_client:
            redis_client.setex("macro_news", 43200, summary)
        print(f"News macro: {len(feed)} articles")
        return summary
    except Exception as e:
        print(f"Erreur news macro: {e}")
        return ""

def trigger_analysis(session):
    try:
        # Récupérer les news macro et les envoyer sur Telegram
        news = fetch_macro_news()
        msg = f"<b>Trading Master V5 — {session}</b>\nAnalyse automatique declenchee."
        if news:
            msg += f"\n\n{news}"
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": TELEGRAM_CHAT_ID,
                  "text": msg,
                  "parse_mode": "HTML"})
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
