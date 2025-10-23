import os
import asyncio
import logging
import time
from typing import List, Dict, Any, Optional

from aiohttp import web
import httpx
from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes


# =========================
# Configuración
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

# Cache simple para TOP
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


# =========================
# APIs de Hyperliquid (wallet)
# =========================
HL_INFO = "https://api.hyperliquid.xyz/info"


async def api_post_json(url: str, payload: Dict[str, Any], timeout=25) -> Any:
    async with httpx.AsyncClient(timeout=timeout) as client:
        r = await client.post(url, json=payload)
        r.raise_for_status()
        return r.json()


async def fetch_wallet_state(addr: str) -> Dict[str, Any]:
    """Consulta estado de la wallet con dos payloads comunes."""
    try:
        data = await api_post_json(HL_INFO, {"type": "clearinghouseState", "user": addr})
        if isinstance(data, dict) and data:
            return data
    except Exception as e:
        logger.debug("clearinghouseState falló: %s", e)

    try:
        data = await api_post_json(HL_INFO, {"type": "userState", "user": addr})
        if isinstance(data, dict) and data:
            return data
    except Exception as e:
        logger.debug("userState falló: %s", e)

    return {}


# =========================
# Scraping del Leaderboard
# =========================
async def fetch_hyperdash_top() -> List[Dict[str, Any]]:
    """
    1) Devuelve cache si está vigente.
    2) Abre https://hyperliquid.xyz/leaderboard
       y prueba 3 estrategias para extraer filas:
         A) __NEXT_DATA__ (Next.js)
         B) <table>
         C) [role="row"]
    """
    if cache_valid():
        return _cache_rows

    from playwright.async_api import async_playwright

    url = "https://hyperliquid.xyz/leaderboard"
    rows: List[Dict[str, Any]] = []

    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()

            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            # Espera extra por si el hydration tarda
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except Exception:
                pass  # seguimos aunque no llegue a networkidle

            # --------------------------
            # Estrategia A: __NEXT_DATA__
            # --------------------------
            try:
                data_from_next = await page.evaluate(
                    """
(() => {
  try {
    const el = document.getElementById('__NEXT_DATA__');
    if (!el) return null;
    const json = JSON.parse(el.textContent || '{}');
    return json || null;
  } catch (e) { return null; }
})()
"""
                )
                if data_from_next:
                    candidate = await page.evaluate(
                        """
(json) => {
  function isObj(x){ return x && typeof x === 'object' && !Array.isArray(x); }
  function scoreArray(arr){
    if (!Array.isArray(arr) || arr.length === 0) return 0;
    const first = arr[0];
    if (!isObj(first)) return 0;
    const keys = Object.keys(first);
    let score = 0;
    if (keys.includes('name') || keys.includes('user') || keys.includes('address')) score++;
    if (keys.includes('pnl') || keys.includes('profit') || keys.includes('equity')) score++;
    if (keys.includes('positionValue') || keys.includes('pv')) score++;
    return score;
  }
  let best = null, bestScore = 0;
  (function walk(x){
    if (Array.isArray(x)){
      const s = scoreArray(x);
      if (s > bestScore){ best = x; bestScore = s; }
    } else if (x && typeof x === 'object'){
      for (const k of Object.keys(x)) walk(x[k]);
    }
  })(json);
  return best;
}
""",
                        data_from_next,
                    )

                    if isinstance(candidate, list) and candidate:
                        parsed = []
                        for i, item in enumerate(candidate[:TOP_LIMIT], start=1):
                            name = (
                                (item.get("name") or item.get("user") or
                                 item.get("address") or item.get("owner") or "—")
                            )
                            pv = item.get("positionValue") or item.get("pv")
                            pnl = item.get("pnl") or item.get("profit") or item.get("equity")
                            pieces = [str(name)]
                            if pv is not None:
                                try: pieces.append(f"PV {fmt_money(float(pv))}")
                                except Exception: pieces.append(f"PV {pv}")
                            if pnl is not None:
                                try: pieces.append(f"PnL {fmt_money(float(pnl))}")
                                except Exception: pieces.append(f"PnL {pnl}")
                            parsed.append({"rank": i, "name": name, "pv": pv, "pnl": pnl, "raw": " | ".join(pieces)})
                        if parsed:
                            rows = parsed
            except Exception as e:
                logger.debug("__NEXT_DATA__ parse falló: %s", e)

            # --------------------------
            # Estrategia B: <table>
            # --------------------------
            if not rows:
                try:
                    await page.wait_for_selector("table", timeout=8000)
                except Exception:
                    pass
                try:
                    parsed_tbl = await page.evaluate(
                        """
(() => {
  const out = [];
  const tbl = document.querySelector("table");
  if (!tbl) return out;
  const body = tbl.querySelector("tbody") || tbl;
  const trs = Array.from(body.querySelectorAll("tr"));
  for (let i = 0; i < trs.length; i++) {
    const tds = Array.from(trs[i].querySelectorAll("td"))
      .map(td => (td.innerText||'').trim())
      .filter(Boolean);
    if (tds.length) out.push({ rank: i+1, raw: tds.join(" | "), cols: tds });
  }
  return out;
})()
"""
                    )
                    if isinstance(parsed_tbl, list) and parsed_tbl:
                        rows = parsed_tbl
                except Exception as e:
                    logger.debug("parse tabla falló: %s", e)

            # --------------------------
            # Estrategia C: role="row"
            # --------------------------
            if not rows:
                try:
                    await page.wait_for_selector('[role="row"]', timeout=8000)
                except Exception:
                    pass
                try:
                    parsed_grid = await page.evaluate(
                        """
(() => {
  const out = [];
  const rws = Array.from(document.querySelectorAll('[role="row"]'));
  for (let i = 0; i < rws.length; i++) {
    const cells = Array.from(rws[i].querySelectorAll('[role="cell"], div, span'))
      .map(x => (x.innerText || '').trim())
      .filter(Boolean);
    if (cells.length >= 2) out.push({ rank: i+1, raw: cells.join(" | "), cols: cells });
  }
  return out;
})()
"""
                    )
                    if isinstance(parsed_grid, list) and parsed_grid:
                        rows = parsed_grid
                except Exception as e:
                    logger.debug("parse grid falló: %s", e)

            await context.close()
            await browser.close()
    except Exception as e:
        logger.warning("Scraping falló: %s", e)
        rows = []

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


