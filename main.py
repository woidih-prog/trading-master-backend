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
import math

# ══════════════════════════════════════════════════════════════
# TRADING MASTER V5 — BACKEND v4
# Nouveautes :
#   1. GEL WEEK-END : /market-status + drapeau market_open sur les donnees
#   2. ETIQUETTE DE FRAICHEUR : received_ts + age_seconds + stale sur les donnees
#   3. BUG REPARE : redis_client -> r (les news macro et le contexte journal
#      etaient silencieusement casses par un NameError)
#   4. BUG REPARE : le bouton NON DECLENCHE etait inatteignable (avale par r_)
#   5. Scheduler aligne sur la discipline : 08h00 et 14h30 heure de Paris
#   6. v4 : CALCUL DE TAILLE DE LOT (/lot-size) — le risque 1% devient reel
#      + garde-fou distance minimale du stop par instrument
# ══════════════════════════════════════════════════════════════

app = Flask(__name__)
CORS(app, origins="*")
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ANTHROPIC_KEY    = os.environ.get("ANTHROPIC_KEY")
REDIS_URL        = os.environ.get("REDIS_URL", "redis://red-d8j855mq1p3s73ff62ig:6379")
DATABASE_URL     = os.environ.get("DATABASE_URL")

PARIS_TZ = pytz.timezone('Europe/Paris')

