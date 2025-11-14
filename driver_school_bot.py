# driver_school_bot.py
# Telegram bot to track driver's extra trips and send weekly/monthly/yearly reports
# with holiday / no-school adjustment on weekly base amount.

import os
import json
from datetime import datetime, date, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Optional

from telegram import Update, InputFile
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
)

DATA_FILE = Path("driver_trips_data.json")
DEFAULT_BASE_WEEKLY = 725.0  # AED
DUBAI_TZ = ZoneInfo("Asia/Dubai")
SCHOOL_DAYS_PER_WEEK = 5  # used to compute per-day base from weekly base

# 🔐 ONLY YOU & ABDULLA CAN USE THE BOT
ALLOWED_USERS = [
    7698278415,   # Faisal
    5034920293    # Abdulla
]


# ---------- Persistence Helpers ----------

def load_data() -> Dict[str, Any]:
    if DATA_FILE.exists():
        try:
            with DATA_FILE.open("r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_data(data: Dict[str, Any]) -> None:
    try:
        with DATA_FILE.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def get_user_record(chat_id: int, data: Dict[str, Any], username: str = "") -> Dict[str, Any]:
    key = str(chat_id)
    if key not in data:
        data[key] = {
            "chat_id": chat_id,
            "base_weekly": DEFAULT_BASE_WEEKLY,
            "trips": [],             # list of {id, date, amount, destination, user_id, user_name}
            "next_trip_id": 1,
            "no_school_dates": [],   # list of "YYYY-MM-DD"
            "last_weekly_report": None,  # ISO datetime string
        }
    # store last username if given
    if username:
        data[key]["last_username"] = username
    return data[key]


def parse_iso_datetime(dt_str: str) -> datetime:
    return datetime.fromisoformat(dt_str)


def parse_date_str(d: str) -> date:
    return datetime.strptime(d, "%Y-%m-%d").date()


def today_dubai() -> date:
    return datetime.now(DUBAI_TZ).date()


# ---------- Authorization ----------

def is_authorized(user_id: Optional[int]) -> bool:
    if user_id is None:
        return False
    return user_id in ALLOWED_USERS


async def ensure_authorized(update: Update) -> bool:
    user = update.effective_user
    if not user or not is_authorized(user.id):
        msg = "❌ You are not authorized to use this bot."
        if update.message:
            await update.message.reply_text(msg)
        elif update.callback_query:
            await update.callback_query.answer(msg, show_alert=True)
        return False
    return True


# ---------- Core Helpers ----------

def filter_trips_by_period(
    trips: List[Dict[str, Any]],
    start_dt: Optional[datetime],
    end_dt: Optional[datetime],
) -> List[Dict[str, Any]]:
    """Filter trips between start_dt (exclusive) and end_dt (inclusive) if provided."""
    result = []
    for t in trips:
        dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
        if start_dt and dt <= start_dt:
            continue
        if end_dt and dt > end_dt:
            continue
        result.append(t)
    return result


def filter_trips_by_day(trips: List[Dict[str, Any]], day: date) -> List[Dict[str, Any]]:
    result = []
    for t in trips:
        dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ).date()
        if dt == day:
            result.append(t)
    return result


def filter_trips_by_month(trips: List[Dict[str, Any]], year: int, month: int) -> List[Dict[str, Any]]:
    result = []
    for t in trips:
        dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
        if dt.year == year and dt.month == month:
            result.append(t)
    return result


def filter_trips_by_year(trips: List[Dict[str, Any]], year: int) -> List[Dict[str, Any]]:
    result = []
    for t in trips:
        dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
        if dt.year == year:
            result.append(t)
    return result


def filter_trips_by_destination(trips: List[Dict[str, Any]], keyword: str) -> List[Dict[str, Any]]:
    k = keyword.lower()
    return [t for t in trips if k in t["destination"].lower()]


def no_school_days_in_period(no_school_dates: List[str], start_dt: Optional[datetime], end_dt: Optional[datetime]) -> int:
    count = 0
    for d_str in no_school_dates:
        d = parse_date_str(d_str)
        d_dt_start = datetime(d.year, d.month, d.day, 0, 0, tzinfo=DUBAI_TZ)
        if start_dt and d_dt_start <= start_dt:
            continue
        if end_dt and d_dt_start > end_dt:
            continue
        count += 1
    return count


