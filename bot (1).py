#!/usr/bin/env python3
"""
ElieIA Trading Bot v3 — Version Finale Optimale
Sources : Yahoo Finance + Polygon.io + Finnhub + Alpha Vantage + Reuters RSS
Déduplication intelligente multi-sources
"""

import asyncio
import aiohttp
import base64
import csv
import hashlib
import io
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime, date, time, timedelta
from pathlib import Path

import pytz
from telegram import Bot, Update, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.constants import ParseMode

# ══════════════════════════════════════════════════════
# CONFIGURATION
# ══════════════════════════════════════════════════════
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "TON_TOKEN")
CHAT_ID        = os.getenv("CHAT_ID",        "TON_CHAT_ID")
FINNHUB_KEY    = os.getenv("FINNHUB_KEY",    "d74h03pr01qno4q1nmng")
ALPHA_KEY      = os.getenv("ALPHA_KEY",      "QGI9OV26C79BW731")
CLAUDE_KEY     = os.getenv("CLAUDE_KEY",     "TA_CLE_CLAUDE")
POLYGON_KEY    = os.getenv("POLYGON_KEY",    "TA_CLE_POLYGON")  # polygon.io gratuit

BRUSSELS = pytz.timezone("Europe/Brussels")
JOURNAL_FILE = Path("journal.csv")

logging.basicConfig(
    format="%(asctime)s — %(levelname)s — %(message)s",
    level=logging.INFO
)
log = logging.getLogger(__name__)

# ══════════════════════════════════════════════════════
# ÉTAT GLOBAL
# ══════════════════════════════════════════════════════
state = {
    # Prix
    "xau_price":       None,
    "xau_prev":        None,
    "eurusd":          None,
    "dxy":             None,
    "sp500":           None,
    "us10y":           None,
    # Déduplication news — hash du titre normalisé
    "seen_hashes":     set(),
    # Alertes
    "last_briefing":   None,
    "last_kz_alert":   None,
    "last_weekly":     None,
    # Trade actif
    "active_trade":    None,
    "watched_levels":  [],
}

# ══════════════════════════════════════════════════════
# SYSTEM PROMPT SMC
# ══════════════════════════════════════════════════════
SMC_SYSTEM = """Tu es ElieIA, assistant de trading SMC d'Elie Lubanguku, trader belge XAU/USD et EUR/USD.

PROFIL : Compte 10 000€ | Risque 1% = 100€ | Lot 0.01 = 1pip = 1€ | Max 3 trades/jour

KILL ZONES (UTC+2) : London 10h-12h | NY 15h30-17h30 | Dead Zone 12h-15h30 | Pre-London 07h-09h

COULEURS OB KASPER :
🟢 Vert=M30 | ⬜ Gris=H1 | ⬛ Noir=H4 | 🩷 Rose=M45 | 🔵 Bleu=M15 | 🟣 Mauve=M5 (déclencheur) | 🔴 Rouge=M1

RÈGLES STRICTES :
- OB M5 Mauve = déclencheur primaire | SL long = low OB M5 -4 pips | SL short = high OB M5 +4 pips
- Zone épuisée (2+ retests) = -15pts | SafeHaven XAU↑/EUR↓ = -15pts
- Ne JAMAIS inventer des confirmations non visibles sur le chart
- OTE = zone 0.618-0.786 Fibonacci sur le move

SCORING /215 :
Technique /140 : FVG+10, IFVG+10, EQL+15, OTE+15, DoubleOTE+25, PremDisc+10, PDH/PDL+10, AsiaRange+10, LiqSweep+15, H4+10, SilverBullet+10, Judas+10, ChoCH+10, POC+10, BougieOB+10, DivRSI+10, Overflow+10
Macro /75 : DiffFedBCE+10, FedTon+5, BCETon+5, US10Y+5, CPI+5, PCE+5, InflDiff+5, GDP+5, ISM+5, NFP+5, DXY+10, RetailContrarian+10, COT+5, SP500+5
Pénalités : SafeHaven-15, AnnonceProche-10, MacroOppose-10, ZoneEpuisee2x-15

SEUILS : <41 SKIP | 41-70 demi-taille | 71-120 normal | 120-170 fort | 170+ parfait

FORMAT TELEGRAM (mobile, concis, max 20 lignes) :
Biais → OB identifiés (couleur+TF) → Score /215 → GO/SKIP → Entrée/SL/TP si valide → Confiance /10
STRICT : ne pas gonfler le score. Si pas de setup = SKIP clair."""

# ══════════════════════════════════════════════════════
# DÉDUPLICATION INTELLIGENTE
# ══════════════════════════════════════════════════════
def news_hash(title: str) -> str:
    """Hash normalisé d'un titre pour déduplication cross-sources"""
    # Normalise : minuscules, retire ponctuation, garde les mots clés
    normalized = re.sub(r'[^\w\s]', '', title.lower())
    words = sorted(normalized.split())[:8]  # 8 premiers mots triés
    return hashlib.md5(" ".join(words).encode()).hexdigest()

def is_duplicate(title: str) -> bool:
    h = news_hash(title)
    if h in state["seen_hashes"]:
        return True
    state["seen_hashes"].add(h)
    # Limite mémoire
    if len(state["seen_hashes"]) > 1000:
        lst = list(state["seen_hashes"])
        state["seen_hashes"] = set(lst[-500:])
    return False

