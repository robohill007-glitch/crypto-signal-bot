#!/usr/bin/env python3
"""
🤖 CryptoSignal AI Bot — Powered by Google Gemini
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
✅ 3 free signals/day
👑 Premium via Solana ($10/month)
🧠 AI signals via Gemini 1.5 Flash (FREE — 1500/day)
📊 5min | 15min | 60min Polymarket BTC signals
"""

import os, json, asyncio, logging, sqlite3
from datetime import datetime, timedelta
from typing import Optional

import aiohttp
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, ContextTypes

# ══════════════════════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════════════════════
BOT_TOKEN        = os.getenv("BOT_TOKEN", "")
SOLANA_RECEIVER  = os.getenv("SOLANA_WALLET", "")
GEMINI_API_KEY   = os.getenv("GEMINI_API_KEY", "")
ADMIN_ID         = int(os.getenv("ADMIN_ID", "0"))
FREE_SIGNALS_PER_DAY = 3
SUBSCRIPTION_DAYS    = 30
DB_PATH              = "bot.db"

GEMINI_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-1.5-flash:generateContent?key=" + "{key}"
)

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════
#  DATABASE
# ══════════════════════════════════════════════════════════════

def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id       INTEGER PRIMARY KEY,
            username      TEXT,
            is_premium    INTEGER DEFAULT 0,
            premium_until TEXT,
            signals_today INTEGER DEFAULT 0,
            reset_date    TEXT DEFAULT '',
            joined        TEXT
        );
        CREATE TABLE IF NOT EXISTS payments (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id     INTEGER,
            tx_sig      TEXT UNIQUE,
            amount_sol  REAL,
            amount_usd  REAL,
            verified_at TEXT
        );
    """)
    con.commit(); con.close()


def upsert_user(uid: int, name: str):
    con = sqlite3.connect(DB_PATH)
    con.execute("""
        INSERT INTO users (user_id, username, joined)
        VALUES (?,?,?)
        ON CONFLICT(user_id) DO UPDATE SET username=excluded.username
    """, (uid, name, datetime.utcnow().isoformat()))
    con.commit(); con.close()


def get_user(uid: int) -> Optional[dict]:
    con = sqlite3.connect(DB_PATH)
    row = con.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()
    con.close()
    if not row: return None
    return dict(zip(["user_id","username","is_premium","premium_until",
                     "signals_today","reset_date","joined"], row))


def can_signal(uid: int) -> tuple[bool, int]:
    u = get_user(uid)
    if not u: return False, 0
    today = datetime.utcnow().strftime("%Y-%m-%d")

    if u["is_premium"] and u["premium_until"]:
        if datetime.fromisoformat(u["premium_until"]) > datetime.utcnow():
            return True, 999
        con = sqlite3.connect(DB_PATH)
        con.execute("UPDATE users SET is_premium=0,premium_until=NULL WHERE user_id=?", (uid,))
        con.commit(); con.close()

    if u["reset_date"] != today:
        con = sqlite3.connect(DB_PATH)
        con.execute("UPDATE users SET signals_today=0,reset_date=? WHERE user_id=?", (today,uid))
        con.commit(); con.close()
        u["signals_today"] = 0

    rem = FREE_SIGNALS_PER_DAY - u["signals_today"]
    return rem > 0, max(rem, 0)


def use_signal(uid: int):
    today = datetime.utcnow().strftime("%Y-%m-%d")
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE users SET signals_today=signals_today+1,reset_date=? WHERE user_id=?",
                (today, uid))
    con.commit(); con.close()


def activate_premium(uid: int, tx: str, sol: float, usd: float):
    until = (datetime.utcnow() + timedelta(days=SUBSCRIPTION_DAYS)).isoformat()
    con = sqlite3.connect(DB_PATH)
    con.execute("UPDATE users SET is_premium=1,premium_until=? WHERE user_id=?", (until, uid))
    con.execute("""INSERT OR IGNORE INTO payments (user_id,tx_sig,amount_sol,amount_usd,verified_at)
                   VALUES (?,?,?,?,?)""", (uid,tx,sol,usd,datetime.utcnow().isoformat()))
    con.commit(); con.close()


# ══════════════════════════════════════════════════════════════
#  MARKET DATA FETCHERS
# ══════════════════════════════════════════════════════════════

async def _get(s: aiohttp.ClientSession, url: str) -> Optional[dict | list]:
    try:
        async with s.get(url, timeout=aiohttp.ClientTimeout(total=12)) as r:
            return await r.json(content_type=None)
    except Exception as e:
        log.warning(f"Fetch failed: {url} — {e}")
        return None


def _rsi(closes: list, p: int = 14) -> float:
    if len(closes) < p+1: return 50.0
    d = [closes[i]-closes[i-1] for i in range(1,len(closes))][-p:]
    g = sum(x for x in d if x>0)/p
    l = sum(-x for x in d if x<0)/p
    return 100.0 if l==0 else round(100-100/(1+g/l), 1)


def _ema(c: list, p: int) -> Optional[float]:
    if len(c)<p: return None
    k, e = 2/(p+1), sum(c[:p])/p
    for x in c[p:]: e = x*k+e*(1-k)
    return e


async def fetch_binance(s) -> Optional[dict]:
    ticker = await _get(s,"https://api.binance.com/api/v3/ticker/24hr?symbol=BTCUSDT")
    klines = await _get(s,"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=5m&limit=60")
    if not ticker or not klines: return None

    closes  = [float(k[4]) for k in klines]
    volumes = [float(k[5]) for k in klines]
    rsi     = _rsi(closes)
    e12, e26 = _ema(closes,12), _ema(closes,26)
    macd    = round((e12-e26),2) if e12 and e26 else 0
    avg_vol = sum(volumes[-10:])/10
    vol_r   = round(volumes[-1]/avg_vol,2) if avg_vol else 1

    def mom(n): return round(((closes[-1]-closes[-1-n])/closes[-1-n])*100,3) if len(closes)>n else 0

    # Bollinger Bands (20 period)
    bb_period = 20
    if len(closes) >= bb_period:
        bb_mean = sum(closes[-bb_period:])/bb_period
        bb_std  = (sum((x-bb_mean)**2 for x in closes[-bb_period:])/bb_period)**0.5
        bb_upper = round(bb_mean + 2*bb_std, 2)
        bb_lower = round(bb_mean - 2*bb_std, 2)
        bb_pos = round((closes[-1]-bb_lower)/(bb_upper-bb_lower)*100, 1) if bb_upper!=bb_lower else 50
    else:
        bb_upper = bb_lower = bb_pos = None

    return {
        "price":      round(float(ticker["lastPrice"]),2),
        "change_24h": round(float(ticker["priceChangePercent"]),2),
        "high_24h":   round(float(ticker["highPrice"]),2),
        "low_24h":    round(float(ticker["lowPrice"]),2),
        "volume_24h": round(float(ticker["quoteVolume"])/1e6,1),
        "rsi":        rsi,
        "macd":       macd,
        "vol_ratio":  vol_r,
        "mom_5m":     mom(1),
        "mom_15m":    mom(3),
        "mom_60m":    mom(12),
        "bb_upper":   bb_upper,
        "bb_lower":   bb_lower,
        "bb_position":bb_pos,   # 0=lower band, 100=upper band
    }


async def fetch_fear_greed(s) -> Optional[dict]:
    d = await _get(s,"https://api.alternative.me/fng/?limit=3")
    if d and d.get("data"):
        return {
            "current": {"value": int(d["data"][0]["value"]),
                        "label": d["data"][0]["value_classification"]},
            "yesterday": int(d["data"][1]["value"]) if len(d["data"])>1 else None,
            "week_ago":  int(d["data"][2]["value"]) if len(d["data"])>2 else None,
        }
    return None


async def fetch_polymarket(s) -> Optional[dict]:
    urls = [
        "https://gamma-api.polymarket.com/markets?search=bitcoin+price&active=true&limit=30",
        "https://gamma-api.polymarket.com/markets?search=BTC&active=true&limit=20",
    ]
    all_markets, probs, vols = [], [], []
    for url in urls:
        d = await _get(s, url)
        if isinstance(d, list): all_markets.extend(d)
        elif isinstance(d, dict): all_markets.extend(d.get("markets",[]))

    for m in all_markets:
        q = m.get("question","").lower()
        if not any(w in q for w in ["bitcoin","btc"]): continue
        for tok in m.get("tokens",[]):
            out = tok.get("outcome","").lower()
            p = float(tok.get("price",0) or 0)
            if out in ("yes","higher","above","up") and 0<p<1:
                probs.append(p)
                vols.append(float(m.get("volume",0) or 0))

    if not probs: return None
    bull = round(sum(probs)/len(probs),3)
    return {
        "bull_prob":    bull,
        "bear_prob":    round(1-bull,3),
        "market_count": len(probs),
        "total_volume": round(sum(vols),0),
    }


async def fetch_global(s) -> dict:
    d = await _get(s,"https://api.coingecko.com/api/v3/global")
    if d and d.get("data"):
        return {
            "btc_dominance": round(d["data"].get("market_cap_percentage",{}).get("btc",50),1),
            "mcap_change_24h": round(d["data"].get("market_cap_change_percentage_24h_usd",0),2),
            "active_cryptos": d["data"].get("active_cryptocurrencies",0),
        }
    return {"btc_dominance":50,"mcap_change_24h":0,"active_cryptos":0}


async def fetch_sol_price(s) -> float:
    d = await _get(s,"https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd")
    return float(d["solana"]["usd"]) if d and "solana" in d else 160.0


# ══════════════════════════════════════════════════════════════
#  GEMINI AI SIGNAL ENGINE
# ══════════════════════════════════════════════════════════════

SYSTEM_PROMPT = """You are an expert cryptocurrency trading analyst specializing in Polymarket prediction market trading.
Your job is to analyze real-time market data and generate precise BTC trading signals for Polymarket UP/DOWN bets.

