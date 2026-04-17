#!/usr/bin/env python3
"""ElieIA Trading Bot v3 Final — Sans Yahoo Finance"""

import asyncio, aiohttp, base64, csv, hashlib, logging, os, re
import xml.etree.ElementTree as ET
from datetime import datetime, date, time, timedelta
from pathlib import Path
import pytz
from telegram import Bot, Update, InputFile
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from telegram.constants import ParseMode

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "TON_TOKEN")
CHAT_ID        = os.getenv("CHAT_ID", "TON_CHAT_ID")
FINNHUB_KEY    = os.getenv("FINNHUB_KEY", "d74h03pr01qno4q1nmng")
ALPHA_KEY      = os.getenv("ALPHA_KEY", "QGI9OV26C79BW731")
CLAUDE_KEY     = os.getenv("CLAUDE_KEY", "TA_CLE_CLAUDE")

BRUSSELS = pytz.timezone("Europe/Brussels")
JOURNAL_FILE = Path("journal.csv")

logging.basicConfig(format="%(asctime)s — %(levelname)s — %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

state = {
    "xau_price": None, "xau_prev": None, "eurusd": None, "dxy": None,
    "seen_hashes": set(), "last_briefing": None, "last_kz_alert": None,
    "last_weekly": None, "active_trade": None, "watched_levels": [],
}

SMC_SYSTEM = """Tu es ElieIA, assistant trading SMC d'Elie Lubanguku, trader belge XAU/USD.
Compte 10 000€ | Risque 1% | Lot 0.01 = 1pip = 1€ | Max 3 trades/jour
Kill Zones UTC+2 : London 10h-12h | NY 15h30-17h30 | Dead Zone 12h-15h30
OB Kasper : 🟢M30 | ⬜H1 | ⬛H4 | 🩷M45 | 🔵M15 | 🟣M5 déclencheur | 🔴M1
Scoring /215 : Technique/140 + Macro/75. Zone épuisée 2+ retests = -15pts.
Format Telegram : concis, mobile, max 20 lignes. Score strict, ne jamais gonfler."""

def news_hash(title):
    n = re.sub(r'[^\w\s]', '', title.lower())
    return hashlib.md5(" ".join(sorted(n.split())[:8]).encode()).hexdigest()

def is_duplicate(title):
    h = news_hash(title)
    if h in state["seen_hashes"]: return True
    state["seen_hashes"].add(h)
    if len(state["seen_hashes"]) > 1000:
        state["seen_hashes"] = set(list(state["seen_hashes"])[-500:])
    return False

CRITICAL = ["iran","hormuz","ceasefire","nuclear","fed","fomc","powell","rate decision",
            "ecb","bce","lagarde","rate hike","rate cut","cpi","inflation","nfp",
            "payroll","gdp","gold","xau","dxy","war","strike","attack","trump","oil","israel"]
IMPORTANT = ["unemployment","manufacturing","pmi","dollar","crude","s&p","nasdaq","china","russia"]

def news_score(title, summary=""):
    t = (title + " " + summary).lower()
    c = sum(1 for k in CRITICAL if k in t)
    i = sum(1 for k in IMPORTANT if k in t)
    if c >= 2: return 3
    if c == 1: return 2
    if i >= 1: return 1
    return 0

def gold_impact(text):
    t = text.lower()
    if any(k in t for k in ["ceasefire","peace","rate cut","dollar weak"]): return "🔴 Baissier or"
    if any(k in t for k in ["war","attack","inflation","safe haven","nuclear"]): return "🟢 Haussier or"
    if any(k in t for k in ["hawkish","no cut","rate hike","strong dollar"]): return "🔴 Baissier (Fed)"
    return ""

async def av_price(session, from_c, to_c):
    try:
        url = (f"https://www.alphavantage.co/query?function=CURRENCY_EXCHANGE_RATE"
               f"&from_currency={from_c}&to_currency={to_c}&apikey={ALPHA_KEY}")
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()
            rate = data.get("Realtime Currency Exchange Rate", {}).get("5. Exchange Rate")
            return float(rate) if rate else None
    except Exception as e:
        log.warning(f"AV {from_c}/{to_c}: {e}")
        return None

async def refresh_prices(session):
    try:
        xau = await av_price(session, "XAU", "USD")
        eur = await av_price(session, "EUR", "USD")
        state["xau_prev"] = state["xau_price"]
        if xau: state["xau_price"] = xau
        if eur: state["eurusd"] = eur
        if eur: state["dxy"] = round(1/eur * 58.6, 2)
    except Exception as e:
        log.error(f"refresh_prices: {e}")

async def fetch_finnhub(session):
    try:
        url = f"https://finnhub.io/api/v1/news?category=general&token={FINNHUB_KEY}"
        async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as r:
            data = await r.json()
            return [{"title": n.get("headline",""), "summary": n.get("summary",""),
                     "source": f"Finnhub/{n.get('source','')}"}
                    for n in (data if isinstance(data, list) else [])[:30]]
    except Exception as e:
        log.warning(f"Finnhub: {e}")
        return []

async def fetch_rss(session, url, source):
    try:
        headers = {"User-Agent": "Mozilla/5.0"}
        async with session.get(url, headers=headers, timeout=aiohttp.ClientTimeout(total=10)) as r:
            text = await r.text()
        root = ET.fromstring(text)
        news = []
        for item in root.findall(".//item")[:20]:
            title = item.findtext("title", "")
            summary = re.sub(r'<[^>]+>', '', item.findtext("description", ""))[:200]
            news.append({"title": title, "summary": summary, "source": source})
        return news
    except Exception as e:
        log.warning(f"RSS {source}: {e}")
        return []

async def fetch_all_news(session):
    try:
        results = await asyncio.gather(
            fetch_finnhub(session),
            fetch_rss(session, "https://feeds.reuters.com/reuters/businessNews", "Reuters"),
            return_exceptions=True
        )
        all_news = []
        for r in results:
            if isinstance(r, list): all_news.extend(r)
        unique = []
        for n in all_news:
            t = n.get("title", "")
            if t and len(t) > 10 and not is_duplicate(t):
                unique.append(n)
        return unique
    except Exception as e:
        log.error(f"fetch_all_news: {e}")
        return []

async def claude_call(session, messages, max_tokens=1000):
    try:
        url = "https://api.anthropic.com/v1/messages"
        headers = {"x-api-key": CLAUDE_KEY, "anthropic-version": "2023-06-01",
                   "content-type": "application/json"}
        payload = {"model": "claude-sonnet-4-20250514", "max_tokens": max_tokens,
                   "system": SMC_SYSTEM, "messages": messages}
        async with session.post(url, headers=headers, json=payload,
                                timeout=aiohttp.ClientTimeout(total=30)) as r:
            data = await r.json()
            return data["content"][0]["text"]
    except Exception as e:
        log.error(f"Claude: {e}")
        return f"❌ Erreur Claude : {e}"

def get_kz(dt):
    t = dt.time()
    if time(7,0) <= t <= time(9,0):    return "🌅 Pre-London"
    if time(10,0) <= t <= time(12,0):  return "🇬🇧 London Kill Zone"
    if time(12,0) < t < time(15,30):   return "😴 Dead Zone"
    if time(15,30) <= t <= time(17,30): return "🇺🇸 NY Kill Zone"
    return "🌙 Hors session"

HEADERS = ["Date","Heure","Pair","Direction","Entrée","SL","TP","Résultat","Pips","EUR","KZ","Notes"]

def init_journal():
    if not JOURNAL_FILE.exists():
        with open(JOURNAL_FILE, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(HEADERS)

def add_trade(t):
    init_journal()
    with open(JOURNAL_FILE, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow([t.get("date",""), t.get("heure",""), t.get("pair","XAU/USD"),
            t.get("direction",""), t.get("entree",""), t.get("sl",""), t.get("tp",""),
            t.get("resultat","⏳"), t.get("pips",""), t.get("eur",""), t.get("kz",""), t.get("notes","")])

def get_stats(period="month"):
    init_journal()
    rows = []
    with open(JOURNAL_FILE, "r", encoding="utf-8") as f:
        for r in csv.DictReader(f): rows.append(r)
    if period == "week":
        mon = (date.today() - timedelta(days=date.today().weekday())).strftime("%d/%m/%Y")
        rows = [r for r in rows if r.get("Date","") >= mon]
    elif period == "month":
        m = date.today().strftime("%m/%Y")
        rows = [r for r in rows if r.get("Date","")[-7:] == m]
    wins   = sum(1 for r in rows if "WIN"  in r.get("Résultat",""))
    losses = sum(1 for r in rows if "LOSS" in r.get("Résultat",""))
    def f(v):
        try: return float(v or 0)
        except: return 0.0
    pips = sum(f(r.get("Pips","")) for r in rows)
    eur  = sum(f(r.get("EUR","")) for r in rows)
    wr   = round(wins/(wins+losses)*100) if wins+losses > 0 else 0
    kz_w = {}
    for r in rows:
        if "WIN" in r.get("Résultat",""):
            k = r.get("KZ",""); kz_w[k] = kz_w.get(k,0)+1
    return {"total": len(rows), "wins": wins, "losses": losses,
            "winrate": wr, "pips": pips, "eur": eur,
            "best_kz": max(kz_w, key=kz_w.get) if kz_w else "N/A"}

def parse_trade(text):
    text = text.lower().strip()
    t = {}
    if "short" in text:  t["direction"] = "SHORT"
    elif "long" in text: t["direction"] = "LONG"
    else: return None
    for pat, key in [(r"(?:short|long)\s+([\d.]+)", "entree"),
                     (r"sl\s*([\d.]+)", "sl"), (r"tp\s*([\d.]+)", "tp")]:
        m = re.search(pat, text)
        if m and key not in t: t[key] = m.group(1)
    if "win" in text: t["resultat"] = "✅ WIN"
    elif "loss" in text: t["resultat"] = "❌ LOSS"
    elif "be" in text: t["resultat"] = "⚖️ BE"
    else: t["resultat"] = "⏳ OPEN"
    m = re.search(r"([+-]?\d+)\s*pips?", text)
    if m: p = int(m.group(1)); t["pips"] = p; t["eur"] = p
    now = datetime.now(BRUSSELS)
    t["date"] = now.strftime("%d/%m/%Y"); t["heure"] = now.strftime("%H:%M"); t["kz"] = get_kz(now)
    return t

async def send(bot, text, chat_id=None):
    cid = chat_id or CHAT_ID
    for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
        try:
            await bot.send_message(chat_id=cid, text=chunk, parse_mode=ParseMode.HTML)
            await asyncio.sleep(0.3)
        except Exception as e:
            log.error(f"Send: {e}")

async def alert_price(bot):
    try:
        xau = state["xau_price"]; prev = state["xau_prev"]
        if not xau or not prev: return
        change_pct = (xau - prev) / prev * 100
        change_pips = abs(xau - prev)
        if abs(change_pct) < 0.5 and change_pips < 25: return
        now = datetime.now(BRUSSELS)
        icon = "🚀" if change_pct > 0 else "📉"
        col  = "🟢" if change_pct > 0 else "🔴"
        lines = [f"⚡️ <b>ALERTE PRIX XAU/USD</b>",
                 f"{col} {icon} {change_pct:+.2f}% — {change_pips:.0f} pips",
                 f"💰 Prix : <b>${xau:,.2f}</b>",
                 f"⏰ {now.strftime('%H:%M')} — {get_kz(now)}"]
        at = state["active_trade"]
        if at:
            entry = at.get("entry", 0); d = at.get("direction","")
            profit = (entry - xau) if d == "SHORT" else (xau - entry)
            if profit >= 20:
                lines.append(f"\n⚠️ <b>BREAKEVEN !</b> {d} en +{profit:.0f} pips")
        for lvl in state["watched_levels"]:
            if abs(xau - lvl["price"]) <= 3:
                lines.append(f"🎯 Niveau <b>{lvl['price']}</b> touché ! {lvl.get('note','')}")
        await send(bot, "\n".join(lines))
    except Exception as e:
        log.error(f"alert_price: {e}")

async def alert_news(bot, session):
    try:
        news = await fetch_all_news(session)
        count = 0
        for n in news:
            if count >= 3: break
            title = n.get("title",""); summary = n.get("summary",""); source = n.get("source","")
            sc = news_score(title, summary)
            if sc < 2: continue
            label  = {3:"🔴 <b>CRITIQUE</b>", 2:"🟠 <b>IMPORTANT</b>"}.get(sc,"")
            now    = datetime.now(BRUSSELS)
            impact = gold_impact(title + " " + summary)
            lines  = [label, f"📰 {title[:150]}"]
            if summary and len(summary) > 20: lines.append(f"<i>{summary[:150]}</i>")
            lines.append(f"🏦 {source} — {now.strftime('%H:%M')} {get_kz(now)}")
            if impact: lines.append(f"🥇 {impact}")
            await send(bot, "\n".join(lines))
            count += 1
            await asyncio.sleep(2)
    except Exception as e:
        log.error(f"alert_news: {e}")

async def alert_kz(bot):
    try:
        now = datetime.now(BRUSSELS); hour = now.strftime("%H:%M")
        xau = state["xau_price"]; xau_str = f"${xau:,.0f}" if xau else "N/A"
        alerts = {
            "09:30": "⏰ <b>London KZ dans 30 min</b>\nPrépare ton analyse !",
            "10:00": f"🇬🇧 <b>LONDON KILL ZONE OUVERTE</b>\nXAU : {xau_str}",
            "15:00": "⏰ <b>NY Kill Zone dans 30 min</b>\nPrépare ton analyse !",
            "15:30": f"🇺🇸 <b>NY KILL ZONE OUVERTE</b>\nXAU : {xau_str}",
            "17:00": "⚠️ <b>NY KZ ferme dans 30 min</b>",
            "17:30": "🚫 <b>NY Kill Zone fermée</b>",
        }
        if hour in alerts and state["last_kz_alert"] != hour:
            state["last_kz_alert"] = hour
            await send(bot, alerts[hour])
    except Exception as e:
        log.error(f"alert_kz: {e}")

async def morning_briefing(bot, session):
    try:
        now = datetime.now(BRUSSELS); today = now.date()
        if state["last_briefing"] == today: return
        if not (time(9,0) <= now.time() <= time(9,10)): return
        state["last_briefing"] = today
        await send(bot, "☀️ <b>Génération briefing matinal...</b>")
        news = await fetch_all_news(session)
        top = [f"- {n.get('title','')[:100]}" for n in news
               if news_score(n.get("title",""), n.get("summary","")) >= 2][:5]
        context = (f"Prix : XAU={state['xau_price']} EUR={state['eurusd']} DXY={state['dxy']}\n"
                   f"News : {chr(10).join(top) if top else 'Pas de news critique'}\n"
                   f"Date : {now.strftime('%A %d %B %Y %H:%M')} Bruxelles")
        messages = [{"role": "user", "content":
            f"Briefing macro complet format Telegram.\n{context}\n"
            "Inclus : XAU prix + corrélations DXY/EUR, Fed/BCE status, inflation CPI, "
            "biais directionnel XAU du jour, score macro /75, niveaux clés. Max 25 lignes."}]
        briefing = await claude_call(session, messages, 1000)
        await send(bot, f"☀️ <b>BRIEFING — {now.strftime('%d/%m/%Y')}</b>\n\n{briefing}")
        s = get_stats("week")
        if s["total"] > 0:
            sign = "+" if s["eur"] >= 0 else ""
            await send(bot, f"📊 Semaine : {s['wins']}W/{s['losses']}L — {s['winrate']}% — {sign}{s['eur']:.0f}€")
    except Exception as e:
        log.error(f"morning_briefing: {e}")

async def weekly_report(bot):
    try:
        now = datetime.now(BRUSSELS); key = now.strftime("%Y-W%W")
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
            f"📊 <b>RAPPORT HEBDO</b>\n\n"
            f"Trades : {s['total']} ({s['wins']}W/{s['losses']}L)\n"
            f"{emoji} Winrate : <b>{s['winrate']}%</b>\n"
            f"📈 Pips : {'+' if s['pips']>=0 else ''}{s['pips']:.0f}\n"
            f"💰 P&L : <b>{sign}{s['eur']:.0f}€</b>\n"
            f"🏆 Meilleure KZ : {s['best_kz']}\n\nBon weekend ! 💪")
        if JOURNAL_FILE.exists():
            with open(JOURNAL_FILE, "rb") as f:
                await bot.send_document(chat_id=CHAT_ID,
                    document=InputFile(f, filename=f"journal_{now.strftime('%Y%m%d')}.csv"),
                    caption="📁 Journal de la semaine")
    except Exception as e:
        log.error(f"weekly_report: {e}")

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    cid = update.effective_chat.id
    await update.message.reply_text(
        f"🎯 <b>ElieIA Bot v3 — Actif !</b>\n\nChat ID : <code>{cid}</code>\n\n"
        f"/prix /kz /briefing /trade /stats /journal\n/surveille 4838 /actif short 4823\n"
        f"📸 Photo de chart → analyse SMC /215 !",
        parse_mode=ParseMode.HTML)

async def cmd_prix(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    xau = state["xau_price"]; eur = state["eurusd"]; dxy = state["dxy"]
    now = datetime.now(BRUSSELS)
    xau_str = f"${xau:,.2f}" if xau else "N/A"
    await update.message.reply_text(
        f"📊 <b>MARCHÉS EN DIRECT</b>\n\n"
        f"🥇 XAU/USD : <b>{xau_str}</b>\n"
        f"💶 EUR/USD : {eur or 'N/A'}\n"
        f"💵 DXY : {dxy or 'N/A'}\n\n"
        f"⏰ {now.strftime('%H:%M')} — {get_kz(now)}",
        parse_mode=ParseMode.HTML)

async def cmd_kz(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    now = datetime.now(BRUSSELS); t = now.time(); countdown = ""
    for target, name in [(time(10,0),"London KZ"), (time(15,30),"NY KZ")]:
        if t < target:
            delta = (datetime.combine(date.today(), target) -
                     datetime.combine(date.today(), t)).seconds // 60
            countdown = f"\n⏳ {name} dans <b>{delta} min</b>"; break
    await update.message.reply_text(
        f"⏰ <b>{now.strftime('%H:%M')}</b>\n{get_kz(now)}{countdown}",
        parse_mode=ParseMode.HTML)

async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    period = (ctx.args[0] if ctx.args else "month")
    s = get_stats(period)
    label = {"week":"cette semaine","month":"ce mois","all":"total"}.get(period,"ce mois")
    if s["total"] == 0:
        await update.message.reply_text(f"📊 Pas de trades {label}."); return
    sign  = "+" if s["eur"] >= 0 else ""
    emoji = "🟢" if s["winrate"] >= 60 else "🟡" if s["winrate"] >= 40 else "🔴"
    await update.message.reply_text(
        f"📊 <b>STATS {label.upper()}</b>\n\n"
        f"Trades : {s['total']} ({s['wins']}W/{s['losses']}L)\n"
        f"{emoji} Winrate : <b>{s['winrate']}%</b>\n"
        f"📈 Pips : {'+' if s['pips']>=0 else ''}{s['pips']:.0f}\n"
        f"💰 P&L : <b>{sign}{s['eur']:.0f}€</b>\n"
        f"🏆 Meilleure KZ : {s['best_kz']}",
        parse_mode=ParseMode.HTML)

async def cmd_trade(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = " ".join(ctx.args) if ctx.args else ""
    if not text:
        await update.message.reply_text(
            "📝 <b>Format :</b>\n/trade short 4823 sl 4830 tp 4771 win +32pips",
            parse_mode=ParseMode.HTML); return
    t = parse_trade(text)
    if not t:
        await update.message.reply_text("❌ Format non reconnu."); return
    add_trade(t)
    await update.message.reply_text(
        f"✅ <b>Trade enregistré</b>\n\n"
        f"{t.get('direction')} @ {t.get('entree','')}\n"
        f"SL : {t.get('sl','')} | TP : {t.get('tp','')}\n"
        f"Résultat : {t.get('resultat','⏳')} | {t.get('pips','')} pips | {t.get('eur','')}€",
        parse_mode=ParseMode.HTML)

async def cmd_journal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not JOURNAL_FILE.exists() or JOURNAL_FILE.stat().st_size < 50:
        await update.message.reply_text("📒 Journal vide."); return
    now = datetime.now(BRUSSELS)
    with open(JOURNAL_FILE, "rb") as f:
        await update.message.reply_document(
            document=InputFile(f, filename=f"journal_{now.strftime('%Y%m%d')}.csv"),
            caption=f"📁 Journal — {now.strftime('%d/%m/%Y')}")

async def cmd_surveille(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage : /surveille 4838 note"); return
    try:
        price = float(ctx.args[0]); note = " ".join(ctx.args[1:])
        state["watched_levels"].append({"price": price, "note": note})
        await update.message.reply_text(f"🎯 Niveau <b>{price}</b> surveillé", parse_mode=ParseMode.HTML)
    except:
        await update.message.reply_text("❌ Exemple : /surveille 4838")

async def cmd_actif(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.args:
        await update.message.reply_text("Usage : /actif short 4823 | /actif off"); return
    if ctx.args[0].lower() == "off":
        state["active_trade"] = None
        await update.message.reply_text("✅ Trade actif désactivé"); return
    try:
        d = ctx.args[0].upper(); e = float(ctx.args[1])
        state["active_trade"] = {"direction": d, "entry": e}
        await update.message.reply_text(
            f"⚡️ Trade actif : <b>{d} @ {e}</b>\n🔔 Alerte BE à +20 pips",
            parse_mode=ParseMode.HTML)
    except:
        await update.message.reply_text("❌ Format : /actif short 4823")

async def cmd_briefing(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("⏳ Briefing macro en cours (~15s)...")
    async with aiohttp.ClientSession() as s:
        news = await fetch_all_news(s)
        top = [f"- {n.get('title','')[:100]}" for n in news
               if news_score(n.get("title",""), n.get("summary","")) >= 2][:5]
        now = datetime.now(BRUSSELS)
        context = (f"Prix : XAU={state['xau_price']} EUR={state['eurusd']} DXY={state['dxy']}\n"
                   f"News : {chr(10).join(top) if top else 'Pas de news'}\n"
                   f"Date : {now.strftime('%A %d %B %Y %H:%M')} Bruxelles")
        messages = [{"role": "user", "content":
            f"Briefing macro complet format Telegram.\n{context}\n"
            "Inclus : XAU + corrélations, Fed/BCE, inflation, biais XAU, score /75, niveaux clés."}]
        briefing = await claude_call(s, messages, 1000)
    await update.message.reply_text(f"📊 <b>BRIEFING</b>\n\n{briefing}", parse_mode=ParseMode.HTML)

async def cmd_aide(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎯 <b>ElieIA Bot v3</b>\n\n"
        "/prix — XAU+EUR/USD+DXY\n/kz — Kill Zone\n/briefing — Macro complet\n"
        "/trade short 4823 sl 4830 tp 4771 win +32pips\n"
        "/stats | /stats week | /stats all\n/journal\n"
        "/surveille 4838 note\n/actif short 4823 | /actif off\n"
        "📸 Photo → analyse SMC /215",
        parse_mode=ParseMode.HTML)

async def handle_photo(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Analyse SMC en cours...")
    try:
        photo = update.message.photo[-1]
        file  = await photo.get_file()
        data  = await file.download_as_bytearray()
        b64   = base64.b64encode(data).decode()
        now   = datetime.now(BRUSSELS)
        ctx_str = f"{now.strftime('%H:%M')} {get_kz(now)}. XAU={state['xau_price']}. {update.message.caption or ''}"
        messages = [{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg", "data": b64}},
            {"type": "text", "text": (f"Analyse chart XAU/USD. {ctx_str}\n"
                "Biais, OB couleur Kasper, score /215 STRICT, GO/SKIP, Entrée/SL/TP, confiance /10.")}
        ]}]
        async with aiohttp.ClientSession() as s:
            result = await claude_call(s, messages, 1200)
        await update.message.reply_text(f"📊 <b>ANALYSE SMC</b>\n\n{result}", parse_mode=ParseMode.HTML)
    except Exception as e:
        await update.message.reply_text(f"❌ Erreur : {e}")

async def main_loop(app):
    bot = app.bot
    log.info("✅ Boucle principale démarrée")
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                await refresh_prices(session)
                await alert_price(bot)
                await alert_news(bot, session)
                await alert_kz(bot)
                await morning_briefing(bot, session)
                await weekly_report(bot)
                await asyncio.sleep(180)
            except Exception as e:
                log.error(f"Loop error: {e}")
                await asyncio.sleep(60)

def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    for cmd, handler in [
        ("start", cmd_start), ("prix", cmd_prix), ("kz", cmd_kz),
        ("stats", cmd_stats), ("trade", cmd_trade), ("journal", cmd_journal),
        ("surveille", cmd_surveille), ("actif", cmd_actif),
        ("briefing", cmd_briefing), ("aide", cmd_aide),
    ]:
        app.add_handler(CommandHandler(cmd, handler))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    init_journal()

    async def run():
        async with app:
            await app.initialize()
            await app.start()
            asyncio.create_task(main_loop(app))
            await app.updater.start_polling(drop_pending_updates=True)
            await asyncio.Event().wait()

    log.info("🚀 ElieIA Bot v3 Final")
    asyncio.run(run())

if __name__ == "__main__":
    main()
