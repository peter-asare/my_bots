"""
====================================================
  BULL RUN ACADEMY — Telegram Trading Journal Bot v3
====================================================
Commands:
  /trade    — Entry, Close, Review, Add Note, Delete
  /stats    — All-time, Weekly, Streak, Summary, Pairs
  /calendar — This Month, Specific Month, Open/Closed Trades
  /tools    — Risk Calculator, Export CSV, Weekly Review
  /help     — Topic-aware help

Features:
  - Auto-posts today calendar to DASHBOARD on every trade
  - Auto-cleanup: 2min pair topics, 30min dashboard
  - Clickable screenshot links in calendar
  - Performance grading, streak tracking, auto duration
  - CSV export with screenshot links
  - Clean delete-as-you-go message flow
"""

import logging, json, os, csv, io, asyncio
from datetime import datetime
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    ConversationHandler, CallbackQueryHandler,
    ContextTypes, filters
)

# ─────────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────────
BOT_TOKEN        = "8927857908:AAEipdQn6Ro8RwhgrWp3dKSCSttGAiqf44g"
GROUP_ID         = -1003948912210
DASHBOARD_THREAD = 7

PAIR_THREADS = {
    "GBPUSD": 6,
    "BTCUSD": 5,
    "XAUUSD": 4,
    "USDCAD": 3,
    "ETHUSD": 2,
}

DATA_FILE = "trades.json"
_BOT = None  # set in main()

# ─────────────────────────────────────────────
#  AUTO-DELETE SYSTEM
# ─────────────────────────────────────────────
# {(chat_id, thread_id): [message_ids]}
_cleanup_msgs: dict = {}
# {(chat_id, thread_id): asyncio.Task}
_dashboard_timers: dict = {}


def track_msg(chat_id: int, thread_id: int, *message_ids: int):
    key = (chat_id, thread_id)
    if key not in _cleanup_msgs:
        _cleanup_msgs[key] = []
    _cleanup_msgs[key].extend(message_ids)


async def safe_delete(chat_id: int, message_id: int):
    try:
        await _BOT.delete_message(chat_id=chat_id, message_id=message_id)
        logger.info(f"🗑 Deleted msg {message_id}")
    except Exception as e:
        logger.warning(f"Could not delete msg {message_id}: {e}")


async def _delete_after_delay(chat_id: int, message_id: int, delay: int):
    await asyncio.sleep(delay)
    await safe_delete(chat_id, message_id)

def _safe_float(val):
    try:
        return float(str(val).replace(",", "").replace(" ", ""))
    except:
        return None




async def flush_tracked(chat_id: int, thread_id: int):
    key  = (chat_id, thread_id)
    msgs = _cleanup_msgs.pop(key, [])
    logger.info(f"🧹 Flushing {len(msgs)} msgs from thread {thread_id}")
    for mid in msgs:
        await safe_delete(chat_id, mid)


async def schedule_pair_flush(chat_id: int, thread_id: int, delay: int = 120):
    logger.info(f"⏱ Pair cleanup in {delay}s — thread {thread_id}")
    await asyncio.sleep(delay)
    await flush_tracked(chat_id, thread_id)


async def post_today_calendar():
    """Post today's trade summary to dashboard after inactivity cleanup."""
    now   = datetime.now()
    data  = load_trades()

    # filter only today's closed trades
    today_str    = now.strftime("%Y-%m-%d")
    today_trades = [
        t for t in data["trades"]
        if t.get("status") == "closed" and
        t.get("closed_at", "").startswith(today_str)
    ]
    open_trades = [t for t in data["trades"] if t.get("status") == "open"]

    wins   = sum(1 for t in today_trades if t.get("outcome") == "WIN")
    losses = sum(1 for t in today_trades if t.get("outcome") == "LOSS")
    pnl    = sum(float(t.get("pnl", 0)) for t in today_trades)
    pnl_icon = "✅" if pnl > 0 else ("❌" if pnl < 0 else "➖")
    pnl_str  = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"

    lines = [
        f"📅 *TODAY — {now.strftime('%A, %d %B %Y')}*",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Trades:  {len(today_trades)}  |  W/L: {wins}/{losses}  |  P&L: {pnl_icon} {pnl_str}",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
    ]

    if today_trades:
        for t in today_trades:
            t_icon  = "✅" if t.get("outcome") == "WIN" else "❌"
            d_icon  = "🟢" if t.get("direction") == "BUY" else "🔴"
            t_pnl   = float(t.get("pnl", 0))
            t_pnl_s = f"+${t_pnl:.2f}" if t_pnl >= 0 else f"-${abs(t_pnl):.2f}"
            pair    = t.get("pair", "?")
            tid_    = t.get("id", "?")
            sess    = t.get("session", "—")
            dur     = t.get("duration", "—")
            link    = t.get("entry_link") or t.get("close_link") or ""
            if link:
                lines.append(f"{t_icon} [#{tid_} {pair} {d_icon} {t_pnl_s}]({link})  _{sess} | {dur}_")
            else:
                lines.append(f"{t_icon} #{tid_} {pair} {d_icon} {t_pnl_s}  _{sess} | {dur}_")
    else:
        lines.append("_No closed trades today._")

    if open_trades:
        lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"🟡 *{len(open_trades)} trade(s) still open:*")
        for t in open_trades:
            d_icon = "🟢" if t.get("direction") == "BUY" else "🔴"
            link   = t.get("entry_link", "")
            if link:
                lines.append(f"   └ [#{t['id']} {t['pair']} {d_icon} @ {t.get('entry')}]({link})")
            else:
                lines.append(f"   └ #{t['id']} {t['pair']} {d_icon} @ {t.get('entry')}")

    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"_Updated: {now.strftime('%d/%m/%Y %H:%M')}_",
    ]

    try:
        await _BOT.send_message(chat_id=GROUP_ID, message_thread_id=DASHBOARD_THREAD, text="\n".join(lines), parse_mode="Markdown")
    except Exception as e:
        logger.warning(f"Today calendar post failed: {e}")


def reset_dashboard_timer(chat_id: int, thread_id: int):
    """Reset the 30-min dashboard cleanup timer on every activity."""
    key = (chat_id, thread_id)

    # cancel existing task
    existing = _dashboard_timers.get(key)
    if existing and not existing.done():
        existing.cancel()
        logger.info(f"⏰ Dashboard timer reset for thread {thread_id}")

    async def _run():
        logger.info(f"⏰ Dashboard 30-min timer started for thread {thread_id}")
        await asyncio.sleep(1800)
        logger.info(f"⏰ Dashboard cleanup firing for thread {thread_id}")
        await flush_tracked(chat_id, thread_id)
        await post_today_calendar()
        _dashboard_timers.pop(key, None)

    loop = asyncio.get_event_loop()
    task = loop.create_task(_run())
    _dashboard_timers[key] = task

# ─────────────────────────────────────────────
#  STATES
# ─────────────────────────────────────────────
(
    PAIR, DIRECTION, SESSION, ENTRY_PRICE, SL, TP, LOT,
    ENTRY_EMOTION, SETUP, ENTRY_SCREENSHOT,
    CLOSE_ID, CLOSE_EXIT, CLOSE_PNL, CLOSE_EMOTION, CLOSE_SCREENSHOT,
    DELETE_ID, DELETE_CONFIRM,
    REVIEW_ID,
    NOTE_ID, NOTE_TEXT,
    RISK_BALANCE, RISK_PERCENT, RISK_SL_PIPS, RISK_PIP_VALUE,
    CAL_MONTH,
) = range(25)

logging.basicConfig(format="%(asctime)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────
#  DATA HELPERS
# ─────────────────────────────────────────────
def load_trades() -> dict:
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            data = json.load(f)
        ids = [t["id"] for t in data["trades"] if "id" in t]
        data["next_id"] = (max(ids) + 1) if ids else 1
        return data
    return {"trades": [], "next_id": 1}


def save_trades(data: dict):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)


def msg_link(message_id: int) -> str:
    cid = str(GROUP_ID).replace("-100", "")
    return f"https://t.me/c/{cid}/{message_id}"


