"""
telegram_bot.py
===============
Bot Telegram qui orchestre Winamax + SofaScore + Flashscore + moteur d'analyse.

Commandes :
  /start    — Bienvenue
  /live     — Matchs en cours
  /upcoming — Prochains matchs (< 3h)
  /analyse  — Analyser un match (boutons interactifs)
  /sport    — Filtrer par sport
  /alertes  — Activer/désactiver les alertes automatiques
  /status   — Statut du bot

Lancement :
  export TELEGRAM_BOT_TOKEN="votre_token"
  python bot/telegram_bot.py
"""

import asyncio
import logging
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from telegram.constants import ParseMode

from scrapers.winamax_scraper import WinamaxPoller, SPORT_IDS
from engine.analysis_engine import AnalysisPipeline, LiveAnalyzer

# ──────────────────────────────────────────────
# Config
# ──────────────────────────────────────────────
BOT_TOKEN        = os.getenv("TELEGRAM_BOT_TOKEN", "VOTRE_TOKEN_ICI")
ALLOWED_CHAT_IDS: set[int] = set()   # vide = accès public
POLL_INTERVAL_S  = 60
LIVE_UPDATE_S    = 90
PREMATCH_HOURS   = 3
MIN_CONFIDENCE   = 50
MAX_MSG_LEN      = 4000

DATA_DIR = Path(__file__).parent.parent / "data"
LOG_DIR  = Path(__file__).parent.parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [Bot] %(levelname)s  %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(LOG_DIR / "bot.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("telegram_bot")


# ──────────────────────────────────────────────
# État global
# ──────────────────────────────────────────────
class BotState:
    def __init__(self):
        self.pipeline     = AnalysisPipeline(output_dir=DATA_DIR)
        self.poller       = WinamaxPoller(
            interval_s    = POLL_INTERVAL_S,
            output_dir    = DATA_DIR,
            scraper_kwargs= {"headless": True, "capture_duration": 60},
        )
        self.live_reports: dict = {}
        self.pre_reports:  dict = {}
        self.alert_chats:  set  = set()
        self.sport_filter: dict = {}
        self.last_sent:    dict = {}

STATE = BotState()


# ──────────────────────────────────────────────
# Utilitaires
# ──────────────────────────────────────────────
def allowed(update: Update) -> bool:
    if not ALLOWED_CHAT_IDS:
        return True
    return (update.effective_chat.id if update.effective_chat else None) in ALLOWED_CHAT_IDS


def truncate(msg: str) -> str:
    return msg if len(msg) <= MAX_MSG_LEN else msg[:MAX_MSG_LEN - 30] + "\n…(tronqué)"


async def send(ctx, chat_id: int, text: str, markup=None) -> None:
    try:
        await ctx.bot.send_message(
            chat_id=chat_id, text=truncate(text),
            parse_mode=ParseMode.HTML,
            reply_markup=markup,
            disable_web_page_preview=True,
        )
    except Exception as exc:
        log.warning(f"Erreur envoi {chat_id}: {exc}")


def sport_keyboard() -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(name, callback_data=f"sport_{sid}")]
        for sid, name in list(SPORT_IDS.items())[:6]
    ]
    buttons.append([InlineKeyboardButton("🌐 Tous les sports", callback_data="sport_all")])
    return InlineKeyboardMarkup(buttons)


def match_keyboard(matches: list) -> InlineKeyboardMarkup:
    buttons = [
        [InlineKeyboardButton(
            f"{m['home'][:13]} vs {m['away'][:13]}",
            callback_data=f"analyse_{m['match_id']}"
        )]
        for m in matches[:10]
    ]
    return InlineKeyboardMarkup(buttons)