def compute_adjusted_base(base_weekly: float, no_school_days: int) -> float:
    if SCHOOL_DAYS_PER_WEEK <= 0:
        return base_weekly
    base_per_day = base_weekly / SCHOOL_DAYS_PER_WEEK
    adjusted = base_weekly - no_school_days * base_per_day
    return max(0.0, adjusted)


# ---------- Bot Handlers ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update):
        return

    chat_id = update.effective_chat.id
    data = load_data()
    user = get_user_record(chat_id, data, username=(update.effective_user.username or ""))

    save_data(data)

    text = (
        "👋 *Driver Trips Tracker Bot*\n\n"
        "Use this bot to track *extra trips* for your driver and adjust weekly school cost.\n\n"
        f"💰 Fixed weekly school amount (default): *{user['base_weekly']:.2f} AED*\n\n"
        "📌 Main commands:\n"
        "• `/trip <amount> <destination>` – add extra trip\n"
        "   Example: `/trip 40 Dubai Mall`\n"
        "• `/list` – show *all* recorded trips\n"
        "• `/report` – weekly-style report (since last report)\n"
        "• `/month [YYYY-MM]` – monthly summary (default: current month)\n"
        "• `/year [YYYY]` – yearly summary (default: current year)\n"
        "• `/export` – export all trips as CSV file\n"
        "• `/delete <id>` – delete a specific trip by its ID\n"
        "• `/filter <YYYY-MM-DD>` – trips on a specific date\n"
        "• `/destination <keyword>` – trips to destinations matching keyword\n"
        "• `/setbase <amount>` – change weekly base amount\n\n"
        "🏫 *Holidays / No School:*\n"
        "• `/noschool` – mark *today* as no-school day\n"
        "• `/noschool YYYY-MM-DD` – mark a specific date as no-school\n"
        "• `/holiday YYYY-MM-DD YYYY-MM-DD` – mark a holiday range as no-school\n\n"
        "📅 Every *Friday at 10:00 (Dubai time)* you’ll get an adjusted weekly report."
    )

    await update.message.reply_text(text, parse_mode="Markdown")


async def set_base(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update):
        return

    chat_id = update.effective_chat.id
    data = load_data()
    user = get_user_record(chat_id, data)

    if not context.args:
        await update.message.reply_text(
            "Please provide an amount.\nExample: `/setbase 725`",
            parse_mode="Markdown",
        )
        return

    try:
        amount = float(context.args[0])
        if amount < 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Invalid amount. Use a positive number.")
        return

    user["base_weekly"] = amount
    save_data(data)

    await update.message.reply_text(
        f"✅ Weekly base amount set to *{amount:.2f} AED*.",
        parse_mode="Markdown",
    )


async def add_trip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update):
        return

    chat_id = update.effective_chat.id
    data = load_data()
    user = get_user_record(chat_id, data, username=(update.effective_user.username or ""))

    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: `/trip <amount> <destination>`\n"
            "Example: `/trip 35 Mall of the Emirates`",
            parse_mode="Markdown",
        )
        return

    # Parse amount
    try:
        amount = float(context.args[0])
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Invalid amount. Use a positive number.")
        return

    dest = " ".join(context.args[1:])
    now = datetime.now(DUBAI_TZ)

    trip_id = user.get("next_trip_id", 1)
    user["next_trip_id"] = trip_id + 1

    trip = {
        "id": trip_id,
        "date": now.isoformat(),
        "amount": amount,
        "destination": dest,
        "user_id": update.effective_user.id if update.effective_user else None,
        "user_name": update.effective_user.first_name if update.effective_user else "",
    }

    user["trips"].append(trip)
    save_data(data)

    pretty_date = now.strftime("%Y-%m-%d %H:%M")
    added_by = trip["user_name"] or "Unknown"

    await update.message.reply_text(
        f"✅ Trip added:\n"
        f"🆔 ID: *{trip_id}*\n"
        f"📍 *{dest}*\n"
        f"💰 *{amount:.2f} AED*\n"
        f"📅 {pretty_date}\n"
        f"👤 Added by: *{added_by}*",
        parse_mode="Markdown",
    )


async def list_trips(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update):
        return

    chat_id = update.effective_chat.id
    data = load_data()
    user = get_user_record(chat_id, data)

    trips = user.get("trips", [])
    if not trips:
        await update.message.reply_text("No trips recorded yet.")
        return

    lines = ["📋 *All recorded trips:*"]
    total = 0.0
    for t in sorted(trips, key=lambda x: x["id"]):
        dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
        date_str = dt.strftime("%Y-%m-%d")
        total += t["amount"]
        added_by = t.get("user_name") or "Unknown"
        lines.append(
            f"- ID {t['id']}: {date_str} — {t['destination']} — *{t['amount']:.2f} AED* (by {added_by})"
        )

    lines.append(f"\n💰 Total of all extra trips: *{total:.2f} AED*")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