def compute_stats(trades: list, days: int = None) -> dict:
    now      = datetime.now()
    filtered = []
    for t in trades:
        if t.get("status") != "closed":
            continue
        if days:
            try:
                opened = datetime.fromisoformat(t["opened_at"])
                if (now - opened).days > days:
                    continue
            except:
                pass
        filtered.append(t)

    wins   = [t for t in filtered if t.get("outcome") == "WIN"]
    losses = [t for t in filtered if t.get("outcome") == "LOSS"]
    total  = len(filtered)
    pnl    = sum(float(t.get("pnl", 0)) for t in filtered)
    wr     = round(len(wins) / total * 100, 1) if total > 0 else 0

    # best pair
    pair_pnl = {}
    for t in filtered:
        p = t.get("pair", "?")
        pair_pnl[p] = pair_pnl.get(p, 0) + float(t.get("pnl", 0))
    best_pair = max(pair_pnl, key=pair_pnl.get) if pair_pnl else "—"

    # best session
    ses_pnl = {}
    for t in filtered:
        s = t.get("session", "?")
        ses_pnl[s] = ses_pnl.get(s, 0) + float(t.get("pnl", 0))
    best_ses = max(ses_pnl, key=ses_pnl.get) if ses_pnl else "—"

    # average RR
    rrs = []
    for t in filtered:
        try:
            rrs.append(float(t.get("rr", 0)))
        except:
            pass
    avg_rr = round(sum(rrs) / len(rrs), 2) if rrs else 0

    # average hold time
    holds = []
    for t in filtered:
        try:
            o = datetime.fromisoformat(t["opened_at"])
            c = datetime.fromisoformat(t["closed_at"])
            holds.append((c - o).total_seconds() / 3600)
        except:
            pass
    avg_hold = round(sum(holds) / len(holds), 1) if holds else 0

    # biggest win / loss
    pnls      = [float(t.get("pnl", 0)) for t in filtered]
    big_win   = max(pnls) if pnls else 0
    big_loss  = min(pnls) if pnls else 0

    # performance grade
    if wr >= 70 and avg_rr >= 2:    grade = "A+ 🏆"
    elif wr >= 60 and avg_rr >= 1.5: grade = "A 🥇"
    elif wr >= 50 and avg_rr >= 1:   grade = "B 🥈"
    elif wr >= 40:                    grade = "C 🥉"
    else:                             grade = "D ❌"

    return {
        "total": total, "wins": len(wins), "losses": len(losses),
        "win_rate": wr, "pnl": round(pnl, 2), "best_pair": best_pair,
        "best_session": best_ses, "avg_rr": avg_rr, "avg_hold": avg_hold,
        "big_win": round(big_win, 2), "big_loss": round(big_loss, 2),
        "grade": grade,
    }


def compute_streak(trades: list) -> dict:
    closed = sorted(
        [t for t in trades if t.get("status") == "closed"],
        key=lambda t: t.get("closed_at", "")
    )
    if not closed:
        return {"current": 0, "current_type": "—", "longest_win": 0, "longest_loss": 0}

    cur = 1; cur_type = closed[-1].get("outcome", "")
    for t in reversed(closed[:-1]):
        if t.get("outcome") == cur_type:
            cur += 1
        else:
            break

    # longest win streak
    lw = lc = 0; best_lw = best_ll = 0
    for t in closed:
        if t.get("outcome") == "WIN":
            lw += 1; lc = 0
            best_lw = max(best_lw, lw)
        else:
            lc += 1; lw = 0
            best_ll = max(best_ll, lc)

    return {
        "current": cur,
        "current_type": cur_type,
        "longest_win": best_lw,
        "longest_loss": best_ll,
    }


def build_calendar_text(year: int, month: int, trades: list) -> str:
    import calendar as cal
    month_name = datetime(year, month, 1).strftime("%B %Y")

    # group trades by day — store summary AND individual trade links
    day_results = {}
    day_trades  = {}   # key → list of trade dicts for that day
    for t in trades:
        if t.get("status") != "closed":
            continue
        try:
            d   = datetime.fromisoformat(t["closed_at"])
            if d.year != year or d.month != month:
                continue
            key = d.strftime("%Y-%m-%d")
            if key not in day_results:
                day_results[key] = {"wins": 0, "losses": 0, "pnl": 0}
                day_trades[key]  = []
            if t.get("outcome") == "WIN":
                day_results[key]["wins"] += 1
            else:
                day_results[key]["losses"] += 1
            day_results[key]["pnl"] += float(t.get("pnl", 0))
            day_trades[key].append(t)
        except:
            continue

    lines = [
        f"📅 *TRADING CALENDAR — {month_name}*",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "`Mo  Tu  We  Th  Fr  Sa  Su`",
    ]
    matrix = cal.monthcalendar(year, month)
    today  = datetime.now().date()

    for week in matrix:
        row_days      = []
        day_pnl_lines = []
        for day in week:
            if day == 0:
                row_days.append("    ")
                continue
            date_obj = datetime(year, month, day).date()
            key      = date_obj.strftime("%Y-%m-%d")
            is_today = (date_obj == today)
            result   = day_results.get(key)
            label    = f"[{day}]" if is_today else f"{day:2d}"
            row_days.append(f"{label:<4}")

            if result:
                pnl     = result["pnl"]
                icon    = "✅" if pnl > 0 else ("❌" if pnl < 0 else "➖")
                pnl_str = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
                count   = result["wins"] + result["losses"]

                # summary line
                day_pnl_lines.append(
                    f"{icon} *{date_obj.strftime('%d %b')}*  {pnl_str}  ({count} trade{'s' if count != 1 else ''})"
                )

                # individual trade links
                for t in day_trades[key]:
                    t_icon  = "✅" if t.get("outcome") == "WIN" else "❌"
                    d_icon  = "🟢" if t.get("direction") == "BUY" else "🔴"
                    t_pnl   = float(t.get("pnl", 0))
                    pnl_s   = f"+${t_pnl:.2f}" if t_pnl >= 0 else f"-${abs(t_pnl):.2f}"
                    pair    = t.get("pair", "?")
                    tid     = t.get("id", "?")

                    # build link — prefer entry_link, fall back to close_link
                    link = t.get("entry_link") or t.get("close_link") or ""
                    if link:
                        trade_line = f"   └ {t_icon} [#{tid} {pair} {d_icon} {pnl_s}]({link})"
                    else:
                        trade_line = f"   └ {t_icon} #{tid} {pair} {d_icon} {pnl_s}"
                    day_pnl_lines.append(trade_line)

        lines.append("`" + "".join(row_days) + "`")
        for pl in day_pnl_lines:
            lines.append(pl)

    month_trades = [
        t for t in trades
        if t.get("status") == "closed" and
        t.get("closed_at", "").startswith(f"{year}-{month:02d}")
    ]
    wins   = sum(1 for t in month_trades if t.get("outcome") == "WIN")
    losses = sum(1 for t in month_trades if t.get("outcome") == "LOSS")
    pnl    = sum(float(t.get("pnl", 0)) for t in month_trades)
    wr     = round(wins / len(month_trades) * 100, 1) if month_trades else 0

    # streak for this month
    streak = compute_streak(month_trades)
    streak_icon = "🔥" if streak["current_type"] == "WIN" else "❄️"

    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        "✅ Profit day   ❌ Loss day   ➖ Break even",
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
        f"Trades:  {len(month_trades)}  |  W/L: {wins}/{losses}  |  WR: {wr}%",
        f"P&L:     ${pnl:+.2f}",
        f"Streak:  {streak_icon} {streak['current']} {streak['current_type'].lower()} streak",
        f"_Updated: {datetime.now().strftime('%d/%m/%Y %H:%M')}_",
    ]
    return "\n".join(lines)


# ─────────────────────────────────────────────
#  MENU COMMANDS
# ─────────────────────────────────────────────
async def trade_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kbd = [
        [InlineKeyboardButton("📥 New Entry",    callback_data="tm:entry"),
         InlineKeyboardButton("✅ Close Trade",  callback_data="tm:close")],
        [InlineKeyboardButton("🔍 Review Trade", callback_data="tm:review"),
         InlineKeyboardButton("📝 Add Note",     callback_data="tm:note")],
        [InlineKeyboardButton("🗑 Delete Trade", callback_data="tm:delete")],
    ]
    sent = await update.message.reply_text(
        "📌 *TRADE MENU*\nWhat would you like to do?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kbd)
    )
    await safe_delete(update.message.chat_id, update.message.message_id)
    ctx.user_data["trade_menu_msg"] = sent.message_id


async def stats_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kbd = [
        [InlineKeyboardButton("📈 All-time Stats", callback_data="sm:alltime"),
         InlineKeyboardButton("📅 Weekly Stats",   callback_data="sm:weekly")],
        [InlineKeyboardButton("🔥 Streak",         callback_data="sm:streak"),
         InlineKeyboardButton("💡 Today Summary",  callback_data="sm:summary")],
        [InlineKeyboardButton("🏆 Pairs",          callback_data="sm:pairs")],
    ]
    sent = await update.message.reply_text(
        "📊 *STATS MENU*\nWhat would you like to see?",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kbd)
    )
    await safe_delete(update.message.chat_id, update.message.message_id)
    tid = update.message.message_thread_id or 0
    track_msg(update.message.chat_id, tid, sent.message_id)
    if tid == DASHBOARD_THREAD:
        reset_dashboard_timer(update.message.chat_id, tid)


async def calendar_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kbd = [
        [InlineKeyboardButton("📅 This Month",     callback_data="cm:thismonth"),
         InlineKeyboardButton("🗓 Specific Month", callback_data="cm:specific")],
        [InlineKeyboardButton("📂 Open Trades",    callback_data="cm:open"),
         InlineKeyboardButton("✅ Closed Trades",  callback_data="cm:closed")],
    ]
    sent = await update.message.reply_text(
        "📅 *CALENDAR MENU*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kbd)
    )
    await safe_delete(update.message.chat_id, update.message.message_id)
    tid = update.message.message_thread_id or 0
    track_msg(update.message.chat_id, tid, sent.message_id)
    if tid == DASHBOARD_THREAD:
        reset_dashboard_timer(update.message.chat_id, tid)


