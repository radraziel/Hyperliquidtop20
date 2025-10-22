# main.py
import os
import re
import sys
import asyncio
import logging
from dataclasses import dataclass
from typing import List, Dict, Any, Optional
from pathlib import Path

from aiohttp import web
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

# =========================
# Config & Logging
# =========================
logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
)
logger = logging.getLogger("hyperliquid-top20-bot")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")  # ej: https://hyperliquidtop20.onrender.com
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "hlhook")  # opcional
PORT = int(os.environ.get("PORT", "10000"))

# Job opcional (broadcast cada 15 min)
BROADCAST_CHAT_ID = os.environ.get("BROADCAST_CHAT_ID", None)  # setea el chat id si quieres auto push
PUSH_EVERY_SECONDS = int(os.environ.get("PUSH_EVERY_SECONDS", "900"))

# Asegura que Playwright y los browsers se guarden en un path dentro del proyecto (empaquetado)
PLAYWRIGHT_BROWSERS_PATH = os.environ.get(
    "PLAYWRIGHT_BROWSERS_PATH",
    "/opt/render/project/src/.playwright"
)
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = PLAYWRIGHT_BROWSERS_PATH

# =========================
# Utils: instalar Chromium si falta (runtime)
# =========================
async def ensure_chromium_installed() -> None:
    """
    Verifica si Chromium está presente; si no, lo instala.
    Render a veces no conserva la caché; esto lo hace robusto.
    """
    browsers_path = Path(PLAYWRIGHT_BROWSERS_PATH)
    chrome_bin = None
    if browsers_path.exists():
        for p in browsers_path.rglob("chrome-linux/chrome"):
            if p.exists():
                chrome_bin = p
                break
    if chrome_bin:
        logger.info("Chromium ya está en: %s", chrome_bin)
        return

    logger.info("Chromium no encontrado. Instalando con 'python -m playwright install chromium'...")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "playwright", "install", "chromium",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    logger.info("Salida de playwright install:\n%s", out.decode(errors="ignore"))
    if proc.returncode != 0:
        raise RuntimeError("No se pudo instalar Chromium en runtime.")

# =========================
# Scraper: Hyperdash Top Traders
# =========================
from playwright.async_api import async_playwright, TimeoutError as PWTimeout

HYPERDASH_URL = "https://hyperdash.info/top-traders"

@dataclass
class TopRow:
    rank: int
    symbol: str
    side: str
    size_usd: str
    owner: str
    address: str

def _parse_money(text: str) -> str:
    """
    Normaliza el tamaño en USD (texto tal cual de la tabla; aquí solo limpiamos espacios).
    Si quieres convertir a número, agrega lógica adicional.
    """
    return re.sub(r"\s+", " ", text or "").strip()

async def fetch_hyperdash_top() -> List[TopRow]:
    """
    Abre la página de Hyperdash, ordena por "Main Position" y devuelve las 20 filas más grandes.
    Usa Playwright correctamente (sin await sobre Locator).
    """
    await ensure_chromium_installed()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        # Cargar página
        await page.goto(HYPERDASH_URL, wait_until="domcontentloaded")
        # Dar tiempo a la red/React
        await page.wait_for_load_state("networkidle")

        # Esperar que exista el table header "Main Position"
        # Primero intenta por role; si no, fallback a selector de texto
        header = page.get_by_role("columnheader", name=re.compile("Main Position", re.I)).first
        try:
            await header.wait_for(state="visible", timeout=15_000)
        except PWTimeout:
            # Fallback: selector por texto
            await page.wait_for_selector("text=Main Position", timeout=15_000)
            header = page.locator("text=Main Position").first

        # Clics para ordenar desc (dos clics suele ponerlo en orden desc)
        await header.click()
        await page.wait_for_timeout(300)
        await header.click()
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(500)

        # Capturar filas
        # Nota: Ajusta el selector si la estructura cambia. Este funciona en la mayoría de tablas HTML.
        rows_locator = page.locator("table >> tbody >> tr")
        row_count = await rows_locator.count()
        results: List[TopRow] = []

        for i in range(min(row_count, 20)):
            row = rows_locator.nth(i)
            cols = row.locator("td")

            # Extrae con defensiva: si alguna columna no existe, devuelve vacío.
            def safe_text(n: int) -> str:
                async def inner() -> str:
                    try:
                        t = await cols.nth(n).text_content()
                        return (t or "").strip()
                    except Exception:
                        return ""
                return asyncio.get_event_loop().run_until_complete(inner())

            # Como estamos dentro de async, mejor hacemos bien el await:
            symbol = (await cols.nth(0).text_content() or "").strip()
            side   = (await cols.nth(1).text_content() or "").strip()
            size   = _parse_money(await cols.nth(2).text_content() or "")
            owner  = (await cols.nth(3).text_content() or "").strip() if await cols.count() > 3 else ""
            # Direcciones suelen venir en link; intentamos primero href
            address = ""
            try:
                link = cols.nth(3).locator("a").first
                if await link.count() > 0:
                    address = (await link.text_content() or "").strip()
                    # si quieres el href:
                    href = await link.get_attribute("href")
                    if href and not address:
                        address = href
            except Exception:
                pass

            results.append(TopRow(
                rank=i + 1,
                symbol=symbol,
                side=side,
                size_usd=size,
                owner=owner,
                address=address
            ))

        await context.close()
        await browser.close()
        return results

