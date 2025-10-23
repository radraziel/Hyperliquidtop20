# main.py
import os
import re
import sys
import json
import asyncio
import logging
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional

from aiohttp import web
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s",
)
logger = logging.getLogger("hyperliquid-top20-bot")

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
PUBLIC_URL = os.environ.get("PUBLIC_URL", "")
WEBHOOK_PATH = os.environ.get("WEBHOOK_PATH", "/webhook")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "hlhook")
PORT = int(os.environ.get("PORT", "10000"))

BROADCAST_CHAT_ID = os.environ.get("BROADCAST_CHAT_ID")
PUSH_EVERY_SECONDS = int(os.environ.get("PUSH_EVERY_SECONDS", "900"))

PLAYWRIGHT_BROWSERS_PATH = os.environ.get(
    "PLAYWRIGHT_BROWSERS_PATH", "/opt/render/project/src/.playwright"
)
os.environ["PLAYWRIGHT_BROWSERS_PATH"] = PLAYWRIGHT_BROWSERS_PATH

HYPERDASH_URL = "https://hyperdash.info/top-traders"

# ----------------------- Utils -----------------------
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
    if not s:
        return 0.0
    txt = s.strip().upper().replace(",", "")
    mult = 1.0
    m = re.search(r"([KMB])\b", txt)
    if m:
        mult = MULTS.get(m.group(1), 1.0)
    n = re.search(r"-?\$?\s*(\d+(\.\d+)?)", txt)
    if not n:
        return 0.0
    return float(n.group(1)) * mult

def format_amount(n: float) -> str:
    n = float(n)
    sign = "-" if n < 0 else ""
    n = abs(n)
    if n >= 1_000_000_000:
        return f"{sign}${n/1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{sign}${n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{sign}${n/1_000:.2f}K"
    return f"{sign}${n:,.0f}"

def guess_side(texts: Iterable[str]) -> str:
    joined = " ".join([t.lower() for t in texts if t])
    if "short" in joined:
        return "short"
    if "long" in joined:
        return "long"
    return "-"

def find_first_addr(texts: Iterable[str]) -> str:
    for t in texts:
        if not t:
            continue
        m = re.search(r"0x[a-fA-F0-9]{6,}", t)
        if m:
            return m.group(0)
    return ""

# ----------------- Playwright bootstrap -----------------
async def ensure_chromium_installed() -> None:
    from pathlib import Path
    browsers_path = Path(PLAYWRIGHT_BROWSERS_PATH)
    chrome = None
    if browsers_path.exists():
        for p in browsers_path.rglob("chrome-linux/chrome"):
            chrome = p
            break
    if chrome:
        logger.info("Chromium ya está en: %s", chrome)
        return

    logger.info("Chromium no encontrado. Instalando en runtime…")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "playwright", "install", "chromium",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    logger.info("Salida install:\n%s", out.decode(errors="ignore"))
    if proc.returncode != 0:
        raise RuntimeError("No se pudo instalar Chromium.")

# ----------------- Scraper robusto -----------------
from playwright.async_api import async_playwright

def walk_json(node: Any) -> Iterable[Any]:
    """Recorre un JSON anidado y rinde todos los dict/list internos."""
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from walk_json(v)
    elif isinstance(node, list):
        for x in node:
            yield from walk_json(x)

