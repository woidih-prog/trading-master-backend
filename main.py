from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import os
import psycopg2
from psycopg2.extras import RealDictCursor
from datetime import datetime, timezone
import pytz
import threading
import time
import redis
import json
import math

# ══════════════════════════════════════════════════════════════
# TRADING MASTER V5 — BACKEND v6
# Nouveautes :
#   1. GEL WEEK-END : /market-status + drapeau market_open sur les donnees
#   2. ETIQUETTE DE FRAICHEUR : received_ts + age_seconds + stale sur les donnees
#   3. BUG REPARE : redis_client -> r (les news macro et le contexte journal
#      etaient silencieusement casses par un NameError)
#   4. BUG REPARE : le bouton NON DECLENCHE etait inatteignable (avale par r_)
#   5. Scheduler aligne sur la discipline : 08h00 et 14h30 heure de Paris
#   6. v4 : CALCUL DE TAILLE DE LOT (/lot-size) — le risque 1% devient reel
#      + garde-fou distance minimale du stop par instrument
#   7. v5 : tailles de contrat CRYPTO ajoutees (le BTC etait calcule comme
#      du forex 100 000 unites -> chiffres absurdes). BTC/ETH = 1 unite/lot.
# ══════════════════════════════════════════════════════════════

app = Flask(__name__)
CORS(app, origins="*")
app.config['MAX_CONTENT_LENGTH'] = 50 * 1024 * 1024

TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
ANTHROPIC_KEY    = os.environ.get("ANTHROPIC_KEY")
REDIS_URL        = os.environ.get("REDIS_URL", "redis://red-d8j855mq1p3s73ff62ig:6379")
DATABASE_URL     = os.environ.get("DATABASE_URL")

# ══════════════════════════════════════════════════════════════
# PHASE 1 MULTI-UTILISATEURS (16/08/2026) — L'ETIQUETTE DE PROPRIETAIRE
#
# Jusqu'ici RENARD ne savait pas QUI. Il n'y avait pas "ton journal",
# il y avait "LE journal" : une seule table commune, sans proprietaire.
# Aucun ecran ne pouvait afficher "mes trades" plutot que "les trades".
#
# Cette phase pose UNE colonne compte_id sur les trois tables, et
# attribue toutes les lignes existantes au proprietaire.
#
# TOUT EST ADDITIF. Si compte_id est absent d'une requete, le
# comportement est EXACTEMENT celui d'avant. Aucune regle de trading
# touchee, aucun changement dans ce que lisent les 8 agents.
# ══════════════════════════════════════════════════════════════
COMPTE_PROPRIETAIRE = os.environ.get("COMPTE_PROPRIETAIRE", "22631676")
PROPRIETAIRE_PRENOM = os.environ.get("PROPRIETAIRE_PRENOM", "Woidih")
PROPRIETAIRE_NOM    = os.environ.get("PROPRIETAIRE_NOM", "TARKHANI")

PARIS_TZ = pytz.timezone('Europe/Paris')

# Les cryptos vivent 24/7 (source Binance) — tout le reste suit les horaires forex
CRYPTO_SYMBOLS = {"BTCUSD", "ETHUSD", "SOLUSD", "XRPUSD", "BNBUSD", "ADAUSD"}

# Au-dela de cet age (en secondes), une donnee est consideree perimee
# quand le marche est ouvert (10 minutes)
STALE_AFTER_SECONDS = 600

# ── REDIS ─────────────────────────────────────────────────────
def _connect_redis():
    """Tente une connexion Redis. Renvoie l'objet connecte ou None."""
    try:
        conn = redis.from_url(REDIS_URL, decode_responses=True,
                              socket_connect_timeout=3, socket_timeout=3)
        conn.ping()
        print("Redis connecte OK")
        return conn
    except Exception as e:
        print(f"Redis erreur connexion: {e}")
        return None

r = _connect_redis()
_last_redis_retry = 0.0

def _get_redis():
    """Renvoie la connexion Redis, en RETENTANT la connexion toutes les 30s
    si elle est tombee. FIX : avant, une connexion ratee au demarrage rendait
    le serveur definitivement sans Redis (panne invisible, 404 partout)."""
    global r, _last_redis_retry
    if r is not None:
        return r
    now = time.time()
    if now - _last_redis_retry >= 30:
        _last_redis_retry = now
        r = _connect_redis()
    return r

mt4_prices_ram  = {}
mt4_candles_ram = {}
mt4_m15_ram     = {}
mt4_daily_ram   = {}
mt4_screenshots_ram = {}
pending_feedback = {}

# Duree de vie des donnees DAILY : 72h.
# Le Daily est envoye rarement par l'EA et rien n'arrive du vendredi soir au
# dimanche soir. Avec 24h, la cle expirait le samedi -> "Daily indisponible"
# tous les lundis, et les 8 agents perdaient la tendance de fond.
DAILY_TTL = 259200  # 72 heures

def redis_set(key, data, ttl=86400):
    """ttl par defaut 24h. FIX : les donnees DAILY doivent survivre au week-end
    (l'EA n'envoie rien du vendredi soir au dimanche soir). Sans TTL long, les
    cles daily expiraient le samedi -> 'Daily indisponible' tous les lundis."""
    global r
    conn = _get_redis()
    if conn:
        try:
            conn.set(key, json.dumps(data), ex=ttl)
            return True
        except Exception as e:
            print(f"Redis SET erreur ({key}): {e}")
            r = None  # marquer la connexion comme tombee -> reconnexion auto
    return False

def redis_get(key):
    global r
    conn = _get_redis()
    if conn:
        try:
            val = conn.get(key)
            if val: return json.loads(val)
        except Exception as e:
            print(f"Redis GET erreur ({key}): {e}")
            r = None  # marquer la connexion comme tombee -> reconnexion auto
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

def init_identite():
    """Cree les tables d'identite, inscrit le proprietaire, pose la colonne
    compte_id sur les trois tables et attribue l'existant.
    Tourne a chaque demarrage : tout est idempotent (IF NOT EXISTS,
    ON CONFLICT DO NOTHING, WHERE compte_id IS NULL)."""
    try:
        conn = get_db(); c = conn.cursor()

        # -- Les personnes ------------------------------------------
        c.execute("""CREATE TABLE IF NOT EXISTS utilisateurs (
            id               SERIAL PRIMARY KEY,
            email            TEXT UNIQUE,
            mot_de_passe     TEXT,
            prenom           TEXT,
            nom              TEXT,
            role             TEXT DEFAULT 'client',
            telegram_chat_id TEXT,
            actif            BOOLEAN DEFAULT true,
            cree_le          TIMESTAMP DEFAULT NOW()
        )""")
        conn.commit()

        # -- Les comptes de trading ---------------------------------
        c.execute("""CREATE TABLE IF NOT EXISTS comptes (
            compte_id        TEXT PRIMARY KEY,
            utilisateur_id   INTEGER REFERENCES utilisateurs(id),
            courtier         TEXT,
            type             TEXT,
            devise           TEXT DEFAULT 'EUR',
            balance_initiale REAL,
            actif            BOOLEAN DEFAULT true,
            note             TEXT,
            cree_le          TIMESTAMP DEFAULT NOW()
        )""")
        conn.commit()

        # -- Le proprietaire ----------------------------------------
        # mot_de_passe reste NULL : aucune authentification en phase 1.
        # Elle viendra en phase 3, avec un hachage (bcrypt/argon2).
        c.execute("""INSERT INTO utilisateurs (id, prenom, nom, role, telegram_chat_id)
                     VALUES (1, %s, %s, 'proprietaire', %s)
                     ON CONFLICT (id) DO UPDATE
                     SET prenom = EXCLUDED.prenom,
                         nom    = EXCLUDED.nom,
                         role   = 'proprietaire'""",
                  (PROPRIETAIRE_PRENOM, PROPRIETAIRE_NOM, TELEGRAM_CHAT_ID))
        conn.commit()

        c.execute("""INSERT INTO comptes
                     (compte_id, utilisateur_id, courtier, type, devise, balance_initiale, note)
                     VALUES (%s, 1, 'Admirals Group AS', 'DEMO', 'EUR', 92000,
                             'Compte du Carnet C - VPS Londres')
                     ON CONFLICT (compte_id) DO NOTHING""",
                  (COMPTE_PROPRIETAIRE,))
        conn.commit()

        # -- La colonne, sur les trois tables -----------------------
        # Un commit par table : sur PostgreSQL, une seule instruction en
        # erreur met TOUTE la transaction en echec (meme piege que les
        # colonnes du journal, corrige plus haut).
        for table in ("journal", "surveillance", "trade_peaks"):
            try:
                c.execute("ALTER TABLE %s ADD COLUMN IF NOT EXISTS compte_id TEXT" % table)
                conn.commit()
                c.execute("UPDATE %s SET compte_id = %%s WHERE compte_id IS NULL" % table,
                          (COMPTE_PROPRIETAIRE,))
                touchees = c.rowcount
                conn.commit()
                c.execute("CREATE INDEX IF NOT EXISTS idx_%s_compte ON %s (compte_id)" % (table, table))
                conn.commit()
                if touchees:
                    print("Identite : %d ligne(s) de %s attribuees a %s"
                          % (touchees, table, COMPTE_PROPRIETAIRE))
            except Exception as e:
                conn.rollback()
                print("Identite table %s : %s" % (table, e))

        conn.close()
        print("Identite OK — proprietaire %s %s, compte %s"
              % (PROPRIETAIRE_PRENOM, PROPRIETAIRE_NOM, COMPTE_PROPRIETAIRE))
    except Exception as e:
        print("Identite erreur: %s" % e)

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
        conn.commit()
        # Ajouter colonnes si elles n'existent pas (migration).
        # FIX : sur PostgreSQL, une seule ALTER qui echoue (colonne deja existante)
        # met TOUTE la transaction en erreur et bloque les colonnes suivantes.
        # Solution : "ADD COLUMN IF NOT EXISTS" + un commit par colonne, avec
        # rollback en cas d'echec pour repartir sur une transaction propre.
        # v7 MEMOIRE : rapports_agents conserve le RAISONNEMENT des 8 agents
        # + orchestrateur. Sans lui, chaque analyse repartait de zero.
        # 16/08 : rr_reel_feu et voie. Le RR reel etait calcule, affiche
        # dans Telegram... et perdu. La voie (avec ou sans Enqueteur) n'etait
        # nulle part. Ce sont les DEUX questions les plus utiles a compter.
        for col in ['rr_reel_feu','rr_reel_valeur','rr_reel_mur','voie',
                    'direction_ok','entree_ok','sortie_ok','raison_sortie','systeme_suivi',
                    'rapports_agents','market_state','regime_ratio','rsi_value','rsi_pente',
                    'trap','cisd','msu','consensus_long','consensus_short','gate_blocked','lecon']:
            try:
                c.execute(f"ALTER TABLE journal ADD COLUMN IF NOT EXISTS {col} TEXT")
                conn.commit()
                print(f"Colonne verifiee/ajoutee : {col}")
            except Exception as e:
                conn.rollback()
                print(f"Colonne {col} : {e}")
        conn.close()
        print("PostgreSQL connecte OK")
    except Exception as e:
        print(f"PostgreSQL erreur: {e}")