# ══════════════════════════════════════════════════════
# FILTRES NEWS
# ══════════════════════════════════════════════════════
CRITICAL = [
    "iran","hormuz","ceasefire","nuclear","fed","fomc","powell",
    "rate decision","rate hike","rate cut","ecb","bce","lagarde",
    "cpi","inflation","nfp","payroll","gdp","gold","xau","dxy",
    "war","strike","attack","trump","oil","israel","hezbollah",
    "lebanon","beige book","ppi","jobs report","treasury yield",
    "federal reserve","interest rate","monetary policy",
]
IMPORTANT = [
    "unemployment","manufacturing","pmi","dollar","crude",
    "s&p","nasdaq","china","russia","opec","recession",
    "yield","bond","sanctions","geopolit",
]

def news_score(title: str, summary: str = "") -> int:
    t = (title + " " + summary).lower()
    c = sum(1 for k in CRITICAL  if k in t)
    i = sum(1 for k in IMPORTANT if k in t)
    if c >= 2: return 3   # CRITIQUE
    if c == 1: return 2   # IMPORTANT
    if i >= 1: return 1   # À NOTER
    return 0

def gold_impact(text: str) -> str:
    t = text.lower()
    if any(k in t for k in ["ceasefire","peace deal","hormuz open","rate cut","dollar weak","fed cut"]):
        return "🔴 Baissier or"
    if any(k in t for k in ["war","attack","strike","inflation high","safe haven","nuclear","escalat"]):
        return "🟢 Haussier or"
    if any(k in t for k in ["fed hold","hawkish","no cut","rate hike","strong dollar","tightening"]):
        return "🔴 Baissier or (Fed)"
    if any(k in t for k in ["dovish","rate cut","easing","weak dollar","dollar falls"]):
        return "🟢 Haussier or (Fed dovish)"
    return ""

# ══════════════════════════════════════════════════════
# SOURCES PRIX — Yahoo Finance (primaire, ~15s)
# ══════════════════════════════════════════════════════
async def yahoo_price(session: aiohttp.ClientSession, symbol: str) -> float | None:
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1m&range=5m"
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; ElieIA/1.0)"}
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=8)) as r:
            if r.status != 200: return None
            data = await r.json()
            closes = data["chart"]["result"][0]["indicators"]["quote"][0]["close"]
            closes = [c for c in closes if c is not None]
            return round(closes[-1], 3) if closes else None
    except:
        return None

async def alpha_price(session: aiohttp.ClientSession, from_c: str, to_c: str) -> float | None:
    url = (f"https://www.alphavantage.co/query?function=CURRENCY_EXCHANGE_RATE"
           f"&from_currency={from_c}&to_currency={to_c}&apikey={ALPHA_KEY}")
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=8)) as r:
            data = await r.json()
            rate = data.get("Realtime Currency Exchange Rate",{}).get("5. Exchange Rate")
            return float(rate) if rate else None
    except:
        return None

async def get_xau(session: aiohttp.ClientSession) -> float | None:
    p = await yahoo_price(session, "XAUUSD=X")
    if not p: p = await alpha_price(session, "XAU", "USD")
    return p

async def get_eurusd(session: aiohttp.ClientSession) -> float | None:
    p = await yahoo_price(session, "EURUSD=X")
    if not p: p = await alpha_price(session, "EUR", "USD")
    return p

async def get_dxy(session: aiohttp.ClientSession) -> float | None:
    p = await yahoo_price(session, "DX-Y.NYB")
    if not p:
        eur = await get_eurusd(session)
        if eur: p = round(1/eur * 58.6, 3)  # approximation DXY
    return p

async def get_sp500(session: aiohttp.ClientSession) -> float | None:
    return await yahoo_price(session, "^GSPC")

async def get_us10y(session: aiohttp.ClientSession) -> float | None:
    return await yahoo_price(session, "^TNX")

async def refresh_all_prices(session: aiohttp.ClientSession):
    """Met à jour tous les prix en parallèle"""
    results = await asyncio.gather(
        get_xau(session),
        get_eurusd(session),
        get_dxy(session),
        get_sp500(session),
        get_us10y(session),
        return_exceptions=True
    )
    xau, eur, dxy, sp5, t10y = results

    state["xau_prev"]  = state["xau_price"]
    state["xau_price"] = xau   if isinstance(xau,  float) else state["xau_price"]
    state["eurusd"]    = eur   if isinstance(eur,  float) else state["eurusd"]
    state["dxy"]       = dxy   if isinstance(dxy,  float) else state["dxy"]
    state["sp500"]     = sp5   if isinstance(sp5,  float) else state["sp500"]
    state["us10y"]     = t10y  if isinstance(t10y, float) else state["us10y"]

# ══════════════════════════════════════════════════════
# SOURCE NEWS 1 — Finnhub
# ══════════════════════════════════════════════════════
async def fetch_finnhub(session: aiohttp.ClientSession) -> list[dict]:
    url = f"https://finnhub.io/api/v1/news?category=general&token={FINNHUB_KEY}"
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()
            return [{"title": n.get("headline",""), "summary": n.get("summary",""),
                     "source": f"Finnhub/{n.get('source','')}", "url": n.get("url","")}
                    for n in (data if isinstance(data, list) else [])[:30]]
    except Exception as e:
        log.warning(f"Finnhub: {e}")
        return []

