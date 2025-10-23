import os
import asyncio
import logging
import time
from typing import List, Dict, Any, Optional

from aiohttp import web
import httpx
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# =========================
# Config / Entorno
# =========================
PORT = int(os.environ.get("PORT", "10000"))
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "hlhook")
BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
TOP_LIMIT = int(os.environ.get("TOP_LIMIT", "20"))
CACHE_TTL_SEC = int(os.environ.get("CACHE_TTL_SEC", "120"))
DEBUG = os.environ.get("DEBUG", "0") == "1"
PW_PATH = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/render/project/src/.playwright")

logger = logging.getLogger("hyperliquid-top20-bot")
handler = logging.StreamHandler()
handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s:%(name)s:%(message)s"))
logger.addHandler(handler)
logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)

# Cache en memoria para TOP
_cache_rows: List[Dict[str, Any]] = []
_cache_ts: float = 0.0

def cache_valid() -> bool:
    return (time.time() - _cache_ts) < CACHE_TTL_SEC and len(_cache_rows) > 0

def set_cache(rows: List[Dict[str, Any]]) -> None:
    global _cache_rows, _cache_ts
    _cache_rows = rows
    _cache_ts = time.time()

def fmt_money(x: Optional[float]) -> str:
    try:
        return f"${x:,.2f}"
    except Exception:
        return str(x) if x is not None else "—"

# ============== API Hyperliquid (fallback y wallet) ==============
HL_INFO = "https://api.hyperliquid.xyz/info"  # endpoint común en Hyperliquid

async def api_post_json(url: str, payload: Dict[str, Any], timeout=20) -> Any:
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        return r.json()

async def fetch_top_via_api(limit: int) -> List[Dict[str, Any]]:
    """
    Intenta un leaderboard vía API pública de Hyperliquid.
    Nota: Si Hyperliquid cambia el payload, ajusta aquí.
    """
    try:
        # Algunos despliegues usan distintos "type". Probamos dos opciones.
        # Opción 1: "leaders"
        try:
            data = await api_post_json(HL_INFO, {"type": "leaders"})
        except Exception:
            data = await api_post_json(HL_INFO, {"type": "leaderboard"})

        rows: List[Dict[str, Any]] = []
        if isinstance(data, dict):
            candidates = data.get("leaders") or data.get("leaderboard") or data.get("data") or []
        else:
            candidates = data

        for i, row in enumerate(candidates[:limit], start=1):
            # Intentamos mapear valores comunes; si no existen, caemos al raw
            name = row.get("name") or row.get("user") or row.get("address") or "—"
            pnl = row.get("pnl") or row.get("profit") or row.get("equity") or None
            pv = row.get("positionValue") or row.get("pv") or None
            raw = []
            if name: raw.append(str(name))
            if pv is not None: raw.append(f"PV {fmt_money(float(pv))}")
            if pnl is not None: raw.append(f"PnL {fmt_money(float(pnl))}")
            rows.append({"rank": i, "name": name, "pv": pv, "pnl": pnl, "raw": " | ".join(raw)})
        return rows
    except Exception as e:
        logger.warning("Leaderboard API fallback falló: %s", e)
        return []

async def fetch_wallet_state(addr: str) -> Dict[str, Any]:
    """
    Consulta un estado de clearinghouse para la wallet.
    """
    try:
        payload = {"type": "clearinghouseState", "user": addr}
        data = await api_post_json(HL_INFO, payload)
        return data if isinstance(data, dict) else {"raw": data}
    except Exception as e:
        logger.warning("clearinghouseState falló: %s", e)
        # Plan B: algún otro tipo común
        try:
            payload = {"type": "userState", "user": addr}
            data = await api_post_json(HL_INFO, payload)
            return data if isinstance(data, dict) else {"raw": data}
        except Exception as e2:
            logger.warning("userState también falló: %s", e2)
            return {}

# ========================= Scraping (Playwright) =========================
async def fetch_hyperdash_top() -> List[Dict[str, Any]]:
    """
    1) Si el cache sirve, devuelve cache.
    2) Intenta scraping con Playwright.
    3) Si no ve filas, usa fallback de API (leaders).
    """
    if cache_valid():
        return _cache_rows

    from playwright.async_api import async_playwright

    url = "https://hyperliquid.xyz/portfolio"

    rows: List[Dict[str, Any]] = []
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # Espera agresiva al render
            try:
                await page.wait_for_selector("table, [role='row']", timeout=25000)
            except Exception:
                await page.wait_for_load_state("networkidle", timeout=15000)

            # Intento 1: tabla tradicional
            rows = await page.evaluate(
                """
(() => {
  const out = [];
  const tbl = document.querySelector("table");
  if (tbl) {
    const body = tbl.querySelector("tbody") || tbl;
    const trs = Array.from(body.querySelectorAll("tr"));
    for (let i = 0; i < trs.length; i++) {
      const tds = Array.from(trs[i].querySelectorAll("td")).map(td => td.innerText.trim());
      if (tds.length > 0) {
        out.push({ rank: i+1, raw: tds.join(" | "), cols: tds });
      }
    }
  }
  return out;
})()
                """
            )

            # Intento 2: grids con role="row"
            if not rows:
                rows = await page.evaluate(
                    """
(() => {
  const out = [];
  const rows = Array.from(document.querySelectorAll('[role="row"]'));
  for (let i = 0; i < rows.length; i++) {
    const cells = Array.from(rows[i].querySelectorAll('[role="cell"], div, span'))
      .map(x => (x.innerText || '').trim())
      .filter(Boolean);
    if (cells.length >= 1) {
      out.push({ rank: i + 1, raw: cells.join(" | "), cols: cells });
    }
  }
  return out;
})()
                    """
                )

            await context.close()
            await browser.close()
    except Exception as e:
        logger.warning("Scraping falló: %s", e)
        rows = []

    # Si scraping no devolvió nada, plan B: API
    if not rows:
        logger.info("Sin filas por scraping; probando API fallback para TOP…")
        rows = await fetch_top_via_api(TOP_LIMIT)

    rows = rows[:TOP_LIMIT]
    if rows:
        set_cache(rows)
    return rows