init_db()
# NOTE : init_identite() n'est PAS appelee ici. Elle doit tourner APRES
# la creation de TOUTES les tables. Au premier demarrage de cette version,
# la table trade_peaks n'existe pas encore a cet endroit du fichier
# (_init_table_peaks() est plus bas) : l'ALTER TABLE echouait en silence
# et la colonne compte_id n'etait jamais posee. L'appel est deplace tout
# en bas du fichier, apres _init_table_surveillance().

# ── GENERATEUR DE LECON (v7) ──────────────────────────────────
# Transforme chaque trade en enseignement exploitable, base sur les CAUSES
# (TRAP, CISD, regime, pente RSI) et JAMAIS sur la paire. Une lecon du type
# "EUR/USD perd, j'evite EUR/USD" serait une conclusion, pas une cause.
def lire_montant(txt):
    """Transforme '-590,60 EUR' ou '+176.50' en nombre. None si illisible.
    v9 : le montant n'etait JAMAIS enregistre (colonne pnl vide sur tous les
    trades). Sans lui, la memoire ne distingue pas une perte de 20 EUR d'une
    perte de 590 EUR : elles pesent pareil."""
    s = (txt or "").strip()
    for c in ("\u20ac", "EUR", "eur", "euros", "euro", " ", "+"):
        s = s.replace(c, "")
    s = s.replace(",", ".")
    try:
        v = float(s)
    except Exception:
        return None
    if v != v or abs(v) > 1000000:   # NaN ou montant aberrant
        return None
    return v

def generer_lecon(t):
    res  = (t.get("resultat") or "").lower()
    trap = str(t.get("trap") or "").lower() in ("true","1","oui")
    cisd = str(t.get("cisd") or "").lower() in ("true","1","oui")
    msu  = str(t.get("msu") or "").lower() in ("true","1","oui")
    ms   = (t.get("market_state") or t.get("contexte_marche") or "").upper()
    try:    pente = float(t.get("rsi_pente")) if t.get("rsi_pente") not in (None,"","None") else None
    except Exception: pente = None
    biais  = (t.get("bias") or "").lower()
    sortie = (t.get("sortie_ok") or "")

    causes = []
    if not trap: causes.append("TRAP non confirme")
    if not cisd: causes.append("CISD absent")
    if not msu:  causes.append("MSU absent")
    if ms in ("RANGE","MANIPULATION"): causes.append("marche en " + ms)
    if pente is not None and abs(pente) >= 3:
        sens_p = "haussiere" if pente > 0 else "baissiere"
        contre = (pente > 0 and biais == "short") or (pente < 0 and biais == "long")
        if contre:
            causes.append("pente RSI %s de %+.1f contraire au sens du trade" % (sens_p, pente))

    presents = []
    if trap: presents.append("TRAP confirme")
    if cisd: presents.append("CISD present")
    if msu:  presents.append("MSU detecte")

    if res == "win":
        base = " + ".join(presents) if presents else "contexte favorable"
        l = "WIN — " + base + " : schema a reconnaitre."
        if causes:
            l += " A noter : " + ", ".join(causes) + " n'ont pas empeche le gain — ces facteurs seuls ne suffisent pas a invalider un setup."
        return l
    if res == "loss":
        if causes:
            return "LOSS — declencheurs manquants a l'entree : " + ", ".join(causes) + ". Verifier leur presence AVANT de valider un setup semblable."
        return "LOSS — tous les declencheurs etaient presents. L'echec vient de l'execution ou de la gestion, pas de la lecture du marche."
    if res == "be":
        if "be_force" in sortie:
            return "BE — direction correcte, sortie anticipee. Le probleme est la gestion, pas la lecture."
        return "BE — lecture correcte mais mouvement insuffisant. Verifier si le TP etait atteignable dans ce contexte."
    return ""

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

# ── PAGE DE CONTROLE : etat de l'entrepot en un coup d'oeil ──
@app.route("/debug", methods=["GET"])
def debug_status():
    """Ouvre cette page dans le navigateur pour voir l'etat du systeme :
    Redis connecte ? Combien de bougies stockees ? Quelle fraicheur ?"""
    out = {"heure_paris": datetime.now(PARIS_TZ).strftime("%Y-%m-%d %H:%M:%S")}
    conn = _get_redis()
    out["redis_connecte"] = bool(conn)
    inventaire = {}
    try:
        if conn:
            for prefix in ["price", "candles", "m15", "daily"]:
                keys = conn.keys(f"{prefix}:*")
                detail = {}
                for k in sorted(keys):
                    try:
                        d = json.loads(conn.get(k) or "{}")
                        ts = d.get("received_ts")
                        detail[k.split(":",1)[1]] = (str(int(time.time()-ts))+"s" if ts else "?")
                    except Exception:
                        detail[k.split(":",1)[1]] = "illisible"
                inventaire[prefix] = {"total": len(keys), "age_par_symbole": detail}
        else:
            for name, ram in [("price", mt4_prices_ram), ("candles", mt4_candles_ram),
                              ("m15", mt4_m15_ram), ("daily", mt4_daily_ram)]:
                inventaire[name] = {"total": len(ram), "source": "RAM (Redis tombe)"}
    except Exception as e:
        out["erreur_inventaire"] = str(e)
    out["inventaire"] = inventaire
    out["aide"] = "age_par_symbole = anciennete du dernier envoi recu. Normal: <120s marche ouvert. Si ca grossit sans fin, le robot n'envoie plus."
    return jsonify(out)

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