You must respond ONLY in valid JSON format with this exact structure:
{
  "direction": "UP" or "DOWN" or "NEUTRAL",
  "confidence": <number 40-95>,
  "summary": "<one line Urdu signal summary>",
  "reasons": [
    "<reason 1 in Urdu with emoji>",
    "<reason 2 in Urdu with emoji>",
    "<reason 3 in Urdu with emoji>",
    "<reason 4 in Urdu with emoji>"
  ],
  "risk": "LOW" or "MEDIUM" or "HIGH",
  "polymarket_edge": "<brief Urdu note about Polymarket probability vs your prediction>"
}

Rules:
- Be HONEST. If signals are mixed, say NEUTRAL with low confidence.
- Consider the timeframe carefully — shorter = more volatile.
- Polymarket odds already price in market consensus — look for EDGE (where market is wrong).
- Only give UP/DOWN if confidence >= 55%.
- All text fields must be in Urdu language.
- Return ONLY the JSON object, no markdown, no explanation."""


async def gemini_signal(session: aiohttp.ClientSession,
                        tf: str,
                        bn: dict,
                        fg: Optional[dict],
                        pm: Optional[dict],
                        gl: dict) -> Optional[dict]:
    """Send market data to Gemini and get AI-powered signal"""

    fg_text = "N/A"
    if fg:
        fg_text = (f"Current: {fg['current']['value']} ({fg['current']['label']}), "
                   f"Yesterday: {fg.get('yesterday','N/A')}, "
                   f"Week ago: {fg.get('week_ago','N/A')}")

    pm_text = "N/A"
    if pm:
        pm_text = (f"Bullish probability: {pm['bull_prob']*100:.1f}%, "
                   f"Bearish: {pm['bear_prob']*100:.1f}%, "
                   f"Active markets: {pm['market_count']}, "
                   f"Total volume: ${pm['total_volume']:,.0f}")

    user_prompt = f"""Analyze this real-time BTC market data and generate a Polymarket trading signal.

