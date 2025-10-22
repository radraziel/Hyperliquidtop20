# main.py
# Bot de Telegram para Top 20 (Main Position) desde HyperDash + detalle por wallet desde Hyperliquid
# Listo para Render (webhook HTTP) con Playwright (Chromium)

import os
import json
import time
import asyncio
import logging
import sys
from pathlib import Path
from datetime import datetime, timezone
from typing import List, Dict, Any, Optional

from aiohttp import web, ClientSession
from telegram import Update
from telegram.ext import (
    Application, ApplicationBuilder, CommandHandler, ContextTypes, Defaults
)

from playwright.async_api import async_playwright

# ============================ Config ============================
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
BASE_URL = os.environ.get("BASE_URL", "")                 # ej: https://<service>.onrender.com
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "hlhook")
PORT = int(os.environ.get("PORT", "8080"))

AUTO_INTERVAL_MIN = int(os.environ.get("AUTO_INTERVAL_MIN", "15"))
TOP_LIMIT = int(os.environ.get("TOP_LIMIT", "20"))
CACHE_TTL_SEC = int(os.environ.get("CACHE_TTL_SEC", "300"))  # 5 minutos
STORAGE_FILE = os.environ.get("STORAGE_FILE", "state.json")

HYPERDASH_TOP_URL = "https://hyperdash.info/top-traders"
HL_INFO = "https://api.hyperliquid.xyz/info"  # Info endpoint oficial (per-user)

logging.basicConfig(level=logging.INFO)

# ======================= Estado / Persistencia ==================
def load_state() -> Dict[str, Any]:
    if not os.path.exists(STORAGE_FILE):
        return {"subscribers": []}
    with open(STORAGE_FILE, "r") as f:
        return json.load(f)

def save_state(st: Dict[str, Any]) -> None:
    tmp = STORAGE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STORAGE_FILE)

STATE = load_state()
CACHE: Dict[str, Any] = {"ts": 0, "rows": []}

# ============================ Utils ============================
def usd(n: float) -> str:
    x = float(n)
    a = abs(x)
    if a >= 1_000_000_000: return f"${x/1_000_000_000:.2f}B"
    if a >= 1_000_000:     return f"${x/1_000_000:.2f}M"
    if a >= 1_000:         return f"${x/1_000:.2f}K"
    return f"${x:.2f}"

def side_emoji(s: str) -> str:
    s = (s or "").lower()
    if "long" in s: return "🟢"
    if "short" in s: return "🔴"
    return "•"

def now_ms() -> int:
    return int(time.time() * 1000)

# =================== Hyperliquid Info endpoint ==================
async def hl_call(body: Dict[str, Any], session: ClientSession) -> Any:
    async with session.post(HL_INFO, json=body, headers={"Content-Type": "application/json"}) as r:
        r.raise_for_status()
        return await r.json()

async def hl_clearinghouse_state(addr: str, session: ClientSession) -> Dict[str, Any]:
    return await hl_call({"type": "clearinghouseState", "user": addr}, session)

async def hl_frontend_open_orders(addr: str, session: ClientSession) -> List[Dict[str, Any]]:
    data = await hl_call({"type": "frontendOpenOrders", "user": addr}, session)
    return data if isinstance(data, list) else []

async def hl_user_fills(addr: str, hours: int, session: ClientSession) -> List[Dict[str, Any]]:
    end_ms = now_ms()
    start_ms = end_ms - hours * 3600 * 1000
    data = await hl_call(
        {"type": "userFillsByTime", "user": addr, "startTime": start_ms, "endTime": end_ms, "aggregateByTime": True},
        session
    )
    return data if isinstance(data, list) else []

