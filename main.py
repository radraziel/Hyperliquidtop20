# main.py
import os
import re
import sys
import asyncio
import logging
from dataclasses import dataclass
from typing import List, Optional
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
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")         # p.ej. https://hyperliquidtop20.onrender.com
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "hlhook")
PORT = int(os.environ.get("PORT", "10000"))

# Opcional: broadcast automático cada 15 min
BROADCAST_CHAT_ID = os.environ.get("BROADCAST_CHAT_ID")  # chat_id numérico (str)
PUSH_EVERY_SECONDS = int(os.environ.get("PUSH_EVERY_SECONDS", "900"))

# Playwright cache local en el proyecto
PLAYWRIGHT_BROWSERS_PATH = os.environ.get(
    "PLAYWRIGHT_BROWSERS_PATH",
    "/opt/render/project/src/.playwright"
)
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = PLAYWRIGHT_BROWSERS_PATH

# =========================
# Asegurar Chromium en runtime (por si Render pierde caché)
# =========================
async def ensure_chromium_installed() -> None:
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
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    logger.info("Salida de playwright install:\n%s", out.decode(errors="ignore"))
    if proc.returncode != 0:
        raise RuntimeError("No se pudo instalar Chromium en runtime.")

# =========================
# Scraper Hyperdash Top Traders (sin depender de “Main Position” visible)
# =========================
from playwright.async_api import async_playwright

HYPERDASH_URL = "https://hyperdash.info/top-traders"

@dataclass
class TopRow:
    rank: int
    symbol: str
    side: str
    size_usd_text: str
    size_usd_num: float
    owner: str
    address: str

MULTS = {"K": 1_000, "M": 1_000_000, "B": 1_000_000_000}

def usd_to_float(s: str) -> float:
    """
    Convierte textos como '$139.86M' / '$1,234,567' / '$2.5B' a número float en USD.
    Si falla, devuelve 0.0
    """
    if not s:
        return 0.0
    txt = s.strip().upper().replace(",", "")
    # extrae multiplicador (K/M/B) si existe al final
    mult = 1.0
    m = re.search(r"([KMB])\b", txt)
    if m:
        mult = MULTS.get(m.group(1), 1.0)
    # extrae número principal con signo
    n = re.search(r"-?\$?(\d+(\.\d+)?)", txt)
    if not n:
        return 0.0
    val = float(n.group(1)) * mult
    return val

def guess_side(texts: List[str]) -> str:
    joined = " ".join(texts).lower()
    if "short" in joined:
        return "short"
    if "long" in joined:
        return "long"
    return "-"

def first_address(texts: List[str]) -> str:
    for t in texts:
        m = re.search(r"0x[a-fA-F0-9]{6,}", t)
        if m:
            return m.group(0)
    return ""

async def fetch_hyperdash_top() -> List[TopRow]:
    """
    Carga la página, espera la tabla, detecta la columna de dinero, parsea todas las filas,
    ordena por tamaño USD (desc) y devuelve Top 20.
    """
    await ensure_chromium_installed()

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx = await browser.new_context(
            viewport={"width": 1366, "height": 900},
            user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        )
        page = await ctx.new_page()
        await page.goto(HYPERDASH_URL, wait_until="domcontentloaded")
        await page.wait_for_load_state("networkidle")

        # Espera a que haya alguna tabla/filas (algunos renders tardan)
        await page.wait_for_selector("table", timeout=30000)
        # multiples frameworks usan roles; probamos primero tbody > tr
        rows_loc = page.locator("table >> tbody >> tr")
        # fallback si no hay tbody explícito
        if await rows_loc.count() == 0:
            rows_loc = page.locator("table tr")

        # Intenta detectar encabezados para ubicar columnas
        headers = []
        ths = page.locator("table thead tr th")
        th_count = await ths.count()
        for i in range(th_count):
            txt = (await ths.nth(i).text_content() or "").strip()
            headers.append(txt)

        # Encuentra índice de columna “Main Position” si existe
        idx_money: Optional[int] = None
        if headers:
            for i, h in enumerate(headers):
                if re.search(r"main\s*position", (h or ""), re.I):
                    idx_money = i
                    break

        results: List[TopRow] = []
        row_count = await rows_loc.count()

        for i in range(row_count):
            row = rows_loc.nth(i)
            cols = row.locator("td")
            c = await cols.count()
            if c == 0:
                # puede ser tabla sin tbody: en ese caso tr>td podría no estar; saltamos
                continue

            # Lee todos los textos de la fila (nos sirve para heurísticas)
            texts = []
            for k in range(c):
                t = await cols.nth(k).inner_text()
                texts.append((t or "").strip())

            # Determina columna de dinero si no hay headers
            money_text = ""
            if idx_money is not None and idx_money < c:
                money_text = texts[idx_money]
            else:
                # Heurística: primera celda que contenga un $ con dígitos
                for t in texts:
                    if "$" in t and re.search(r"\$\s*\d", t):
                        money_text = t
                        break

            size_num = usd_to_float(money_text)
            if size_num == 0.0:
                # Si no hay valor $ claro, no consideramos esta fila
                continue

            # símbolo: heurística: primera celda con letras tipo ticker (A-Z/.:)
            symbol = "-"
            for t in texts:
                if re.fullmatch(r"[A-Z0-9:\-\.]{2,10}", t.replace("PERP", "").replace(" ", "")):
                    symbol = t
                    break
            # si no, toma col 0
            if symbol == "-" and c > 0:
                symbol = texts[0]

            side = guess_side(texts)

            # Owner / Address: intenta leer link 0x..., si no, heurística por texto
            address = ""
            try:
                link = row.locator("a:has-text('0x')")
                if await link.count() > 0:
                    txt = await link.first.text_content()
                    address = (txt or "").strip()
            except Exception:
                pass
            if not address:
                address = first_address(texts)

            owner = ""
            # si la tabla tiene una columna de “Owner” o similar, usa esa
            if headers:
                for i_h, h in enumerate(headers):
                    if re.search(r"owner|trader|name", (h or ""), re.I) and i_h < c:
                        owner = texts[i_h]
                        break

            results.append(TopRow(
                rank=0,  # lo llenamos tras ordenar
                symbol=symbol or "-",
                side=side or "-",
                size_usd_text=money_text or "-",
                size_usd_num=size_num,
                owner=owner or "",
                address=address or "",
            ))

        await ctx.close()
        await browser.close()

        # Ordena por tamaño USD desc y toma top 20
        results.sort(key=lambda r: abs(r.size_usd_num), reverse=True)
        for i, r in enumerate(results[:20], start=1):
            r.rank = i
        return results[:20]