# =========================
# Handlers de Telegram
# =========================
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

        equity = state.get("equity") or state.get("equityUsd") or state.get("equityUSD")
        pos_val = state.get("positionValue") or state.get("pv") or state.get("position_value")
        upnl = state.get("uPnL") or state.get("unrealizedPnl") or state.get("upnl")

        lines = [f"🔎 Wallet: `{addr}`"]
        if equity is not None:
            try:
                lines.append(f"• Equity: {fmt_money(float(equity))}")
            except Exception:
                lines.append(f"• Equity: {equity}")
        if pos_val is not None:
            try:
                lines.append(f"• Position Value: {fmt_money(float(pos_val))}")
            except Exception:
                lines.append(f"• Position Value: {pos_val}")
        if upnl is not None:
            try:
                lines.append(f"• uPnL: {fmt_money(float(upnl))}")
            except Exception:
                lines.append(f"• uPnL: {upnl}")

        # Si no hubo campos reconocibles, muestra claves para guiar el ajuste
        if len(lines) == 1:
            keys = ", ".join(list(state.keys())[:15])
            lines.append(f"(Campos disponibles: {keys} …)")

        # Posiciones si existieran
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


# =========================
# Webhook (aiohttp)
# =========================
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
        # Evita errores si ya no está corriendo
        try:
            await tg_app.stop()
        except Exception:
            pass
        try:
            await tg_app.shutdown()
        except Exception:
            pass
        # No llamar tg_app.post_stop(); puede ser None según versión

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