# ===================== Scraping HyperDash Top ===================
async def fetch_hyperdash_top(pw) -> List[Dict[str, Any]]:
    """
    Abre hyperdash.info/top-traders, ordena por 'Main Position' DESC y extrae:
    address, symbol, side, notional. Devuelve top limitado por TOP_LIMIT.
    """
    global CACHE
    if (now_ms() - CACHE["ts"]) < CACHE_TTL_SEC * 1000 and CACHE["rows"]:
        return CACHE["rows"]

    browser = await pw.chromium.launch(headless=True)
    # User-Agent ayuda a evitar bloqueos esporádicos
    page = await browser.new_page(user_agent=(
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ))
    await page.goto(HYPERDASH_TOP_URL, wait_until="networkidle")

    # Ordenar por Main Position DESC (dos clics para asegurar descendente)
    header = await page.get_by_text("Main Position").first
    await header.click()
    await header.click()

    # Dar un pequeño tiempo a que reordene
    await page.wait_for_timeout(1600)

    rows_data: List[Dict[str, Any]] = []
    rows = await page.locator("table >> tbody >> tr").all()
    for r in rows:
        cells = await r.locator("td").all_inner_texts()
        if len(cells) < 4:
            continue
        text_row = " | ".join([c.strip() for c in cells if c.strip()])

        import re
        # Dirección 0x...
        m_addr = re.search(r"(0x[a-fA-F0-9]{40})", text_row)
        addr = m_addr.group(1) if m_addr else ""

        # "$220.0M Short BTC" / "$190K Long ETH"
        m_mp = re.search(r"\$([\d\.,]+)\s*([KMB])?\s+(Long|Short)\s+([A-Z0-9\-\/]+)", text_row, re.I)
        if not (addr and m_mp):
            continue
        qty = float(m_mp.group(1).replace(",", ""))
        scale = (m_mp.group(2) or "").upper()
        mult = 1.0
        if scale == "K": mult = 1_000.0
        elif scale == "M": mult = 1_000_000.0
        elif scale == "B": mult = 1_000_000_000.0
        notional = qty * mult
        side = m_mp.group(3).capitalize()
        symbol = m_mp.group(4).upper()

        rows_data.append({
            "address": addr,
            "symbol": symbol,
            "side": side,
            "notional": notional
        })
        if len(rows_data) >= TOP_LIMIT:
            break

    await browser.close()
    if rows_data:
        CACHE["ts"] = now_ms()
        CACHE["rows"] = rows_data
    return rows_data