# Les cryptos vivent 24/7 (source Binance) — tout le reste suit les horaires forex
CRYPTO_SYMBOLS = {"BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "BNBUSD", "ADAUSD"}

# Au-dela de cet age (en secondes), une donnee est consideree perimee
# quand le marche est ouvert (10 minutes)
STALE_AFTER_SECONDS = 600

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

# ── GEL WEEK-END + FRAICHEUR (helpers) ───────────────────────
def is_crypto(symbol):
    return symbol.upper().replace("/", "") in CRYPTO_SYMBOLS

def forex_market_open(now=None):
    """Le forex ferme vendredi 23h (Paris) et rouvre dimanche 23h (Paris)."""
    now = now or datetime.now(PARIS_TZ)
    wd = now.weekday()  # lundi=0 ... dimanche=6
    if wd == 5:
        return False                      # samedi : ferme toute la journee
    if wd == 4 and now.hour >= 23:
        return False                      # vendredi apres 23h
    if wd == 6 and now.hour < 23:
        return False                      # dimanche avant 23h
    return True

def market_open_for(symbol):
    """Crypto = toujours ouvert. Le reste = horaires forex."""
    if is_crypto(symbol):
        return True
    return forex_market_open()

def stamp(data):
    """Ajoute l'heure de reception (etiquette de fraicheur) sur une donnee recue."""
    data["received_ts"] = time.time()
    data["received_at"] = datetime.now(PARIS_TZ).strftime("%Y-%m-%d %H:%M:%S")
    return data

def enrich(data, symbol):
    """Ajoute age, fraicheur et etat du marche sur une donnee renvoyee."""
    out = dict(data)
    opened = market_open_for(symbol)
    out["market_open"] = opened
    ts = out.get("received_ts")
    if ts:
        age = int(time.time() - ts)
        out["age_seconds"] = age
        # Une donnee n'est "perimee" que si le marche est ouvert
        # (le week-end, il est normal que les donnees soient figees)
        out["stale"] = bool(opened and age > STALE_AFTER_SECONDS)
    else:
        out["age_seconds"] = None
        out["stale"] = None
    if not opened:
        out["market_notice"] = "MARCHE FERME — donnees figees (week-end forex). Reouverture dimanche 23h Paris."
    return out

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
        for col in ['direction_ok','entree_ok','sortie_ok','raison_sortie','systeme_suivi']:
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
    forex = "OUVERT" if forex_market_open() else "FERME (week-end)"
    return f"Trading Master V5 Backend OK — Redis: {redis_status} — DB: PostgreSQL — Forex: {forex}"

# ── ETAT DU MARCHE (pour l'interface et l'Enqueteur) ─────────
@app.route("/market-status", methods=["GET"])
def market_status():
    now = datetime.now(PARIS_TZ)
    opened = forex_market_open(now)
    return jsonify({
        "forex_open": opened,
        "crypto_open": True,
        "now_paris": now.strftime("%Y-%m-%d %H:%M:%S"),
        "weekday": now.weekday(),
        "notice": None if opened else "Marche forex FERME — seules les cryptos sont analysables. Reouverture dimanche 23h Paris."
    })

# ── TAILLE DE LOT (securite v4) ───────────────────────────────
# Le systeme affichait "Risque 1%" mais laissait le trader mettre 1 lot fixe.
# Resultat : risque reel entre 0,3% et 3% selon l'instrument (cas Or : -893 EUR).
# Cette route calcule la taille exacte : risque EUR / (distance SL x taille contrat / taux EUR)

CONTRACT_SIZES = {
    "GOLD": 100, "SILVER": 5000,          # onces par lot (Admiral)
    "BRENT": 100, "CRUDOIL": 100,          # barils par lot
    "US100": 1, "[SP500]": 1, "[DJI30]": 1,
    "GERMANY40": 1, "[FTSE100]": 1         # indices : 1 point = ~1 unite (a verifier)
}
QUOTE_OVERRIDES = {
    "GOLD": "USD", "SILVER": "USD", "BRENT": "USD", "CRUDOIL": "USD",
    "US100": "USD", "[SP500]": "USD", "[DJI30]": "USD",
    "GERMANY40": "EUR", "[FTSE100]": "GBP"
}
FALLBACK_EUR = {"USD": 1.14, "JPY": 184.5, "CAD": 1.62, "CHF": 0.92, "GBP": 0.856, "EUR": 1.0}

# Distance minimale du stop par instrument (garde-fou anti "0,2 pip")
MIN_STOP_DIST = {
    "GOLD": 3.0, "SILVER": 0.30, "BRENT": 0.30, "CRUDOIL": 0.30,
    "US100": 15.0, "[SP500]": 8.0, "[DJI30]": 30.0, "GERMANY40": 15.0, "[FTSE100]": 10.0
}

def eur_rate(quote):
    """Combien de 'quote' pour 1 EUR — via les prix live MT4, sinon taux de secours."""
    if quote == "EUR":
        return 1.0
    d = redis_get(f"price:EUR{quote}") or mt4_prices_ram.get("EUR" + quote)
    if d and d.get("bid"):
        try:
            v = float(d["bid"])
            if v > 0:
                return v
        except Exception:
            pass
    return FALLBACK_EUR.get(quote, 1.0)

@app.route("/lot-size", methods=["GET"])
def lot_size():
    symbol = request.args.get("symbol", "").upper().replace("/", "")
    try:
        entry = float(request.args.get("entry"))
        sl = float(request.args.get("sl"))
    except Exception:
        return jsonify({"error": "Parametres entry et sl requis (nombres)"}), 400
    try:
        balance = float(request.args.get("balance", os.environ.get("ACCOUNT_BALANCE", "92000")))
        risk_pct = float(request.args.get("risk_pct", "1"))
    except Exception:
        balance, risk_pct = 92000.0, 1.0

    dist = abs(entry - sl)
    if dist <= 0:
        return jsonify({"error": "SL identique a l'entree"}), 400

    contract = CONTRACT_SIZES.get(symbol, 100000)  # forex standard : 100 000 unites
    quote = QUOTE_OVERRIDES.get(symbol, symbol[-3:] if len(symbol) >= 6 else "USD")
    rate = eur_rate(quote)

    risk_per_lot_quote = dist * contract          # perte au SL pour 1 lot, en devise de cotation
    risk_per_lot_eur = risk_per_lot_quote / rate if rate > 0 else risk_per_lot_quote
    target_risk_eur = balance * risk_pct / 100.0

    lots = target_risk_eur / risk_per_lot_eur if risk_per_lot_eur > 0 else 0
    lots = math.floor(lots * 100) / 100.0          # arrondi vers le bas, pas de sur-risque
    warning = None
    if lots < 0.01:
        lots = 0.01
        warning = "Stop tres large : meme 0.01 lot depasse le risque cible"
    if lots > 5:
        lots = 5.0
        warning = "Taille plafonnee a 5 lots par securite"
    if symbol in ("US100", "[SP500]", "[DJI30]", "GERMANY40", "[FTSE100]"):
        warning = "Indice : valeur du point a verifier chez Admiral — lot indicatif"

    # Garde-fou : distance minimale du stop
    is_jpy = symbol.endswith("JPY")
    min_stop = MIN_STOP_DIST.get(symbol, 0.15 if is_jpy else 0.0015)
    stop_ok = dist >= min_stop

    risk_real_eur = round(lots * risk_per_lot_eur, 2)
    return jsonify({
        "symbol": symbol,
        "lots": lots,
        "risk_eur": risk_real_eur,
        "risk_pct_reel": round(risk_real_eur / balance * 100, 2) if balance > 0 else None,
        "distance_sl": round(dist, 5),
        "stop_ok": stop_ok,
        "min_stop": min_stop,
        "stop_warning": None if stop_ok else f"STOP TROP SERRE : {round(dist,5)} < minimum {min_stop} pour {symbol} — niveaux a recalculer",
        "warning": warning,
        "balance_utilisee": balance
    })

@app.route("/market-status/<symbol>", methods=["GET"])
def market_status_symbol(symbol):
    key = symbol.upper().replace("/", "")
    opened = market_open_for(key)
    return jsonify({
        "symbol": key,
        "is_crypto": is_crypto(key),
        "market_open": opened,
        "now_paris": datetime.now(PARIS_TZ).strftime("%Y-%m-%d %H:%M:%S")
    })

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
            "inline_keyboard": [
                [
                    {"text": "✅ WIN",  "callback_data": f"r_{trade_id}_win"},
                    {"text": "❌ LOSS", "callback_data": f"r_{trade_id}_loss"},
                    {"text": "➖ BE",   "callback_data": f"r_{trade_id}_be"}
                ],
                [
                    {"text": "⏭️ NON DÉCLENCHÉ", "callback_data": f"r_{trade_id}_nondeclenche"}
                ]
            ]
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

    # ── RESULTAT (WIN / LOSS / BE / NON DECLENCHE) ───────────
    # FIX : le cas "nondeclenche" est traite ICI, dans la premiere branche r_
    # (avant, il etait dans un elif plus bas qui ne pouvait jamais etre atteint)
    elif callback_data.startswith("r_"):
        parts = callback_data.split("_")
        if len(parts) == 3:
            trade_id = parts[1]
            resultat = parts[2]

            if resultat == "nondeclenche":
                fb = pending_feedback.get(trade_id, {})
                fb["resultat"] = "nondeclenche"
                fb["step"] = "nondeclenche_raison"
                pending_feedback[trade_id] = fb
                answer_callback(callback_id, "Ordre non déclenché")
                edit_tg_markup(chat_id, message_id, {"inline_keyboard": [
                    [{"text": "📍 Point d'entrée trop loin", "callback_data": f"nd_{trade_id}_entree_loin"}],
                    [{"text": "📊 Spread trop large", "callback_data": f"nd_{trade_id}_spread"}],
                    [{"text": "🔄 Renversement avant déclenchement", "callback_data": f"nd_{trade_id}_renversement"}],
                    [{"text": "⏰ Expiré / Weekend", "callback_data": f"nd_{trade_id}_expire"}],
                    [{"text": "❓ Autre raison", "callback_data": f"nd_{trade_id}_autre"}],
                ]})
                send_tg(chat_id, "⏭️ <b>Ordre non déclenché</b>\nQuelle est la raison ?")
            else:
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
            fb["step"] = "systeme"
            pending_feedback[trade_id] = fb
            send_tg(chat_id,
                f"{label_d} noté.\n\n<b>🦊 Système suivi ?</b>\nEntrée au signal, SL/TP du plan, zéro main ?",
                {"inline_keyboard": [[
                    {"text": "✅ OUI — plan respecté", "callback_data": f"s_{trade_id}_oui"},
                    {"text": "❌ NON — hors système",  "callback_data": f"s_{trade_id}_non"}
                ]]})

    # ── SYSTEME SUIVI ? (discipline) ─────────────────────────
    elif callback_data.startswith("s_"):
        parts = callback_data.split("_")
        if len(parts) == 3:
            trade_id = parts[1]
            suivi    = parts[2]  # oui / non
            label_s = "✅ Plan respecté" if suivi == "oui" else "❌ Hors système"
            try:
                conn = get_db()
                c = conn.cursor()
                c.execute("UPDATE journal SET systeme_suivi=%s WHERE id=%s",
                    (suivi, int(trade_id)))
                conn.commit()
                conn.close()
            except Exception as e:
                print(f"Erreur systeme_suivi: {e}")
            fb = pending_feedback.get(trade_id, {})
            fb["systeme_suivi"] = suivi
            fb["step"] = "commentaire"
            pending_feedback[trade_id] = fb
            answer_callback(callback_id, label_s)
            edit_tg_markup(chat_id, message_id, {"inline_keyboard": []})
            send_tg(chat_id,
                f"{label_s} noté.\n\n<b>💬 Commentaire sur ce trade ?</b>\nTape ton analyse, ce que tu as vu, pourquoi tu as pris ou raté ce trade.\n\nOu appuie sur Passer pour terminer.",
                {"inline_keyboard": [[{"text": "⏭️ Passer", "callback_data": f"skip_comment_{trade_id}"}]]})

    # ── RAISON NON DÉCLENCHÉ ─────────────────────────────────────
    elif callback_data.startswith("nd_"):
        parts = callback_data.split("_")
        trade_id = parts[1]
        raison_code = "_".join(parts[2:])
        raisons = {
            "entree_loin": "Point d'entrée trop loin du prix",
            "spread": "Spread trop large — prix entre BID/ASK",
            "renversement": "Renversement avant déclenchement",
            "expire": "Ordre expiré / Weekend",
            "autre": "Autre raison"
        }
        raison_txt = raisons.get(raison_code, raison_code)
        fb = pending_feedback.get(trade_id, {})
        fb["raison_sortie"] = raison_txt
        fb["step"] = "nondeclenche_direction"
        pending_feedback[trade_id] = fb
        answer_callback(callback_id, "Raison notée")
        edit_tg_markup(chat_id, message_id, {"inline_keyboard": [
            [{"text": "✅ Direction correcte", "callback_data": f"ndd_{trade_id}_oui"}],
            [{"text": "❌ Direction incorrecte", "callback_data": f"ndd_{trade_id}_non"}],
        ]})
        send_tg(chat_id, f"Raison : <i>{raison_txt}</i>\n\nLa direction du système était-elle correcte ?")

    # ── DIRECTION NON DÉCLENCHÉ ───────────────────────────────────
    elif callback_data.startswith("ndd_"):
        parts = callback_data.split("_")
        trade_id = parts[1]
        direction_ok = parts[2]
        fb = pending_feedback.get(trade_id, {})
        raison_txt = fb.get("raison_sortie", "")
        dir_label = "✅ correcte" if direction_ok == "oui" else "❌ incorrecte"
        try:
            conn = get_db()
            c = conn.cursor()
            c.execute("""UPDATE journal SET resultat='nondeclenche',
                direction_ok=%s, raison_sortie=%s,
                commentaire=%s WHERE id=%s""",
                (direction_ok, raison_txt,
                 f"Ordre non déclenché — {raison_txt}", int(trade_id)))
            conn.commit()
            conn.close()
        except Exception as e:
            print(f"Erreur nondeclenche: {e}")
        answer_callback(callback_id, "Journalisé !")
        edit_tg_markup(chat_id, message_id, {"inline_keyboard": []})
        send_tg(chat_id,
            f"<b>⏭️ Trade #{trade_id} — NON DÉCLENCHÉ</b>\n"
            f"Raison : <i>{raison_txt}</i>\n"
            f"Direction système : {dir_label}\n\n"
            f"💬 Ajoute un commentaire ou appuie sur Passer.",
            {"inline_keyboard": [[{"text": "⏭️ Passer", "callback_data": f"skip_comment_{trade_id}"}]]})
        fb["step"] = "commentaire"
        pending_feedback[trade_id] = fb

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
    data = stamp(data)
    if not redis_set(f"price:{symbol}", data): mt4_prices_ram[symbol] = data
    return jsonify({"success": True})

@app.route("/price/<symbol>", methods=["GET"])
def get_price(symbol):
    key = symbol.upper().replace("/","")
    data = redis_get(f"price:{key}") or mt4_prices_ram.get(key)
    if data: return jsonify(enrich(data, key))
    return jsonify({"error": "Prix non disponible", "market_open": market_open_for(key)}), 404

# ── BOUGIES H1 ────────────────────────────────────────────────
@app.route("/candles", methods=["POST"])
def receive_candles():
    data = request.get_json(force=True, silent=True)
    if not data: return jsonify({"error": "JSON invalide"}), 400
    symbol = data.get("symbol","").upper().replace("/","")
    data = stamp(data)
    if not redis_set(f"candles:{symbol}", data): mt4_candles_ram[symbol] = data
    print(f"Bougies H1: {symbol} — {len(data.get('candles',[]))} bougies")
    return jsonify({"success": True})

@app.route("/candles/<symbol>", methods=["GET"])
def get_candles(symbol):
    key = symbol.upper().replace("/","")
    data = redis_get(f"candles:{key}") or mt4_candles_ram.get(key)
    if data: return jsonify(enrich(data, key))
    return jsonify({"error": "Bougies non disponibles", "market_open": market_open_for(key)}), 404

# ── BOUGIES M15 ───────────────────────────────────────────────
@app.route("/m15", methods=["POST"])
def receive_m15():
    raw = request.get_data(as_text=True)
    data = request.get_json(force=True, silent=True)
    if not data:
        print(f"M15 JSON echec: {raw[:200]}")
        return jsonify({"error": "JSON invalide"}), 400
    symbol = data.get("symbol","").upper().replace("/","")
    data = stamp(data)
    if not redis_set(f"m15:{symbol}", data): mt4_m15_ram[symbol] = data
    print(f"Bougies M15: {symbol} — {len(data.get('candles',[]))} bougies")
    return jsonify({"success": True})

@app.route("/m15/<symbol>", methods=["GET"])
def get_m15(symbol):
    key = symbol.upper().replace("/","")
    data = redis_get(f"m15:{key}") or mt4_m15_ram.get(key)
    if data: return jsonify(enrich(data, key))
    return jsonify({"error": "Bougies M15 non disponibles", "market_open": market_open_for(key)}), 404

# ── BOUGIES DAILY ─────────────────────────────────────────────
@app.route("/daily", methods=["POST"])
def receive_daily():
    data = request.get_json(force=True, silent=True)
    if not data: return jsonify({"error": "JSON invalide"}), 400
    symbol = data.get("symbol","").upper().replace("/","")
    data = stamp(data)
    if not redis_set(f"daily:{symbol}", data): mt4_daily_ram[symbol] = data
    print(f"Daily: {symbol} — {len(data.get('candles',[]))} bougies")
    return jsonify({"success": True})

@app.route("/daily/<symbol>", methods=["GET"])
def get_daily(symbol):
    key = symbol.upper().replace("/","")
    data = redis_get(f"daily:{key}") or mt4_daily_ram.get(key)
    if data: return jsonify(enrich(data, key))
    return jsonify({"error": "Daily non disponible", "market_open": market_open_for(key)}), 404

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
        c.execute("SELECT COUNT(*) as n, COALESCE(SUM(pnl),0) as p FROM journal WHERE systeme_suivi='non' AND resultat IN ('win','loss','be')")
        row = c.fetchone()
        stats["hors_systeme"] = {"total": row["n"], "pnl": round(row["p"], 2)}
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
        # L'agent Memoire n'apprend QUE des trades ou le plan a ete respecte
        # (systeme_suivi='oui'). Les anciens trades sans etiquette (NULL) restent
        # comptes pour ne pas effacer l'historique. Les trades 'non' (hors systeme)
        # sont journalises mais jugent la discipline du trader, pas le systeme.
        FILTRE = """
            resultat IN ('win','loss','be')
            AND (systeme_suivi = 'oui' OR systeme_suivi IS NULL)
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

        # Stats ordres non déclenchés (apprentissage direction)
        nd_correct = 0
        c.execute("SELECT COUNT(*) as n FROM journal WHERE resultat='nondeclenche'")
        total_nd = c.fetchone()["n"]
        if total_nd > 0:
            c.execute("""SELECT COUNT(*) as n FROM journal
                WHERE resultat='nondeclenche' AND direction_ok='oui'""")
            nd_correct = c.fetchone()["n"]
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

        # Ajouter stats ordres non déclenchés
        if total_nd > 0:
            pct_nd = round(nd_correct/total_nd*100)
            ctx += f"\nORDRES NON DÉCLENCHÉS ({total_nd} ordres) : "
            ctx += f"Direction correcte {pct_nd}% des fois. "
            ctx += "Ces ordres n'ont pas été exécutés mais la direction système était correcte dans la majorité des cas. "

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

        # ── DISCIPLINE : trades hors systeme (non comptes ci-dessus) ──
        c.execute("""SELECT COUNT(*) as n, COALESCE(SUM(pnl),0) as p
            FROM journal WHERE systeme_suivi='non' AND resultat IN ('win','loss','be')""")
        indis = c.fetchone()
        if indis and indis['n'] > 0:
            ctx += f"\nDISCIPLINE : {indis['n']} trades HORS SYSTEME exclus des stats "
            ctx += f"ci-dessus (PnL cumule : {round(indis['p'],2)} EUR). "
            ctx += "Ces trades jugent le trader, pas le systeme — ne pas en tenir compte dans le score."

        # ── ETAT DU MARCHE (gel week-end pour les agents) ─────
        if not forex_market_open():
            ctx += "\n\nMARCHE FERME : Nous sommes le week-end. Le forex, les metaux, "
            ctx += "les indices et les petroles sont FERMES — leurs donnees sont figees "
            ctx += "depuis vendredi 23h Paris. AUCUN signal ne doit etre emis sur ces "
            ctx += "instruments. Seules les cryptos (donnees Binance) sont analysables. "
            ctx += "Reouverture dimanche 23h Paris."

        # Ajouter les news macro si disponibles
        # FIX : c'etait "redis_client" (variable inexistante) -> NameError silencieux
        # qui vidait tout le contexte journal. Corrige en "r".
        if r:
            try:
                news = r.get("macro_news")
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
    # Aligne sur la discipline de trading :
    # 08h00 Paris = London Open · 14h30 Paris = NY Open · jamais le week-end
    analyzed_today = {'london': None, 'ny': None}
    while True:
        try:
            now = datetime.now(PARIS_TZ)
            h, m, day = now.hour, now.minute, now.weekday()
            today = now.strftime('%Y-%m-%d')
            if day < 5:
                if h == 8 and m == 0 and analyzed_today['london'] != today:
                    analyzed_today['london'] = today
                    trigger_analysis('London Open')
                if h == 14 and m == 30 and analyzed_today['ny'] != today:
                    analyzed_today['ny'] = today
                    trigger_analysis('NY Open')
        except Exception as e:
            print(f"Scheduler error: {e}")
        time.sleep(30)

def fetch_macro_news():
    """Récupère les news macro depuis Alpha Vantage"""
    try:
        url = f"https://www.alphavantage.co/query?function=NEWS_SENTIMENT&topics=forex,economy_macro,financial_markets&sort=LATEST&limit=10&apikey=UCP44WUC4UHAJ2I8"
        resp = requests.get(url, timeout=10)
        data = resp.json()
        feed = data.get("feed", [])
        if not feed:
            return ""
        summary = "NEWS MACRO DU JOUR:\n"
        for item in feed[:5]:
            title = item.get("title","")
            sentiment = item.get("overall_sentiment_label","neutral")
            summary += f"- {title} [{sentiment}]\n"
        # FIX : c'etait "redis_client" (variable inexistante) — les news
        # n'etaient jamais sauvegardees. Corrige en "r".
        if r:
            try:
                r.setex("macro_news", 43200, summary)
            except: pass
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
