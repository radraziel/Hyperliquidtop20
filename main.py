import os
import re
import json
import asyncio
import logging
from typing import List, Dict, Any, Optional

import httpx
from aiohttp import web
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
)

# ---------- Config & Logging ----------

BOT_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
BASE_URL = os.getenv("BASE_URL", "").rstrip("/")
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "hlhook")
BROADCAST_CHATID = os.getenv("BROADCAST_CHATID")  # opcional
DEBUG = os.getenv("DEBUG", "0") == "1"

PORT = int(os.getenv("PORT", "10000"))
HOST = "0.0.0.0"

# Dónde instaló Render el Chromium de Playwright en build
# (coincide con el BUILD COMMAND que te doy más abajo)
PLAYWRIGHT_BIN_HINTS = [
    "/opt/render/project/src/.playwright/chromium-1140/chrome-linux/chrome",
    "/opt/render/.cache/ms-playwright/chromium-1140/chrome-linux/chrome",
]

LOG_LEVEL = logging.DEBUG if DEBUG else logging.INFO
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)s:%(name)s:%(message)s"
)
log = logging.getLogger("hyperliquid-top20-bot")

if not BOT_TOKEN or not BASE_URL:
    log.warning("Faltan TELEGRAM_TOKEN y/o BASE_URL en variables de entorno.")

# ---------- Utilidades ----------

def format_money(s: str) -> str:
    """
    Normaliza strings como '$139.86M' o '139,860,000' a '$139.86M' cuando se puede.
    Solo para mejor display; no es crítico para la lógica.
    """
    s = s.strip()
    if s.startswith("$"):
        return s
    # intenta como número plano
    try:
        v = float(s.replace(",", ""))
        if abs(v) >= 1_000_000_000:
            return f"${v/1_000_000_000:.2f}B"
        if abs(v) >= 1_000_000:
            return f"${v/1_000_000:.2f}M"
        if abs(v) >= 1_000:
            return f"${v/1_000:.2f}K"
        return f"${v:.2f}"
    except Exception:
        return s

def chromium_executable() -> Optional[str]:
    for p in PLAYWRIGHT_BIN_HINTS:
        if os.path.exists(p):
            return p
    return None

# ---------- Scraper /top con Playwright ----------

async def fetch_hyperdash_top() -> List[Dict[str, Any]]:
    """
    Abre https://hyperdash.info/top-traders y extrae las primeras 20 filas
    ordenadas por 'Main Position'. La página es SPA, así que:
      - Esperamos 'networkidle'
      - Si no vemos tabla visible, leemos el state del DOM con evaluate()
    Devuelve: lista de dicts con: rank, wallet, main_position, side (Long/Short), symbol
    """
    from playwright.async_api import async_playwright

    url = "https://hyperdash.info/top-traders?sort=main_position"

    # Playwright
    exe = chromium_executable()
    if exe:
        log.info(f"Chromium ya está en: {exe}")
    else:
        log.warning("No se encontró ruta fija de Chromium; Playwright usará su bin por defecto.")

    rows: List[Dict[str, Any]] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            executable_path=exe, headless=True, args=["--no-sandbox"]
        )
        try:
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1366, "height": 900},
            )
            # Bloquea recursos pesados
            await context.route("**/*", lambda route: (
                route.abort() if route.request.resource_type in {"image", "media", "font"} else route.continue_()
            ))

            page = await context.new_page()
            await page.goto(url, wait_until="domcontentloaded", timeout=60_000)
            # Deja que la SPA renderice
            await page.wait_for_load_state("networkidle", timeout=60_000)

            # 1) Primer intento: hay <table> visible
            try:
                await page.wait_for_selector("table", timeout=30_000, state="visible")
                # Algunas tablas son virtualizadas, pero probamos:
                trhandles = await page.locator("table tbody tr").all()
                if trhandles:
                    for i, tr in enumerate(trhandles[:30]):  # tomamos un poco más por si hay headers
                        tds = await tr.locator("td").all_inner_texts()
                        if len(tds) < 3:
                            continue
                        # La estructura exacta puede cambiar; intentamos heurísticas:
                        # Suelen estar: rank / wallet / ... / main position / side / symbol ...
                        text_line = " | ".join(tds)
                        # Busca la wallet (0x...)
                        m_wallet = re.search(r"(0x[a-fA-F0-9]{40})", text_line)
                        wallet = m_wallet.group(1) if m_wallet else tds[1].strip()

                        # Main Position: algo como $139.86M
                        m_mp = re.search(r"\$[0-9\.\,]+[KMB]?", text_line)
                        main_pos = m_mp.group(0) if m_mp else tds[-1].strip()

                        # Side & symbol si se ven
                        side = "Long" if "Long" in text_line else ("Short" if "Short" in text_line else "?")
                        m_sym = re.search(r"\b[A-Z]{3,5}\b", text_line)
                        symbol = m_sym.group(0) if m_sym else "?"

                        rows.append({
                            "rank": i + 1,
                            "wallet": wallet,
                            "main_position": main_pos,
                            "side": side,
                            "symbol": symbol,
                        })

            except Exception:
                # 2) Fallback: leer del DOM con evaluate (páginas con listas virtualizadas)
                dom_rows = await page.evaluate("""
                () => {
                  const out = [];
                  // Busca cualquier nodo que parezca fila con wallet y "Main Position"
                  // Esto es heurístico para cambios de UI.
                  const rows = Array.from(document.querySelectorAll("tr, [role='row']"));
                  for (const [idx, r] of rows.entries()) {
                    const txt = r.innerText || "";
                    if (!txt) continue;
                    if (!/0x[a-fA-F0-9]{40}/.test(txt)) continue;
                    const mWallet = txt.match(/0x[a-fA-F0-9]{40}/);
                    const mMP = txt.match(/\\$[0-9\\.,]+[KMB]?/);
                    const mSide = txt.match(/Long|Short/);
                    const mSym = txt.match(/\\b[A-Z]{3,5}\\b/);
                    out.push({
                      rank: idx + 1,
                      wallet: mWallet ? mWallet[0] : "",
                      main_position: mMP ? mMP[0] : "",
                      side: mSide ? mSide[0] : "?",
                      symbol: mSym ? mSym[0] : "?"
                    });
                  }
                  return out.slice(0, 30);
                }
                """)
                for i, r in enumerate(dom_rows):
                    r["rank"] = i + 1
                    rows.append(r)

            # Limpia, ordena y top 20
            def mp_value(s: str) -> float:
                s = s.replace("$", "").replace(",", "").upper()
                mult = 1.0
                if s.endswith("K"): mult, s = 1_000, s[:-1]
                elif s.endswith("M"): mult, s = 1_000_000, s[:-1]
                elif s.endswith("B"): mult, s = 1_000_000_000, s[:-1]
                try:
                    return float(s) * mult
                except Exception:
                    return 0.0

            # quita filas incompletas y ordena por main_position
            cleaned = [
                r for r in rows
                if r.get("wallet") and r.get("main_position")
            ]
            # Puede que la UI ya venga ordenada; igual lo re-ordenamos
            cleaned.sort(key=lambda r: mp_value(r["main_position"]), reverse=True)
            top20 = cleaned[:20]

            # Rerank
            for i, r in enumerate(top20):
                r["rank"] = i + 1
                r["main_position"] = format_money(r["main_position"])

            return top20

        finally:
            await browser.close()