# ──────────────────────────────────────────────
# Commandes
# ──────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    await update.message.reply_html(
        "⚽ <b>Sport Analysis Bot</b>\n\n"
        "Analyse en temps réel tous les matchs Winamax.\n\n"
        "<b>Commandes :</b>\n"
        "/live — Matchs en cours\n"
        "/upcoming — Prochains matchs (&lt; 3h)\n"
        "/analyse — Analyser un match\n"
        "/sport — Filtrer par sport\n"
        "/alertes — Alertes automatiques\n"
        "/status — Statut du bot"
    )


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    cached = WinamaxPoller.load_cached(DATA_DIR)
    live_n = sum(1 for m in cached if m.get("status") == "LIVE")
    pre_n  = sum(1 for m in cached if m.get("status") == "PREMATCH")
    p      = DATA_DIR / "winamax_matches.json"
    age    = f"{time.time() - p.stat().st_mtime:.0f}s" if p.exists() else "—"
    await update.message.reply_html(
        f"🤖 <b>Statut</b>\n\n"
        f"📡 Matchs en cache : {len(cached)}\n"
        f"🔴 Live : {live_n}  |  🕐 Pré-match : {pre_n}\n"
        f"⏱ Âge cache : {age}\n"
        f"🔔 Alertes actives : {len(STATE.alert_chats)} chats\n"
        f"📊 Rapports générés : {len(STATE.pre_reports)}"
    )


