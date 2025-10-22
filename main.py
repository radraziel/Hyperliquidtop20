import os, json, asyncio, logging, time
from datetime import datetime, timezone
from typing import List, Dict, Any, Tuple, Optional

from aiohttp import ClientSession
from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

from playwright.async_api import async_playwright

# ========= Config =========
BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
BASE_URL = os.environ.get("BASE_URL", "")              # p.ej. https://tu-app.onrender.com
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "hlhook")
PORT = int(os.environ.get("PORT", "8080"))
AUTO_INTERVAL_MIN = int(os.environ.get("AUTO_INTERVAL_MIN", "15"))
TOP_LIMIT = int(os.environ.get("TOP_LIMIT", "20"))
CACHE_TTL_SEC = int(os.environ.get("CACHE_TTL_SEC", "300"))  # 5 min
STORAGE_FILE = os.environ.get("STORAGE_FILE", "state.json")

HYPERDASH_TOP_URL = "https://hyperdash.info/top-traders"
HL_INFO = "https://api.hyperliquid.xyz/info"  # Info endpoint oficial (per-user) — órdenes, fills, posiciones

# ========= Estado / Persistencia =========
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

# ========= Utilidades =========
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

# ========= Hyperliquid (Info endpoint) =========
async def hl_call(body: Dict[str, Any], session: ClientSession) -> Any:
    async with session.post(HL_INFO, json=body, headers={"Content-Type":"application/json"}) as r:
        r.raise_for_status()
        return await r.json()

async def hl_clearinghouse_state(addr: str, session: ClientSession) -> Dict[str, Any]:
    return await hl_call({"type":"clearinghouseState", "user": addr}, session)

async def hl_frontend_open_orders(addr: str, session: ClientSession) -> List[Dict[str, Any]]:
    data = await hl_call({"type":"frontendOpenOrders", "user": addr}, session)
    return data if isinstance(data, list) else []

async def hl_user_fills(addr: str, hours: int, session: ClientSession) -> List[Dict[str, Any]]:
    end_ms = now_ms()
    start_ms = end_ms - hours*3600*1000
    data = await hl_call({"type":"userFillsByTime","user":addr,"startTime":start_ms,"endTime":end_ms,"aggregateByTime":True}, session)
    return data if isinstance(data, list) else []

# ========= HyperDash scraping (Playwright) =========
async def fetch_hyperdash_top(playwright) -> List[Dict[str, Any]]:
    """
    Abre hyperdash.info/top-traders, ordena por 'Main Position' (notional) desc
    y extrae top filas con: address, symbol (main), side, notional.
    """
    global CACHE
    if (now_ms() - CACHE["ts"]) < CACHE_TTL_SEC*1000 and CACHE["rows"]:
        return CACHE["rows"]

    browser = await playwright.chromium.launch(headless=True)
    page = await browser.new_page()
    await page.goto(HYPERDASH_TOP_URL, wait_until="networkidle")  # deja cargar los datos
    # Busca la cabecera y da click en 'Main Position' para ordenar desc
    # Ajusta selectores si cambia el DOM:
    # Texto 'Main Position' en header => click hasta que muestre orden descendente (p. ej. caret ↓)
    header = await page.get_by_text("Main Position").first
    await header.click()
    # Segundo click para forzar descendente (por si default es asc)
    await header.click()

    # Espera a que las filas aparezcan
    # Suelen ser elementos tipo <tr> con celdas: Rank, Address, Main Position, Side, etc.
    await page.wait_for_timeout(1200)

    rows_data: List[Dict[str, Any]] = []
    # Captura máximo ~TOP_LIMIT*1.5 por seguridad, y luego cortamos
    # Selector tolerante: cualquier fila de tabla visible dentro de la sección "Top Traders"
    rows = await page.locator("table >> tbody >> tr").all()
    for r in rows:
        cells = await r.locator("td").all_inner_texts()
        if len(cells) < 4:
            continue
        # Heurística: cells podrían verse así:
        # [rank, shortAddr/fullAddr, trader name? , main position (e.g. $220.3M Short BTC), ...]
        text_row = " | ".join([c.strip() for c in cells if c.strip()])
        # Extraer dirección (busca 0x....)
        import re
        m_addr = re.search(r"(0x[a-fA-F0-9]{40})", text_row)
        addr = m_addr.group(1) if m_addr else ""
        # Extrae side + symbol + notional del campo "Main Position"
        # Ejemplos: "$220.0M Short BTC" / "$190.0M Long ETH"
        # Permitimos K/M/B:
        m_mp = re.search(r"\$([\d\.,]+)\s*([KMB])?\s+(Long|Short)\s+([A-Z\-\/]+)", text_row, re.I)
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
            "side": side,            # "Long" o "Short"
            "notional": notional
        })
        if len(rows_data) >= TOP_LIMIT:
            break

    await browser.close()
    # Cachea
    if rows_data:
        CACHE["ts"] = now_ms()
        CACHE["rows"] = rows_data
    return rows_data

# ========= Render / Webhook infra =========
from aiohttp import web
HTTP_SESSION: Optional[ClientSession] = None
TG_APP: Optional[Application] = None