def format_top_markdown(rows: List[TopRow]) -> str:
    if not rows:
        return "_No se encontraron filas en la tabla._"
    lines = [
        "*Top 20 – Main Position (Hyperdash)*",
        f"_Fuente: {HYPERDASH_URL}_",
        "",
        "Rank | Side | Size (USD) | Symbol | Owner/Addr",
        ":--: | :--- | ----------: | :----- | :--------",
    ]
    for r in rows:
        owner_addr = r.owner or r.address or "-"
        # linea compacta
        lines.append(f"{r.rank} | {r.side or '-'} | {r.size_usd or '-'} | {r.symbol or '-'} | {owner_addr}")
    return "\n".join(lines)

# =========================
# Telegram Handlers
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "👋 Hola! Soy el bot de *Hyperliquid Top*.\n\n"
        "Comandos:\n"
        "• /top — Muestra el Top 20 por *Main Position* ($)\n"
        "• /wallet <address> — (opcional) datos de wallet\n"
        "\n"
        "Este bot puede publicar cada 15 min si configuras BROADCAST_CHAT_ID."
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)

async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        rows = await fetch_hyperdash_top()
        msg = format_top_markdown(rows)
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    except Exception as e:
        logger.exception("Fallo en /top")
        await update.message.reply_text(f"⚠️ Error al generar el Top: `{e}`", parse_mode=ParseMode.MARKDOWN)

async def cmd_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Placeholder mínimo para no romper. Si ya tienes lógica previa, reemplázala.
    # Uso: /wallet <address>
    if not context.args:
        await update.message.reply_text("Uso: `/wallet <address>`", parse_mode=ParseMode.MARKDOWN)
        return
    address = context.args[0]
    await update.message.reply_text(
        f"🔎 Wallet consultada: `{address}`\n(Implementa aquí tu lógica de wallet)",
        parse_mode=ParseMode.MARKDOWN
    )

# =========================
# AIOHTTP Web App (Webhook + Healthz)
# =========================
async def handle_healthz(request: web.Request) -> web.Response:
    return web.Response(text="ok")

async def handle_webhook(request: web.Request) -> web.Response:
    # Opcional: valida secret por query (?secret=)
    secret = request.query.get("secret")
    if WEBHOOK_SECRET and secret != WEBHOOK_SECRET:
        return web.Response(status=403, text="forbidden")

    data = await request.json()
    application: Application = request.app["tg_app"]
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return web.Response(text="ok")

async def periodic_job(app: Application):
    """Job opcional para enviar /top cada N segundos a un chat fijo (si BROADCAST_CHAT_ID está seteado)."""
    if not BROADCAST_CHAT_ID:
        return
    try:
        rows = await fetch_hyperdash_top()
        msg = format_top_markdown(rows)
        await app.bot.send_message(chat_id=int(BROADCAST_CHAT_ID), text=msg, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
    except Exception:
        logger.exception("Fallo en periodic_job")

async def on_startup(aio_app: web.Application):
    # Salva-vidas: si por alguna razón el build no dejó Chromium, instálalo aquí
    await ensure_chromium_installed()

    tg_app: Application = await create_tg_app()
    await tg_app.initialize()
    await tg_app.start()

    # set webhook (si PUBLIC_URL configurado)
    if PUBLIC_URL:
        url = f"{PUBLIC_URL}{WEBHOOK_PATH}"
        try:
            await tg_app.bot.set_webhook(url=url)
            logger.info("Webhook seteado: %s", url)
        except Exception:
            logger.exception("No se pudo setear webhook")

    aio_app["tg_app"] = tg_app

    # Programar job periódico si procede
    if BROADCAST_CHAT_ID:
        # usa job_queue de PTB
        tg_app.job_queue.run_repeating(lambda ctx: asyncio.create_task(periodic_job(tg_app)), interval=PUSH_EVERY_SECONDS, first=10)

async def on_cleanup(aio_app: web.Application):
    tg_app: Application = aio_app.get("tg_app")
    if tg_app:
        await tg_app.stop()
        await tg_app.shutdown()

def build_web_app() -> web.Application:
    aio_app = web.Application()
    aio_app.router.add_get("/healthz", handle_healthz)
    aio_app.router.add_post(WEBHOOK_PATH, handle_webhook)

    aio_app.on_startup.append(on_startup)
    aio_app.on_cleanup.append(on_cleanup)
    return aio_app

# =========================
# Telegram Application factory
# =========================
async def create_tg_app() -> Application:
    if not TOKEN:
        raise RuntimeError("Falta TELEGRAM_BOT_TOKEN")
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("wallet", cmd_wallet))
    # Alias de texto simple: "top20" o "top"
    app.add_handler(CommandHandler("top20", cmd_top))

    return app

# =========================
# Main entry
# =========================
if __name__ == "__main__":
    # Server aiohttp (Render usará PORT)
    web.run_app(build_web_app(), host="0.0.0.0", port=PORT)