def extract_candidates_from_json(j: Any) -> List[TopRow]:
    """
    Busca objetos que parezcan filas: que tengan monto en USD, símbolo, etc.
    Heurística genérica para adaptarnos a cambios.
    """
    rows: List[TopRow] = []
    for obj in walk_json(j):
        if not isinstance(obj, dict):
            continue

        # posibles llaves de USD/texto
        usd_fields = [
            "mainPosition", "main_position", "positionUsd", "mainUsd", "usd",
            "notionalUsd", "pv", "sizeUsd", "valueUsd"
        ]
        text_fields = [
            "mainPositionText", "main_position_text", "positionUsdText",
            "sizeUsdText", "pvText", "valueText"
        ]

        size_val: Optional[float] = None
        size_txt: Optional[str] = None

        # intenta numérica primero
        for k in usd_fields:
            if k in obj and isinstance(obj[k], (int, float, str)):
                if isinstance(obj[k], str):
                    size_val = usd_to_float(obj[k])
                    size_txt = obj[k]
                else:
                    size_val = float(obj[k])
                    size_txt = format_amount(size_val)
                break

        # si no, intenta textual que traiga $
        if size_val is None:
            for k in text_fields:
                if k in obj and isinstance(obj[k], str) and "$" in obj[k]:
                    size_txt = obj[k]
                    size_val = usd_to_float(obj[k])
                    break

        if size_val is None or size_val == 0:
            continue

        # símbolo / par
        symbol = obj.get("symbol") or obj.get("asset") or obj.get("pair") or "-"
        if isinstance(symbol, dict):
            symbol = symbol.get("name") or symbol.get("symbol") or "-"

        # lado
        side = obj.get("side") or obj.get("positionSide") or "-"
        if isinstance(side, dict):
            side = side.get("name") or "-"

        # owner/address
        owner = obj.get("owner") or obj.get("name") or ""
        address = (
            obj.get("address") or obj.get("wallet") or obj.get("trader") or ""
        )
        if isinstance(address, dict):
            address = address.get("address") or address.get("id") or ""

        # valida dirección si es texto mixto
        if address and not re.match(r"^0x[a-fA-F0-9]{6,}", str(address)):
            # intenta extraer 0x...
            m = re.search(r"0x[a-fA-F0-9]{6,}", str(address))
            if m:
                address = m.group(0)

        rows.append(TopRow(
            rank=0,
            symbol=str(symbol) if symbol else "-",
            side=str(side) if side else "-",
            size_usd_text=size_txt or format_amount(size_val),
            size_usd_num=float(size_val),
            owner=str(owner) if owner else "",
            address=str(address) if address else "",
        ))
    return rows