# ══════════════════════════════════════════════════════════════
# SOMMET DES TRADES (mesure trailing) — v9 REPARE le 12/08/2026
#
# CE QUI ETAIT CASSE :
#   1. DOUBLONS. L'EA envoie plusieurs fois le meme ticket (retry, boucle de
#      fermeture). Chaque envoi etait AJOUTE a la liste. Resultat constate :
#      20 lignes pour 5 trades reels.
#   2. ECRASEMENT PAR DES ZEROS. Le dernier envoi arrive souvent avec
#      peak_r = 0 (position deja fermee, plus rien a mesurer). Ces zeros
#      etaient comptes comme de vrais sommets.
#   3. SYNTHESE FAUSSEE. Consequence des deux precedents : le 12/08 la
#      synthese annoncait "15% ont atteint 1R / 15% ont atteint 1.5R /
#      15% ont atteint 2R" — trois fois le meme chiffre, parce que c'etait
#      simplement 3 doublons d'un seul trade sur 20 lignes. Le sommet moyen
#      affiche (0.71R) etait ecrase par les zeros. Toute la synthese etait
#      inutilisable, et c'est precisement la mesure qui doit servir a regler
#      le trailing.
#   4. STOCKAGE DANS /tmp. Efface a chaque redeploiement Render. Meme erreur
#      que la surveillance avant sa correction du 10/08.
#
# CE QUI CHANGE :
#   - UN SEUL enregistrement par ticket (cle primaire en base).
#   - Le sommet conserve est le MAXIMUM jamais recu : un envoi a 0 ne peut
#     plus effacer un sommet a 2.93.
#   - Stockage en PostgreSQL, avec repli sur la memoire vive si la base
#     est indisponible.
#   - La synthese compare le sommet des GAGNANTS et celui des PERDANTS :
#     c'est cette comparaison qui dira si le trailing coupe trop tot.
#
# AVERTISSEMENT SUR L'UNITE : l'EA calcule le R sur la distance REELLE du
# stop au moment de l'ouverture, pas sur le risque prevu par le signal.
# Quand l'entree reelle differe de l'entree planifiee, les deux ne sont pas
# la meme chose. Ne pas melanger ces R avec ceux calcules a la main depuis
# les euros. A trancher plus tard.
# ══════════════════════════════════════════════════════════════
TRADE_PEAKS = {}   # repli memoire : { ticket -> enregistrement }