# ══════════════════════════════════════════════════════
# SOURCE NEWS 2 — Polygon.io
# ══════════════════════════════════════════════════════
async def fetch_polygon(session: aiohttp.ClientSession) -> list[dict]:
    if not POLYGON_KEY or POLYGON_KEY == "TA_CLE_POLYGON":
        return []
    url = (f"https://api.polygon.io/v2/reference/news"
           f"?limit=20&order=desc&sort=published_utc&apiKey={POLYGON_KEY}")
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()
            results = data.get("results", [])
            return [{"title": n.get("title",""), "summary": n.get("description",""),
                     "source": f"Polygon/{n.get('publisher',{}).get('name','')}", "url": n.get("article_url","")}
                    for n in results]
    except Exception as e:
        log.warning(f"Polygon: {e}")
        return []

# ══════════════════════════════════════════════════════
# SOURCE NEWS 3 — Reuters RSS (temps réel gratuit)
# ══════════════════════════════════════════════════════
RSS_FEEDS = [
    ("https://feeds.reuters.com/reuters/businessNews", "Reuters Business"),
    ("https://feeds.reuters.com/reuters/worldNews",    "Reuters World"),
    ("https://feeds.reuters.com/reuters/UKTopNews",    "Reuters Top"),
]

async def fetch_rss(session: aiohttp.ClientSession, url: str, source: str) -> list[dict]:
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
            text = await r.text()
        root  = ET.fromstring(text)
        items = root.findall(".//item")
        news  = []
        for item in items[:20]:
            title   = item.findtext("title",   "")
            summary = item.findtext("description", "")
            link    = item.findtext("link",    "")
            # Retire les tags HTML du summary
            summary = re.sub(r'<[^>]+>', '', summary)[:300]
            news.append({"title": title, "summary": summary, "source": source, "url": link})
        return news
    except Exception as e:
        log.warning(f"RSS {source}: {e}")
        return []

async def fetch_all_rss(session: aiohttp.ClientSession) -> list[dict]:
    tasks = [fetch_rss(session, url, src) for url, src in RSS_FEEDS]
    results = await asyncio.gather(*tasks, return_exceptions=True)
    all_news = []
    for r in results:
        if isinstance(r, list):
            all_news.extend(r)
    return all_news

# ══════════════════════════════════════════════════════
# SOURCE NEWS 4 — Alpha Vantage News Sentiment
# ══════════════════════════════════════════════════════
async def fetch_alphavantage_news(session: aiohttp.ClientSession) -> list[dict]:
    url = (f"https://www.alphavantage.co/query?function=NEWS_SENTIMENT"
           f"&tickers=FOREX:XAU&sort=LATEST&limit=20&apikey={ALPHA_KEY}")
    try:
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()
            feed = data.get("feed", [])
            return [{"title": n.get("title",""), "summary": n.get("summary",""),
                     "source": f"AlphaVantage/{n.get('source','')}", "url": n.get("url","")}
                    for n in feed[:20]]
    except Exception as e:
        log.warning(f"AV News: {e}")
        return []

# ══════════════════════════════════════════════════════
# AGRÉGATION TOUTES SOURCES
# ══════════════════════════════════════════════════════
async def fetch_all_news(session: aiohttp.ClientSession) -> list[dict]:
    """Récupère et déduplique les news de toutes les sources"""
    tasks = [
        fetch_finnhub(session),
        fetch_polygon(session),
        fetch_all_rss(session),
        fetch_alphavantage_news(session),
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    all_news = []
    for r in results:
        if isinstance(r, list):
            all_news.extend(r)

    # Déduplication intelligente
    unique = []
    for n in all_news:
        title = n.get("title","")
        if not title or len(title) < 10: continue
        if not is_duplicate(title):
            unique.append(n)

    return unique

# ══════════════════════════════════════════════════════
# CLAUDE API
# ══════════════════════════════════════════════════════
async def claude_call(session: aiohttp.ClientSession, messages: list, max_tokens: int = 1000) -> str:
    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "x-api-key": CLAUDE_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": "claude-sonnet-4-20250514",
        "max_tokens": max_tokens,
        "system": SMC_SYSTEM,
        "messages": messages,
    }
    try:
        async with session.post(url, headers=headers, json=payload,
                                timeout=aiohttp.ClientTimeout(total=30)) as r:
            data = await r.json()
            return data["content"][0]["text"]
    except Exception as e:
        return f"❌ Erreur Claude API : {e}"

async def analyse_chart(session: aiohttp.ClientSession, image_b64: str, context: str) -> str:
    messages = [{
        "role": "user",
        "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": image_b64}},
            {"type": "text",  "text": (
                f"Analyse ce chart XAU/USD selon ma méthodologie SMC. {context}\n\n"
                "Donne-moi en format Telegram mobile :\n"
                "1. Biais (haussier/baissier/neutre)\n"
                "2. OB identifiés avec couleur Kasper exacte\n"
                "3. FVG/IFVG visibles\n"
                "4. BOS/ChoCH confirmés\n"
                "5. Liquidité ($$$ sweepés/cibles)\n"
                "6. Score /215 STRICT (ne pas gonfler)\n"
                "7. GO ou SKIP avec raison claire\n"
                "8. Si GO : Entrée / SL / TP / RR / Lots\n"
                "9. Confiance /10\n\n"
                "IMPORTANT : Si un critère n'est pas visible sur le chart, ne le score pas."
            )}
        ]
    }]
    return await claude_call(session, messages, max_tokens=1200)