SIGNAL TIMEFRAME: {tf} (This is how long the Polymarket bet will run)

═══ BINANCE LIVE DATA ═══
Current Price:    ${bn['price']:,}
24h Change:       {bn['change_24h']:+}%
24h High/Low:     ${bn['high_24h']:,} / ${bn['low_24h']:,}
24h Volume:       ${bn['volume_24h']}M USDT

Technical Indicators:
• RSI (14):       {bn['rsi']} {"[OVERSOLD - bullish]" if bn['rsi']<30 else "[OVERBOUGHT - bearish]" if bn['rsi']>70 else "[NEUTRAL]"}
• MACD:           {bn['macd']} {"[BULLISH]" if bn['macd']>0 else "[BEARISH]"}
• Volume Ratio:   {bn['vol_ratio']}x (vs 10-period avg)
• BB Position:    {bn['bb_position']}% {"[near upper band - resistance]" if bn.get('bb_position') and bn['bb_position']>80 else "[near lower band - support]" if bn.get('bb_position') and bn['bb_position']<20 else ""}

Price Momentum:
• Last 5min:      {bn['mom_5m']:+}%
• Last 15min:     {bn['mom_15m']:+}%
• Last 60min:     {bn['mom_60m']:+}%

═══ MARKET SENTIMENT ═══
Fear & Greed Index: {fg_text}