def _init_table_peaks():
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS trade_peaks (
            ticket TEXT PRIMARY KEY,
            symbol TEXT,
            peak_r DOUBLE PRECISION,
            profit_final DOUBLE PRECISION,
            nb_envois INTEGER DEFAULT 1,
            premier_ts TEXT,
            dernier_ts TEXT
        )""")
        conn.commit(); conn.close()
        print("Table trade_peaks OK")
        return True
    except Exception as e:
        print(f"Table trade_peaks erreur: {e}")
        return False

_init_table_peaks()

def _migrer_anciens_peaks():
    """Recupere l'ancien fichier /tmp en dedoublonnant : un ticket = son
    sommet MAXIMUM. Ne tourne qu'une fois, les doublons ne peuvent pas
    reapparaitre grace a la cle primaire."""
    try:
        if not os.path.exists("/tmp/trade_peaks.json"):
            return
        with open("/tmp/trade_peaks.json") as f:
            anciens = json.load(f)
        if not isinstance(anciens, list) or not anciens:
            return
        meilleurs = {}
        for a in anciens:
            t = str(a.get("ticket") or "")
            if not t:
                continue
            try:    pr = float(a.get("peak_r") or 0)
            except Exception: pr = 0.0
            if t not in meilleurs or pr > meilleurs[t]["peak_r"]:
                meilleurs[t] = {"ticket": t, "symbol": a.get("symbol", ""),
                                "peak_r": pr, "profit_final": a.get("profit_final", 0),
                                "ts": a.get("ts", "")}
            elif a.get("profit_final"):
                meilleurs[t]["profit_final"] = a.get("profit_final")
        for t, m in meilleurs.items():
            _ecrire_peak(m["ticket"], m["symbol"], m["peak_r"], m["profit_final"], m["ts"])
        print(f"Migration sommets : {len(anciens)} lignes -> {len(meilleurs)} trades")
        try:    os.rename("/tmp/trade_peaks.json", "/tmp/trade_peaks.json.migre")
        except Exception: pass
    except Exception as e:
        print(f"Migration sommets erreur: {e}")

def _ecrire_peak(ticket, symbol, peak_r, profit_final, ts):
    """Ecrit ou met a jour UN ticket. Le sommet conserve est le maximum
    jamais recu — un envoi tardif a 0 ne peut plus rien effacer."""
    ticket = str(ticket)
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("""INSERT INTO trade_peaks
                     (ticket, symbol, peak_r, profit_final, nb_envois, premier_ts, dernier_ts)
                     VALUES (%s,%s,%s,%s,1,%s,%s)
                     ON CONFLICT (ticket) DO UPDATE SET
                       symbol       = COALESCE(NULLIF(EXCLUDED.symbol,''), trade_peaks.symbol),
                       peak_r       = GREATEST(trade_peaks.peak_r, EXCLUDED.peak_r),
                       profit_final = CASE WHEN EXCLUDED.profit_final <> 0
                                           THEN EXCLUDED.profit_final
                                           ELSE trade_peaks.profit_final END,
                       nb_envois    = trade_peaks.nb_envois + 1,
                       dernier_ts   = EXCLUDED.dernier_ts""",
                  (ticket, symbol or "", float(peak_r or 0),
                   float(profit_final or 0), ts, ts))
        conn.commit(); conn.close()
        return True
    except Exception as e:
        print(f"Ecriture sommet erreur: {e}")
        # Repli memoire, avec la meme regle du maximum
        ex = TRADE_PEAKS.get(ticket)
        pr = float(peak_r or 0)
        if ex is None:
            TRADE_PEAKS[ticket] = {"ticket": ticket, "symbol": symbol or "",
                                   "peak_r": pr, "profit_final": profit_final or 0,
                                   "nb_envois": 1, "premier_ts": ts, "dernier_ts": ts}
        else:
            ex["peak_r"] = max(ex.get("peak_r", 0), pr)
            if profit_final:
                ex["profit_final"] = profit_final
            if symbol:
                ex["symbol"] = symbol
            ex["nb_envois"] = ex.get("nb_envois", 1) + 1
            ex["dernier_ts"] = ts
        return False

def _lire_peaks():
    """Renvoie la liste des trades, un par ticket, sommet decroissant."""
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("""SELECT ticket, symbol, peak_r, profit_final, nb_envois,
                            premier_ts, dernier_ts
                     FROM trade_peaks ORDER BY dernier_ts DESC LIMIT 300""")
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        if rows:
            return rows
    except Exception as e:
        print(f"Lecture sommets erreur: {e}")
    return list(TRADE_PEAKS.values())

_migrer_anciens_peaks()

@app.route("/trade-peak", methods=["POST"])
def trade_peak():
    try:
        d = request.get_json(force=True) or {}
    except Exception:
        return jsonify({"error": "json invalide"}), 400
    ticket = d.get("ticket")
    if not ticket:
        return jsonify({"error": "ticket requis"}), 400
    ts = datetime.now(timezone.utc).isoformat()
    en_base = _ecrire_peak(ticket, d.get("symbol", ""),
                           d.get("peak_r", 0), d.get("profit_final", 0), ts)
    return jsonify({"ok": True, "ticket": str(ticket),
                    "stockage": "base" if en_base else "memoire (base indisponible)"})

@app.route("/trade-peaks", methods=["GET"])
def trade_peaks_list():
    """Mesure du trailing : jusqu'ou le trade est alle, ou il s'est arrete.
    Un trade = une ligne. Les doublons et les zeros ne comptent plus."""
    trades = [t for t in _lire_peaks() if t.get("peak_r") is not None]
    n = len(trades)
    synthese = {}
    if n > 0:
        pr = sorted(float(t["peak_r"]) for t in trades)
        gagnants = [float(t["peak_r"]) for t in trades if float(t.get("profit_final") or 0) > 0]
        perdants = [float(t["peak_r"]) for t in trades if float(t.get("profit_final") or 0) < 0]
        def pct(seuil):
            return round(100 * sum(1 for x in pr if x >= seuil) / n)
        def moy(lst):
            return round(sum(lst) / len(lst), 2) if lst else None
        milieu = pr[n // 2] if n % 2 else (pr[n // 2 - 1] + pr[n // 2]) / 2
        synthese = {
            "nb_trades": n,
            "sommet_moyen_R": moy(pr),
            "sommet_median_R": round(milieu, 2),
            "sommet_max_R": round(pr[-1], 2),
            "ont_atteint_1R_pct": pct(1.0),
            "ont_atteint_1_5R_pct": pct(1.5),
            "ont_atteint_2R_pct": pct(2.0),
            "ont_atteint_3R_pct": pct(3.0),
            "nb_gagnants": len(gagnants),
            "nb_perdants": len(perdants),
            "sommet_moyen_gagnants_R": moy(gagnants),
            "sommet_moyen_perdants_R": moy(perdants),
            "lecture": ("Si le sommet moyen des PERDANTS est eleve (>0.7R), le trailing "
                        "ou le BE laissent filer des trades qui avaient commence a "
                        "fonctionner. Si le sommet moyen des GAGNANTS est tres au-dessus "
                        "du gain encaisse, le trailing coupe trop tot."),
            "avertissement_unite": ("R calcule par l'EA sur la distance REELLE du stop a "
                                    "l'ouverture. Ne pas melanger avec un R recalcule "
                                    "depuis les euros et le risque prevu.")
        }
    return jsonify({"synthese": synthese, "nb_trades": n, "trades": trades})

# ── TAILLE DE LOT (securite v4) ───────────────────────────────
# Le systeme affichait "Risque 1%" mais laissait le trader mettre 1 lot fixe.
# Resultat : risque reel entre 0,3% et 3% selon l'instrument (cas Or : -893 EUR).
# Cette route calcule la taille exacte : risque EUR / (distance SL x taille contrat / taux EUR)

CONTRACT_SIZES = {
    "GOLD": 100, "SILVER": 5000,          # onces par lot (Admiral)
    "BRENT": 100, "CRUDOIL": 100,          # barils par lot
    "US100": 1, "[SP500]": 1, "[DJI30]": 1,
    "GERMANY40": 1, "[FTSE100]": 1,        # indices : 1 point = ~1 unite (a verifier)
    # Cryptos (CFD Admiral : 1 lot = 1 unite pour BTC/ETH)
    "BTCUSD": 1, "ETHUSD": 1,
    # Cryptos hors MT4 (Binance, non executables sur le compte) : indicatif
    "SOLUSD": 1, "BNBUSD": 1, "XRPUSD": 1000, "ADAUSD": 1000
}
QUOTE_OVERRIDES = {
    "GOLD": "USD", "SILVER": "USD", "BRENT": "USD", "CRUDOIL": "USD",
    "US100": "USD", "[SP500]": "USD", "[DJI30]": "USD",
    "GERMANY40": "EUR", "[FTSE100]": "GBP",
    "BTCUSD": "USD", "ETHUSD": "USD", "SOLUSD": "USD",
    "BNBUSD": "USD", "XRPUSD": "USD", "ADAUSD": "USD"
}
FALLBACK_EUR = {"USD": 1.14, "JPY": 184.5, "CAD": 1.62, "CHF": 0.92, "GBP": 0.856, "EUR": 1.0}

# Distance minimale du stop par instrument (garde-fou anti "0,2 pip")
MIN_STOP_DIST = {
    "GOLD": 3.0, "SILVER": 0.30, "BRENT": 0.30, "CRUDOIL": 0.30,
    "US100": 15.0, "[SP500]": 8.0, "[DJI30]": 30.0, "GERMANY40": 15.0, "[FTSE100]": 10.0,
    "BTCUSD": 300.0, "ETHUSD": 15.0, "SOLUSD": 1.5,
    "BNBUSD": 5.0, "XRPUSD": 0.02, "ADAUSD": 0.01
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
    if symbol in ("SOLUSD", "BNBUSD", "XRPUSD", "ADAUSD"):
        warning = "Crypto hors MT4 (source Binance) — non executable sur le compte demo, taille indicative"

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
            # ── ETAPE MONTANT (v9) : on attend un NOMBRE ────────────
            trade_montant = None
            for tid, fb in list(pending_feedback.items()):
                if fb.get("step") == "montant":
                    trade_montant = tid
                    break
            if trade_montant:
                val = lire_montant(text_msg)
                if val is None:
                    send_tg(chat_id_msg,
                        "❌ Je n'ai pas compris ce nombre.\n"
                        "Tape juste le montant, exemple : <code>-590.60</code>\n"
                        "Ou appuie sur Passer dans le message précédent.")
                    return jsonify({"ok": True})
                try:
                    conn = get_db(); c = conn.cursor()
                    c.execute("UPDATE journal SET pnl=%s WHERE id=%s",
                              (val, int(trade_montant)))
                    conn.commit()
                    # Reecrire la LECON en y ajoutant le montant. C'est la lecon
                    # que les agents relisent avant chaque analyse — pas la
                    # colonne pnl. Sans ca, le montant reste invisible pour eux.
                    c.execute("SELECT lecon FROM journal WHERE id=%s", (int(trade_montant),))
                    row = c.fetchone()
                    if row and row.get("lecon") and "[Impact :" not in row["lecon"]:
                        signe = "+" if val >= 0 else ""
                        neuf = f"{row['lecon']} [Impact : {signe}{val} EUR]"
                        c.execute("UPDATE journal SET lecon=%s WHERE id=%s",
                                  (neuf, int(trade_montant)))
                        conn.commit()
                    conn.close()
                    print(f"Montant trade #{trade_montant}: {val} EUR")
                except Exception as e:
                    print(f"Erreur montant: {e}")
                fb = pending_feedback[trade_montant]
                fb["step"] = "systeme"
                pending_feedback[trade_montant] = fb
                signe = "+" if val >= 0 else ""
                send_tg(chat_id_msg,
                    f"💰 <b>{signe}{val} €</b> enregistré.\n\n"
                    f"<b>🦊 Système suivi ?</b>\nEntrée au signal, SL/TP du plan, zéro main ?",
                    {"inline_keyboard": [[
                        {"text": "✅ OUI — plan respecté", "callback_data": f"s_{trade_montant}_oui"},
                        {"text": "❌ NON — hors système",  "callback_data": f"s_{trade_montant}_non"}
                    ]]})
                return jsonify({"ok": True})

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
                # v7 : generer la LECON (causale) des que le verdict est connu
                try:
                    c.execute("SELECT * FROM journal WHERE id=%s", (int(trade_id),))
                    row = c.fetchone()
                    if row:
                        lec = generer_lecon(dict(row))
                        if lec:
                            c.execute("UPDATE journal SET lecon=%s WHERE id=%s", (lec, int(trade_id)))
                            conn.commit()
                            print(f"Lecon #{trade_id}: {lec}")
                except Exception as e:
                    conn.rollback(); print(f"Lecon erreur: {e}")
                conn.close()
                print(f"Journal mis a jour: trade #{trade_id} = {resultat} / {contexte} / {difficulte}")
            except Exception as e:
                print(f"Erreur update journal: {e}")
            answer_callback(callback_id, "Presque fini !")
            edit_tg_markup(chat_id, message_id, {"inline_keyboard": []})
            # v9 : nouvelle etape MONTANT avant "systeme suivi"
            fb["resultat"] = resultat
            fb["contexte"] = contexte
            fb["difficulte"] = difficulte
            fb["step"] = "montant"
            pending_feedback[trade_id] = fb
            send_tg(chat_id,
                f"{label_d} noté.\n\n<b>💰 Montant du trade ?</b>\n"
                f"Tape le résultat NET en euros.\n"
                f"Exemple : <code>-590.60</code> ou <code>176.50</code>",
                {"inline_keyboard": [[
                    {"text": "⏭️ Passer", "callback_data": f"skipmontant_{trade_id}"}
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
    elif callback_data.startswith("skipmontant_"):
        trade_id = callback_data.replace("skipmontant_", "")
        fb = pending_feedback.get(trade_id, {})
        fb["step"] = "systeme"
        pending_feedback[trade_id] = fb
        answer_callback(callback_id, "Montant ignoré")
        edit_tg_markup(chat_id, message_id, {"inline_keyboard": []})
        send_tg(chat_id,
            "Montant non renseigné.\n\n<b>🦊 Système suivi ?</b>\n"
            "Entrée au signal, SL/TP du plan, zéro main ?",
            {"inline_keyboard": [[
                {"text": "✅ OUI — plan respecté", "callback_data": f"s_{trade_id}_oui"},
                {"text": "❌ NON — hors système",  "callback_data": f"s_{trade_id}_non"}
            ]]})

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

# ── ROUTE SUPPRIMEE le 11/08/2026 : /admin/reset-journal ─────
# Elle effacait TOUS les trades sauf ceux du 19/06/2026, protegee par une
# cle ecrite en clair dans ce fichier — lui-meme public sur GitHub.
# N'importe qui pouvait vider le journal avec une simple adresse web.
# Ne pas la remettre. Pour effacer des trades, passer par la base.

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
    if not redis_set(f"daily:{symbol}", data, ttl=DAILY_TTL): mt4_daily_ram[symbol] = data
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
             direction_ok,entree_ok,sortie_ok,raison_sortie,
             rapports_agents,market_state,regime_ratio,rsi_value,rsi_pente,
             trap,cisd,msu,consensus_long,consensus_short,gate_blocked,
             rr_reel_feu,rr_reel_valeur,rr_reel_mur,voie)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
                    %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            RETURNING id''',
            (data.get("date"),data.get("pair"),data.get("tf"),data.get("session"),
             data.get("score"),data.get("decision"),data.get("bias"),
             data.get("entry"),data.get("sl"),data.get("tp"),data.get("rr"),
             data.get("resultat"),data.get("contexte_marche"),data.get("difficulte"),
             data.get("pnl"),data.get("commentaire"),datetime.now().isoformat(),
             data.get("direction_ok"),data.get("entree_ok"),
             data.get("sortie_ok"),data.get("raison_sortie"),
             data.get("rapports_agents"),
             str(data.get("market_state")) if data.get("market_state") is not None else None,
             str(data.get("regime_ratio")) if data.get("regime_ratio") is not None else None,
             str(data.get("rsi_value")) if data.get("rsi_value") is not None else None,
             str(data.get("rsi_pente")) if data.get("rsi_pente") is not None else None,
             str(data.get("trap")), str(data.get("cisd")), str(data.get("msu")),
             str(data.get("consensus_long")), str(data.get("consensus_short")),
             str(data.get("gate_blocked")),
             data.get("rr_reel_feu"), data.get("rr_reel_valeur"),
             data.get("rr_reel_mur"), data.get("voie")))
        # Phase 1 : etiquette de proprietaire. Si la page ne l'envoie pas
        # (c'est le cas aujourd'hui), on met le compte du proprietaire.
        # Rien a modifier dans index.html ni dans l'EA.
        try:
            c.execute("UPDATE journal SET compte_id=%s WHERE id=(SELECT MAX(id) FROM journal)",
                      (data.get("compte_id") or COMPTE_PROPRIETAIRE,))
        except Exception as e:
            print(f"compte_id add_trade: {e}")
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
        # Filtre OPTIONNEL par compte. Sans le parametre, comportement
        # strictement identique a avant.
        cid = request.args.get("compte_id")
        if cid:
            c.execute("SELECT * FROM journal WHERE compte_id=%s ORDER BY id DESC LIMIT 100", (cid,))
        else:
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
        # FIX WINRATE : le winrate ne doit compter QUE les trades reellement pris
        # (win + loss + be). Avant, 'done' incluait aussi les 'nondeclenche', 'passe',
        # etc., ce qui gonflait le denominateur et ecrasait le winrate (ex: 12% au lieu du vrai).
        trades_pris = stats["wins"] + stats["losses"] + stats["be"]
        stats["trades_pris"] = trades_pris
        stats["winrate"] = round(stats["wins"]/trades_pris*100) if trades_pris > 0 else 0
        # Compter les non-declenches a part (info utile, pas dans le winrate)
        c.execute("SELECT COUNT(*) as n FROM journal WHERE resultat='nondeclenche'")
        stats["non_declenches"] = c.fetchone()["n"]
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
        ctx += ("REGLE D'USAGE DE CES STATISTIQUES (obligatoire pour TOUS les agents) :\n"
                "1) Ces stats servent a COMPRENDRE le contexte des pertes passees "
                "(ex: pertes concentrees en RANGE ou sans TRAP confirme), PAS a punir "
                "mecaniquement une paire ou une session.\n"
                "2) SEUL l'Agent Memoire applique un bonus/malus historique (max ±1pt). "
                "Les AUTRES agents (Liquidite, Structure, Zones, Timing, Risk, Backtest, Eco) "
                "ne doivent appliquer AUCUN malus base sur ces winrates historiques — "
                "ils analysent le marche ACTUEL uniquement. Tout double-comptage fausse le score.\n"
                "3) Une stat sur N<10 trades est une indication, pas une loi.\n")

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
            ctx += ("  LECTURE : c'est le TYPE DE MARCHE qui explique les pertes, pas la paire. "
                    "Si RANGE affiche un winrate tres bas, la lecon est : eviter les entrees "
                    "directionnelles en range — pas eviter telle paire ou telle session.\n")

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

        # ── MEMOIRE DES RAISONNEMENTS (v7) ──────────────────────
        # Le systeme relit ce que SES agents avaient ecrit dans des conditions
        # comparables, avec le verdict. C'est l'enfant qui relit ses notes.
        try:
            pair_q = request.args.get("pair")
            ms_q   = request.args.get("market_state")
            sql = ("SELECT date, pair, resultat, score, pnl, market_state, rapports_agents "
                   "FROM journal WHERE rapports_agents IS NOT NULL "
                   "AND resultat IN ('win','loss','be') ")
            params = []
            if pair_q:
                sql += "AND pair = %s "
                params.append(pair_q)
            if ms_q:
                sql += "AND market_state = %s "
                params.append(ms_q)
            sql += "ORDER BY id DESC LIMIT 4"
            c.execute(sql, tuple(params))
            passes = c.fetchall()
            # Si rien sur cette paire, elargir a tous les trades
            if not passes and pair_q:
                c.execute("SELECT date, pair, resultat, score, pnl, market_state, rapports_agents "
                          "FROM journal WHERE rapports_agents IS NOT NULL "
                          "AND resultat IN ('win','loss','be') ORDER BY id DESC LIMIT 3")
                passes = c.fetchall()
            if passes:
                ctx += "\n\nCE QUE TU AVAIS ECRIT DANS DES CONDITIONS COMPARABLES :\n"
                for p in passes:
                    res = (p.get("resultat") or "?").upper()
                    pnl = p.get("pnl")
                    pnl_txt = f" | {round(pnl,2)} EUR" if pnl is not None else ""
                    ctx += f"--- {p.get('date','?')} | {p.get('pair','?')} | {res} | score {p.get('score','?')}{pnl_txt} | regime {p.get('market_state') or 'n/a'} ---\n"
                    try:
                        rap = json.loads(p["rapports_agents"]) if isinstance(p["rapports_agents"], str) else p["rapports_agents"]
                        for k, v in (rap or {}).items():
                            if not isinstance(v, dict):
                                continue
                            vote = v.get("vote", "")
                            vote_txt = f" [vote {vote}]" if vote and vote != "n/a" else ""
                            concl = str(v.get("conclusion", ""))[:220]
                            ctx += f"  {k.upper()} ({v.get('score','?')}){vote_txt} : {concl}\n"
                    except Exception:
                        pass
                ctx += ("  INSTRUCTION : relis ces raisonnements. Si tu t'appretes a ecrire la meme "
                        "chose sur un cas qui a PERDU, cherche ce qui avait ete manque a l'epoque "
                        "avant de valider. Si le cas avait GAGNE, verifie que les memes conditions "
                        "sont reellement presentes aujourd'hui — ne les suppose pas.\n")
            # ── BIBLIOTHEQUE DE LECONS (v7) ────────────────────────
            # Chaque trade devient une regle exploitable, formulee en CAUSES.
            c.execute("SELECT lecon, resultat FROM journal WHERE lecon IS NOT NULL "
                      "AND lecon <> '' ORDER BY id DESC LIMIT 12")
            lecons = c.fetchall()
            if lecons:
                ctx += "\nLECONS TIREES DES TRADES PRECEDENTS :\n"
                for l in lecons:
                    ctx += f"  - {l['lecon']}\n"
                ctx += ("  REGLE DE LECTURE OBLIGATOIRE : apprends des CAUSES, jamais des CONCLUSIONS.\n"
                        "  INTERDIT : 'cette paire a perdu, donc je l'evite' — c'est une conclusion, "
                        "elle ne dit rien du marche d'aujourd'hui.\n"
                        "  CORRECT : 'il manquait TRAP et CISD la derniere fois ; sont-ils presents "
                        "MAINTENANT ? Si oui, le setup est valide malgre l'echec passe. Si non, prudence.'\n"
                        "  Une paire n'est jamais coupable. Ce sont les declencheurs absents qui le sont.\n")
        except Exception as e:
            print(f"Memoire raisonnements: {e}")

        conn.close()
        return jsonify({"context": ctx, "has_data": True, "total_trades": total_done})
    except Exception as e:
        print(f"Erreur journal/context: {e}")
        return jsonify({"context": "", "has_data": False})

# ══════════════════════════════════════════════════════════════
# COMPTEURS AUTOMATIQUES (16/08/2026) — LECTURE SEULE
#
# Compter a la main est la meilleure methode : elle oblige a regarder
# chaque trade. C'est comme ca qu'ont ete trouves le stop de l'or et
# le TP place au-dela du mur. Mais elle demande une discipline que
# personne ne tient sur 100 trades.
#
# Cette route compte a la place. Elle ne DECIDE rien, ne modifie rien,
# n'ecrit rien. Elle lit le journal et pose, pour chaque question,
# quatre nombres : gains, pertes, taux, montant.
#
# Et surtout elle applique les deux garde-fous qu'un tableau ordinaire
# n'applique jamais :
#   1) FRAGILITE : de combien de points le taux bougerait-il si on
#      retirait UN SEUL trade ? Si un trade change la conclusion,
#      il n'y a pas de conclusion.
#   2) EFFECTIF : en dessous de 10 trades dans une case, on ne conclut
#      pas. En dessous de 20, on reste prudent.
#
# Le verdict ne dit JAMAIS "prouve". Au mieux "piste a surveiller".
# ══════════════════════════════════════════════════════════════

def _vrai(v):
    return str(v or "").strip().lower() in ("true", "1", "oui", "yes")

def _boite(lignes):
    """Un paquet de trades -> gains, pertes, taux, montant, fragilite."""
    g = sum(1 for t in lignes if (t.get("resultat") or "") == "win")
    p = sum(1 for t in lignes if (t.get("resultat") or "") == "loss")
    b = sum(1 for t in lignes if (t.get("resultat") or "") == "be")
    n = g + p + b
    if n == 0:
        return {"n": 0, "gains": 0, "pertes": 0, "be": 0, "taux": None,
                "pnl": None, "fragilite_points": None}
    taux = 100.0 * g / n
    # Fragilite : le pire des deux cas, retirer un gain ou retirer une perte
    frag = 0.0
    if n > 1:
        if g > 0: frag = max(frag, abs(taux - 100.0 * (g - 1) / (n - 1)))
        if p > 0: frag = max(frag, abs(taux - 100.0 * g / (n - 1)))
    montants = [float(t["pnl"]) for t in lignes if t.get("pnl") is not None]
    return {
        "n": n, "gains": g, "pertes": p, "be": b,
        "taux": round(taux),
        "pnl": round(sum(montants), 2) if montants else None,
        "pnl_connu_sur": len(montants),
        "fragilite_points": round(frag)
    }

def _verdict(a, b):
    """Que peut-on dire honnetement de la comparaison de deux boites ?"""
    if a["n"] == 0 or b["n"] == 0:
        return {"verdict": "RIEN A DIRE", "raison": "une des deux cases est vide"}
    petit = min(a["n"], b["n"])
    if petit < 10:
        return {"verdict": "DONNEE INSUFFISANTE",
                "raison": "seulement %d trade(s) dans la plus petite case, il en faut 20" % petit}
    ecart = abs(a["taux"] - b["taux"])
    frag = max(a["fragilite_points"] or 0, b["fragilite_points"] or 0)
    if frag >= ecart:
        return {"verdict": "TROP FRAGILE",
                "raison": "un seul trade deplacerait le taux de %d points, "
                          "pour un ecart mesure de %d points" % (frag, ecart)}
    if ecart < 10:
        return {"verdict": "AUCUNE DIFFERENCE",
                "raison": "%d points d'ecart, c'est du bruit" % ecart}
    if petit < 20:
        return {"verdict": "PISTE FAIBLE",
                "raison": "%d points d'ecart, mais seulement %d trades dans la plus petite case"
                          % (ecart, petit)}
    return {"verdict": "PISTE A SURVEILLER",
            "raison": "%d points d'ecart sur %d trades minimum. Continuer a compter, "
                      "ne rien coder avant 50." % (ecart, petit)}

@app.route("/compteurs", methods=["GET"])
def compteurs():
    """Compte tout seul ce qu'il faudrait compter a la main.
    Lecture seule. Filtre optionnel : ?compte_id=XXX"""
    try:
        conn = get_db(); c = conn.cursor()
        sql = ("SELECT resultat, pnl, trap, cisd, msu, market_state, contexte_marche, "
               "consensus_long, consensus_short, session, difficulte, systeme_suivi, "
               "score, rsi_value, rsi_pente, bias, rr_reel_feu, voie "
               "FROM journal WHERE resultat IN ('win','loss','be')")
        params = []
        if request.args.get("compte_id"):
            sql += " AND compte_id = %s"; params.append(request.args.get("compte_id"))
        try:
            c.execute(sql, tuple(params))
        except Exception:
            # rr_reel_feu / voie pas encore posees : on relit sans elles
            conn.rollback()
            sql = sql.replace(", rr_reel_feu, voie", "")
            c.execute(sql, tuple(params))
        trades = [dict(r) for r in c.fetchall()]
        conn.close()

        total = len(trades)
        if total == 0:
            return jsonify({"trades_pris": 0, "message": "aucun trade pris a compter"})

        def couper(test):
            oui = [t for t in trades if test(t)]
            non = [t for t in trades if not test(t)]
            A, B = _boite(oui), _boite(non)
            return {"avec": A, "sans": B, **_verdict(A, B)}

        def ecart_cons(t):
            try:    return abs(int(t.get("consensus_long") or 0) - int(t.get("consensus_short") or 0))
            except Exception: return 0

        questions = {
            "TRAP_confirme":        couper(lambda t: _vrai(t.get("trap"))),
            "CISD_confirme":        couper(lambda t: _vrai(t.get("cisd"))),
            "MSU_detecte":          couper(lambda t: _vrai(t.get("msu"))),
            "consensus_FORT_6plus": couper(lambda t: ecart_cons(t) >= 6),
            "marche_RANGE":         couper(lambda t: (t.get("contexte_marche") or "").lower() == "range"),
            "marche_MANIPULATION":  couper(lambda t: (t.get("contexte_marche") or "").lower() == "manipulation"),
            "systeme_suivi_OUI":    couper(lambda t: (t.get("systeme_suivi") or "") == "oui"),
            "score_70_ou_plus":     couper(lambda t: (t.get("score") or 0) >= 70),
        }

        # Questions qui ne repondent que si les champs existent
        if any("rr_reel_feu" in t for t in trades):
            def feu(t): return (t.get("rr_reel_feu") or "").upper()
            questions["RR_reel_VERT"]  = couper(lambda t: feu(t) == "VERT")
            questions["RR_reel_ROUGE"] = couper(lambda t: feu(t) == "ROUGE")
        else:
            questions["RR_reel"] = {"verdict": "NON ENREGISTRE",
                "raison": "le RR reel est calcule et affiche, mais la page ne l'envoie pas au journal"}
        if any("voie" in t for t in trades):
            questions["via_ENQUETEUR"] = couper(lambda t: (t.get("voie") or "").upper().startswith("AVEC"))
        else:
            questions["voie_Enqueteur"] = {"verdict": "NON ENREGISTRE",
                "raison": "avec ou sans Enqueteur n'est pas enregistre dans le journal"}

        # Par difficulte et par session : simple repartition, pas de comparaison
        def grouper(champ):
            out = {}
            for t in trades:
                k = (t.get(champ) or "(non renseigne)")
                out.setdefault(k, []).append(t)
            return {k: _boite(v) for k, v in sorted(out.items())}

        global_ = _boite(trades)
        return jsonify({
            "compte_id": request.args.get("compte_id") or "tous",
            "trades_pris": total,
            "global": global_,
            "questions": questions,
            "par_difficulte": grouper("difficulte"),
            "par_session": grouper("session"),
            "par_regime": grouper("market_state"),
            "mode_d_emploi": {
                "1": "Chaque question coupe les trades en deux cases et compare les taux.",
                "2": "fragilite_points = de combien le taux bougerait si on retirait UN trade. "
                     "Si ce nombre depasse l'ecart mesure, la comparaison ne vaut rien.",
                "3": "Le verdict ne dit jamais PROUVE. Au mieux PISTE A SURVEILLER.",
                "4": "Il faut 20 trades minimum dans la plus petite case, 50 pour coder une regle."
            }
        })
    except Exception as e:
        print(f"compteurs: {e}")
        return jsonify({"error": str(e)}), 500

@app.route("/identite", methods=["GET"])
def identite():
    """Qui est qui. Lecture seule, sert a verifier la phase 1."""
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT id, prenom, nom, role, telegram_chat_id, actif, cree_le "
                  "FROM utilisateurs ORDER BY id")
        users = [dict(r) for r in c.fetchall()]
        c.execute("SELECT compte_id, utilisateur_id, courtier, type, devise, "
                  "balance_initiale, actif, note FROM comptes ORDER BY compte_id")
        cpts = [dict(r) for r in c.fetchall()]
        repartition = {}
        for t in ("journal", "surveillance", "trade_peaks"):
            try:
                c.execute("SELECT COALESCE(compte_id,'(sans etiquette)') AS cid, COUNT(*) AS n "
                          "FROM %s GROUP BY 1 ORDER BY 2 DESC" % t)
                repartition[t] = {r["cid"]: r["n"] for r in c.fetchall()}
            except Exception as e:
                repartition[t] = {"erreur": str(e)}
        conn.close()
        return jsonify({"utilisateurs": users, "comptes": cpts,
                        "repartition_des_lignes": repartition,
                        "compte_par_defaut": COMPTE_PROPRIETAIRE})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/lecons", methods=["GET"])
def lecons():
    """Bibliotheque des enseignements. Filtres: ?resultat=loss"""
    try:
        conn = get_db(); c = conn.cursor()
        sql = ("SELECT id, date, pair, resultat, score, pnl, market_state, lecon "
               "FROM journal WHERE lecon IS NOT NULL AND lecon <> '' ")
        params = []
        if request.args.get("resultat"):
            sql += "AND resultat = %s "; params.append(request.args.get("resultat"))
        if request.args.get("pair"):
            sql += "AND pair = %s "; params.append(request.args.get("pair"))
        if request.args.get("compte_id"):
            sql += "AND compte_id = %s "; params.append(request.args.get("compte_id"))
        sql += "ORDER BY id DESC LIMIT 100"
        c.execute(sql, tuple(params))
        rows = [dict(r) for r in c.fetchall()]
        conn.close()
        return jsonify({"total": len(rows), "lecons": rows})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/raisonnements", methods=["GET"])
def raisonnements():
    """Relire les analyses passees : /raisonnements?pair=GBP/CAD&market_state=RANGE"""
    try:
        conn = get_db(); c = conn.cursor()
        sql = ("SELECT id, date, pair, resultat, score, pnl, market_state, rsi_value, "
               "rsi_pente, trap, rapports_agents FROM journal "
               "WHERE rapports_agents IS NOT NULL ")
        params = []
        if request.args.get("pair"):
            sql += "AND pair = %s "; params.append(request.args.get("pair"))
        if request.args.get("market_state"):
            sql += "AND market_state = %s "; params.append(request.args.get("market_state"))
        if request.args.get("resultat"):
            sql += "AND resultat = %s "; params.append(request.args.get("resultat"))
        sql += "ORDER BY id DESC LIMIT 20"
        c.execute(sql, tuple(params))
        rows = [dict(r) for r in c.fetchall()]
        for r0 in rows:
            try:
                if isinstance(r0.get("rapports_agents"), str):
                    r0["rapports_agents"] = json.loads(r0["rapports_agents"])
            except Exception:
                pass
        conn.close()
        return jsonify({"total": len(rows), "analyses": rows})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ══════════════════════════════════════════════════════════════
# SURVEILLANCE DES SIGNAUX BLOQUES (v8)
# Un signal ATTENDRE disparaissait sans qu'on sache si le blocage
# etait justifie. Desormais il est SUIVI : verification legere toutes
# les 30 min (aucun appel IA), abandon a 4h, et TOUT est journalise.
# ══════════════════════════════════════════════════════════════
SURVEILLANCE_MINUTES = 30        # frequence de verification
SURVEILLANCE_MAX_HEURES = 4      # au-dela : abandon (aligne sur MT4)

# v8.1 : stockage en BASE, plus dans /tmp.
# /tmp est efface a chaque redeploiement Render, et le thread de fond mourait
# pendant les mises en veille -> aucune verification pendant toute une nuit.
def _init_table_surveillance():
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("""CREATE TABLE IF NOT EXISTS surveillance (
            trade_id TEXT PRIMARY KEY,
            donnees TEXT,
            cree_ts DOUBLE PRECISION,
            statut TEXT
        )""")
        conn.commit(); conn.close()
        print("Table surveillance OK")
    except Exception as e:
        print(f"Table surveillance erreur: {e}")

_init_table_surveillance()

# ── PHASE 1 MULTI-UTILISATEURS : APPEL FINAL ──────────────────
# Placee ici, et nulle part ailleurs : toutes les tables (journal,
# surveillance, trade_peaks) existent a ce stade. La migration peut
# donc poser la colonne compte_id sur les trois sans en manquer une.
init_identite()

def _lire_surveillance():
    """Renvoie les signaux encore EN_SURVEILLANCE, depuis la base."""
    out = {}
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("SELECT trade_id, donnees FROM surveillance WHERE statut='EN_SURVEILLANCE'")
        for row in c.fetchall():
            try:
                out[row["trade_id"]] = json.loads(row["donnees"])
            except Exception:
                pass
        conn.close()
    except Exception as e:
        print(f"Lecture surveillance: {e}")
    return out

def _ecrire_surveillance(tid, sig, statut="EN_SURVEILLANCE"):
    try:
        conn = get_db(); c = conn.cursor()
        c.execute("""INSERT INTO surveillance (trade_id, donnees, cree_ts, statut)
                     VALUES (%s,%s,%s,%s)
                     ON CONFLICT (trade_id) DO UPDATE
                     SET donnees=EXCLUDED.donnees, statut=EXCLUDED.statut""",
                  (str(tid), json.dumps(sig), sig.get("cree_ts", time.time()), statut))
        conn.commit(); conn.close()
    except Exception as e:
        print(f"Ecriture surveillance: {e}")

@app.route("/surveiller", methods=["POST"])
def surveiller():
    """La page enregistre ici chaque signal bloque (ATTENDRE)."""
    d = request.get_json(force=True, silent=True) or {}
    tid = str(d.get("trade_id") or int(time.time()))
    sig = {
        "trade_id": tid,
        "pair": d.get("pair"),
        "symbol": (d.get("pair") or "").replace("/", ""),
        "mt4_symbol": d.get("mt4_symbol"),
        "bias": d.get("bias"),
        "entry": d.get("entry"),
        "sl": d.get("sl"),
        "tp": d.get("tp"),
        "score": d.get("score"),
        "raison_blocage": d.get("raison_blocage"),
        "consensus_long": d.get("consensus_long"),
        "consensus_short": d.get("consensus_short"),
        "market_state": d.get("market_state"),
        "rsi_value": d.get("rsi_value"),
        "rsi_pente": d.get("rsi_pente"),
        "cree_ts": time.time(),
        "cree_le": datetime.now(PARIS_TZ).strftime("%Y-%m-%d %H:%M:%S"),
        "verifications": 0,
        "statut": "EN_SURVEILLANCE"
    }
    _ecrire_surveillance(tid, sig)
    print(f"Surveillance ouverte : {d.get('pair')} #{tid}")
    return jsonify({"ok": True})

@app.route("/surveilles", methods=["GET"])
def liste_surveilles():
    """Consulter les signaux en cours de surveillance."""
    verifier_signaux_en_attente()          # v8.1 : on verifie au passage
    out = []
    for v in _lire_surveillance().values():
        v2 = dict(v)
        v2["age_minutes"] = round((time.time() - v.get("cree_ts", 0)) / 60)
        out.append(v2)
    out.sort(key=lambda x: x.get("cree_ts", 0), reverse=True)
    return jsonify({"total": len(out), "signaux": out})

def _prix_actuel(sig):
    """Prix live du symbole, ou None."""
    for key in [sig.get("mt4_symbol"), sig.get("symbol")]:
        if not key:
            continue
        d = redis_get(f"price:{key.upper()}") or mt4_prices_ram.get(key.upper())
        if d and d.get("bid"):
            try:
                return float(d["bid"]), d
            except Exception:
                pass
    return None, None

def _rsi_actuel(sig, period=14):
    """RSI(14) Wilder sur les bougies H1 en cache. None si indisponible."""
    for key in [sig.get("mt4_symbol"), sig.get("symbol")]:
        if not key:
            continue
        d = redis_get(f"candles:{key.upper()}") or mt4_candles_ram.get(key.upper())
        if not d:
            continue
        try:
            closes = [float(c["c"]) for c in d.get("candles", [])]
            if len(closes) < period + 1:
                continue
            gains = losses = 0.0
            for i in range(1, period + 1):
                delta = closes[i] - closes[i-1]
                if delta > 0: gains += delta
                else:         losses -= delta
            avg_g, avg_l = gains / period, losses / period
            for i in range(period + 1, len(closes)):
                delta = closes[i] - closes[i-1]
                avg_g = (avg_g * (period - 1) + (delta if delta > 0 else 0)) / period
                avg_l = (avg_l * (period - 1) + (-delta if delta < 0 else 0)) / period
            if avg_l == 0:
                return 100.0
            return 100 - (100 / (1 + avg_g / avg_l))
        except Exception:
            continue
    return None

def motif_toujours_actif(sig):
    """Le motif du blocage est-il TOUJOURS present ?
    Renvoie (actif, explication).

    FIX 10/08 : la surveillance proposait une relance des que le regime
    s'alignait, SANS verifier si la cause du blocage avait disparu. Cas reel
    (GBP/JPY) : bloque a RSI 81,1 ; notification de relance envoyee alors que
    le RSI etait monte a 82,18 — la cause s'etait AGGRAVEE. Le message ne
    montrait que ce qui s'etait ameliore."""
    raison = (sig.get("raison_blocage") or "").upper()

    # Blocage par la boussole RSI : verifiable sans appel IA
    if "RSI" in raison or "BOUSSOLE" in raison:
        rsi = _rsi_actuel(sig)
        if rsi is None:
            return (True, "RSI non recalculable — motif suppose toujours actif")
        if rsi >= 68 or rsi <= 32:
            depart = sig.get("rsi_value")
            dep_txt = f" (etait a {round(float(depart),1)})" if depart else ""
            return (True, f"RSI toujours en zone extreme : {round(rsi,1)}{dep_txt}")
        return (False, f"RSI revenu en zone neutre : {round(rsi,1)}")

    # Contradiction entre agents : NON verifiable sans refaire tourner les 8 agents
    if "CONTRADICTION" in raison:
        return (True, "contradiction entre agents — non verifiable sans nouvelle analyse")

    # Score sous le seuil : depend des 8 agents, donc non verifiable ici
    if "SCORE" in raison:
        return (True, "score sous seuil — a reevaluer par une analyse complete")

    return (False, "")