async def macro_briefing(session: aiohttp.ClientSession) -> str:
    xau   = state["xau_price"]
    eur   = state["eurusd"]
    dxy   = state["dxy"]
    sp5   = state["sp500"]
    t10y  = state["us10y"]
    now   = datetime.now(BRUSSELS)

    # Corrélation regime
    regime = "Indéterminé"
    if xau and state["xau_prev"]:
        xau_up = xau > state["xau_prev"]
        if eur:
            eur_prev = state.get("eurusd_prev", eur)
            eur_up = eur > eur_prev
            if xau_up and not eur_up:     regime = "⚠️ SafeHaven Divergence (-15pts si trade)"
            elif xau_up and eur_up:       regime = "🛡️ Risk-Off Aligné"
            elif not xau_up and eur_up:   regime = "📈 Risk-On Normal"
            elif not xau_up and not eur_up: regime = "💥 Crise/Dollar Fort"

    context = f"""
DONNÉES TEMPS RÉEL ({now.strftime('%H:%M')} Bruxelles) :
- XAU/USD : {xau or 'N/A'}
- EUR/USD : {eur or 'N/A'}
- DXY : {dxy or 'N/A'}
- S&P500 : {sp5 or 'N/A'}
- US 10Y : {t10y or 'N/A'}%
- Régime corrélation : {regime}
- Date : {now.strftime('%A %d %B %Y')}
"""
    messages = [{
        "role": "user",
        "content": (
            f"Génère le briefing macro complet pour aujourd'hui.\n{context}\n\n"
            "Inclus OBLIGATOIREMENT en format Telegram (émojis, concis, max 25 lignes) :\n"
            "1. Prix XAU + variation + régime corrélation\n"
            "2. DXY + EUR/USD + US10Y + S&P500\n"
            "3. Décisions Fed/BCE récentes + impact\n"
            "4. Inflation (CPI 3.3%, Core 2.6%) + impact\n"
            "5. Données macro d'aujourd'hui importantes\n"
            "6. Géopolitique Iran/Hormuz status actuel\n"
            "7. Biais directionnel XAU du jour (haussier/baissier/neutre)\n"
            "8. Score macro /75 estimé\n"
            "9. Niveaux clés XAU à surveiller aujourd'hui\n"
            "10. Un conseil de trading pour la journée"
        )
    }]
    return await claude_call(session, messages, max_tokens=1200)

# ══════════════════════════════════════════════════════
# JOURNAL CSV
# ══════════════════════════════════════════════════════
HEADERS = ["Date","Heure","Pair","Direction","Entrée","SL","TP","Résultat","Pips","EUR","KZ","Notes"]