async def fetch_hyperdash_top() -> List[TopRow]:
    """Intenta extraer el Top 20 desde Hyperdash con varias estrategias y logs."""
    def dlog(*a):
        if os.getenv("DEBUG") == "1":
            logger.info("DEBUG " + " ".join(str(x) for x in a))

    await ensure_chromium_installed()
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-gpu",
            ],
        )
        ctx = await browser.new_context(
            viewport={"width": 1440, "height": 900},
            user_agent=("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"),
            locale="en-US",
            timezone_id="UTC",
        )
        await ctx.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = { runtime: {} };
            Object.defineProperty(navigator, 'languages', { get: () => ['en-US','en'] });
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
        """)
        page = await ctx.new_page()

        captured_json: List[Dict[str, Any]] = []

        def is_json_like(resp) -> bool:
            try:
                ct = (resp.headers or {}).get("content-type", "")
            except Exception:
                ct = ""
            return "application/json" in ct or resp.url.endswith(".json")

        async def on_response(resp):
            if is_json_like(resp):
                try:
                    data = await resp.json()
                    size = len(json.dumps(data)) if data is not None else 0
                    dlog(f"JSON url={resp.url} size≈{size}")
                    captured_json.append({"url": resp.url, "data": data})
                except Exception as e:
                    dlog(f"JSON parse fail url={resp.url} err={e!r}")

        page.on("response", on_response)

        await page.goto(HYPERDASH_URL, wait_until="domcontentloaded")
        await page.wait_for_load_state("networkidle")
        await page.wait_for_timeout(1000)

        # -------- A) __NEXT_DATA__ y otros scripts JSON --------
        try:
            # Next.js
            script = page.locator("script#__NEXT_DATA__")
            if await script.count() > 0:
                raw = await script.first.inner_text()
                j = json.loads(raw)
                dlog(f"NEXT_DATA bytes={len(raw)}")
                rows = extract_candidates_from_json(j)
                if rows:
                    rows.sort(key=lambda r: abs(r.size_usd_num), reverse=True)
                    for i, r in enumerate(rows[:20], start=1):
                        r.rank = i
                    await ctx.close(); await browser.close()
                    return rows[:20]
        except Exception as e:
            dlog(f"NEXT_DATA error {e!r}")

        # Otros <script type="application/json"> típicos (Nuxt/Remix/etc)
        try:
            scripts = page.locator("script[type='application/json']")
            sc = await scripts.count()
            dlog(f"script[type=application/json] count={sc}")
            for i in range(min(sc, 12)):  # límite por seguridad
                raw = (await scripts.nth(i).inner_text()) or ""
                if raw.strip().startswith("{") or raw.strip().startswith("["):
                    j = json.loads(raw)
                    rows = extract_candidates_from_json(j)
                    if rows:
                        rows.sort(key=lambda r: abs(r.size_usd_num), reverse=True)
                        for k, r in enumerate(rows[:20], start=1):
                            r.rank = k
                        await ctx.close(); await browser.close()
                        return rows[:20]
        except Exception as e:
            dlog(f"script[json] error {e!r}")

        # -------- B) Respuestas XHR/JSON capturadas --------
        try:
            await page.wait_for_timeout(1500)  # margen para últimos XHR
            dlog(f"captured_json count={len(captured_json)}")
            combined: List[TopRow] = []
            for blob in captured_json:
                combined.extend(extract_candidates_from_json(blob["data"]))
            if combined:
                combined.sort(key=lambda r: abs(r.size_usd_num), reverse=True)
                for i, r in enumerate(combined[:20], start=1):
                    r.rank = i
                await ctx.close(); await browser.close()
                return combined[:20]
        except Exception as e:
            dlog(f"XHR parse error {e!r}")

        # -------- C) Fallback DOM (si hay) --------
        rows: List[TopRow] = []
        try:
            # cualquier elemento que parezca grilla/tabla
            await page.wait_for_selector("table, [role='table'], [data-radix-scroll-area-viewport]", timeout=8000)
        except Exception:
            pass

        try:
            trs = page.locator("table >> tbody >> tr")
            if await trs.count() == 0:
                trs = page.locator("table tr")
            rc = await trs.count()
            dlog(f"DOM rows={rc}")
            for i in range(rc):
                tds = trs.nth(i).locator("td")
                c = await tds.count()
                texts = [(await tds.nth(k).inner_text() or "").strip() for k in range(c)]
                if not texts:
                    continue
                size_txt = ""
                for t in texts:
                    if "$" in t and re.search(r"\$\s*\d", t):
                        size_txt = t; break
                size_num = usd_to_float(size_txt)
                if size_num == 0:
                    continue
                symbol = "-"
                for t in texts:
                    if re.fullmatch(r"[A-Z0-9:\-\.]{2,12}", t.replace("PERP","").replace(" ","")):
                        symbol = t; break
                if symbol == "-" and texts:
                    symbol = texts[0]
                side = guess_side(texts)
                addr = find_first_addr(texts)
                rows.append(TopRow(
                    rank=0, symbol=symbol, side=side,
                    size_usd_text=size_txt or format_amount(size_num),
                    size_usd_num=size_num, owner="", address=addr
                ))
        except Exception as e:
            dlog(f"DOM parse error {e!r}")

        # -------- D) Último recurso: trocear body.innerText --------
        if not rows:
            body_text = await page.evaluate("document.body.innerText || ''")
            dlog(f"body chars={len(body_text)}")
            for ln in (ln.strip() for ln in body_text.splitlines() if "$" in ln):
                size_num = usd_to_float(ln)
                if size_num == 0:
                    continue
                side = guess_side([ln])
                m_sym = re.search(r"\b[A-Z]{2,10}\b", ln)
                symbol = m_sym.group(0) if m_sym else "-"
                addr = find_first_addr([ln])
                rows.append(TopRow(
                    rank=0, symbol=symbol, side=side,
                    size_usd_text=ln, size_usd_num=size_num,
                    owner="", address=addr
                ))

        await ctx.close(); await browser.close()

        rows.sort(key=lambda r: abs(r.size_usd_num), reverse=True)
        for i, r in enumerate(rows[:20], start=1):
            r.rank = i
        return rows[:20]

def format_top_markdown(rows: List[TopRow]) -> str:
    if not rows:
        return "_No se pudieron extraer filas (la página no devolvió datos visibles)._"
    lines = [
        "*Top 20 — Main Position ($) — Hyperdash*",
        f"_Fuente: {HYPERDASH_URL}_",
        "",
        "Rank | Side | Size (USD) | Symbol | Owner/Addr",
        ":--: | :--- | ----------: | :----- | :--------",
    ]
    for r in rows:
        who = r.owner or r.address or "-"
        lines.append(f"{r.rank} | {r.side} | {r.size_usd_text} | {r.symbol} | {who}")
    return "\n".join(lines)

# ----------------- Telegram handlers -----------------
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
        await update.message.reply_text(
            f"⚠️ Error al generar el Top: `{e}`\n"
            "Si vuelve a fallar, inténtalo otra vez en 1-2 min.",
            parse_mode=ParseMode.MARKDOWN
        )

async def cmd_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Uso: `/wallet <address>`", parse_mode=ParseMode.MARKDOWN)
        return
    address = context.args[0]
    await update.message.reply_text(
        f"🔎 Wallet consultada: `{address}`\n(Implementa aquí tu lógica de wallet)",
        parse_mode=ParseMode.MARKDOWN
    )

# ----------------- HTTP (webhook & healthz) -----------------
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