def verifier_un_signal(tid, sig):
    """Verification LEGERE : aucun appel IA. Renvoie (statut, message)."""
    age_h = (time.time() - sig.get("cree_ts", 0)) / 3600.0
    prix, data = _prix_actuel(sig)

    if prix is None:
        return ("EN_SURVEILLANCE", None)   # pas de prix : on reessaiera

    try:
        entry = float(sig.get("entry"))
        sl    = float(sig.get("sl"))
        tp    = float(sig.get("tp"))
    except Exception:
        return ("ABANDON", "niveaux illisibles")

    est_long = (sig.get("bias") == "long")

    # 1) Le train est parti : le prix a depasse le TP sans nous
    if (est_long and prix >= tp) or (not est_long and prix <= tp):
        return ("ABANDON_TP_ATTEINT",
                f"le prix a atteint le TP ({tp}) sans nous — blocage COUTEUX")

    # 2) Setup mort : le prix a depasse le SL
    if (est_long and prix <= sl) or (not est_long and prix >= sl):
        return ("ABANDON_SL_DEPASSE",
                f"le prix a depasse le SL ({sl}) — blocage JUSTIFIE, perte evitee")

    # 3) Le regime a-t-il change en notre faveur ?
    regime_now = (data or {}).get("market_state")
    regime_avant = sig.get("market_state")
    aligne = False
    if regime_now:
        if est_long and regime_now in ("TENDANCE", "TREND_UP"):
            aligne = True
        if (not est_long) and regime_now in ("TENDANCE", "TREND_DOWN"):
            aligne = True
    if aligne and regime_now != regime_avant:
        # FIX 10/08 : ne proposer une relance QUE si le motif du blocage est leve.
        encore, explication = motif_toujours_actif(sig)
        if encore:
            # Le contexte s'ameliore mais la cause demeure : on informe sans inviter.
            return ("EN_SURVEILLANCE_MOTIF_ACTIF",
                    f"regime passe a {regime_now} (aligne) MAIS blocage maintenu — {explication}")
        return ("RELANCER",
                f"le regime est passe a {regime_now} et le motif du blocage est leve"
                + (f" ({explication})" if explication else "") + " — relance une analyse")

    # 4) Trop vieux
    if age_h >= SURVEILLANCE_MAX_HEURES:
        dist = abs(prix - entry)
        return ("ABANDON_EXPIRE",
                f"{SURVEILLANCE_MAX_HEURES}h sans declenchement (prix a {round(dist,5)} de l'entree)")

    return ("EN_SURVEILLANCE", None)