def init_journal():
    if not JOURNAL_FILE.exists():
        with open(JOURNAL_FILE, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(HEADERS)

def add_trade(t: dict):
    init_journal()
    with open(JOURNAL_FILE, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([
            t.get("date",""), t.get("heure",""), t.get("pair","XAU/USD"),
            t.get("direction",""), t.get("entree",""), t.get("sl",""), t.get("tp",""),
            t.get("resultat","⏳"), t.get("pips",""), t.get("eur",""),
            t.get("kz",""), t.get("notes",""),
        ])

def get_stats(period: str = "month") -> dict:
    init_journal()
    rows = []
    with open(JOURNAL_FILE, "r", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            rows.append(r)

    if period == "week":
        today = date.today()
        mon   = (today - timedelta(days=today.weekday())).strftime("%d/%m/%Y")
        rows  = [r for r in rows if r.get("Date","") >= mon]
    elif period == "month":
        m = date.today().strftime("%m/%Y")
        rows = [r for r in rows if r.get("Date","")[-7:] == m]

    wins   = sum(1 for r in rows if "WIN"  in r.get("Résultat",""))
    losses = sum(1 for r in rows if "LOSS" in r.get("Résultat",""))
    be     = sum(1 for r in rows if "BE"   in r.get("Résultat",""))
    opens  = sum(1 for r in rows if "⏳"   in r.get("Résultat",""))

    def to_float(v):
        try: return float(v or 0)
        except: return 0.0

    pips = sum(to_float(r.get("Pips","")) for r in rows)
    eur  = sum(to_float(r.get("EUR",""))  for r in rows)
    wr   = round(wins/(wins+losses)*100) if wins+losses > 0 else 0

    kz_w = {}
    for r in rows:
        if "WIN" in r.get("Résultat",""):
            k = r.get("KZ","")
            kz_w[k] = kz_w.get(k,0)+1
    best_kz = max(kz_w, key=kz_w.get) if kz_w else "N/A"

    # RR moyen
    rrs = []
    for r in rows:
        try:
            e = float(r.get("Entrée","0"))
            s = float(r.get("SL","0"))
            t = float(r.get("TP","0"))
            if e and s and t:
                risk   = abs(e-s)
                reward = abs(e-t)
                if risk > 0: rrs.append(reward/risk)
        except: pass
    avg_rr = round(sum(rrs)/len(rrs), 1) if rrs else 0

    return {
        "total": len(rows), "wins": wins, "losses": losses, "be": be, "open": opens,
        "winrate": wr, "pips": pips, "eur": eur, "best_kz": best_kz, "avg_rr": avg_rr
    }

def parse_trade(text: str) -> dict | None:
    text = text.lower().strip()
    t = {}
    if "short" in text:  t["direction"] = "SHORT"
    elif "long" in text: t["direction"] = "LONG"
    else: return None

    for pat, key in [
        (r"(?:short|long)\s+([\d.]+)", "entree"),
        (r"(?:entree|entrée|entry|@)\s*([\d.]+)", "entree"),
        (r"sl\s*([\d.]+)", "sl"),
        (r"tp\s*([\d.]+)", "tp"),
    ]:
        m = re.search(pat, text)
        if m and key not in t: t[key] = m.group(1)

    if   "win"       in text: t["resultat"] = "✅ WIN"
    elif "loss"      in text: t["resultat"] = "❌ LOSS"
    elif "be"        in text or "breakeven" in text: t["resultat"] = "⚖️ BE"
    else:                      t["resultat"] = "⏳ OPEN"

    m = re.search(r"([+-]?\d+)\s*pips?", text)
    if m:
        p = int(m.group(1))
        t["pips"] = p
        t["eur"]  = p

    now = datetime.now(BRUSSELS)
    t["date"]  = now.strftime("%d/%m/%Y")
    t["heure"] = now.strftime("%H:%M")
    t["kz"]    = get_kz(now)
    t["notes"] = re.sub(r"(short|long|sl|tp|win|loss|be|pips?|[\d.]+)", "", text).strip()
    return t

# ══════════════════════════════════════════════════════
# HELPERS KZ
# ══════════════════════════════════════════════════════
def get_kz(dt: datetime) -> str:
    t = dt.time()
    if time(7,0)  <= t <= time(9,0):   return "🌅 Pre-London"
    if time(10,0) <= t <= time(12,0):  return "🇬🇧 London Kill Zone"
    if time(12,0) <  t <  time(15,30): return "😴 Dead Zone"
    if time(15,30)<= t <= time(17,30): return "🇺🇸 NY Kill Zone"
    return "🌙 Hors session"

# ══════════════════════════════════════════════════════
# ENVOI TELEGRAM
# ══════════════════════════════════════════════════════
async def send(bot: Bot, text: str, chat_id: str = None):
    cid = chat_id or CHAT_ID
    for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
        try:
            await bot.send_message(chat_id=cid, text=chunk, parse_mode=ParseMode.HTML)
            await asyncio.sleep(0.3)
        except Exception as e:
            log.error(f"Send: {e}")

# ══════════════════════════════════════════════════════
# ALERTES AUTOMATIQUES
# ══════════════════════════════════════════════════════
async def alert_price(bot: Bot):
    xau  = state["xau_price"]
    prev = state["xau_prev"]
    if not xau or not prev: return

    change_pct  = (xau - prev) / prev * 100
    change_pips = abs(xau - prev)
    if abs(change_pct) < 0.5 and change_pips < 25: return

    now  = datetime.now(BRUSSELS)
    icon = "🚀" if change_pct > 0 else "📉"
    col  = "🟢" if change_pct > 0 else "🔴"

    lines = [
        f"⚡️ <b>ALERTE PRIX XAU/USD</b>",
        f"{col} {icon} {change_pct:+.2f}% — {change_pips:.0f} pips",
        f"💰 Prix : <b>${xau:,.2f}</b>",
        f"💶 EUR/USD : {state['eurusd'] or 'N/A'}",
        f"💵 DXY : {state['dxy'] or 'N/A'}",
        f"⏰ {now.strftime('%H:%M')} — {get_kz(now)}",
    ]

    # Alerte BE trade actif
    at = state["active_trade"]
    if at:
        entry = at.get("entry", 0)
        d     = at.get("direction","")
        profit = (entry - xau) if d == "SHORT" else (xau - entry)
        if profit >= 20:
            lines.append(f"\n⚠️ <b>BREAKEVEN !</b> Trade {d} en +{profit:.0f} pips")

    # Niveaux surveillés
    for lvl in state["watched_levels"]:
        if abs(xau - lvl["price"]) <= 3:
            lines.append(f"🎯 Niveau <b>{lvl['price']}</b> touché ! {lvl.get('note','')}")

    await send(bot, "\n".join(lines))

async def alert_news(bot: Bot, session: aiohttp.ClientSession):
    news = await fetch_all_news(session)
    count = 0
    for n in news:
        if count >= 5: break  # Max 5 alertes par cycle
        title   = n.get("title","")
        summary = n.get("summary","")
        source  = n.get("source","")
        sc = news_score(title, summary)
        if sc < 2: continue

        label  = {3:"🔴 <b>CRITIQUE</b>", 2:"🟠 <b>IMPORTANT</b>"}.get(sc,"")
        now    = datetime.now(BRUSSELS)
        impact = gold_impact(title + " " + summary)

        lines = [label, f"📰 {title[:150]}"]
        if summary and len(summary) > 20:
            lines.append(f"<i>{summary[:200]}</i>")
        lines.append(f"🏦 {source} — {now.strftime('%H:%M')} {get_kz(now)}")
        if impact: lines.append(f"🥇 {impact}")

        await send(bot, "\n".join(lines))
        count += 1
        await asyncio.sleep(2)

async def alert_kz(bot: Bot):
    now  = datetime.now(BRUSSELS)
    hour = now.strftime("%H:%M")

    alerts = {
        "09:30": "⏰ <b>London KZ dans 30 min</b>\nPrépare ton analyse — 10h00 arrive !",
        "10:00": f"🇬🇧 <b>LONDON KILL ZONE OUVERTE</b>\nXAU : ${state['xau_price']:,.0f}\n{get_kz(now)}",
        "15:00": "⏰ <b>NY Kill Zone dans 30 min</b>\nPrépare ton analyse — 15h30 arrive !",
        "15:30": f"🇺🇸 <b>NY KILL ZONE OUVERTE</b>\nXAU : ${state['xau_price']:,.0f}\n{get_kz(now)}",
        "17:00": "⚠️ <b>NY KZ ferme dans 30 min</b>\nWeekend = gap risk Iran. Pense à fermer.",
        "17:30": "🚫 <b>NY Kill Zone fermée</b>\nPlus de trades. Gère tes positions.",
    }

    if hour in alerts and state["last_kz_alert"] != hour:
        state["last_kz_alert"] = hour
        price = state["xau_price"]
        msg = alerts[hour]
        if price and "{" not in msg:  # Pas déjà formaté
            msg = msg
        await send(bot, msg)

async def morning_briefing(bot: Bot, session: aiohttp.ClientSession):
    now   = datetime.now(BRUSSELS)
    today = now.date()
    if state["last_briefing"] == today: return
    if not (time(9,0) <= now.time() <= time(9,10)): return
    state["last_briefing"] = today

    await send(bot, "☀️ <b>Génération briefing matinal...</b>")
    briefing = await macro_briefing(session)
    await send(bot, f"☀️ <b>BRIEFING ElieIA — {now.strftime('%d/%m/%Y')}</b>\n\n{briefing}")

    # Stats semaine
    s = get_stats("week")
    if s["total"] > 0:
        sign = "+" if s["eur"] >= 0 else ""
        await send(bot,
            f"📊 <b>Semaine en cours :</b> {s['wins']}W/{s['losses']}L — "
            f"Winrate {s['winrate']}% — {sign}{s['eur']:.0f}€ — RR moy 1:{s['avg_rr']}"
        )

async def weekly_report(bot: Bot):
    now = datetime.now(BRUSSELS)
    key = now.strftime("%Y-W%W")
    if now.weekday() != 4: return
    if not (time(17,30) <= now.time() <= time(17,40)): return
    if state["last_weekly"] == key: return
    state["last_weekly"] = key

    s = get_stats("week")
    if s["total"] == 0:
        await send(bot, "📊 Pas de trades cette semaine.")
        return

    sign = "+" if s["eur"] >= 0 else ""
    emoji = "🟢" if s["winrate"] >= 60 else "🟡" if s["winrate"] >= 40 else "🔴"

    await send(bot,
        f"📊 <b>RAPPORT HEBDOMADAIRE</b>\n\n"
        f"Trades : {s['total']} ({s['wins']}W / {s['losses']}L / {s['be']}BE)\n"
        f"{emoji} Winrate : <b>{s['winrate']}%</b>\n"
        f"📈 Pips : {'+' if s['pips']>=0 else ''}{s['pips']:.0f}\n"
        f"💰 P&L : <b>{sign}{s['eur']:.0f}€</b>\n"
        f"⚖️ RR moyen : 1:{s['avg_rr']}\n"
        f"🏆 Meilleure KZ : {s['best_kz']}\n\n"
        f"Bon weekend Elie ! 💪"
    )
    if JOURNAL_FILE.exists():
        with open(JOURNAL_FILE, "rb") as f:
            await bot.send_document(
                chat_id=CHAT_ID,
                document=InputFile(f, filename=f"journal_{now.strftime('%Y%m%d')}.csv"),
                caption="📁 Journal complet de la semaine"
            )

# ══════════════════════════════════════════════════════
# COMMANDES
# ══════════════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    await update.message.reply_text(
        f"🎯 <b>ElieIA Bot v3 — Actif !</b>\n\n"
        f"Chat ID : <code>{cid}</code>\n\n"
        f"<b>Commandes :</b>\n"
        f"/prix — XAU + EUR/USD + DXY + S&P500 + US10Y\n"
        f"/kz — Kill Zone + countdown\n"
        f"/briefing — Briefing macro complet\n"
        f"/trade — Enregistrer un trade\n"
        f"/stats — Stats du mois\n"
        f"/stats week — Stats semaine\n"
        f"/journal — Recevoir le CSV\n"
        f"/surveille 4838 [note] — Surveiller niveau\n"
        f"/niveaux — Voir niveaux surveillés\n"
        f"/actif short 4823 — Alerte BE auto\n"
        f"/aide — Aide complète\n\n"
        f"📸 <b>Envoie une photo de chart</b> → analyse SMC /215 !",
        parse_mode=ParseMode.HTML
    )

async def cmd_prix(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(BRUSSELS)

    # Corrélation regime
    regime = "N/A"
    xau  = state["xau_price"]
    eur  = state["eurusd"]
    prev = state["xau_prev"]
    if xau and eur and prev:
        xau_up = xau > prev
        eur_up = eur > (state.get("eurusd_prev") or eur)
        if   xau_up and not eur_up:  regime = "⚠️ SafeHaven Divergence"
        elif xau_up and eur_up:      regime = "🛡️ Risk-Off Aligné"
        elif not xau_up and eur_up:  regime = "📈 Risk-On Normal"
        else:                         regime = "💥 Crise / Dollar Fort"

    await update.message.reply_text(
        f"📊 <b>MARCHÉS EN DIRECT</b>\n\n"
        f"🥇 XAU/USD : <b>${xau:,.2f}</b>\n"
        f"💶 EUR/USD : {eur}\n"
        f"💵 DXY : {state['dxy']}\n"
        f"📈 S&P500 : {state['sp500']}\n"
        f"📉 US 10Y : {state['us10y']}%\n\n"
        f"🔗 Régime : {regime}\n"
        f"⏰ {now.strftime('%H:%M')} — {get_kz(now)}" if xau else "❌ Données non disponibles",
        parse_mode=ParseMode.HTML
    )

async def cmd_kz(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(BRUSSELS)
    kz  = get_kz(now)
    t   = now.time()
    countdown = ""
    targets = [time(10,0), time(15,30)]
    for target in targets:
        if t < target:
            delta = (datetime.combine(date.today(), target) -
                     datetime.combine(date.today(), t)).seconds // 60
            name  = "London KZ" if target == time(10,0) else "NY KZ"
            countdown = f"\n⏳ {name} dans <b>{delta} min</b>"
            break

    await update.message.reply_text(
        f"⏰ <b>{now.strftime('%H:%M')}</b> Bruxelles\n{kz}{countdown}",
        parse_mode=ParseMode.HTML
    )

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    period = (ctx.args[0] if ctx.args else "month")
    s = get_stats(period)
    label = {"week":"cette semaine","month":"ce mois","all":"total"}.get(period,"ce mois")
    if s["total"] == 0:
        await update.message.reply_text(f"📊 Pas de trades {label}.")
        return
    sign  = "+" if s["eur"] >= 0 else ""
    emoji = "🟢" if s["winrate"] >= 60 else "🟡" if s["winrate"] >= 40 else "🔴"
    await update.message.reply_text(
        f"📊 <b>STATS {label.upper()}</b>\n\n"
        f"Trades : {s['total']} ({s['wins']}W / {s['losses']}L / {s['be']}BE)\n"
        f"{emoji} Winrate : <b>{s['winrate']}%</b>\n"
        f"⚖️ RR moyen : 1:{s['avg_rr']}\n"
        f"📈 Pips : {'+' if s['pips']>=0 else ''}{s['pips']:.0f}\n"
        f"💰 P&L : <b>{sign}{s['eur']:.0f}€</b>\n"
        f"🏆 Meilleure KZ : {s['best_kz']}\n\n"
        f"/stats week | /stats month | /stats all",
        parse_mode=ParseMode.HTML
    )

async def cmd_trade(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await update.message.reply_text(
            "📝 <b>Formats acceptés :</b>\n"
            "/trade short 4823 sl 4830 tp 4771\n"
            "/trade long 4798 sl 4791 tp 4820 win +22pips\n"
            "/trade short 4815 sl 4822 tp 4787 loss -7pips\n"
            "/trade short 4810 be",
            parse_mode=ParseMode.HTML
        )
        return
    t = parse_trade(text)
    if not t:
        await update.message.reply_text("❌ Format non reconnu.")
        return
    add_trade(t)
    rr = ""
    try:
        e = float(t.get("entree",0)); s = float(t.get("sl",0)); tp = float(t.get("tp",0))
        if e and s and tp:
            r = abs(e-s); rew = abs(e-tp)
            if r > 0: rr = f"\nRR : 1:{rew/r:.1f}"
    except: pass

    await update.message.reply_text(
        f"✅ <b>Trade enregistré</b>\n\n"
        f"{t.get('direction')} @ {t.get('entree','')}\n"
        f"SL : {t.get('sl','')} | TP : {t.get('tp','')}\n"
        f"Résultat : {t.get('resultat','⏳')}\n"
        f"Pips : {t.get('pips','')} | P&L : {t.get('eur','')}€{rr}\n"
        f"KZ : {t.get('kz','')}",
        parse_mode=ParseMode.HTML
    )

async def cmd_journal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not JOURNAL_FILE.exists() or JOURNAL_FILE.stat().st_size < 50:
        await update.message.reply_text("📒 Journal vide.")
        return
    now = datetime.now(BRUSSELS)
    with open(JOURNAL_FILE, "rb") as f:
        await update.message.reply_document(
            document=InputFile(f, filename=f"journal_{now.strftime('%Y%m%d')}.csv"),
            caption=f"📁 Journal ElieIA — {now.strftime('%d/%m/%Y')}"
        )

async def cmd_surveille(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage : /surveille 4838 résistance H4")
        return
    try:
        price = float(ctx.args[0])
        note  = " ".join(ctx.args[1:])
        state["watched_levels"].append({"price": price, "note": note})
        await update.message.reply_text(
            f"🎯 Niveau <b>{price}</b> surveillé\n{note}",
            parse_mode=ParseMode.HTML
        )
    except:
        await update.message.reply_text("❌ Exemple : /surveille 4838 résistance")

async def cmd_niveaux(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    lvls = state["watched_levels"]
    if not lvls:
        await update.message.reply_text("Aucun niveau surveillé. Utilise /surveille 4838")
        return
    lines = ["🎯 <b>Niveaux surveillés :</b>"]
    for i, l in enumerate(lvls):
        lines.append(f"{i+1}. {l['price']} — {l.get('note','')}")
    lines.append("\n/clearniveaux pour effacer")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)

async def cmd_clearniveaux(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    state["watched_levels"] = []
    await update.message.reply_text("✅ Niveaux effacés")

async def cmd_actif(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage : /actif short 4823 | /actif off")
        return
    if ctx.args[0].lower() == "off":
        state["active_trade"] = None
        await update.message.reply_text("✅ Trade actif désactivé")
        return
    try:
        d = ctx.args[0].upper(); e = float(ctx.args[1])
        state["active_trade"] = {"direction": d, "entry": e}
        await update.message.reply_text(
            f"⚡️ Trade actif : <b>{d} @ {e}</b>\n🔔 Alerte BE à +20 pips",
            parse_mode=ParseMode.HTML
        )
    except:
        await update.message.reply_text("❌ Format : /actif short 4823")

async def cmd_briefing(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Briefing macro en cours (~15s)...")
    async with aiohttp.ClientSession() as s:
        b = await macro_briefing(s)
    now = datetime.now(BRUSSELS)
    await update.message.reply_text(
        f"📊 <b>BRIEFING MACRO — {now.strftime('%H:%M')}</b>\n\n{b}",
        parse_mode=ParseMode.HTML
    )

async def cmd_aide(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎯 <b>ElieIA Bot v3 — Aide</b>\n\n"
        "<b>Marché :</b>\n"
        "/prix — XAU+EUR+DXY+SP500+US10Y+régime\n"
        "/kz — Kill Zone + countdown\n"
        "/briefing — Analyse macro complète\n\n"
        "<b>Journal :</b>\n"
        "/trade short 4823 sl 4830 tp 4771 win +32pips\n"
        "/stats | /stats week | /stats all\n"
        "/journal — Fichier CSV complet\n\n"
        "<b>Surveillance :</b>\n"
        "/surveille 4838 résistance H4\n"
        "/niveaux — Voir les niveaux actifs\n"
        "/clearniveaux — Effacer les niveaux\n"
        "/actif short 4823 — Alerte BE\n"
        "/actif off — Désactiver\n\n"
        "<b>Analyse :</b>\n"
        "📸 Photo de chart → analyse SMC /215\n\n"
        "<b>Alertes auto :</b>\n"
        "⚡️ XAU > 0.5% → alerte prix\n"
        "🔴/🟠 News filtrées 4 sources\n"
        "☀️ Briefing 9h00 auto\n"
        "⏰ Alerts 30min avant KZ\n"
        "⚠️ Alerte BE trade actif\n"
        "📊 Rapport vendredi 17h30",
        parse_mode=ParseMode.HTML
    )

# ══════════════════════════════════════════════════════
# HANDLER PHOTO
# ══════════════════════════════════════════════════════
async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Analyse SMC en cours...")
    photo = update.message.photo[-1]
    file  = await photo.get_file()
    data  = await file.download_as_bytearray()
    b64   = base64.b64encode(data).decode()
    now   = datetime.now(BRUSSELS)
    ctx_str = (
        f"{now.strftime('%H:%M')} heure belge. {get_kz(now)}. "
        f"XAU={state['xau_price']} EUR={state['eurusd']} DXY={state['dxy']}. "
        f"{update.message.caption or ''}"
    )
    async with aiohttp.ClientSession() as s:
        result = await analyse_chart(s, b64, ctx_str)
    await update.message.reply_text(
        f"📊 <b>ANALYSE SMC</b>\n\n{result}",
        parse_mode=ParseMode.HTML
    )

# ══════════════════════════════════════════════════════
# BOUCLE PRINCIPALE
# ══════════════════════════════════════════════════════
async def main_loop(app: Application):
    bot   = app.bot
    cycle = 0
    log.info("✅ Boucle principale démarrée")

    async with aiohttp.ClientSession() as session:
        while True:
            try:
                # Actualisation prix toutes les 3 min
                await refresh_all_prices(session)

                # Alerte prix
                await alert_price(bot)

                # News toutes les 3 min
                await alert_news(bot, session)

                # Alertes KZ
                await alert_kz(bot)

                # Briefing 9h
                await morning_briefing(bot, session)

                # Rapport hebdo vendredi
                await weekly_report(bot)

                cycle += 1
                await asyncio.sleep(180)  # 3 minutes

            except Exception as e:
                log.error(f"Loop error: {e}")
                await asyncio.sleep(60)

def main():
    app = (
        Application.builder()
        .token(TELEGRAM_TOKEN)
        .build()
    )
    for cmd, handler in [
        ("start",       cmd_start),
        ("prix",        cmd_prix),
        ("kz",          cmd_kz),
        ("stats",       cmd_stats),
        ("trade",       cmd_trade),
        ("journal",     cmd_journal),
        ("surveille",   cmd_surveille),
        ("niveaux",     cmd_niveaux),
        ("clearniveaux",cmd_clearniveaux),
        ("actif",       cmd_actif),
        ("briefing",    cmd_briefing),
        ("aide",        cmd_aide),
    ]:
        app.add_handler(CommandHandler(cmd, handler))

    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    log.info("🚀 ElieIA Bot v3 démarré")
    init_journal()
    
    async def run():
        async with app:
            await app.initialize()
            await app.start()
            asyncio.create_task(main_loop(app))
            await app.updater.start_polling(drop_pending_updates=True)
            await asyncio.Event().wait()
    
    asyncio.run(run())

if __name__ == "__main__":
    main()
