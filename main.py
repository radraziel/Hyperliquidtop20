import os
import asyncio
import logging
import time
from typing import List, Dict, Any, Optional

from aiohttp import web
from telegram import Update
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# =========================
# Configuración / Entorno
# =========================
PORT = int(os.environ.get("PORT", "10000"))
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "hlhook")
BOT_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")  # <- NOMBRE CORRECTO
TOP_LIMIT = int(os.environ.get("TOP_LIMIT", "20"))
CACHE_TTL_SEC = int(os.environ.get("CACHE_TTL_SEC", "120"))
DEBUG = os.environ.get("DEBUG", "0") == "1"

# Playwright: Render instala los browsers en esta ruta durante el build
PW_PATH = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "/opt/render/project/src/.playwright")

# =========================
# Logging
# =========================
logger = logging.getLogger("hyperliquid-top20-bot")
handler = logging.StreamHandler()
fmt = logging.Formatter("%(asctime)s %(levelname)s:%(name)s:%(message)s")
handler.setFormatter(fmt)
logger.addHandler(handler)
logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)

# =========================
# Cache sencillo en memoria
# =========================
_cache_rows: List[Dict[str, Any]] = []
_cache_ts: float = 0.0


# =========================
# Utilidades
# =========================
def cache_valid() -> bool:
    return (time.time() - _cache_ts) < CACHE_TTL_SEC and len(_cache_rows) > 0


def set_cache(rows: List[Dict[str, Any]]) -> None:
    global _cache_rows, _cache_ts
    _cache_rows = rows
    _cache_ts = time.time()


def fmt_money(x: Optional[float]) -> str:
    if x is None:
        return "—"
    try:
        return f"${x:,.2f}"
    except Exception:
        return str(x)


# =========================
# Scraper (Playwright)
# =========================
async def fetch_hyperdash_top() -> List[Dict[str, Any]]:
    """
    Intenta raspar la tabla del dashboard de Hyperliquid.
    Devuelve una lista de filas (dict). Si falla, devuelve [].
    """
    # Evita pedir la web si el cache es válido
    if cache_valid():
        if DEBUG:
            logger.debug("Cache TOP válido, devolviendo %d filas", len(_cache_rows))
        return _cache_rows

    from playwright.async_api import async_playwright

    # Pequeño helper para loguear si Chromium está en el lugar esperado
    chromium_exe = os.path.join(PW_PATH, "chromium-1140", "chrome-linux", "chrome")
    if os.path.exists(chromium_exe):
        logger.info("Chromium disponible en: %s", chromium_exe)
    else:
        logger.info("No se encontró Chromium en %s (Playwright lo resolverá automáticamente)", chromium_exe)

    url = "https://hyperliquid.xyz/portfolio"  # enlace que suele listar posiciones

    rows: List[Dict[str, Any]] = []
    try:
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)  # executable_path no es necesario si PW_PATH está en env
            context = await browser.new_context()
            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)

            # Esperas robustas: primero intentamos 'table', sino fallback a algo de texto típico
            try:
                await page.wait_for_selector("table", timeout=30000)
            except Exception:
                # Fallback por si usan estructura sin <table>
                await page.wait_for_load_state("networkidle", timeout=15000)

            # Intento 1: leer mediante DOM clásico (table > tbody > tr)
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
      if (tds.length >= 1) {
        out.push({ rank: i + 1, raw: tds.join(" | "), cols: tds });
      }
    }
  }
  return out;
})()
                """
            )

            # Si no hay filas, intentar otro selector (divs con role="row")
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
        logger.exception("Error raspeando Hyperliquid: %s", e)
        rows = []

    # recorta al límite deseado
    rows = rows[:TOP_LIMIT]
    if rows:
        set_cache(rows)
    return rows


def build_top_message(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "No se pudieron extraer filas (la página no devolvió datos visibles)."

    # Como no conocemos 100% la estructura, mostramos una columna "raw" legible
    lines = [f"🏆 Top {len(rows)} — Main Position (estimado)\n"]
    for r in rows:
        rank = r.get("rank", "•")
        raw = r.get("raw", "")
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
        "• /wallet <address> — (opcional) datos de wallet\n\n"
        "Este bot usa Playwright para leer el tablero de Hyperliquid.\n"
        "Si ves errores, intenta de nuevo en unos segundos."
    )
    await update.message.reply_text(msg)


async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        rows = await fetch_hyperdash_top()
        text = build_top_message(rows)
        await update.message.reply_text(text)
    except Exception as e:
        logger.exception("Fallo en /top")
        await update.message.reply_text(
            f"⚠️ Error al generar el Top: {type(e).__name__}: {e}"
        )


async def cmd_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Placeholder: solo eco de la dirección. (Puedes implementar tu lógica real aquí)
    addr = " ".join(context.args).strip() if context.args else ""
    if not addr:
        await update.message.reply_text("Uso: /wallet <0x...>")
        return
    await update.message.reply_text(
        f"🔎 Wallet consultada:\n{addr}\n(Implementa aquí tu lógica de wallet)"
    )


def wire_handlers(app: Application) -> None:
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("wallet", cmd_wallet))


# =========================
# Web (aiohttp) + Webhook
# =========================
async def handle_webhook(request: web.Request) -> web.StreamResponse:
    # Seguridad simple por query param
    if request.query.get("secret") != WEBHOOK_SECRET:
        return web.Response(status=403, text="forbidden")

    data = await request.json()
    tg_app: Application = request.app["tg_app"]
    update = Update.de_json(data, tg_app.bot)
    await tg_app.process_update(update)
    return web.json_response({"ok": True})


def build_web_app() -> web.Application:
    app = web.Application()

    # Construimos la Telegram Application
    tg_app: Application = ApplicationBuilder().token(BOT_TOKEN).build()
    wire_handlers(tg_app)
    app["tg_app"] = tg_app

    async def on_startup(_: web.Application):
        # Validaciones de entorno
        if not BOT_TOKEN or not BASE_URL:
            logger.warning("Faltan TELEGRAM_TOKEN y/o BASE_URL en variables de entorno.")

        # Inicializar y arrancar la Telegram App (requerido en modo webhook)
        await tg_app.initialize()
        await tg_app.start()

        # Configurar webhook en Telegram
        webhook_url = f"{BASE_URL}{WEBHOOK_PATH}?secret={WEBHOOK_SECRET}"
        await tg_app.bot.set_webhook(webhook_url)
        logger.info("Webhook configurado -> %s", webhook_url)

    async def on_cleanup(_: web.Application):
        # Parada ordenada del bot
        await tg_app.stop()
        await tg_app.shutdown()
        await tg_app.post_stop()

    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    # Rutas
    app.router.add_post(WEBHOOK_PATH, handle_webhook)
    app.router.add_get("/healthz", lambda r: web.Response(text="OK"))
    # Render hace probes a '/' -> responde 404 amigable
    app.router.add_get("/", lambda r: web.Response(status=404, text="Not Found"))

    return app


# =========================
# Main
# =========================
def main():
    # Logueo del path de browsers de Playwright
    if PW_PATH and os.path.isdir(PW_PATH):
        logger.info("PLAYWRIGHT_BROWSERS_PATH=%s", PW_PATH)
    else:
        logger.info("PLAYWRIGHT_BROWSERS_PATH no encontrado (PW lo manejará igualmente)")

    app = build_web_app()
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