def build_top_message(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "No se pudieron extraer filas (la página no devolvió datos visibles)."
    lines = [f"🏆 Top {len(rows)} — Main Position (estimado)\n"]
    for r in rows:
        rank = r.get("rank", "•")
        raw = r.get("raw") or " | ".join(r.get("cols", [])) or "—"
        lines.append(f"{rank}. {raw}")
    return "\n".join(lines)

# ========================= Handlers Telegram =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = (
        "👋 ¡Hola! Soy el bot de Hyperliquid Top.\n\n"
        "Comandos:\n"
        "• /top — Muestra el Top 20 por Main Position ($)\n"
        "• /wallet <address> — Estado simple de la wallet\n\n"
        "Si ves errores, vuelve a intentar en unos segundos."
    )
    await update.message.reply_text(msg)

async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        rows = await fetch_hyperdash_top()
        await update.message.reply_text(build_top_message(rows))
    except Exception as e:
        logger.exception("Fallo en /top")
        await update.message.reply_text(f"⚠️ Error al generar el Top: {type(e).__name__}: {e}")

async def cmd_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    addr = " ".join(context.args).strip() if context.args else ""
    if not addr:
        await update.message.reply_text("Uso: /wallet <0x...>")
        return

    try:
        state = await fetch_wallet_state(addr)
        if not state:
            await update.message.reply_text("No se pudo obtener estado para esa wallet.")
            return

        # Intento formatear algunos campos comunes
        equity = state.get("equity") or state.get("equityUsd") or state.get("equityUSD")
        pos_val = state.get("positionValue") or state.get("pv") or state.get("position_value")
        upnl = state.get("uPnL") or state.get("unrealizedPnl") or state.get("upnl")

        lines = [f"🔎 Wallet: `{addr}`"]
        if equity is not None: lines.append(f"• Equity: {fmt_money(float(equity))}")
        if pos_val is not None: lines.append(f"• Position Value: {fmt_money(float(pos_val))}")
        if upnl is not None: lines.append(f"• uPnL: {fmt_money(float(upnl))}")

        # Si trae posiciones en algún arreglo:
        positions = state.get("positions") or state.get("openPositions") or []
        if isinstance(positions, list) and positions:
            lines.append(f"\nPosiciones activas ({min(len(positions),5)} mostradas):")
            for p in positions[:5]:
                sym = p.get("symbol") or p.get("asset") or "?"
                sz = p.get("size") or p.get("sz") or p.get("amount")
                entry = p.get("entry") or p.get("entryPx") or p.get("entryPrice")
                lines.append(f"• {sym}: sz={sz} entry={entry}")

        await update.message.reply_text("\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        logger.exception("Fallo en /wallet")
        await update.message.reply_text(f"⚠️ Error consultando wallet: {type(e).__name__}: {e}")

def wire_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("wallet", cmd_wallet))

# ========================= Webhook (aiohttp) =========================
async def handle_webhook(request: web.Request) -> web.Response:
    if request.query.get("secret") != WEBHOOK_SECRET:
        return web.Response(status=403, text="forbidden")
    data = await request.json()
    tg_app: Application = request.app["tg_app"]
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return web.json_response({"ok": True})

def build_web_app() -> web.Application:
    app = web.Application()
    tg_app: Application = ApplicationBuilder().token(BOT_TOKEN).build()
    wire_handlers(tg_app)
    app["tg_app"] = tg_app

    async def on_startup(_: web.Application):
        if not BOT_TOKEN or not BASE_URL:
            logger.warning("Faltan TELEGRAM_TOKEN y/o BASE_URL.")
        await tg_app.initialize()
        await tg_app.start()
        webhook_url = f"{BASE_URL}{WEBHOOK_PATH}?secret={WEBHOOK_SECRET}"
        await tg_app.bot.set_webhook(webhook_url)
        logger.info("Webhook configurado -> %s", webhook_url)

    async def on_cleanup(_: web.Application):
        await tg_app.stop()
        await tg_app.shutdown()
        await tg_app.post_stop()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    app.router.add_post(WEBHOOK_PATH, handle_webhook)
    app.router.add_get("/healthz", lambda r: web.Response(text="OK"))
    app.router.add_get("/", lambda r: web.Response(status=404, text="Not Found"))
    return app

def main():
    if PW_PATH and os.path.isdir(PW_PATH):
        logger.info("PLAYWRIGHT_BROWSERS_PATH=%s", PW_PATH)
    web.run_app(build_web_app(), host="0.0.0.0", port=PORT)

if __name__ == "__main__":
    main()