async def cmd_live(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    chat_id = update.effective_chat.id
    await update.message.reply_html("🔄 Récupération des matchs live…")
    cached  = WinamaxPoller.load_cached(DATA_DIR)
    sport_f = STATE.sport_filter.get(chat_id)
    live    = [m for m in cached if m.get("status") == "LIVE"
               and (sport_f is None or m.get("sport_id") == sport_f)]

    if not live:
        await update.message.reply_html("😴 Aucun match live en ce moment.")
        return

    await update.message.reply_html(
        f"🔴 <b>{len(live)} match(s) en cours</b>",
        reply_markup=match_keyboard(live)
    )
    for m in live[:5]:
        sh, sa = m.get("score_home", "?"), m.get("score_away", "?")
        odds   = m.get("odds", {})
        await send(ctx, chat_id,
            f"⚽ <b>{m['home']} {sh}–{sa} {m['away']}</b>\n"
            f"⏱ {m.get('minute','?')}' | {m.get('league','?')}\n"
            f"💰 {odds.get('home','—')} / {odds.get('draw','—')} / {odds.get('away','—')}"
        )
        await asyncio.sleep(0.3)


async def cmd_upcoming(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    chat_id = update.effective_chat.id
    cutoff  = time.time() + PREMATCH_HOURS * 3600
    cached  = WinamaxPoller.load_cached(DATA_DIR)
    sport_f = STATE.sport_filter.get(chat_id)
    upcoming = sorted([
        m for m in cached
        if m.get("status") == "PREMATCH" and m.get("start_ts", 0) <= cutoff
        and (sport_f is None or m.get("sport_id") == sport_f)
    ], key=lambda m: m.get("start_ts", 0))

    if not upcoming:
        await update.message.reply_html(f"😴 Aucun match dans les {PREMATCH_HOURS}h.")
        return

    lines = [f"🕐 <b>{len(upcoming)} match(s) à venir</b>\n"]
    for m in upcoming[:15]:
        ts  = m.get("start_ts", 0)
        dt  = datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M")
        eta = max(0, (ts - time.time()) / 60)
        lines.append(
            f"• {dt} UTC ({eta:.0f}min) — "
            f"<b>{m['home']}</b> vs <b>{m['away']}</b> [{m.get('sport_name','?')}]"
        )
    await update.message.reply_html(
        "\n".join(lines), reply_markup=match_keyboard(upcoming)
    )


async def cmd_analyse(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    cached = WinamaxPoller.load_cached(DATA_DIR)
    if not cached:
        await update.message.reply_html("⚠️ Aucun match en cache, patiente…")
        return
    await update.message.reply_html(
        "🎯 Choisis un match :", reply_markup=match_keyboard(cached[:20])
    )


async def cmd_sport(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    await update.message.reply_html("🏆 Filtre par sport :", reply_markup=sport_keyboard())


async def cmd_alertes(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    if not allowed(update):
        return
    cid = update.effective_chat.id
    if cid in STATE.alert_chats:
        STATE.alert_chats.discard(cid)
        await update.message.reply_html("🔕 Alertes <b>désactivées</b>.")
    else:
        STATE.alert_chats.add(cid)
        await update.message.reply_html(
            f"🔔 Alertes <b>activées</b> !\n"
            f"Tu recevras les analyses (score ≥ {MIN_CONFIDENCE}/100)."
        )


# ──────────────────────────────────────────────
# Callbacks boutons
# ──────────────────────────────────────────────
async def callback_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    query   = update.callback_query
    chat_id = query.message.chat_id
    data    = query.data
    await query.answer()

    if data.startswith("sport_"):
        val = data[6:]
        if val == "all":
            STATE.sport_filter[chat_id] = None
            await query.edit_message_text("🌐 Filtre retiré.")
        else:
            sid = int(val)
            STATE.sport_filter[chat_id] = sid
            await query.edit_message_text(f"✅ Filtre : {SPORT_IDS.get(sid, '?')}")
        return

    if data.startswith("analyse_"):
        match_id = data[8:]
        cached   = WinamaxPoller.load_cached(DATA_DIR)
        match    = next((m for m in cached if m.get("match_id") == match_id), None)
        if not match:
            await query.edit_message_text("⚠️ Match introuvable ou expiré.")
            return
        await query.edit_message_text(
            f"🔍 Analyse : {match['home']} vs {match['away']}…\n(30–60s)"
        )
        try:
            report = await STATE.pipeline.run(match)
            STATE.pre_reports[match_id] = report
            await send(ctx, chat_id, report.to_telegram_message())
        except Exception as exc:
            log.error(f"Erreur analyse {match_id}: {exc}", exc_info=True)
            await send(ctx, chat_id, f"❌ Erreur : {exc}")


# ──────────────────────────────────────────────
# Tâches de fond
# ──────────────────────────────────────────────
async def background_poller(app: Application) -> None:
    log.info("🔄 Poller Winamax démarré")
    while True:
        try:
            await STATE.poller._tick()
        except Exception as exc:
            log.error(f"Poller: {exc}")
        await asyncio.sleep(POLL_INTERVAL_S)


async def background_live_updates(app: Application) -> None:
    await asyncio.sleep(30)
    while True:
        try:
            cached = WinamaxPoller.load_cached(DATA_DIR)
            for m in [x for x in cached if x.get("status") == "LIVE"][:8]:
                mid = m.get("match_id", "")
                pre = STATE.pre_reports.get(mid)
                if not pre:
                    continue
                lr = LiveAnalyzer().update(pre, m)
                STATE.live_reports[mid] = lr
                for cid in list(STATE.alert_chats):
                    sf = STATE.sport_filter.get(cid)
                    if sf and m.get("sport_id") != sf:
                        continue
                    await send(app, cid, lr.to_telegram_message())
        except Exception as exc:
            log.error(f"Live updates: {exc}")
        await asyncio.sleep(LIVE_UPDATE_S)


async def background_prematch_alerts(app: Application) -> None:
    await asyncio.sleep(60)
    while True:
        try:
            cached = WinamaxPoller.load_cached(DATA_DIR)
            now    = time.time()
            for m in cached:
                if m.get("status") != "PREMATCH":
                    continue
                mid = m.get("match_id", "")
                ts  = m.get("start_ts", 0)
                if not (45 * 60 <= ts - now <= 75 * 60):
                    continue
                if mid in STATE.last_sent:
                    continue
                try:
                    report = await STATE.pipeline.run(m)
                    STATE.pre_reports[mid] = report
                    STATE.last_sent[mid]   = now
                    if report.confidence.overall < MIN_CONFIDENCE:
                        continue
                    msg = report.to_telegram_message()
                    for cid in list(STATE.alert_chats):
                        sf = STATE.sport_filter.get(cid)
                        if sf and m.get("sport_id") != sf:
                            continue
                        await send(app, cid, msg)
                    await asyncio.sleep(5)
                except Exception as exc:
                    log.error(f"Alerte {mid}: {exc}")
        except Exception as exc:
            log.error(f"Prematch alerts: {exc}")
        await asyncio.sleep(300)


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────
def main() -> None:
    if BOT_TOKEN == "VOTRE_TOKEN_ICI":
        print("❌ Définis TELEGRAM_BOT_TOKEN dans config.py ou en variable d'env.")
        sys.exit(1)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CommandHandler("live",     cmd_live))
    app.add_handler(CommandHandler("upcoming", cmd_upcoming))
    app.add_handler(CommandHandler("analyse",  cmd_analyse))
    app.add_handler(CommandHandler("sport",    cmd_sport))
    app.add_handler(CommandHandler("alertes",  cmd_alertes))
    app.add_handler(CallbackQueryHandler(callback_handler))

    loop = asyncio.get_event_loop()
    loop.create_task(background_poller(app))
    loop.create_task(background_live_updates(app))
    loop.create_task(background_prematch_alerts(app))

    log.info("🚀 Bot démarré")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