def build_top_message(rows: List[Dict[str, Any]]) -> str:
    if not rows:
        return "No se pudieron extraer filas (la página no devolvió datos visibles)."
    lines = []
    for r in rows:
        lines.append(
            f"*#{r['rank']} — {r['wallet']}*  —  {r.get('side','?')} {r.get('symbol','?')}\n"
            f"`{r['main_position']}`"
        )
    return "\n\n".join(lines)

# ---------- /wallet usando API pública de Hyperliquid (mejor que scrape) ----------

HL_API = "https://api.hyperliquid.xyz/info"

async def query_trader_state(user_addr: str) -> Optional[Dict[str, Any]]:
    """
    Llama al endpoint público para traderState (si cambiara, ver logs DEBUG).
    """
    params = {"type": "traderState", "user": user_addr}
    async with httpx.AsyncClient(timeout=30) as client:
        r = await client.get(HL_API, params=params)
        r.raise_for_status()
        data = r.json()
        # La forma exacta puede variar. Devolvemos el json crudo.
        return data

def summarize_wallet(data: Dict[str, Any], addr: str) -> str:
    """
    Hace un resumen legible de posiciones abiertas y algunas métricas si existen.
    (El formato real del response puede cambiar; por eso hay 'get' defensivos).
    """
    if not data:
        return "❓ No se recibió información de la wallet."

    lines = [f"🔎 *Wallet consultada:*\n`{addr}`"]

    # Intento de campos comunes (ajusta si ves cambios en DEBUG=1)
    positions = data.get("assetPositions") or data.get("openPositions") or data.get("positions") or []
    if not isinstance(positions, list):
        positions = []

    if positions:
        lines.append("\n*Posiciones activas:*")
        for p in positions[:10]:
            sym = p.get("asset") or p.get("symbol") or "?"
            szi = p.get("szi") or p.get("size") or "?"
            px = p.get("px") or p.get("entryPx") or p.get("entry") or "?"
            pv = p.get("positionValue") or p.get("pv") or "?"
            roe = p.get("roe") or p.get("ROE") or p.get("unrealizedPnlPct") or "?"
            pv_fmt = format_money(str(pv))
            lines.append(f"• {sym}: szi={szi} pv={pv_fmt} entry={px} ROE={roe}")
    else:
        lines.append("\nNo se encontraron posiciones abiertas en la respuesta.")

    # Últimos fills si vinieran en el payload
    fills = data.get("fills24h") or data.get("fills") or []
    if isinstance(fills, list) and fills:
        lines.append("\n*Fills 24h (top 5):*")
        for f in fills[:5]:
            # Campos típicos
            sym = f.get("symbol") or f.get("asset") or "?"
            q = f.get("qty") or f.get("sz") or "?"
            px = f.get("px") or f.get("price") or "?"
            ts = f.get("time") or f.get("timestamp") or ""
            emoji = "🔴" if str(f.get("side","")).lower().startswith("sell") else "🟢"
            lines.append(f"{emoji} {sym} {q}@{px} {ts}")

    return "\n".join(lines)