def build_weekly_report_text(user: Dict[str, Any], since: Optional[datetime], until: datetime) -> str:
    trips = user.get("trips", [])
    base = user.get("base_weekly", DEFAULT_BASE_WEEKLY)
    no_school_dates = user.get("no_school_dates", [])

    period_trips = filter_trips_by_period(trips, since, until)
    total_extra = sum(t["amount"] for t in period_trips)

    # no-school days in this period
    n_noschool = no_school_days_in_period(no_school_dates, since, until)
    adjusted_base = compute_adjusted_base(base, n_noschool)
    grand_total = adjusted_base + total_extra

    if since:
        period_str = f"{since.astimezone(DUBAI_TZ).strftime('%Y-%m-%d')} → {until.astimezone(DUBAI_TZ).strftime('%Y-%m-%d')}"
    else:
        period_str = f"up to {until.astimezone(DUBAI_TZ).strftime('%Y-%m-%d')}"

    lines = [
        f"📊 *Driver Weekly Report* ({period_str})",
        "",
        f"🧾 Extra trips count in this period: *{len(period_trips)}*",
        f"💰 Extra trips total: *{total_extra:.2f} AED*",
        "",
        f"🎓 Fixed school weekly (full): *{base:.2f} AED*",
        f"📅 No-school days in this period: *{n_noschool}*",
        f"🎯 Adjusted base for this period: *{adjusted_base:.2f} AED*",
        "",
        f"✅ *Total to pay: {grand_total:.2f} AED*",
    ]

    if period_trips:
        lines.append("\n📋 Trip details in this period:")
        for t in period_trips:
            dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
            date_str = dt.strftime("%Y-%m-%d")
            added_by = t.get("user_name") or "Unknown"
            lines.append(
                f"- ID {t['id']}: {date_str} — {t['destination']} — *{t['amount']:.2f} AED* (by {added_by})"
            )

    return "\n".join(lines)


async def report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update):
        return

    chat_id = update.effective_chat.id
    data = load_data()
    user = get_user_record(chat_id, data)

    now = datetime.now(DUBAI_TZ)
    last_report_str = user.get("last_weekly_report")
    since = parse_iso_datetime(last_report_str) if last_report_str else None

    text = build_weekly_report_text(user, since, now)
    user["last_weekly_report"] = now.isoformat()
    save_data(data)

    await update.message.reply_text(text, parse_mode="Markdown")


# ---------- Weekly Job ----------

async def weekly_report_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    now = datetime.now(DUBAI_TZ)

    for key, user in list(data.items()):
        chat_id = user.get("chat_id")
        if not chat_id:
            continue

        last_report_str = user.get("last_weekly_report")
        since = parse_iso_datetime(last_report_str) if last_report_str else None

        trips = user.get("trips", [])
        period_trips = filter_trips_by_period(trips, since, now)
        no_school_dates = user.get("no_school_dates", [])
        noschool_count = no_school_days_in_period(no_school_dates, since, now)

        if not period_trips and noschool_count == 0:
            user["last_weekly_report"] = now.isoformat()
            continue

        text = build_weekly_report_text(user, since, now)
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=text,
                parse_mode="Markdown",
            )
            user["last_weekly_report"] = now.isoformat()
        except Exception:
            continue

    save_data(data)


# ---------- Month / Year / Filter / Destination / Delete / Export ----------