async def tools_menu(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    kbd = [
        [InlineKeyboardButton("🧮 Risk Calculator", callback_data="tolm:risk"),
         InlineKeyboardButton("📤 Export CSV",      callback_data="tolm:export")],
        [InlineKeyboardButton("🗑 Delete Trade",    callback_data="tolm:delete"),
         InlineKeyboardButton("📆 Weekly Review",   callback_data="tolm:weekly")],
    ]
    sent = await update.message.reply_text(
        "🛠 *TOOLS MENU*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(kbd)
    )
    await safe_delete(update.message.chat_id, update.message.message_id)
    tid = update.message.message_thread_id or 0
    track_msg(update.message.chat_id, tid, sent.message_id)
    if tid == DASHBOARD_THREAD:
        reset_dashboard_timer(update.message.chat_id, tid)


async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg        = update.message
    thread_id  = msg.message_thread_id
    chat_id    = msg.chat_id

    # detect which topic this is
    is_dashboard = (thread_id == DASHBOARD_THREAD)
    is_pair      = thread_id in PAIR_THREADS.values()
    pair_name    = next((k for k, v in PAIR_THREADS.items() if v == thread_id), None)

    if is_dashboard:
        text = (
            "📊 *DASHBOARD COMMANDS*\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "/stats    — Performance stats menu\n"
            "  ├ 📈 All-time stats & grade\n"
            "  ├ 📅 Weekly breakdown\n"
            "  ├ 🔥 Win/loss streak\n"
            "  ├ 💡 Today's summary\n"
            "  └ 🏆 Performance by pair\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "/calendar — Trading calendar\n"
            "  ├ 📅 This month's P&L calendar\n"
            "  └ 🗓 View any specific month\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "/tools    — Utility tools\n"
            "  ├ 🧮 Risk/lot size calculator\n"
            "  ├ 📤 Export trades as CSV\n"
            "  ├ 🗑 Delete a trade log\n"
            "  └ 📆 Post weekly review\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "_/cancel — Cancel any action_\n"
            "_/skip   — Skip screenshot step_"
        )
    elif is_pair:
        text = (
            f"📌 *{pair_name} TOPIC COMMANDS*\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "/trade    — Trade menu\n"
            f"  ├ 📥 Log new {pair_name} entry\n"
            f"  ├ ✅ Close open {pair_name} trade\n"
            "  ├ 🔍 Review any trade by ID\n"
            "  ├ 📂 View all open trades\n"
            "  └ 📝 Add note to a trade\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "/tools    — Quick tools\n"
            "  ├ 🧮 Risk/lot size calculator\n"
            "  └ 📤 Export trades as CSV\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "_/cancel — Cancel any action_\n"
            "_/skip   — Skip screenshot step_\n"
            "_Type 'back' at any step to go back_"
        )
    else:
        # general / private chat
        text = (
            "🤖 *Bull Run Academy — Journal Bot*\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "/trade    — Log & manage trades\n"
            "/stats    — Performance stats\n"
            "/calendar — Trading calendar\n"
            "/tools    — Risk calc, export, delete\n"
            "/help     — Show this menu\n"
            "━━━━━━━━━━━━━━━━━━━━\n"
            "_Use /help in a pair topic for trade commands._\n"
            "_Use /help in DASHBOARD for stats commands._"
        )
    await msg.reply_text(text, parse_mode="Markdown")


# ─────────────────────────────────────────────
#  MENU CALLBACK ROUTER
# ─────────────────────────────────────────────
async def menu_router(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q      = update.callback_query
    await q.answer()
    data   = q.data

    # TRADE MENU
    if data == "tm:entry":
        ctx.user_data.clear()
        await safe_delete(q.message.chat_id, q.message.message_id)
        pairs = list(PAIR_THREADS.keys())
        kbd   = [[InlineKeyboardButton(p, callback_data=f"pair:{p}")] for p in pairs]
        sent  = await ctx.bot.send_message(
            chat_id=q.message.chat_id,
            message_thread_id=q.message.message_thread_id,
            text="📌 *New Trade Entry*\nSelect the pair:",
            parse_mode="Markdown",
            reply_markup=InlineKeyboardMarkup(kbd)
        )
        ctx.user_data["last_bot_msg"] = sent.message_id
        return PAIR

    elif data == "tm:close":
        await safe_delete(q.message.chat_id, q.message.message_id)
        return await _close_start(q.message, ctx)

    elif data == "tm:review":
        await q.edit_message_text("🔍 Enter the *Trade ID* to review:", parse_mode="Markdown")
        ctx.user_data["last_bot_msg"] = q.message.message_id
        ctx.user_data["menu_chat_id"] = q.message.chat_id
        return REVIEW_ID

    elif data == "tm:note":
        await q.edit_message_text("📝 Enter the *Trade ID* to add a note to:", parse_mode="Markdown")
        ctx.user_data["last_bot_msg"] = q.message.message_id
        ctx.user_data["menu_chat_id"] = q.message.chat_id
        return NOTE_ID

    elif data == "tm:delete":
        await q.edit_message_text("🗑 Enter the *Trade ID* to delete:", parse_mode="Markdown")
        ctx.user_data["last_bot_msg"] = q.message.message_id
        ctx.user_data["menu_chat_id"] = q.message.chat_id
        return DELETE_ID

    # STATS MENU
    elif data == "sm:alltime":
        await safe_delete(q.message.chat_id, q.message.message_id)
        await _show_alltime_stats(q.message, ctx)
    elif data == "sm:weekly":
        await safe_delete(q.message.chat_id, q.message.message_id)
        await _show_weekly_stats(q.message, ctx)
    elif data == "sm:streak":
        await safe_delete(q.message.chat_id, q.message.message_id)
        await _show_streak(q.message, ctx)
    elif data == "sm:summary":
        await safe_delete(q.message.chat_id, q.message.message_id)
        await _show_summary(q.message, ctx)
    elif data == "sm:pairs":
        await safe_delete(q.message.chat_id, q.message.message_id)
        await _show_pairs(q.message, ctx)

    # CALENDAR MENU
    elif data == "cm:thismonth":
        await safe_delete(q.message.chat_id, q.message.message_id)
        now = datetime.now()
        d   = load_trades()
        msg = build_calendar_text(now.year, now.month, d["trades"])
        try:
            await ctx.bot.send_message(
                chat_id=GROUP_ID, message_thread_id=DASHBOARD_THREAD,
                text=msg, parse_mode="Markdown"
            )
        except:
            await q.message.reply_text(msg, parse_mode="Markdown")

    elif data == "cm:specific":
        await q.edit_message_text(
            "Enter month and year:\n_(Format: MM YYYY — e.g. `05 2026`)_",
            parse_mode="Markdown"
        )
        ctx.user_data["last_bot_msg"] = q.message.message_id
        return CAL_MONTH

    elif data == "cm:open":
        await safe_delete(q.message.chat_id, q.message.message_id)
        await _show_open_trades(q.message, ctx)

    elif data == "cm:closed":
        await safe_delete(q.message.chat_id, q.message.message_id)
        await _show_closed_trades(q.message, ctx)

    # TOOLS MENU
    elif data == "tolm:risk":
        await q.edit_message_text(
            "🧮 *Risk Calculator*\nEnter your *account balance* (e.g. 1000):",
            parse_mode="Markdown"
        )
        ctx.user_data["last_bot_msg"] = q.message.message_id
        return RISK_BALANCE

    elif data == "tolm:export":
        await safe_delete(q.message.chat_id, q.message.message_id)
        await _export_csv(q.message, ctx)

    elif data == "tolm:delete":
        await q.edit_message_text("🗑 Enter the *Trade ID* to delete:", parse_mode="Markdown")
        ctx.user_data["last_bot_msg"] = q.message.message_id
        ctx.user_data["menu_chat_id"] = q.message.chat_id
        return DELETE_ID

    elif data == "tolm:weekly":
        await safe_delete(q.message.chat_id, q.message.message_id)
        await _post_weekly(q.message, ctx)

    return ConversationHandler.END


# ─────────────────────────────────────────────
#  ENTRY FLOW
# ─────────────────────────────────────────────
async def got_pair(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["pair"] = q.data.split(":")[1]
    kbd = [
        [InlineKeyboardButton("🟢 BUY",  callback_data="dir:BUY"),
         InlineKeyboardButton("🔴 SELL", callback_data="dir:SELL")]
    ]
    sent = await ctx.bot.send_message(
        chat_id=q.message.chat_id,
        message_thread_id=q.message.message_thread_id,
        text=f"Pair: *{ctx.user_data['pair']}*\nDirection?",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kbd)
    )
    await safe_delete(q.message.chat_id, q.message.message_id)
    ctx.user_data["last_bot_msg"] = sent.message_id
    return DIRECTION


async def got_direction(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["direction"] = q.data.split(":")[1]
    sessions = ["London", "New York", "Asian", "Overlap"]
    kbd = [[InlineKeyboardButton(s, callback_data=f"ses:{s}")] for s in sessions]
    sent = await ctx.bot.send_message(
        chat_id=q.message.chat_id,
        message_thread_id=q.message.message_thread_id,
        text=f"Direction: *{ctx.user_data['direction']}*\nSession?",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kbd)
    )
    await safe_delete(q.message.chat_id, q.message.message_id)
    ctx.user_data["last_bot_msg"] = sent.message_id
    return SESSION


async def got_session(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["session"] = q.data.split(":")[1]
    sent = await ctx.bot.send_message(
        chat_id=q.message.chat_id,
        message_thread_id=q.message.message_thread_id,
        text=f"Session: *{ctx.user_data['session']}*\nEnter your *entry price*:\n_Type 'back' to go back_",
        parse_mode="Markdown"
    )
    await safe_delete(q.message.chat_id, q.message.message_id)
    ctx.user_data["last_bot_msg"] = sent.message_id
    return ENTRY_PRICE


async def got_entry_price(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    text    = update.message.text.strip()

    try:
        if text.lower() == "back":
            pairs = list(PAIR_THREADS.keys())
            kbd   = [[InlineKeyboardButton(p, callback_data=f"pair:{p}")] for p in pairs]
            sent  = await update.message.reply_text("◀️ Select the *pair*:", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kbd))
            if ctx.user_data.get("last_bot_msg"):
                await safe_delete(chat_id, ctx.user_data["last_bot_msg"])
            await safe_delete(chat_id, update.message.message_id)
            ctx.user_data["last_bot_msg"] = sent.message_id
            return PAIR

        ctx.user_data["entry"] = text
        sent = await update.message.reply_text("Enter your *Stop Loss* price:\n_Type 'back' to go back_", parse_mode="Markdown")
        if ctx.user_data.get("last_bot_msg"):
            await safe_delete(chat_id, ctx.user_data["last_bot_msg"])
        await safe_delete(chat_id, update.message.message_id)
        ctx.user_data["last_bot_msg"] = sent.message_id
        return SL
    except Exception as e:
        logger.error(f"got_entry_price error: {e}")
        sent = await update.message.reply_text("Enter your *Stop Loss* price:\n_Type 'back' to go back_", parse_mode="Markdown")
        ctx.user_data["last_bot_msg"] = sent.message_id
        return SL


async def got_sl(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    text    = update.message.text.strip()

    if text.lower() == "back":
        sent = await update.message.reply_text("◀️ Enter your *entry price*:\n_Type 'back' to go back_", parse_mode="Markdown")
        if ctx.user_data.get("last_bot_msg"):
            await safe_delete(chat_id, ctx.user_data["last_bot_msg"])
        await safe_delete(chat_id, update.message.message_id)
        ctx.user_data["last_bot_msg"] = sent.message_id
        return ENTRY_PRICE

    ctx.user_data["sl"] = text
    sent = await update.message.reply_text("Enter your *Take Profit* price:\n_Type 'back' to go back_", parse_mode="Markdown")
    if ctx.user_data.get("last_bot_msg"):
        await safe_delete(chat_id, ctx.user_data["last_bot_msg"])
    await safe_delete(chat_id, update.message.message_id)
    ctx.user_data["last_bot_msg"] = sent.message_id
    return TP


async def got_tp(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    text    = update.message.text.strip()

    if text.lower() == "back":
        sent = await update.message.reply_text("◀️ Enter your *Stop Loss* price:\n_Type 'back' to go back_", parse_mode="Markdown")
        if ctx.user_data.get("last_bot_msg"):
            await safe_delete(chat_id, ctx.user_data["last_bot_msg"])
        await safe_delete(chat_id, update.message.message_id)
        ctx.user_data["last_bot_msg"] = sent.message_id
        return SL

    ctx.user_data["tp"] = text
    sent = await update.message.reply_text("Enter your *lot size* (e.g. 0.10):\n_Type 'back' to go back_", parse_mode="Markdown")
    if ctx.user_data.get("last_bot_msg"):
        await safe_delete(chat_id, ctx.user_data["last_bot_msg"])
    await safe_delete(chat_id, update.message.message_id)
    ctx.user_data["last_bot_msg"] = sent.message_id
    return LOT


async def got_lot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    text    = update.message.text.strip()

    if text.lower() == "back":
        sent = await update.message.reply_text("◀️ Enter your *Take Profit* price:\n_Type 'back' to go back_", parse_mode="Markdown")
        if ctx.user_data.get("last_bot_msg"):
            await safe_delete(chat_id, ctx.user_data["last_bot_msg"])
        await safe_delete(chat_id, update.message.message_id)
        ctx.user_data["last_bot_msg"] = sent.message_id
        return TP

    ctx.user_data["lot"] = text
    kbd = [
        [InlineKeyboardButton("😌 Calm",        callback_data="emo:Calm"),
         InlineKeyboardButton("💪 Confident",   callback_data="emo:Confident")],
        [InlineKeyboardButton("😰 FOMO",        callback_data="emo:FOMO"),
         InlineKeyboardButton("😤 Impatient",   callback_data="emo:Impatient")],
        [InlineKeyboardButton("😟 Anxious",     callback_data="emo:Anxious"),
         InlineKeyboardButton("😎 Disciplined", callback_data="emo:Disciplined")],
    ]
    sent = await update.message.reply_text("How are you *feeling* going into this trade?", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kbd))
    if ctx.user_data.get("last_bot_msg"):
        await safe_delete(chat_id, ctx.user_data["last_bot_msg"])
    await safe_delete(chat_id, update.message.message_id)
    ctx.user_data["last_bot_msg"] = sent.message_id
    return ENTRY_EMOTION


async def got_entry_emotion(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["emotion"] = q.data.split(":")[1]
    sent = await ctx.bot.send_message(
        chat_id=q.message.chat_id,
        message_thread_id=q.message.message_thread_id,
        text=f"Emotion: *{ctx.user_data['emotion']}*\n\nDescribe your *setup & confluences*\n_(e.g. Breaker Block + FVG)_\n_Type 'back' to go back_:",
        parse_mode="Markdown"
    )
    await safe_delete(q.message.chat_id, q.message.message_id)
    ctx.user_data["last_bot_msg"] = sent.message_id
    return SETUP


async def got_setup(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    text    = update.message.text.strip()

    if text.lower() == "back":
        kbd = [
            [InlineKeyboardButton("😌 Calm",        callback_data="emo:Calm"),
             InlineKeyboardButton("💪 Confident",   callback_data="emo:Confident")],
            [InlineKeyboardButton("😰 FOMO",        callback_data="emo:FOMO"),
             InlineKeyboardButton("😤 Impatient",   callback_data="emo:Impatient")],
            [InlineKeyboardButton("😟 Anxious",     callback_data="emo:Anxious"),
             InlineKeyboardButton("😎 Disciplined", callback_data="emo:Disciplined")],
        ]
        sent = await update.message.reply_text("◀️ How are you *feeling*?", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kbd))
        if ctx.user_data.get("last_bot_msg"):
            await safe_delete(chat_id, ctx.user_data["last_bot_msg"])
        await safe_delete(chat_id, update.message.message_id)
        ctx.user_data["last_bot_msg"] = sent.message_id
        return ENTRY_EMOTION

    u          = ctx.user_data
    u["setup"] = text
    try:
        e = _safe_float(u.get("entry")); s = _safe_float(u.get("sl")); t = _safe_float(u.get("tp"))
        if e and s and t:
            risk = abs(e - s); reward = abs(t - e)
            rr   = round(reward / risk, 1) if risk else 0
        else:
            rr = "?"
    except:
        rr = "?"
    u["rr"] = str(rr)

    sent = await update.message.reply_text(
        "📸 Send your *trade screenshot*\n_(photo from desktop/phone)_\n\nOr /skip to post without one.",
        parse_mode="Markdown"
    )
    if ctx.user_data.get("last_bot_msg"):
        await safe_delete(chat_id, ctx.user_data["last_bot_msg"])
    await safe_delete(chat_id, update.message.message_id)
    ctx.user_data["last_bot_msg"] = sent.message_id
    return ENTRY_SCREENSHOT








async def got_entry_screenshot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u        = ctx.user_data
    data     = load_trades()
    trade_id = data["next_id"]
    u["id"]  = trade_id
    u["status"]    = "open"
    u["opened_at"] = datetime.now().isoformat()

    icon    = "🟢" if u["direction"] == "BUY" else "🔴"
    caption = (
        f"📌 *TRADE ENTRY — #{trade_id}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Pair:       *{u['pair']}*\n"
        f"Date:       {datetime.now().strftime('%d/%m/%Y %H:%M')}\n"
        f"Session:    {u['session']}\n"
        f"Direction:  {icon} *{u['direction']}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Entry:      `{u['entry']}`\n"
        f"SL:         `{u['sl']}`\n"
        f"TP:         `{u['tp']}`\n"
        f"RR:         1:{u['rr']}\n"
        f"Lot Size:   {u['lot']}\n"
        f"Emotions:   {u.get('emotion', '—')}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Setup:      {u['setup']}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"_Trade ID: #{trade_id} | Use /trade → Close to log result_"
    )

    thread_id  = PAIR_THREADS.get(u["pair"])
    entry_link = ""
    try:
        if update.message.photo:
            photo    = update.message.photo[-1].file_id
            sent     = await ctx.bot.send_photo(chat_id=GROUP_ID, message_thread_id=thread_id, photo=photo, caption=caption, parse_mode="Markdown")
        else:
            sent     = await ctx.bot.send_message(chat_id=GROUP_ID, message_thread_id=thread_id, text=caption, parse_mode="Markdown")
        entry_link = msg_link(sent.message_id)
    except Exception as e:
        logger.warning(f"Post failed: {e}")
        await update.message.reply_text(caption, parse_mode="Markdown")

    trade_record               = dict(u)
    trade_record["entry_link"] = entry_link
    data["trades"].append(trade_record)
    data["next_id"] += 1
    save_trades(data)

    # auto-update dashboard with today's calendar
    asyncio.get_event_loop().create_task(post_today_calendar())

    tid  = update.message.message_thread_id or 0
    # delete screenshot prompt and user's photo message
    if ctx.user_data.get("last_bot_msg"):
        await safe_delete(update.message.chat_id, ctx.user_data["last_bot_msg"])
    await safe_delete(update.message.chat_id, update.message.message_id)

    sent = await update.message.reply_text(f"✅ Trade *#{trade_id}* logged!\n🔗 {entry_link}", parse_mode="Markdown")
    # delete confirmation after 5 seconds
    asyncio.get_event_loop().create_task(
        _delete_after_delay(update.message.chat_id, sent.message_id, 5)
    )

    ctx.user_data.clear()
    return ConversationHandler.END


async def skip_entry_screenshot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    return await got_entry_screenshot(update, ctx)


# ─────────────────────────────────────────────
#  CLOSE FLOW
# ─────────────────────────────────────────────
async def _close_start(message, ctx):
    data   = load_trades()
    open_t = [t for t in data["trades"] if t.get("status") == "open"]
    if not open_t:
        sent = await message.reply_text("No open trades found.")
        await safe_delete(message.chat_id, sent.message_id)
        return ConversationHandler.END
    lines = "\n".join([f"#{t['id']} — {t['pair']} {t['direction']} @ {t['entry']}" for t in open_t])
    sent  = await message.reply_text(f"Open trades:\n`{lines}`\n\nEnter the *Trade ID* to close:", parse_mode="Markdown")
    ctx.user_data["last_bot_msg"] = sent.message_id
    return CLOSE_ID


async def got_close_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    try:
        ctx.user_data["close_id"] = int(update.message.text.strip().replace("#", ""))
    except:
        sent = await update.message.reply_text("❌ Invalid ID. Try again.")
        if ctx.user_data.get("last_bot_msg"):
            await safe_delete(chat_id, ctx.user_data["last_bot_msg"])
        await safe_delete(chat_id, update.message.message_id)
        ctx.user_data["last_bot_msg"] = sent.message_id
        return CLOSE_ID
    sent = await update.message.reply_text("Enter your *exit price*:", parse_mode="Markdown")
    if ctx.user_data.get("last_bot_msg"):
        await safe_delete(chat_id, ctx.user_data["last_bot_msg"])
    await safe_delete(chat_id, update.message.message_id)
    ctx.user_data["last_bot_msg"] = sent.message_id
    return CLOSE_EXIT


async def got_close_exit(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    ctx.user_data["exit"] = update.message.text.strip()
    sent = await update.message.reply_text("Enter *P&L* (e.g. +63.50 or -25.00):", parse_mode="Markdown")
    if ctx.user_data.get("last_bot_msg"):
        await safe_delete(chat_id, ctx.user_data["last_bot_msg"])
    await safe_delete(chat_id, update.message.message_id)
    ctx.user_data["last_bot_msg"] = sent.message_id
    return CLOSE_PNL


async def got_close_pnl(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    ctx.user_data["pnl"] = update.message.text.strip()
    kbd = [
        [InlineKeyboardButton("😌 Calm",        callback_data="cemo:Calm"),
         InlineKeyboardButton("💪 Confident",   callback_data="cemo:Confident")],
        [InlineKeyboardButton("😰 FOMO",        callback_data="cemo:FOMO"),
         InlineKeyboardButton("😤 Impatient",   callback_data="cemo:Impatient")],
        [InlineKeyboardButton("😟 Anxious",     callback_data="cemo:Anxious"),
         InlineKeyboardButton("😎 Disciplined", callback_data="cemo:Disciplined")],
    ]
    sent = await update.message.reply_text("How did you *feel* during/after this trade?", parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kbd))
    if ctx.user_data.get("last_bot_msg"):
        await safe_delete(chat_id, ctx.user_data["last_bot_msg"])
    await safe_delete(chat_id, update.message.message_id)
    ctx.user_data["last_bot_msg"] = sent.message_id
    return CLOSE_EMOTION


async def got_close_emotion(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    ctx.user_data["close_emotion"] = q.data.split(":")[1]
    sent = await ctx.bot.send_message(
        chat_id=q.message.chat_id,
        message_thread_id=q.message.message_thread_id,
        text=f"Emotion: *{ctx.user_data['close_emotion']}*\n\n📸 Send your *closed trade screenshot*\nOr /skip to post without one.",
        parse_mode="Markdown"
    )
    await safe_delete(q.message.chat_id, q.message.message_id)
    ctx.user_data["last_bot_msg"] = sent.message_id
    return CLOSE_SCREENSHOT


async def got_close_screenshot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u        = ctx.user_data
    emotion  = u.get("close_emotion", "—")
    trade_id = u["close_id"]

    data  = load_trades()
    trade = next((t for t in data["trades"] if t["id"] == trade_id), None)
    if not trade:
        await update.message.reply_text(f"Trade #{trade_id} not found.")
        return ConversationHandler.END

    pnl_val = float(u["pnl"].replace("+", ""))
    outcome = "WIN ✅" if pnl_val > 0 else ("LOSS ❌" if pnl_val < 0 else "BREAK EVEN ➖")

    # auto calculate duration
    duration_str = "—"
    try:
        opened_raw = trade.get("opened_at", "")
        if opened_raw:
            opened   = datetime.fromisoformat(opened_raw)
            closed   = datetime.now()
            diff     = closed - opened
            total_m  = int(diff.total_seconds() // 60)
            hrs      = total_m // 60
            mins     = total_m % 60
            if hrs > 0:
                duration_str = f"{hrs}h {mins}m"
            else:
                duration_str = f"{mins}m"
    except Exception as e:
        logger.warning(f"Duration calc failed: {e}")

    trade["exit"]       = u["exit"]
    trade["pnl"]        = pnl_val
    trade["outcome"]    = "WIN" if pnl_val > 0 else "LOSS"
    trade["emotion"]    = emotion
    trade["status"]     = "closed"
    trade["closed_at"]  = datetime.now().isoformat()
    trade["duration"]   = duration_str

    icon    = "🟢" if trade["direction"] == "BUY" else "🔴"
    caption = (
        f"{'✅' if pnl_val > 0 else '❌'} *TRADE CLOSED — #{trade_id}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Pair:       *{trade['pair']}*\n"
        f"Direction:  {icon} {trade['direction']}\n"
        f"Entry:      `{trade['entry']}`  →  Exit: `{u['exit']}`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"P&L:        *{'+' if pnl_val>0 else ''}{pnl_val}*\n"
        f"Outcome:    {outcome}\n"
        f"Duration:   {duration_str}\n"
        f"Emotions:   {emotion}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"_Closed {datetime.now().strftime('%d/%m/%Y %H:%M')}_"
    )

    thread_id  = PAIR_THREADS.get(trade["pair"])
    close_link = ""
    try:
        if update.message.photo:
            photo = update.message.photo[-1].file_id
            sent  = await ctx.bot.send_photo(chat_id=GROUP_ID, message_thread_id=thread_id, photo=photo, caption=caption, parse_mode="Markdown")
        else:
            sent  = await ctx.bot.send_message(chat_id=GROUP_ID, message_thread_id=thread_id, text=caption, parse_mode="Markdown")
        close_link = msg_link(sent.message_id)
    except Exception as e:
        logger.warning(f"Post failed: {e}")
        await update.message.reply_text(caption, parse_mode="Markdown")

    trade["close_link"] = close_link
    save_trades(data)

    # auto-update dashboard with today's calendar
    asyncio.get_event_loop().create_task(post_today_calendar())

    # delete screenshot prompt and photo
    if ctx.user_data.get("last_bot_msg"):
        await safe_delete(update.message.chat_id, ctx.user_data["last_bot_msg"])
    await safe_delete(update.message.chat_id, update.message.message_id)

    sent = await update.message.reply_text(f"✅ Trade closed & dashboard updated!\n🔗 {close_link}", parse_mode="Markdown")
    asyncio.get_event_loop().create_task(_delete_after_delay(update.message.chat_id, sent.message_id, 5))

    ctx.user_data.clear()
    return ConversationHandler.END


async def skip_close_screenshot(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    return await got_close_screenshot(update, ctx)


# ─────────────────────────────────────────────
#  REVIEW TRADE
# ─────────────────────────────────────────────
async def got_review_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    # delete the prompt and user's ID message
    if ctx.user_data.get("last_bot_msg"):
        await safe_delete(chat_id, ctx.user_data["last_bot_msg"])
    await safe_delete(chat_id, update.message.message_id)

    try:
        trade_id = int(update.message.text.strip().replace("#", ""))
    except:
        sent = await update.message.reply_text("Invalid ID. Try again or /cancel")
        ctx.user_data["last_bot_msg"] = sent.message_id
        return REVIEW_ID

    data  = load_trades()
    trade = next((t for t in data["trades"] if t["id"] == trade_id), None)
    if not trade:
        sent = await update.message.reply_text(f"Trade #{trade_id} not found.")
        asyncio.get_event_loop().create_task(_delete_after_delay(chat_id, sent.message_id, 5))
        return ConversationHandler.END

    icon    = "🟢" if trade.get("direction") == "BUY" else "🔴"
    status  = trade.get("status", "")
    pnl     = trade.get("pnl", "—")
    pnl_str = f"${float(pnl):+.2f}" if pnl != "—" else "—"

    msg = (
        f"🔍 *TRADE REVIEW — #{trade_id}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Pair:       *{trade.get('pair')}*\n"
        f"Direction:  {icon} {trade.get('direction')}\n"
        f"Session:    {trade.get('session', '—')}\n"
        f"Status:     {'🟡 Open' if status == 'open' else ('✅ Win' if trade.get('outcome') == 'WIN' else '❌ Loss')}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Entry:      `{trade.get('entry')}`\n"
        f"SL:         `{trade.get('sl')}`\n"
        f"TP:         `{trade.get('tp')}`\n"
        f"Exit:       `{trade.get('exit', '—')}`\n"
        f"RR:         1:{trade.get('rr', '—')}\n"
        f"Lot Size:   {trade.get('lot', '—')}\n"
        f"P&L:        {pnl_str}\n"
        f"Duration:   {trade.get('duration', '—')}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Emotions:   {trade.get('emotion', '—')}\n"
        f"Setup:      {trade.get('setup', '—')}\n"
        f"Note:       {trade.get('note', '—')}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Opened:     {trade.get('opened_at', '—')[:16]}\n"
        f"Closed:     {trade.get('closed_at', '—')[:16]}\n"
    )
    if trade.get("entry_link"):
        msg += f"📸 [Entry Screenshot]({trade['entry_link']})\n"
    if trade.get("close_link"):
        msg += f"📸 [Close Screenshot]({trade['close_link']})\n"

    # review report stays permanently — no auto-delete
    await update.message.reply_text(msg, parse_mode="Markdown")
    ctx.user_data.clear()
    return ConversationHandler.END


# ─────────────────────────────────────────────
#  OPEN & CLOSED TRADES
# ─────────────────────────────────────────────
async def _show_open_trades(message, ctx):
    data   = load_trades()
    open_t = [t for t in data["trades"] if t.get("status") == "open"]
    if not open_t:
        sent = await ctx.bot.send_message(
            chat_id=message.chat_id,
            message_thread_id=message.message_thread_id,
            text="📭 No open trades currently."
        )
        asyncio.get_event_loop().create_task(_delete_after_delay(message.chat_id, sent.message_id, 10))
        return
    lines = ["📂 *OPEN TRADES*\n━━━━━━━━━━━━━━━━━━━━"]
    for t in open_t:
        icon = "🟢" if t.get("direction") == "BUY" else "🔴"
        link = t.get("entry_link", "")
        if link:
            lines.append(f"[#{t['id']} {t['pair']} {icon} {t['direction']}]({link})\n   Entry: `{t.get('entry')}` | SL: `{t.get('sl')}` | RR: 1:{t.get('rr')} | Since: {t.get('opened_at','')[:10]}")
        else:
            lines.append(f"#{t['id']} — *{t['pair']}* {icon} {t['direction']}\n   Entry: `{t.get('entry')}` | SL: `{t.get('sl')}` | RR: 1:{t.get('rr')} | Since: {t.get('opened_at','')[:10]}")
    await ctx.bot.send_message(
        chat_id=message.chat_id,
        message_thread_id=message.message_thread_id,
        text="\n".join(lines),
        parse_mode="Markdown"
    )


async def _show_closed_trades(message, ctx):
    data     = load_trades()
    closed_t = [t for t in data["trades"] if t.get("status") == "closed"]
    if not closed_t:
        sent = await ctx.bot.send_message(
            chat_id=message.chat_id,
            message_thread_id=message.message_thread_id,
            text="📭 No closed trades yet."
        )
        asyncio.get_event_loop().create_task(_delete_after_delay(message.chat_id, sent.message_id, 10))
        return
    recent = closed_t[-10:][::-1]
    lines  = ["✅ *CLOSED TRADES — Last 10*\n━━━━━━━━━━━━━━━━━━━━"]
    for t in recent:
        icon   = "✅" if t.get("outcome") == "WIN" else "❌"
        d_icon = "🟢" if t.get("direction") == "BUY" else "🔴"
        pnl    = float(t.get("pnl", 0))
        pnl_s  = f"+${pnl:.2f}" if pnl >= 0 else f"-${abs(pnl):.2f}"
        link   = t.get("close_link") or t.get("entry_link", "")
        date   = t.get("closed_at", "")[:10]
        if link:
            lines.append(f"{icon} [#{t['id']} {t['pair']} {d_icon} {pnl_s}]({link})  _{date}_")
        else:
            lines.append(f"{icon} #{t['id']} {t['pair']} {d_icon} {pnl_s}  _{date}_")
    await ctx.bot.send_message(
        chat_id=message.chat_id,
        message_thread_id=message.message_thread_id,
        text="\n".join(lines),
        parse_mode="Markdown"
    )


# ─────────────────────────────────────────────
#  ADD NOTE
# ─────────────────────────────────────────────
async def got_note_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if ctx.user_data.get("last_bot_msg"):
        await safe_delete(chat_id, ctx.user_data["last_bot_msg"])
    await safe_delete(chat_id, update.message.message_id)
    try:
        ctx.user_data["note_id"] = int(update.message.text.strip().replace("#", ""))
    except:
        sent = await update.message.reply_text("Invalid ID. Try again or /cancel")
        ctx.user_data["last_bot_msg"] = sent.message_id
        return NOTE_ID
    sent = await update.message.reply_text(
        f"📝 Enter your note for Trade #{ctx.user_data['note_id']}:",
        parse_mode="Markdown"
    )
    ctx.user_data["last_bot_msg"] = sent.message_id
    return NOTE_TEXT


async def got_note_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id  = update.message.chat_id
    if ctx.user_data.get("last_bot_msg"):
        await safe_delete(chat_id, ctx.user_data["last_bot_msg"])
    await safe_delete(chat_id, update.message.message_id)
    note     = update.message.text.strip()
    trade_id = ctx.user_data["note_id"]
    data     = load_trades()
    trade    = next((t for t in data["trades"] if t["id"] == trade_id), None)
    if not trade:
        sent = await update.message.reply_text(f"Trade #{trade_id} not found.")
        asyncio.get_event_loop().create_task(_delete_after_delay(chat_id, sent.message_id, 5))
        return ConversationHandler.END
    trade["note"] = note
    save_trades(data)
    await update.message.reply_text(f"✅ Note added to Trade #{trade_id}:\n_{note}_", parse_mode="Markdown")
    ctx.user_data.clear()
    return ConversationHandler.END



# ─────────────────────────────────────────────
#  STATS FUNCTIONS
# ─────────────────────────────────────────────
async def _show_alltime_stats(message, ctx):
    data  = load_trades()
    s     = compute_stats(data["trades"])
    open_count = len([t for t in data["trades"] if t.get("status") == "open"])
    msg = (
        f"📈 *ALL-TIME STATS*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Total Trades:   {s['total']}\n"
        f"Wins / Losses:  {s['wins']} / {s['losses']}\n"
        f"Win Rate:       {s['win_rate']}%\n"
        f"Total P&L:      ${s['pnl']:+.2f}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Avg RR:         1:{s['avg_rr']}\n"
        f"Avg Hold Time:  {s['avg_hold']}h\n"
        f"Biggest Win:    +${s['big_win']}\n"
        f"Biggest Loss:   -${abs(s['big_loss'])}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Best Pair:      {s['best_pair']}\n"
        f"Best Session:   {s['best_session']}\n"
        f"Open Trades:    {open_count}\n"
        f"Grade:          {s['grade']}\n"
    )
    sent = await message.reply_text(msg, parse_mode="Markdown")
    tid  = message.message_thread_id or 0
    track_msg(message.chat_id, tid, sent.message_id)
    if tid == DASHBOARD_THREAD:
        reset_dashboard_timer(message.chat_id, tid)


async def _show_weekly_stats(message, ctx):
    data = load_trades()
    s    = compute_stats(data["trades"], days=7)
    week = datetime.now().strftime("W%W %Y")
    msg  = (
        f"📅 *WEEKLY STATS — {week}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Trades:     {s['total']}\n"
        f"Wins:       {s['wins']}  |  Losses: {s['losses']}\n"
        f"Win Rate:   {s['win_rate']}%\n"
        f"P&L:        ${s['pnl']:+.2f}\n"
        f"Avg RR:     1:{s['avg_rr']}\n"
        f"Best Pair:  {s['best_pair']}\n"
        f"Grade:      {s['grade']}\n"
    )
    await message.reply_text(msg, parse_mode="Markdown")


async def _show_streak(message, ctx):
    data   = load_trades()
    streak = compute_streak(data["trades"])
    icon   = "🔥" if streak["current_type"] == "WIN" else ("❄️" if streak["current_type"] == "LOSS" else "—")
    msg    = (
        f"🔥 *STREAK TRACKER*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Current Streak:   {icon} {streak['current']} {streak['current_type'].lower()}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Longest Win Streak:  🔥 {streak['longest_win']}\n"
        f"Longest Loss Streak: ❄️ {streak['longest_loss']}\n"
    )
    await message.reply_text(msg, parse_mode="Markdown")


async def _show_summary(message, ctx):
    data  = load_trades()
    today = datetime.now().strftime("%Y-%m-%d")
    today_trades = [
        t for t in data["trades"]
        if t.get("status") == "closed" and t.get("closed_at", "").startswith(today)
    ]
    open_t = [t for t in data["trades"] if t.get("status") == "open"]
    pnl    = sum(float(t.get("pnl", 0)) for t in today_trades)
    wins   = sum(1 for t in today_trades if t.get("outcome") == "WIN")
    losses = sum(1 for t in today_trades if t.get("outcome") == "LOSS")
    emotions = [t.get("emotion", "") for t in today_trades if t.get("emotion")]

    msg = (
        f"💡 *TODAY'S SUMMARY — {datetime.now().strftime('%d %b %Y')}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Trades Today:  {len(today_trades)}\n"
        f"Wins / Losses: {wins} / {losses}\n"
        f"Today P&L:     ${pnl:+.2f}\n"
        f"Open Trades:   {len(open_t)}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Emotions Today: {', '.join(set(emotions)) if emotions else '—'}\n"
    )
    if open_t:
        msg += "\n*Open:*\n"
        for t in open_t:
            icon = "🟢" if t.get("direction") == "BUY" else "🔴"
            msg += f"#{t['id']} {t['pair']} {icon} @ {t.get('entry')}\n"
    await message.reply_text(msg, parse_mode="Markdown")


async def _show_pairs(message, ctx):
    data     = load_trades()
    pair_map = {}
    for t in data["trades"]:
        if t.get("status") != "closed":
            continue
        p = t.get("pair", "?")
        if p not in pair_map:
            pair_map[p] = {"trades": 0, "wins": 0, "pnl": 0}
        pair_map[p]["trades"] += 1
        if t.get("outcome") == "WIN":
            pair_map[p]["wins"] += 1
        pair_map[p]["pnl"] += float(t.get("pnl", 0))

    lines = ["🏆 *PAIR PERFORMANCE*\n━━━━━━━━━━━━━━━━━━━━"]
    for pair, s in sorted(pair_map.items(), key=lambda x: x[1]["pnl"], reverse=True):
        wr = round(s["wins"] / s["trades"] * 100, 1) if s["trades"] else 0
        lines.append(f"*{pair}*  {s['trades']} trades | {wr}% WR | ${s['pnl']:+.2f}")
    if not pair_map:
        lines.append("No closed trades yet.")
    await message.reply_text("\n".join(lines), parse_mode="Markdown")


# ─────────────────────────────────────────────
#  WEEKLY POST
# ─────────────────────────────────────────────
async def _post_weekly(message, ctx):
    data  = load_trades()
    s     = compute_stats(data["trades"], days=7)
    week  = datetime.now().strftime("W%W %Y")
    msg   = (
        f"📊 *WEEKLY REVIEW — {week}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Total Trades:  {s['total']}\n"
        f"Wins:          {s['wins']}\n"
        f"Losses:        {s['losses']}\n"
        f"Win Rate:      {s['win_rate']}%\n"
        f"Total P&L:     ${s['pnl']:+.2f}\n"
        f"Avg RR:        1:{s['avg_rr']}\n"
        f"Best Pair:     {s['best_pair']}\n"
        f"Best Session:  {s['best_session']}\n"
        f"Grade:         {s['grade']}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"_Posted: {datetime.now().strftime('%d/%m/%Y %H:%M')}_\n"
        f"_Add your weekly lesson below ⬇️_"
    )
    try:
        await ctx.bot.send_message(chat_id=GROUP_ID, message_thread_id=DASHBOARD_THREAD, text=msg, parse_mode="Markdown")
        await message.reply_text("✅ Weekly review posted to DASHBOARD!")
    except Exception as e:
        await message.reply_text(msg, parse_mode="Markdown")


# ─────────────────────────────────────────────
#  CALENDAR SPECIFIC MONTH
# ─────────────────────────────────────────────
async def got_cal_month(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        parts = update.message.text.strip().split()
        month = int(parts[0]); year = int(parts[1])
    except:
        await update.message.reply_text("❌ Invalid format. Use MM YYYY — e.g. `05 2026`", parse_mode="Markdown")
        return CAL_MONTH
    data = load_trades()
    msg  = build_calendar_text(year, month, data["trades"])
    try:
        await ctx.bot.send_message(chat_id=GROUP_ID, message_thread_id=DASHBOARD_THREAD, text=msg, parse_mode="Markdown")
        await update.message.reply_text("✅ Calendar posted to DASHBOARD!")
    except:
        await update.message.reply_text(msg, parse_mode="Markdown")
    return ConversationHandler.END


# ─────────────────────────────────────────────
#  RISK CALCULATOR
# ─────────────────────────────────────────────
async def got_risk_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        ctx.user_data["balance"] = float(update.message.text.strip().replace(",", ""))
    except:
        await update.message.reply_text("❌ Invalid amount. Enter a number e.g. 1000")
        return RISK_BALANCE
    await update.message.reply_text("Enter your *risk %* per trade (e.g. 1 or 2):", parse_mode="Markdown")
    return RISK_PERCENT


async def got_risk_percent(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        ctx.user_data["risk_pct"] = float(update.message.text.strip().replace("%", ""))
    except:
        await update.message.reply_text("❌ Invalid. Enter a number e.g. 1")
        return RISK_PERCENT
    await update.message.reply_text("Enter your *SL in pips* (e.g. 35):", parse_mode="Markdown")
    return RISK_SL_PIPS


async def got_risk_sl_pips(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        ctx.user_data["sl_pips"] = float(update.message.text.strip())
    except:
        await update.message.reply_text("❌ Invalid. Enter a number e.g. 35")
        return RISK_SL_PIPS
    kbd = [
        [InlineKeyboardButton("Forex pair (10)",  callback_data="pipval:10"),
         InlineKeyboardButton("Gold/XAUUSD (1)",  callback_data="pipval:1")],
        [InlineKeyboardButton("Crypto BTC (1)",   callback_data="pipval:1"),
         InlineKeyboardButton("Custom value",     callback_data="pipval:custom")],
    ]
    await update.message.reply_text(
        "Select your *instrument type*\n_(pip value per 1 standard lot)_:",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kbd)
    )
    return RISK_PIP_VALUE


async def got_risk_pip_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    val = q.data.split(":")[1]
    if val == "custom":
        await q.edit_message_text(
            "Enter the *pip value per 1 standard lot* for your instrument:\n"
            "_(e.g. for most Forex pairs: 10, for Gold: 1)_",
            parse_mode="Markdown"
        )
        return RISK_PIP_VALUE

    pip_value   = float(val)
    balance     = ctx.user_data["balance"]
    risk_pct    = ctx.user_data["risk_pct"]
    sl_pips     = ctx.user_data["sl_pips"]

    # correct formula: lot_size = risk_amount / (sl_pips * pip_value_per_lot)
    risk_amount = balance * (risk_pct / 100)
    lot_size    = round(risk_amount / (sl_pips * pip_value), 2)

    msg = (
        f"🧮 *RISK CALCULATOR RESULT*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Account Balance:  ${balance:,.2f}\n"
        f"Risk %:           {risk_pct}%\n"
        f"Risk Amount:      ${risk_amount:,.2f}\n"
        f"SL (pips):        {sl_pips}\n"
        f"Pip Value/Lot:    ${pip_value}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ *Lot Size:     {lot_size}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"_Never risk more than you can afford to lose._"
    )
    await q.edit_message_text(msg, parse_mode="Markdown")
    ctx.user_data.clear()
    return ConversationHandler.END


async def got_custom_pip_value(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        pip_value = float(update.message.text.strip())
    except:
        await update.message.reply_text("❌ Invalid. Enter a number e.g. 10")
        return RISK_PIP_VALUE

    balance     = ctx.user_data["balance"]
    risk_pct    = ctx.user_data["risk_pct"]
    sl_pips     = ctx.user_data["sl_pips"]
    risk_amount = balance * (risk_pct / 100)
    lot_size    = round(risk_amount / (sl_pips * pip_value), 2)

    msg = (
        f"🧮 *RISK CALCULATOR RESULT*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"Account Balance:  ${balance:,.2f}\n"
        f"Risk %:           {risk_pct}%\n"
        f"Risk Amount:      ${risk_amount:,.2f}\n"
        f"SL (pips):        {sl_pips}\n"
        f"Pip Value/Lot:    ${pip_value}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ *Lot Size:     {lot_size}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"_Never risk more than you can afford to lose._"
    )
    await update.message.reply_text(msg, parse_mode="Markdown")
    ctx.user_data.clear()
    return ConversationHandler.END


# ─────────────────────────────────────────────
#  EXPORT CSV
# ─────────────────────────────────────────────
async def _export_csv(message, ctx):
    data   = load_trades()
    trades = data["trades"]
    if not trades:
        await message.reply_text("No trades to export yet.")
        return

    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "ID", "Pair", "Direction", "Session", "Status", "Outcome",
        "Entry", "Stop Loss", "Take Profit", "RR", "Lot Size",
        "P&L ($)", "Emotion", "Setup", "Note", "Duration",
        "Opened At", "Closed At", "Entry Screenshot", "Close Screenshot"
    ])
    for t in trades:
        writer.writerow([
            t.get("id", ""), t.get("pair", ""), t.get("direction", ""),
            t.get("session", ""), t.get("status", ""), t.get("outcome", ""),
            t.get("entry", ""), t.get("sl", ""), t.get("tp", ""),
            t.get("rr", ""), t.get("lot", ""), t.get("pnl", ""),
            t.get("emotion", ""), t.get("setup", ""), t.get("note", ""),
            t.get("duration", ""),
            t.get("opened_at", "")[:16] if t.get("opened_at") else "",
            t.get("closed_at", "")[:16] if t.get("closed_at") else "",
            t.get("entry_link", ""), t.get("close_link", ""),
        ])

    output.seek(0)
    filename  = f"BullRunAcademy_{datetime.now().strftime('%Y%m%d_%H%M')}.csv"
    csv_bytes = io.BytesIO(output.getvalue().encode("utf-8"))
    csv_bytes.name = filename

    await message.reply_document(
        document=csv_bytes, filename=filename,
        caption=(
            f"📊 *Bull Run Academy — Trade Export*\n"
            f"Total trades: {len(trades)}\n"
            f"Exported: {datetime.now().strftime('%d/%m/%Y %H:%M')}"
        ),
        parse_mode="Markdown"
    )


# ─────────────────────────────────────────────
#  DELETE FLOW
# ─────────────────────────────────────────────
async def _delete_start(message, ctx):
    data   = load_trades()
    trades = data["trades"]
    if not trades:
        await message.reply_text("No trades found to delete.")
        return ConversationHandler.END
    recent = trades[-10:][::-1]
    lines  = ["🗑 *Select Trade ID to delete:*\n"]
    for t in recent:
        status = "🟡 open" if t.get("status") == "open" else f"{'✅' if t.get('outcome') == 'WIN' else '❌'} closed"
        lines.append(f"#{t['id']} — {t.get('pair','?')} {t.get('direction','?')} | {status} | {t.get('opened_at','')[:10]}")
    lines.append("\n_Enter the Trade ID to delete, or /cancel_")
    await message.reply_text("\n".join(lines), parse_mode="Markdown")
    return DELETE_ID


async def got_delete_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id

    try:
        trade_id = int(update.message.text.strip().replace("#", ""))
    except:
        sent = await update.message.reply_text("❌ Invalid ID. Enter a number or /cancel")
        if ctx.user_data.get("last_bot_msg"):
            await safe_delete(chat_id, ctx.user_data["last_bot_msg"])
        await safe_delete(chat_id, update.message.message_id)
        ctx.user_data["last_bot_msg"] = sent.message_id
        return DELETE_ID

    data  = load_trades()
    trade = next((t for t in data["trades"] if t["id"] == trade_id), None)
    if not trade:
        sent = await update.message.reply_text(f"❌ Trade #{trade_id} not found.")
        if ctx.user_data.get("last_bot_msg"):
            await safe_delete(chat_id, ctx.user_data["last_bot_msg"])
        await safe_delete(chat_id, update.message.message_id)
        ctx.user_data["last_bot_msg"] = sent.message_id
        return DELETE_ID

    ctx.user_data["delete_id"] = trade_id
    kbd = [[
        InlineKeyboardButton("✅ Yes, delete", callback_data="del:yes"),
        InlineKeyboardButton("❌ No, cancel",  callback_data="del:no"),
    ]]
    sent = await update.message.reply_text(
        f"⚠️ *Delete Trade #{trade_id}?*\n"
        f"{trade.get('pair')} {trade.get('direction')} | "
        f"{'P&L: $' + str(trade.get('pnl','—')) if trade.get('status') == 'closed' else 'Status: Open'}\n"
        f"_This cannot be undone._",
        parse_mode="Markdown", reply_markup=InlineKeyboardMarkup(kbd)
    )
    # delete prompt and user's ID message AFTER sending confirmation
    if ctx.user_data.get("last_bot_msg"):
        await safe_delete(chat_id, ctx.user_data["last_bot_msg"])
    await safe_delete(chat_id, update.message.message_id)
    ctx.user_data["last_bot_msg"] = sent.message_id
    return DELETE_CONFIRM


async def got_delete_confirm(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data.split(":")[1] == "no":
        await safe_delete(q.message.chat_id, q.message.message_id)
        ctx.user_data.clear()
        return ConversationHandler.END

    trade_id       = ctx.user_data["delete_id"]
    data           = load_trades()
    before         = len(data["trades"])
    data["trades"] = [t for t in data["trades"] if t["id"] != trade_id]

    if len(data["trades"]) < before:
        ids             = [t["id"] for t in data["trades"] if "id" in t]
        data["next_id"] = (max(ids) + 1) if ids else 1
        save_trades(data)
        await q.edit_message_text(
            f"✅ Trade #{trade_id} deleted.\n🔄 Next trade will be *#{data['next_id']}*",
            parse_mode="Markdown"
        )
        # auto-delete confirmation after 5 seconds
        asyncio.get_event_loop().create_task(
            _delete_after_delay(q.message.chat_id, q.message.message_id, 5)
        )
    else:
        await q.edit_message_text(f"❌ Trade #{trade_id} not found.")
        asyncio.get_event_loop().create_task(
            _delete_after_delay(q.message.chat_id, q.message.message_id, 5)
        )

    ctx.user_data.clear()
    return ConversationHandler.END


# ─────────────────────────────────────────────
#  CANCEL / SKIP
# ─────────────────────────────────────────────
async def cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    if ctx.user_data.get("last_bot_msg"):
        await safe_delete(chat_id, ctx.user_data["last_bot_msg"])
    await safe_delete(chat_id, update.message.message_id)
    ctx.user_data.clear()
    return ConversationHandler.END


# ─────────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────────
# ─────────────────────────────────────────────
#  KEEP-ALIVE FOR RENDER DEPLOYMENT
# ─────────────────────────────────────────────
async def keep_alive():
    from aiohttp import web

    async def health(request):
        return web.Response(text="Bull Run Academy Bot is running")

    webapp  = web.Application()
    webapp.router.add_get("/", health)
    runner  = web.AppRunner(webapp)
    await runner.setup()
    port    = int(os.environ.get("PORT", 8080))
    site    = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info(f"Keep-alive running on port {port}")


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # master conversation handler covering all flows
    master_conv = ConversationHandler(
        entry_points=[
            CommandHandler("trade",    trade_menu),
            CommandHandler("stats",    stats_menu),
            CommandHandler("calendar", calendar_menu),
            CommandHandler("tools",    tools_menu),
            CallbackQueryHandler(menu_router, pattern="^(tm:|sm:|cm:|tolm:)"),
        ],
        states={
            # entry
            PAIR:             [CallbackQueryHandler(got_pair,             pattern="^pair:")],
            DIRECTION:        [CallbackQueryHandler(got_direction,        pattern="^dir:")],
            SESSION:          [CallbackQueryHandler(got_session,          pattern="^ses:")],
            ENTRY_PRICE:      [MessageHandler(filters.TEXT & ~filters.COMMAND, got_entry_price)],
            SL:               [MessageHandler(filters.TEXT & ~filters.COMMAND, got_sl)],
            TP:               [MessageHandler(filters.TEXT & ~filters.COMMAND, got_tp)],
            LOT:              [MessageHandler(filters.TEXT & ~filters.COMMAND, got_lot)],
            ENTRY_EMOTION:    [CallbackQueryHandler(got_entry_emotion,    pattern="^emo:")],
            SETUP:            [MessageHandler(filters.TEXT & ~filters.COMMAND, got_setup)],
            ENTRY_SCREENSHOT: [
                MessageHandler(filters.PHOTO, got_entry_screenshot),
                CommandHandler("skip", skip_entry_screenshot),
            ],
            # close
            CLOSE_ID:         [MessageHandler(filters.TEXT & ~filters.COMMAND, got_close_id)],
            CLOSE_EXIT:       [MessageHandler(filters.TEXT & ~filters.COMMAND, got_close_exit)],
            CLOSE_PNL:        [MessageHandler(filters.TEXT & ~filters.COMMAND, got_close_pnl)],
            CLOSE_EMOTION:    [CallbackQueryHandler(got_close_emotion,    pattern="^cemo:")],
            CLOSE_SCREENSHOT: [
                MessageHandler(filters.PHOTO, got_close_screenshot),
                CommandHandler("skip", skip_close_screenshot),
            ],
            # review
            REVIEW_ID:        [MessageHandler(filters.TEXT & ~filters.COMMAND, got_review_id)],
            # note
            NOTE_ID:          [MessageHandler(filters.TEXT & ~filters.COMMAND, got_note_id)],
            NOTE_TEXT:        [MessageHandler(filters.TEXT & ~filters.COMMAND, got_note_text)],
            # delete
            DELETE_ID:        [MessageHandler(filters.TEXT & ~filters.COMMAND, got_delete_id)],
            DELETE_CONFIRM:   [CallbackQueryHandler(got_delete_confirm,   pattern="^del:")],
            # risk calc
            RISK_BALANCE:     [MessageHandler(filters.TEXT & ~filters.COMMAND, got_risk_balance)],
            RISK_PERCENT:     [MessageHandler(filters.TEXT & ~filters.COMMAND, got_risk_percent)],
            RISK_SL_PIPS:     [MessageHandler(filters.TEXT & ~filters.COMMAND, got_risk_sl_pips)],
            RISK_PIP_VALUE:   [
                CallbackQueryHandler(got_risk_pip_value, pattern="^pipval:"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, got_custom_pip_value),
            ],
            # calendar
            CAL_MONTH:        [MessageHandler(filters.TEXT & ~filters.COMMAND, got_cal_month)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        per_message=False,
        per_chat=True,
        per_user=True,
        allow_reentry=True,
    )

    app.add_handler(master_conv)
    app.add_handler(CommandHandler("help", help_cmd))

    global _BOT
    _BOT = app.bot

    async def on_startup(app):
        asyncio.ensure_future(keep_alive())
    app.post_init = on_startup
    logger.info("🤖 Bull Run Academy Bot v3 is running...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