async def tg_webhook(request: web.Request):
    if request.query.get("secret") != WEBHOOK_SECRET:
        return web.Response(status=401, text="forbidden")
    data = await request.json()
    update = Update.de_json(data, TG_APP.bot)
    await TG_APP.update_queue.put(update)
    return web.Response(text="ok")

async def health(_):
    return web.Response(text="ok")

# ========= Reportes =========
def fmt_order(o: Dict[str, Any]) -> str:
    px = o.get("limitPx")
    sz = o.get("sz") or o.get("origSz")
    sd = o.get("side")
    coin = o.get("coin")
    tms = o.get("timestamp")
    t = ""
    if isinstance(tms, (int, float)):
        t = datetime.fromtimestamp(int(tms)/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    return f"{'🟢' if (sd or '').lower().startswith('b') else '🔴' if (sd or '').lower().startswith('s') else '•'} {coin} {sz}@{px} {t}"

def fmt_fill(f: Dict[str, Any]) -> str:
    coin = f.get("coin")
    dirc = f.get("dir") or f.get("side")
    px = f.get("px")
    sz = f.get("sz")
    tms = f.get("time")
    t = ""
    if isinstance(tms, (int, float)):
        t = datetime.fromtimestamp(int(tms)/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%MZ")
    bullet = "🟢" if (dirc or "").lower().startswith("b") or "long" in (dirc or "").lower() else "🔴" if "short" in (dirc or "").lower() or (dirc or "").lower().startswith("s") else "•"
    return f"{bullet} {coin} {sz}@{px} {t}"

def fmt_wallet_card(rank: int, p: Dict[str, Any], ch: Dict[str, Any], open_orders: List[Dict[str, Any]], fills: List[Dict[str, Any]]) -> str:
    head = f"*#{rank}* {side_emoji(p['side'])} `{p['address']}` — {p['symbol']} — *{usd(p['notional'])}*"
    lines = [head]
    # Posiciones activas
    pos_lines = []
    for a in ch.get("assetPositions", []):
        pos = a.get("position", {})
        if not pos: continue
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
    # Órdenes
    if open_orders:
        lines.append("_Órdenes abiertas (top 5):_")
        for o in open_orders[:5]:
            lines.append(f"• {fmt_order(o)}")
    # Fills
    if fills:
        lines.append("_Fills 24h (top 5):_")
        for f in fills[:5]:
            lines.append(f"• {fmt_fill(f)}")
    return "\n".join(lines)

async def build_top_report(playwright) -> str:
    global HTTP_SESSION
    top = await fetch_hyperdash_top(playwright)
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
        res = results[i-1]
        if isinstance(res, tuple):
            ch, oo, ff = res
        card = fmt_wallet_card(i, p, ch or {}, oo or [], ff or [])
        out.append(card)
        out.append("")  # separador
    out.append("_Fuente ranking: HyperDash Top Traders. Datos de detalle: Hyperliquid Info endpoint._")
    return "\n".join(out)

# ========= Telegram Commands =========
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
        try:
            ch = await hl_clearinghouse_state(addr, sess)
            oo = await hl_frontend_open_orders(addr, sess)
            ff = await hl_user_fills(addr, 24, sess)
        except Exception as e:
            await update.message.reply_text(f"Error consultando {addr}: {e}")
            return
    pseudo = {"address": addr, "symbol": "—", "side": "•", "notional": float(ch.get("marginSummary", {}).get("totalNtlPos", 0.0))}
    await update.message.reply_text(fmt_wallet_card(1, pseudo, ch, oo, ff), parse_mode="Markdown")

async def cmd_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if chat_id not in STATE["subscribers"]:
        STATE["subscribers"].append(chat_id); save_state(STATE)
    await update.message.reply_text("Listo. Enviaré el top cada 15 min a este chat.")

async def cmd_unsub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if chat_id in STATE["subscribers"]:
        STATE["subscribers"].remove(chat_id); save_state(STATE)
    await update.message.reply_text("He dejado de enviar reportes a este chat.")

# ========= Scheduler =========
async def periodic_job(app: Application):
    while True:
        await asyncio.sleep(AUTO_INTERVAL_MIN*60)
        subs = list(STATE.get("subscribers", []))
        if not subs: continue
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

# ========= Boot (Render webhook) =========
from telegram.ext import Defaults
async def create_tg_app() -> Application:
    defaults = Defaults(parse_mode=None)
    app = ApplicationBuilder().token(BOT_TOKEN).defaults(defaults).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("wallet", cmd_wallet))
    app.add_handler(CommandHandler("subscribe", cmd_sub))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsub))
    return app

async def on_startup(app: web.Application):
    global TG_APP
    TG_APP = await create_tg_app()
    app["tg_app"] = TG_APP
    app.router.add_post("/webhook", tg_webhook)
    app.router.add_get("/healthz", health)
    if BASE_URL:
        await TG_APP.bot.set_webhook(f"{BASE_URL}/webhook?secret={WEBHOOK_SECRET}")
    app.loop.create_task(periodic_job(TG_APP))

async def on_cleanup(app: web.Application):
    pass

def create_web() -> web.Application:
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    web.run_app(create_web(), host="0.0.0.0", port=PORT)