async def month_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update):
        return

    chat_id = update.effective_chat.id
    data = load_data()
    user = get_user_record(chat_id, data)

    today = datetime.now(DUBAI_TZ)
    if context.args:
        try:
            year_month = context.args[0]
            year, month = year_month.split("-")
            year = int(year)
            month = int(month)
        except Exception:
            await update.message.reply_text("Use: `/month YYYY-MM` (e.g. `/month 2025-11`)", parse_mode="Markdown")
            return
    else:
        year, month = today.year, today.month

    trips = user.get("trips", [])
    m_trips = filter_trips_by_month(trips, year, month)
    total = sum(t["amount"] for t in m_trips)

    if not m_trips:
        await update.message.reply_text(f"No trips recorded in {year}-{month:02d}.")
        return

    lines = [f"📅 *Monthly Report* {year}-{month:02d}", ""]
    lines.append(f"🧾 Trips: *{len(m_trips)}*")
    lines.append(f"💰 Extra total: *{total:.2f} AED*")
    lines.append("\n📋 Details:")
    for t in sorted(m_trips, key=lambda x: x["id"]):
        dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
        date_str = dt.strftime("%Y-%m-%d")
        added_by = t.get("user_name") or "Unknown"
        lines.append(
            f"- ID {t['id']}: {date_str} — {t['destination']} — *{t['amount']:.2f} AED* (by {added_by})"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def year_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update):
        return

    chat_id = update.effective_chat.id
    data = load_data()
    user = get_user_record(chat_id, data)

    today = datetime.now(DUBAI_TZ)
    if context.args:
        try:
            year = int(context.args[0])
        except Exception:
            await update.message.reply_text("Use: `/year YYYY` (e.g. `/year 2025`)", parse_mode="Markdown")
            return
    else:
        year = today.year

    trips = user.get("trips", [])
    y_trips = filter_trips_by_year(trips, year)
    total = sum(t["amount"] for t in y_trips)

    if not y_trips:
        await update.message.reply_text(f"No trips recorded in {year}.")
        return

    lines = [f"📅 *Yearly Report* {year}", ""]
    lines.append(f"🧾 Trips: *{len(y_trips)}*")
    lines.append(f"💰 Extra total: *{total:.2f} AED*")
    lines.append("\n📋 Details:")
    for t in sorted(y_trips, key=lambda x: x["id"]):
        dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
        date_str = dt.strftime("%Y-%m-%d")
        added_by = t.get("user_name") or "Unknown"
        lines.append(
            f"- ID {t['id']}: {date_str} — {t['destination']} — *{t['amount']:.2f} AED* (by {added_by})"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def delete_trip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update):
        return

    chat_id = update.effective_chat.id
    data = load_data()
    user = get_user_record(chat_id, data)

    if not context.args:
        await update.message.reply_text("Use: `/delete <trip_id>`", parse_mode="Markdown")
        return

    try:
        trip_id = int(context.args[0])
    except Exception:
        await update.message.reply_text("Trip ID must be a number.")
        return

    trips = user.get("trips", [])
    new_trips = [t for t in trips if t["id"] != trip_id]

    if len(new_trips) == len(trips):
        await update.message.reply_text(f"No trip found with ID {trip_id}.")
        return

    user["trips"] = new_trips
    save_data(data)

    await update.message.reply_text(f"✅ Trip with ID {trip_id} deleted.")


async def filter_by_date_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update):
        return

    if not context.args:
        await update.message.reply_text("Use: `/filter YYYY-MM-DD`", parse_mode="Markdown")
        return

    try:
        d = parse_date_str(context.args[0])
    except Exception:
        await update.message.reply_text("Date format must be `YYYY-MM-DD`.", parse_mode="Markdown")
        return

    chat_id = update.effective_chat.id
    data = load_data()
    user = get_user_record(chat_id, data)
    trips = user.get("trips", [])

    day_trips = filter_trips_by_day(trips, d)
    if not day_trips:
        await update.message.reply_text(f"No trips on {d}.")
        return

    total = sum(t["amount"] for t in day_trips)
    lines = [f"📅 Trips on {d}:", ""]
    for t in sorted(day_trips, key=lambda x: x["id"]):
        added_by = t.get("user_name") or "Unknown"
        lines.append(
            f"- ID {t['id']}: {t['destination']} — *{t['amount']:.2f} AED* (by {added_by})"
        )
    lines.append(f"\n💰 Total that day: *{total:.2f} AED*")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def destination_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update):
        return

    if not context.args:
        await update.message.reply_text("Use: `/destination <keyword>`", parse_mode="Markdown")
        return

    keyword = " ".join(context.args)
    chat_id = update.effective_chat.id
    data = load_data()
    user = get_user_record(chat_id, data)
    trips = user.get("trips", [])

    dest_trips = filter_trips_by_destination(trips, keyword)
    if not dest_trips:
        await update.message.reply_text(f"No trips found matching '{keyword}'.")
        return

    total = sum(t["amount"] for t in dest_trips)
    lines = [f"📍 Trips with destination containing '{keyword}':", ""]
    for t in sorted(dest_trips, key=lambda x: x["id"]):
        dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
        d_str = dt.strftime("%Y-%m-%d")
        added_by = t.get("user_name") or "Unknown"
        lines.append(
            f"- ID {t['id']}: {d_str} — {t['destination']} — *{t['amount']:.2f} AED* (by {added_by})"
        )
    lines.append(f"\n💰 Total for these trips: *{total:.2f} AED*")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update):
        return

    chat_id = update.effective_chat.id
    data = load_data()
    user = get_user_record(chat_id, data)
    trips = user.get("trips", [])

    if not trips:
        await update.message.reply_text("No trips to export.")
        return

    # Create CSV
    csv_lines = ["id,date,amount,destination,user_id,user_name"]
    for t in sorted(trips, key=lambda x: x["id"]):
        csv_lines.append(
            f"{t['id']},{t['date']},{t['amount']},\"{t['destination'].replace('\"', '\"\"')}\",{t.get('user_id','')},\"{(t.get('user_name') or '').replace('\"', '\"\"')}\""
        )

    filename = f"driver_trips_{chat_id}.csv"
    with open(filename, "w", encoding="utf-8") as f:
        f.write("\n".join(csv_lines))

    await update.message.reply_document(
        document=InputFile(filename),
        filename=filename,
        caption="📄 All trips exported as CSV.",
    )