def format_top_markdown(rows: List[TopRow]) -> str:
    if not rows:
        return "_No se encontraron filas en la tabla._"
    lines = [
        "*Top 20 – Main Position (estimado por $) — Hyperdash*",
        f"_Fuente: {HYPERDASH_URL}_",
        "",
        "Rank | Side | Size (USD) | Symbol | Owner/Addr",
        ":--: | :--- | ----------: | :----- | :--------",
    ]
    for r in rows:
        who = r.owner or r.address or "-"
        lines.append(f"{r.rank} | {r.side} | {r.size_usd_text} | {r.symbol} | {who}")
    return "\n".join(lines)

# =========================
# Telegram
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = (
        "👋 Hola! Soy el bot de *Hyperliquid Top*.\n\n"
        "Comandos:\n"
        "• /top — Muestra el Top 20 por *Main Position* ($)\n"
        "• /wallet <address> — (opcional) datos de wallet\n\n"
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
    if not context.args:
        await update.message.reply_text("Uso: `/wallet <address>`", parse_mode=ParseMode.MARKDOWN)
        return
    address = context.args[0]
    await update.message.reply_text(
        f"🔎 Wallet consultada: `{address}`\n(Implementa aquí tu lógica de wallet)",
        parse_mode=ParseMode.MARKDOWN
    )

# =========================
# Web (Webhook + Healthz)
# =========================
async def handle_healthz(request: web.Request) -> web.Response:
    return web.Response(text="ok")

async def handle_webhook(request: web.Request) -> web.Response:
    if WEBHOOK_SECRET and request.query.get("secret") != WEBHOOK_SECRET:
        return web.Response(status=403, text="forbidden")
    data = await request.json()
    application: Application = request.app["tg_app"]
    update = Update.de_json(data, application.bot)
    await application.process_update(update)
    return web.Response(text="ok")

async def periodic_job(app: Application):
    if not BROADCAST_CHAT_ID:
        return
    try:
        rows = await fetch_hyperdash_top()
        msg = format_top_markdown(rows)
        await app.bot.send_message(
            chat_id=int(BROADCAST_CHAT_ID),
            text=msg,
            parse_mode=ParseMode.MARKDOWN,
            disable_web_page_preview=True,
        )
    except Exception:
        logger.exception("Fallo en periodic_job")

async def on_startup(aio_app: web.Application):
    await ensure_chromium_installed()
    tg_app: Application = await create_tg_app()
    await tg_app.initialize()
    await tg_app.start()

    if PUBLIC_URL:
        url = f"{PUBLIC_URL}{WEBHOOK_PATH}"
        try:
            await tg_app.bot.set_webhook(url=url)
            logger.info("Webhook seteado: %s", url)
        except Exception:
            logger.exception("No se pudo setear webhook")

    aio_app["tg_app"] = tg_app
    if BROADCAST_CHAT_ID:
        tg_app.job_queue.run_repeating(
            lambda ctx: asyncio.create_task(periodic_job(tg_app)),
            interval=PUSH_EVERY_SECONDS,
            first=10,
        )

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

async def create_tg_app() -> Application:
    if not TOKEN:
        raise RuntimeError("Falta TELEGRAM_BOT_TOKEN")
    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("top", cmd_top))
    app.add_handler(CommandHandler("top20", cmd_top))
    app.add_handler(CommandHandler("wallet", cmd_wallet))
    return app

if __name__ == "__main__":
    web.run_app(build_web_app(), host="0.0.0.0", port=PORT)