# ========================= Formateadores ========================
def fmt_order(o: Dict[str, Any]) -> str:
    px = o.get("limitPx")
    sz = o.get("sz") or o.get("origSz")
    sd = o.get("side", "")
    coin = o.get("coin")
    tms = o.get("timestamp")
    t = ""
    if isinstance(tms, (int, float)):
        t = datetime.fromtimestamp(int(tms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    bullet = "🟢" if sd.lower().startswith("b") else "🔴" if sd.lower().startswith("s") else "•"
    return f"{bullet} {coin} {sz}@{px} {t}"

def fmt_fill(f: Dict[str, Any]) -> str:
    coin = f.get("coin")
    dirc = (f.get("dir") or f.get("side") or "").lower()
    px = f.get("px")
    sz = f.get("sz")
    tms = f.get("time")
    t = ""
    if isinstance(tms, (int, float)):
        t = datetime.fromtimestamp(int(tms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    bullet = "🟢" if ("long" in dirc or dirc.startswith("b")) else "🔴" if ("short" in dirc or dirc.startswith("s")) else "•"
    return f"{bullet} {coin} {sz}@{px} {t}"

def fmt_wallet_card(rank: int, p: Dict[str, Any], ch: Dict[str, Any],
                    open_orders: List[Dict[str, Any]], fills: List[Dict[str, Any]]) -> str:
    head = f"*#{rank}* {side_emoji(p.get('side',''))} `{p['address']}` — {p['symbol']} — *{usd(p['notional'])}*"
    lines = [head]

    # Posiciones activas
    pos_lines = []
    for a in ch.get("assetPositions", []):
        pos = a.get("position", {})
        if not pos:
            continue
        coin = pos.get("coin")
        pv = pos.get("positionValue")
        entryPx = pos.get("entryPx")
        liqPx = pos.get("liquidationPx")
        roe = pos.get("returnOnEquity")
        szi = pos.get("szi")
        pos_lines.append(f"• {coin}: szi={szi} pv={usd(pv)} entry={entryPx} liq={liqPx} ROE={roe}")
    if pos_lines:
        lines.append("_Posiciones activas:_")
        lines += pos_lines[:6]

    if open_orders:
        lines.append("_Órdenes abiertas (top 5):_")
        for o in open_orders[:5]:
            lines.append(f"• {fmt_order(o)}")

    if fills:
        lines.append("_Fills 24h (top 5):_")
        for f in fills[:5]:
            lines.append(f"• {fmt_fill(f)}")

    return "\n".join(lines)

# ============================ Reportes ==========================
async def build_top_report(pw) -> str:
    top = await fetch_hyperdash_top(pw)
    if not top:
        return "No pude obtener datos de HyperDash (Top Traders). Intenta más tarde."

    out = [f"*Top {len(top)} por 'Main Position' (HyperDash) con detalle de Hyperliquid*"]
    async with ClientSession() as sess:
        tasks = []
        for p in top:
            addr = p["address"]
            tasks.append(asyncio.gather(
                hl_clearinghouse_state(addr, sess),
                hl_frontend_open_orders(addr, sess),
                hl_user_fills(addr, 24, sess)
            ))
        results = await asyncio.gather(*tasks, return_exceptions=True)

    for i, p in enumerate(top, start=1):
        ch, oo, ff = {}, [], []
        res = results[i - 1]
        if isinstance(res, tuple):
            ch, oo, ff = res
        out.append(fmt_wallet_card(i, p, ch or {}, oo or [], ff or []))
        out.append("")
    out.append("_Fuente ranking: HyperDash Top Traders. Detalle: Hyperliquid Info endpoint._")
    return "\n".join(out)

# ======================== Telegram Commands ====================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Comandos:\n"
        "/top — Top 20 (Main Position)\n"
        "/wallet <0x...> — Detalle de una wallet\n"
        "/subscribe — Reporte cada 15 min\n"
        "/unsubscribe — Detener reportes"
    )

async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE):
    async with async_playwright() as pw:
        msg = await build_top_report(pw)
    await update.message.reply_text(msg, parse_mode="Markdown")

async def cmd_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Uso: /wallet 0xABC...")
        return
    addr = context.args[0].strip()
    async with ClientSession() as sess:
        ch = await hl_clearinghouse_state(addr, sess)
        oo = await hl_frontend_open_orders(addr, sess)
        ff = await hl_user_fills(addr, 24, sess)
    pseudo = {
        "address": addr,
        "symbol": "—",
        "side": "•",
        "notional": float(ch.get("marginSummary", {}).get("totalNtlPos", 0.0))
    }
    await update.message.reply_text(fmt_wallet_card(1, pseudo, ch, oo, ff), parse_mode="Markdown")

async def cmd_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if chat_id not in STATE["subscribers"]:
        STATE["subscribers"].append(chat_id)
        save_state(STATE)
    await update.message.reply_text("Listo. Enviaré el top cada 15 min a este chat.")

async def cmd_unsub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if chat_id in STATE["subscribers"]:
        STATE["subscribers"].remove(chat_id)
        save_state(STATE)
    await update.message.reply_text("He dejado de enviar reportes a este chat.")

# ============================= Scheduler =======================
async def periodic_job(app: Application):
    while True:
        await asyncio.sleep(AUTO_INTERVAL_MIN * 60)
        subs = list(STATE.get("subscribers", []))
        if not subs:
            continue
        try:
            async with async_playwright() as pw:
                text = await build_top_report(pw)
            for chat_id in subs:
                try:
                    await app.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
                except Exception as e:
                    logging.warning(f"Error enviando a {chat_id}: {e}")
        except Exception as e:
            logging.error(f"Error en broadcast: {e}")

# ============================ Webhook HTTP =====================
TG_APP: Optional[Application] = None  # guardamos referencia en la app web

async def tg_webhook(request: web.Request):
    if request.query.get("secret") != WEBHOOK_SECRET:
        return web.Response(status=401, text="forbidden")
    data = await request.json()
    update = Update.de_json(data, request.app["tg_app"].bot)
    # Encolamos el update para que lo procese la Application
    await request.app["tg_app"].update_queue.put(update)
    return web.Response(text="ok")

async def health(_):
    return web.Response(text="ok")

async def root(_):
    return web.Response(text="Bot online")

# ============================== Boot ===========================
async def create_tg_app() -> Application:
    defaults = Defaults(parse_mode=None)  # manejamos parse_mode por mensaje
    app = ApplicationBuilder().token(BOT_TOKEN).defaults(defaults).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("wallet", cmd_wallet))
    app.add_handler(CommandHandler("subscribe", cmd_sub))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsub))
    return app

async def on_startup(aio_app: web.Application):
    tg_app = await create_tg_app()

    # 🔧 ¡IMPORTANTE! Inicializar y arrancar la Application para que consuma la queue
    await tg_app.initialize()
    await tg_app.start()

    aio_app["tg_app"] = tg_app
    aio_app.router.add_post("/webhook", tg_webhook)
    aio_app.router.add_get("/healthz", health)
    aio_app.router.add_get("/", root)

    # Configurar webhook en Telegram
    if BASE_URL:
        await tg_app.bot.set_webhook(f"{BASE_URL}/webhook?secret={WEBHOOK_SECRET}")

    # Lanzar tarea periódica (sin usar loop deprecated)
    asyncio.create_task(periodic_job(tg_app))

async def on_cleanup(aio_app: web.Application):
    tg_app = aio_app.get("tg_app")
    if tg_app:
        await tg_app.stop()
        await tg_app.shutdown()

def create_web() -> web.Application:
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app

if __name__ == "__main__":
    web.run_app(create_web(), host="0.0.0.0", port=PORT)