# ---------- No School / Holiday Commands ----------

async def noschool_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update):
        return

    chat_id = update.effective_chat.id
    data = load_data()
    user = get_user_record(chat_id, data)

    if context.args:
        try:
            d = parse_date_str(context.args[0])
        except Exception:
            await update.message.reply_text("Date format must be `YYYY-MM-DD`.", parse_mode="Markdown")
            return
    else:
        d = today_dubai()

    d_str = d.strftime("%Y-%m-%d")
    if d_str not in user["no_school_dates"]:
        user["no_school_dates"].append(d_str)
        save_data(data)
        await update.message.reply_text(f"✅ Marked {d_str} as a no-school day.")
    else:
        await update.message.reply_text(f"ℹ️ {d_str} is already marked as no-school.")


async def holiday_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update):
        return

    if len(context.args) != 2:
        await update.message.reply_text(
            "Use: `/holiday YYYY-MM-DD YYYY-MM-DD`\nExample: `/holiday 2025-12-02 2025-12-05`",
            parse_mode="Markdown",
        )
        return

    try:
        start_d = parse_date_str(context.args[0])
        end_d = parse_date_str(context.args[1])
    except Exception:
        await update.message.reply_text("Dates must be in `YYYY-MM-DD` format.", parse_mode="Markdown")
        return

    if end_d < start_d:
        await update.message.reply_text("End date must be after or equal to start date.")
        return

    chat_id = update.effective_chat.id
    data = load_data()
    user = get_user_record(chat_id, data)
    no_school_dates = set(user.get("no_school_dates", []))

    cur = start_d
    added = 0
    while cur <= end_d:
        d_str = cur.strftime("%Y-%m-%d")
        if d_str not in no_school_dates:
            no_school_dates.add(d_str)
            added += 1
        cur += timedelta(days=1)

    user["no_school_dates"] = sorted(no_school_dates)
    save_data(data)

    await update.message.reply_text(
        f"✅ Holiday set from {start_d} to {end_d}. Added {added} no-school days."
    )


# ---------- Main ----------

def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Please set BOT_TOKEN environment variable.")

    application = Application.builder().token(token).build()

    # Command handlers
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("setbase", set_base))
    application.add_handler(CommandHandler("trip", add_trip))
    application.add_handler(CommandHandler("list", list_trips))
    application.add_handler(CommandHandler("report", report))
    application.add_handler(CommandHandler("month", month_report))
    application.add_handler(CommandHandler("year", year_report))
    application.add_handler(CommandHandler("delete", delete_trip))
    application.add_handler(CommandHandler("filter", filter_by_date_cmd))
    application.add_handler(CommandHandler("destination", destination_cmd))
    application.add_handler(CommandHandler("export", export_cmd))
    application.add_handler(CommandHandler("noschool", noschool_cmd))
    application.add_handler(CommandHandler("holiday", holiday_cmd))

    # Weekly job: Friday 10:00 Dubai time
    application.job_queue.run_daily(
        weekly_report_job,
        time=time(hour=10, minute=0, tzinfo=DUBAI_TZ),
        days=(4,),  # Monday=0 ... Friday=4
        name="weekly_report",
    )

    application.run_polling()


if __name__ == "__main__":
    main()