═══ POLYMARKET PREDICTION MARKET ═══
{pm_text}

═══ GLOBAL CRYPTO MARKET ═══
BTC Dominance:    {gl.get('btc_dominance',50)}%
Market Cap 24h:   {gl.get('mcap_change_24h',0):+}%

Based on ALL this data, what is your {tf} Polymarket BTC UP/DOWN signal?
Respond in JSON only."""

    payload = {
        "system_instruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
        "generationConfig": {
            "temperature": 0.3,
            "maxOutputTokens": 600,
            "responseMimeType": "application/json"
        }
    }

    url = GEMINI_URL.format(key=GEMINI_API_KEY)

    try:
        async with session.post(url, json=payload,
                                timeout=aiohttp.ClientTimeout(total=20)) as r:
            data = await r.json(content_type=None)

        if "error" in data:
            log.error(f"Gemini error: {data['error']}")
            return None

        text = data["candidates"][0]["content"]["parts"][0]["text"]
        # Clean any accidental markdown
        text = text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        return json.loads(text)

    except json.JSONDecodeError as e:
        log.error(f"Gemini JSON parse error: {e}")
        return None
    except Exception as e:
        log.error(f"Gemini call failed: {e}")
        return None


# ══════════════════════════════════════════════════════════════
#  SOLANA PAYMENT VERIFIER
# ══════════════════════════════════════════════════════════════

async def verify_sol_tx(session: aiohttp.ClientSession, uid: int, tx_sig: str) -> dict:
    try:
        con = sqlite3.connect(DB_PATH)
        dup = con.execute("SELECT id FROM payments WHERE tx_sig=?", (tx_sig,)).fetchone()
        con.close()
        if dup:
            return {"ok": False, "err": "یہ TX پہلے ہی استعمال ہو چکی ہے"}

        payload = {
            "jsonrpc":"2.0","id":1,
            "method":"getTransaction",
            "params":[tx_sig,{"encoding":"json","maxSupportedTransactionVersion":0}]
        }
        async with session.post("https://api.mainnet-beta.solana.com", json=payload,
                                timeout=aiohttp.ClientTimeout(total=20)) as r:
            data = await r.json()

        if not data or not data.get("result"):
            return {"ok":False,"err":"ٹرانزیکشن نہیں ملی — Solscan.io پر چیک کریں"}

        tx = data["result"]
        if tx.get("meta",{}).get("err"):
            return {"ok":False,"err":"ٹرانزیکشن on-chain fail ہوئی"}

        keys   = tx["transaction"]["message"]["accountKeys"]
        pre_b  = tx["meta"]["preBalances"]
        post_b = tx["meta"]["postBalances"]

        recv_idx = None
        for i, k in enumerate(keys):
            addr = k if isinstance(k,str) else k.get("pubkey","")
            if addr == SOLANA_RECEIVER:
                recv_idx = i; break

        if recv_idx is None:
            return {"ok":False,"err":f"ہمارے والیٹ پر نہیں آئی\nصحیح والیٹ:\n`{SOLANA_RECEIVER}`"}

        sol_rcvd = (post_b[recv_idx]-pre_b[recv_idx])/1e9
        sp       = await fetch_sol_price(session)
        usd_rcvd = sol_rcvd * sp

        if usd_rcvd < 9.0:
            return {"ok":False,"err":f"کم ادائیگی: ${usd_rcvd:.2f} (کم از کم $10 چاہیے)"}

        return {"ok":True,"sol":round(sol_rcvd,4),"usd":round(usd_rcvd,2)}

    except Exception as e:
        return {"ok":False,"err":f"خرابی: {str(e)[:100]}"}


# ══════════════════════════════════════════════════════════════
#  KEYBOARDS
# ══════════════════════════════════════════════════════════════

MAIN_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("🧠 AI سگنل لیں",       callback_data="signal_menu")],
    [InlineKeyboardButton("👑 پریمیم ($10/ماہ)",  callback_data="premium_info")],
    [InlineKeyboardButton("ℹ️ میری اسٹیٹس",       callback_data="status")],
])

TF_KB = InlineKeyboardMarkup([
    [
        InlineKeyboardButton("⚡ 5 min",  callback_data="sig:5m"),
        InlineKeyboardButton("🕐 15 min", callback_data="sig:15m"),
        InlineKeyboardButton("⏰ 60 min", callback_data="sig:60m"),
    ],
    [InlineKeyboardButton("« واپس", callback_data="back")],
])


# ══════════════════════════════════════════════════════════════
#  TELEGRAM HANDLERS
# ══════════════════════════════════════════════════════════════

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.username or u.first_name)
    await update.message.reply_text(
        f"🤖 *CryptoSignal AI Bot*\n\n"
        f"سلام {u.first_name}! میں Google Gemini AI سے BTC Polymarket سگنل دیتا ہوں۔\n\n"
        f"🧠 *AI-Powered:* Gemini 1.5 Flash\n"
        f"✅ *مفت:* 3 سگنل فی دن\n"
        f"👑 *پریمیم:* لامحدود — $10/ماہ (Solana)\n\n"
        f"📊 Binance + Polymarket + Fear&Greed + Bollinger Bands",
        parse_mode="Markdown", reply_markup=MAIN_KB
    )


async def cmd_signal(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.username or u.first_name)
    ok, rem = can_signal(u.id)
    if not ok:
        await update.message.reply_text(
            "❌ آج کے 3 مفت سگنل ختم!\n👑 پریمیم لیں — لامحدود AI سگنل",
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("👑 پریمیم خریدیں", callback_data="premium_info")
            ]])
        )
        return
    rem_txt = "∞" if rem==999 else str(rem)
    await update.message.reply_text(
        f"🧠 *AI ٹائم فریم منتخب کریں*\n\nباقی سگنل: *{rem_txt}*",
        parse_mode="Markdown", reply_markup=TF_KB
    )


async def cmd_pay(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    upsert_user(u.id, u.username or u.first_name)
    if not ctx.args:
        await update.message.reply_text(
            "استعمال: `/pay TRANSACTION_SIGNATURE`",
            parse_mode="Markdown"
        )
        return
    tx = ctx.args[0].strip()
    if len(tx) < 60:
        await update.message.reply_text("❌ غلط TX Signature۔ Solscan.io سے کاپی کریں۔")
        return
    msg = await update.message.reply_text("⏳ Solana blockchain پر verify ہو رہا ہے...")
    async with aiohttp.ClientSession() as sess:
        res = await verify_sol_tx(sess, u.id, tx)
    if res["ok"]:
        activate_premium(u.id, tx, res["sol"], res["usd"])
        await msg.edit_text(
            f"✅ *پریمیم چالو!*\n\n💰 ${res['usd']} ({res['sol']} SOL)\n"
            f"👑 30 دن کی پریمیم فعال\n\n/signal سے لامحدود AI سگنل! 🚀",
            parse_mode="Markdown"
        )
    else:
        await msg.edit_text(f"❌ *تصدیق ناکام*\n\n{res['err']}", parse_mode="Markdown")


async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID: return
    con = sqlite3.connect(DB_PATH)
    total   = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    premium = con.execute("SELECT COUNT(*) FROM users WHERE is_premium=1").fetchone()[0]
    pays    = con.execute("SELECT COUNT(*) FROM payments").fetchone()[0]
    revenue = con.execute("SELECT COALESCE(SUM(amount_usd),0) FROM payments").fetchone()[0]
    con.close()
    await update.message.reply_text(
        f"📊 *Admin Stats*\n\n👥 یوزرز: `{total}`\n👑 پریمیم: `{premium}`\n"
        f"💰 ادائیگیاں: `{pays}` (${revenue:.2f})",
        parse_mode="Markdown"
    )


# ── SIGNAL SENDER ─────────────────────────────────────────────

async def send_ai_signal(tf: str, uid: int, edit_fn):
    await edit_fn("🧠 *AI سوچ رہا ہے...*\n\nPolymarket + Binance + Sentiment ڈیٹا جمع ہو رہا ہے...")

    async with aiohttp.ClientSession() as sess:
        bn, fg, pm, gl = await asyncio.gather(
            fetch_binance(sess), fetch_fear_greed(sess),
            fetch_polymarket(sess), fetch_global(sess),
            return_exceptions=True
        )

    # Graceful fallbacks
    if isinstance(bn, Exception) or not bn:
        await edit_fn("❌ Binance ڈیٹا نہیں ملا۔ دوبارہ کوشش کریں۔"); return
    fg = fg if not isinstance(fg, Exception) else None
    pm = pm if not isinstance(pm, Exception) else None
    gl = gl if not isinstance(gl, Exception) else {}

    await edit_fn("🧠 *Gemini AI تجزیہ کر رہا ہے...*\n\n6 indicators چیک ہو رہے ہیں...")

    async with aiohttp.ClientSession() as sess:
        ai = await gemini_signal(sess, tf, bn, fg, pm, gl)

    if not ai:
        await edit_fn("❌ AI سگنل نہیں بنا۔ دوبارہ کوشش کریں۔"); return

    # Count usage
    u = get_user(uid)
    if not (u and u["is_premium"]): use_signal(uid)
    _, rem = can_signal(uid)
    rem_txt = "∞" if rem==999 else str(rem)

    direction = ai.get("direction","NEUTRAL")
    confidence = ai.get("confidence", 50)
    summary   = ai.get("summary","")
    reasons   = ai.get("reasons",[])
    risk      = ai.get("risk","MEDIUM")
    pm_edge   = ai.get("polymarket_edge","")

    dir_icon = {"UP":"🟢","DOWN":"🔴","NEUTRAL":"⚪"}.get(direction,"⚪")
    dir_text = {"UP":"📈 UP (خریدیں)","DOWN":"📉 DOWN (بیچیں)","NEUTRAL":"⚠️ NEUTRAL (انتظار کریں)"}.get(direction,"⚠️")
    risk_icon = {"LOW":"🟢","MEDIUM":"🟡","HIGH":"🔴"}.get(risk,"🟡")

    reasons_text = "\n".join(f"   {r}" for r in reasons[:4])

    pm_line = ""
    if pm:
        pm_line = f"\n🏦 Polymarket: UP {pm['bull_prob']*100:.0f}% | DOWN {pm['bear_prob']*100:.0f}%"

    fg_line = ""
    if fg:
        fg_line = f"\n😰 Fear&Greed: {fg['current']['value']} ({fg['current']['label']})"

    msg = (
        f"{dir_icon} *{tf.upper()} AI SIGNAL*\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"📌 سمت:     *{dir_text}*\n"
        f"💪 اعتماد:  *{confidence}%*\n"
        f"{risk_icon} خطرہ:     *{risk}*\n"
        f"💰 BTC:     *${bn['price']:,}*\n"
        f"📊 RSI:     *{bn['rsi']}*"
        f"{fg_line}{pm_line}\n\n"
        f"*🧠 AI تجزیہ:*\n_{summary}_\n\n"
        f"*📋 وجوہات:*\n{reasons_text}\n\n"
        f"*💡 Polymarket Edge:*\n_{pm_edge}_\n\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"🔋 باقی: *{rem_txt}* | 🕐 {datetime.utcnow().strftime('%H:%M')} UTC\n"
        f"_⚠️ صرف تعلیمی مقاصد — اپنی ذمہ داری پر ٹریڈ کریں_"
    )

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("🔄 نیا سگنل", callback_data="signal_menu"),
        InlineKeyboardButton("👑 پریمیم",   callback_data="premium_info"),
    ]])
    await edit_fn(msg, kb)


# ── CALLBACK ROUTER ───────────────────────────────────────────

async def on_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q   = update.callback_query
    uid = q.from_user.id
    await q.answer()

    async def edit(text, kb=None):
        await q.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)

    d = q.data

    if d == "back":
        await edit("🤖 *CryptoSignal AI Bot*\n\nکیا کریں؟", MAIN_KB)

    elif d == "signal_menu":
        ok, rem = can_signal(uid)
        if not ok:
            await edit("❌ آج کے سگنل ختم!\n👑 پریمیم لیں",
                       InlineKeyboardMarkup([[
                           InlineKeyboardButton("👑 پریمیم", callback_data="premium_info"),
                           InlineKeyboardButton("« واپس",   callback_data="back")
                       ]]))
            return
        rem_txt = "∞" if rem==999 else str(rem)
        await edit(f"🧠 *ٹائم فریم منتخب کریں*\n\nباقی AI سگنل: *{rem_txt}*", TF_KB)

    elif d.startswith("sig:"):
        tf = d.split(":")[1]
        ok, _ = can_signal(uid)
        if not ok:
            await edit("❌ سگنل ختم! /start سے پریمیم لیں۔"); return
        async def ef(text, kb=None):
            await q.edit_message_text(text, parse_mode="Markdown", reply_markup=kb)
        await send_ai_signal(tf, uid, ef)

    elif d == "premium_info":
        await edit(
            f"👑 *پریمیم سبسکرپشن*\n\n"
            f"💰 قیمت: *$10/ماہ* (Solana)\n"
            f"🧠 فائدے: لامحدود Gemini AI سگنل\n\n"
            f"*ادائیگی:*\n\n"
            f"1️⃣ اس والیٹ پر SOL بھیجیں:\n`{SOLANA_RECEIVER}`\n\n"
            f"2️⃣ Solscan.io پر TX Signature کاپی کریں\n\n"
            f"3️⃣ یہ کمانڈ دیں:\n`/pay TX_SIGNATURE`\n\n"
            f"⚡ تصدیق فوری، 30 دن پریمیم!",
            InlineKeyboardMarkup([[InlineKeyboardButton("« واپس", callback_data="back")]])
        )

    elif d == "status":
        u = get_user(uid)
        ok, rem = can_signal(uid)
        rem_txt = "∞" if rem==999 else str(rem)
        is_pr = u and u["is_premium"]
        until = ""
        if is_pr and u.get("premium_until"):
            dt = datetime.fromisoformat(u["premium_until"]).strftime("%d %b %Y")
            until = f"\n📅 تک: *{dt}*"
        await edit(
            f"{'👑' if is_pr else '🆓'} *میری اسٹیٹس*\n\n"
            f"🏷️ پلان: *{'پریمیم ✅' if is_pr else 'مفت'}*{until}\n"
            f"🧠 باقی AI سگنل: *{rem_txt}*\n"
            f"🆔 ID: `{uid}`",
            InlineKeyboardMarkup([[InlineKeyboardButton("« واپس", callback_data="back")]])
        )


# ══════════════════════════════════════════════════════════════
#  MAIN
# ══════════════════════════════════════════════════════════════

def main():
    if not BOT_TOKEN:    raise SystemExit("❌ BOT_TOKEN missing")
    if not GEMINI_API_KEY: raise SystemExit("❌ GEMINI_API_KEY missing")
    if not SOLANA_RECEIVER: raise SystemExit("❌ SOLANA_WALLET missing")

    init_db()
    log.info("✅ DB ready | 🧠 Gemini AI ready")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start",  cmd_start))
    app.add_handler(CommandHandler("signal", cmd_signal))
    app.add_handler(CommandHandler("pay",    cmd_pay))
    app.add_handler(CommandHandler("admin",  cmd_admin))
    app.add_handler(CallbackQueryHandler(on_callback))

    log.info("🤖 AI Bot polling started...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