# ---------- Handlers de Telegram ----------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    txt = (
        "👋 Hola! Soy el bot de *Hyperliquid Top*.\n\n"
        "*Comandos:*\n"
        "• /top — Muestra el Top 20 por *Main Position ($)*\n"
        "• /wallet `<address>` — (opcional) datos de wallet\n\n"
        "Este bot puede publicar cada 15 min si configuras *BROADCAST_CHATID*."
    )
    await update.effective_message.reply_text(txt, parse_mode=ParseMode.MARKDOWN)

async def cmd_top(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    try:
        rows = await fetch_hyperdash_top()
        msg = build_top_message(rows)
        await update.effective_message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        log.exception("Fallo en /top")
        await update.effective_message.reply_text(
            f"⚠️ Error al generar el Top: {e}\n\n"
            "Call log:\n"
            "si el error fue por timeout, reintenta en unos segundos.",
            disable_web_page_preview=True,
        )

async def cmd_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.effective_message.reply_text("Uso: `/wallet 0x...`", parse_mode=ParseMode.MARKDOWN)
        return
    addr = context.args[0].strip()
    if not re.fullmatch(r"0x[a-fA-F0-9]{40}", addr):
        await update.effective_message.reply_text("Dirección no válida. Debe ser `0x...` (40 hex).",
                                                  parse_mode=ParseMode.MARKDOWN)
        return
    try:
        data = await query_trader_state(addr)
        if DEBUG:
            log.debug("TraderState raw: %s", json.dumps(data)[:1000])
        msg = summarize_wallet(data or {}, addr)
        await update.effective_message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)
    except Exception as e:
        log.exception("Fallo en /wallet")
        await update.effective_message.reply_text(f"⚠️ Error consultando wallet: {e}")

# (Opcional) suscripción simple: cada 15 min manda el /top al chat configurado
_subscribers = set()

async def cmd_subscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    _subscribers.add(chat_id)
    await update.message.reply_text("✅ Suscrito a reportes cada 15 min en este chat.")

async def cmd_unsubscribe(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    _subscribers.discard(chat_id)
    await update.message.reply_text("🛑 Suscripción detenida para este chat.")

async def periodic_job(app: Application):
    # Si prefieres forzar sólo BROADCAST_CHATID, comenta la parte de _subscribers.
    while True:
        try:
            rows = await fetch_hyperdash_top()
            msg = build_top_message(rows)
            targets = set(_subscribers)
            if BROADCAST_CHATID:
                targets.add(int(BROADCAST_CHATID))
            for chat_id in targets:
                try:
                    await app.bot.send_message(chat_id, msg, parse_mode=ParseMode.MARKDOWN, disable_web_page_preview=True)
                except Exception:
                    log.exception("Error enviando broadcast a %s", chat_id)
        except Exception:
            log.exception("Fallo en periodic_job")
        await asyncio.sleep(15 * 60)

# ---------- AioHTTP Webhook App ----------

async def handle_healthz(request: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "hyperliquid-top20-bot"})

async def handle_webhook(request: web.Request) -> web.Response:
    # Validación simple de secret
    if request.query.get("secret") != WEBHOOK_SECRET:
        return web.json_response({"ok": False, "error": "bad secret"}, status=403)
    data = await request.json()
    # Pasamos el update a PTB
    update = Update.de_json(data=data, bot=request.app["tg_app"].bot)
    await request.app["tg_app"].process_update(update)
    return web.json_response({"ok": True})

async def on_startup(app: web.Application) -> None:
    # Set webhook de Telegram apuntando a este servicio
    webhook_url = f"{BASE_URL}/webhook?secret={WEBHOOK_SECRET}"
    await app["tg_app"].bot.set_webhook(url=webhook_url)
    log.info("Webhook set -> %s", webhook_url)

async def on_cleanup(app: web.Application) -> None:
    await app["tg_app"].shutdown()
    await app["tg_app"].stop()
    log.info("Telegram Application stopped")

def build_web_app() -> web.Application:
    tg_app = Application.builder().token(BOT_TOKEN).build()

    # Handlers
    tg_app.add_handler(CommandHandler("start", cmd_start))
    tg_app.add_handler(CommandHandler("top", cmd_top))
    tg_app.add_handler(CommandHandler("wallet", cmd_wallet))
    tg_app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    tg_app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))

    # Lanza tarea periódica
    asyncio.get_event_loop().create_task(periodic_job(tg_app))

    web_app = web.Application()
    web_app["tg_app"] = tg_app
    web_app.router.add_get("/healthz", handle_healthz)
    web_app.router.add_post("/webhook", handle_webhook)

    web_app.on_startup.append(on_startup)
    web_app.on_cleanup.append(on_cleanup)
    return web_app

def main():
    app = build_web_app()
    web.run_app(app, host=HOST, port=PORT)

if __name__ == "__main__":
    main()