def journaliser_surveillance(sig, statut, message):
    """Ecrit l'issue dans le journal — meme tracabilite que les trades pris."""
    try:
        conn = get_db(); c = conn.cursor()
        commentaire = f"SIGNAL BLOQUE — {statut}. {message or ''} " \
                      f"(blocage initial : {sig.get('raison_blocage') or 'consensus faible'})"
        c.execute("""UPDATE journal SET resultat=%s, commentaire=%s, raison_sortie=%s
                     WHERE id=%s""",
                  ("signal_bloque", commentaire[:900], statut, int(sig["trade_id"])))
        conn.commit(); conn.close()
        print(f"Surveillance journalisee #{sig['trade_id']} : {statut}")
    except Exception as e:
        print(f"Surveillance journal erreur: {e}")

_derniere_verif_surv = 0.0

def verifier_signaux_en_attente(force=False):
    """v8.1 : appelee A CHAQUE REQUETE (pas un thread de fond, qui mourait
    pendant les mises en veille de Render). On ne verifie qu'une fois toutes
    les SURVEILLANCE_MINUTES pour ne pas surcharger."""
    global _derniere_verif_surv
    maintenant = time.time()
    if not force and (maintenant - _derniere_verif_surv) < SURVEILLANCE_MINUTES * 60:
        return
    _derniere_verif_surv = maintenant

    signaux = _lire_surveillance()
    if not signaux:
        return

    for tid, sig in signaux.items():
        try:
            statut, message = verifier_un_signal(tid, sig)
            sig["verifications"] = sig.get("verifications", 0) + 1
            sig["derniere_verif"] = datetime.now(PARIS_TZ).strftime("%d/%m %H:%M")

            if statut == "EN_SURVEILLANCE":
                _ecrire_surveillance(tid, sig)      # on garde le compteur a jour
                continue

            # Motif encore actif : on informe UNE SEULE FOIS, puis on continue
            # de surveiller. Pas d'invitation a relancer, pas de sortie du suivi.
            if statut == "EN_SURVEILLANCE_MOTIF_ACTIF":
                if not sig.get("info_motif_envoyee"):
                    age_m = round((maintenant - sig.get("cree_ts", 0)) / 60)
                    txt = ("SURVEILLANCE - CONTEXTE AMELIORE MAIS BLOCAGE MAINTENU\n"
                           + str(sig.get("pair") or "?") + " | bloque il y a " + str(age_m) + " min\n"
                           + str(message) + "\n"
                           + "-> Ne pas relancer : la cause du blocage est toujours la.")
                    try:
                        send_tg(TELEGRAM_CHAT_ID, txt)
                    except Exception as e:
                        print(f"Surveillance Telegram: {e}")
                    sig["info_motif_envoyee"] = True
                _ecrire_surveillance(tid, sig)
                continue

            age_min = round((maintenant - sig.get("cree_ts", 0)) / 60)
            entete = {"RELANCER": "RELANCE POSSIBLE",
                      "ABANDON_TP_ATTEINT": "GAIN MANQUE",
                      "ABANDON_SL_DEPASSE": "PERTE EVITEE",
                      "ABANDON_EXPIRE": "SIGNAL EXPIRE",
                      "ABANDON": "SIGNAL ABANDONNE"}.get(statut, "SIGNAL SUIVI")

            # Texte simple : ni emoji ni caractere exotique (cause des messages vides)
            lignes = []
            lignes.append("SURVEILLANCE - " + entete)
            lignes.append(str(sig.get("pair") or "?") + " | bloque il y a " + str(age_min) + " min")
            lignes.append("Score " + str(sig.get("score") or "?") +
                          " | consensus " + str(sig.get("consensus_long")) +
                          "/" + str(sig.get("consensus_short")))
            if message:
                lignes.append(str(message))
            if statut == "RELANCER":
                lignes.append("-> Relance une analyse sur cette paire.")
            elif statut == "ABANDON_SL_DEPASSE":
                lignes.append("-> Le systeme a eu RAISON de bloquer.")
            elif statut == "ABANDON_TP_ATTEINT":
                lignes.append("-> Le systeme a eu TORT de bloquer.")
            txt = "\n".join(lignes)

            try:
                send_tg(TELEGRAM_CHAT_ID, txt)
            except Exception as e:
                print(f"Surveillance Telegram: {e}")

            journaliser_surveillance(sig, statut, message)
            sig["statut"] = statut
            _ecrire_surveillance(tid, sig, statut)   # sort de EN_SURVEILLANCE
            print(f"Surveillance #{tid} -> {statut}")
        except Exception as e:
            print(f"Surveillance signal {tid}: {e}")

@app.before_request
def _hook_surveillance():
    """Chaque requete au backend declenche une verification si le delai est ecoule."""
    try:
        if request.path not in ("/", "/debug"):
            verifier_signaux_en_attente()
    except Exception:
        pass

@app.route("/surveiller/verifier", methods=["GET"])
def forcer_verification():
    """Forcer une verification immediate (utile pour tester)."""
    verifier_signaux_en_attente(force=True)
    return jsonify({"ok": True, "restants": len(_lire_surveillance())})

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
