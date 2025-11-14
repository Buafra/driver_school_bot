# driver_school_bot.py
# Shared-ledger driver trips bot:
# - One ledger shared by all authorized users (you + Abdulla)
# - Weekly/monthly/yearly reports
# - No-school / holiday adjustments
# - Weekly auto report Friday 10:00 (Dubai time)
# Requires: python-telegram-bot[job-queue]==21.4

import os
import json
from datetime import datetime, date, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Optional

from telegram import Update, InputFile
from telegram.ext import Application, CommandHandler, ContextTypes, JobQueue

# --------- Constants & Storage ---------

DATA_FILE = Path("driver_trips_data.json")
DEFAULT_BASE_WEEKLY = 725.0  # AED
DUBAI_TZ = ZoneInfo("Asia/Dubai")
SCHOOL_DAYS_PER_WEEK = 5

# Authorized users (user IDs you gave me)
ALLOWED_USERS = [
    7698278415,  # Faisal
    5034920293,  # Abdulla
]


def load_data() -> Dict[str, Any]:
    if DATA_FILE.exists():
        try:
            with DATA_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    else:
        data = {}
    return ensure_structure(data)


def save_data(data: Dict[str, Any]) -> None:
    try:
        with DATA_FILE.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def ensure_structure(data: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(data, dict):
        data = {}
    if "base_weekly" not in data:
        data["base_weekly"] = DEFAULT_BASE_WEEKLY
    if "trips" not in data:
        data["trips"] = []  # list of {id, date, amount, destination, user_id, user_name}
    if "next_trip_id" not in data:
        data["next_trip_id"] = 1
    if "no_school_dates" not in data:
        data["no_school_dates"] = []  # list of "YYYY-MM-DD"
    if "subscribers" not in data:
        data["subscribers"] = []  # chat_ids to receive weekly report
    return data


# --------- Helper Functions ---------

def parse_iso_datetime(dt_str: str) -> datetime:
    return datetime.fromisoformat(dt_str)


def parse_date_str(d: str) -> date:
    return datetime.strptime(d, "%Y-%m-%d").date()


def today_dubai() -> date:
    return datetime.now(DUBAI_TZ).date()


def filter_trips_by_period(trips: List[Dict[str, Any]],
                           start_dt: Optional[datetime],
                           end_dt: Optional[datetime]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for t in trips:
        dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
        if start_dt and dt < start_dt:
            continue
        if end_dt and dt > end_dt:
            continue
        out.append(t)
    return out


def filter_trips_by_month(trips: List[Dict[str, Any]], year: int, month: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for t in trips:
        dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
        if dt.year == year and dt.month == month:
            out.append(t)
    return out


def filter_trips_by_year(trips: List[Dict[str, Any]], year: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for t in trips:
        dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
        if dt.year == year:
            out.append(t)
    return out


def filter_trips_by_day(trips: List[Dict[str, Any]], d: date) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for t in trips:
        dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ).date()
        if dt == d:
            out.append(t)
    return out


def filter_by_destination(trips: List[Dict[str, Any]], keyword: str) -> List[Dict[str, Any]]:
    k = keyword.lower()
    return [t for t in trips if k in t["destination"].lower()]


def no_school_days_in_period(no_school_dates: List[str],
                             since: Optional[datetime],
                             until: datetime) -> int:
    count = 0
    for d_str in no_school_dates:
        d = parse_date_str(d_str)
        dt = datetime(d.year, d.month, d.day, 0, 0, tzinfo=DUBAI_TZ)
        if since and dt < since:
            continue
        if dt > until:
            continue
        count += 1
    return count


def compute_adjusted_base(base_weekly: float, no_school_days: int) -> float:
    if SCHOOL_DAYS_PER_WEEK <= 0:
        return base_weekly
    base_per_day = base_weekly / SCHOOL_DAYS_PER_WEEK
    adjusted = base_weekly - no_school_days * base_per_day
    return max(adjusted, 0.0)


def week_range_now() -> (datetime, datetime):
    """Return (start_of_week_Monday_00:00, now) in Dubai time."""
    now = datetime.now(DUBAI_TZ)
    week_start_date = now.date() - timedelta(days=now.weekday())  # Monday
    since = datetime(week_start_date.year, week_start_date.month, week_start_date.day,
                     0, 0, tzinfo=DUBAI_TZ)
    return since, now


# --------- Authorization ---------

async def ensure_authorized(update: Update) -> bool:
    user = update.effective_user
    if not user or user.id not in ALLOWED_USERS:
        if update.message:
            await update.message.reply_text("❌ You are not authorized to use this bot.")
        return False
    return True


# --------- Report Builder ---------

def build_weekly_report_text(data: Dict[str, Any],
                             since: datetime,
                             until: datetime) -> str:
    trips = data["trips"]
    base = data["base_weekly"]
    no_school = data["no_school_dates"]

    period_trips = filter_trips_by_period(trips, since, until)
    total_extra = sum(t["amount"] for t in period_trips)

    ns_days = no_school_days_in_period(no_school, since, until)
    adjusted_base = compute_adjusted_base(base, ns_days)
    total_to_pay = adjusted_base + total_extra

    period_str = f"{since.date()} → {until.date()}"

    lines = [
        f"📊 *Weekly Driver Report* ({period_str})",
        "",
        f"🧾 Extra trips count: *{len(period_trips)}*",
        f"💰 Extra trips total: *{total_extra:.2f} AED*",
        "",
        f"🎓 Base weekly (full): *{base:.2f} AED*",
        f"📅 No-school days this week: *{ns_days}*",
        f"🎯 Adjusted base: *{adjusted_base:.2f} AED*",
        "",
        f"✅ *Total to pay this week: {total_to_pay:.2f} AED*",
    ]

    if period_trips:
        lines.append("\n📋 Trip details:")
        for t in period_trips:
            dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
            d_str = dt.strftime("%Y-%m-%d")
            lines.append(
                f"- {d_str}: {t['destination']} — *{t['amount']:.2f} AED* (by {t.get('user_name','?')})"
            )

    return "\n".join(lines)


# --------- Command Handlers ---------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update):
        return

    chat_id = update.effective_chat.id
    data = load_data()

    # register this chat as a subscriber for weekly reports
    if chat_id not in data["subscribers"]:
        data["subscribers"].append(chat_id)
        save_data(data)

    msg = (
        "👋 *DriverSchoolBot — Shared Ledger*\n\n"
        "All trips from you and Abdulla go into *one* ledger.\n\n"
        "Main commands:\n"
        "• `/trip <amount> <destination>` – add extra trip\n"
        "• `/list` – list all trips (you + Abdulla)\n"
        "• `/report` – this week’s report (Mon → now)\n"
        "• `/month [YYYY-MM]` – monthly summary\n"
        "• `/year [YYYY]` – yearly summary\n"
        "• `/delete <id>` – delete a trip by ID\n"
        "• `/filter YYYY-MM-DD` – trips on a specific day\n"
        "• `/destination <keyword>` – filter by destination\n"
        "• `/export` – export all trips as CSV\n"
        "• `/setbase <amount>` – change weekly base (default 725 AED)\n\n"
        "Holidays / No school:\n"
        "• `/noschool` — mark *today* as no-school\n"
        "• `/noschool YYYY-MM-DD` — mark specific day\n"
        "• `/holiday YYYY-MM-DD YYYY-MM-DD` — mark range as no-school\n\n"
        "🔔 Auto weekly report every *Friday 10:00 (Dubai)*."
    )

    await update.message.reply_text(msg, parse_mode="Markdown")


async def set_base(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update):
        return

    if not context.args:
        await update.message.reply_text("Usage: `/setbase 725`", parse_mode="Markdown")
        return

    try:
        amount = float(context.args[0])
    except ValueError:
        await update.message.reply_text("Amount must be a number.")
        return

    data = load_data()
    data["base_weekly"] = amount
    save_data(data)

    await update.message.reply_text(f"✅ Weekly base updated to {amount:.2f} AED")


async def add_trip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update):
        return

    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: `/trip <amount> <destination>`\nExample: `/trip 35 Dubai Mall`",
            parse_mode="Markdown",
        )
        return

    try:
        amount = float(context.args[0])
    except ValueError:
        await update.message.reply_text("Amount must be a number.")
        return

    destination = " ".join(context.args[1:])
    now = datetime.now(DUBAI_TZ)

    data = load_data()
    trip_id = data["next_trip_id"]
    data["next_trip_id"] += 1

    trip = {
        "id": trip_id,
        "date": now.isoformat(),
        "amount": amount,
        "destination": destination,
        "user_id": update.effective_user.id,
        "user_name": update.effective_user.first_name or "User",
    }
    data["trips"].append(trip)
    save_data(data)

    pretty = now.strftime("%Y-%m-%d %H:%M")

    await update.message.reply_text(
        f"✅ Trip added\n"
        f"🆔 ID: {trip_id}\n"
        f"📅 {pretty}\n"
        f"📍 {destination}\n"
        f"💰 {amount:.2f} AED"
    )


async def list_trips(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update):
        return

    data = load_data()
    trips = data["trips"]

    if not trips:
        await update.message.reply_text("No trips recorded yet.")
        return

    lines = ["📋 *All trips (shared ledger):*"]
    total = 0.0
    for t in sorted(trips, key=lambda x: x["id"]):
        dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
        d_str = dt.strftime("%Y-%m-%d")
        total += t["amount"]
        lines.append(
            f"- ID {t['id']}: {d_str} — {t['destination']} — *{t['amount']:.2f} AED* (by {t.get('user_name','?')})"
        )

    lines.append(f"\n💰 Total extra (all time): *{total:.2f} AED*")

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Manual weekly report: always this week (Mon → now)."""
    if not await ensure_authorized(update):
        return

    data = load_data()
    since, now = week_range_now()

    text = build_weekly_report_text(data, since, now)
    await update.message.reply_text(text, parse_mode="Markdown")


async def month_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update):
        return

    data = load_data()
    trips = data["trips"]

    today = datetime.now(DUBAI_TZ)
    if context.args:
        try:
            ym = context.args[0]
            year_str, month_str = ym.split("-")
            year = int(year_str)
            month = int(month_str)
        except Exception:
            await update.message.reply_text("Use: `/month YYYY-MM` e.g. `/month 2025-11`",
                                            parse_mode="Markdown")
            return
    else:
        year, month = today.year, today.month

    m_trips = filter_trips_by_month(trips, year, month)
    if not m_trips:
        await update.message.reply_text(f"No trips in {year}-{month:02d}.")
        return

    total = sum(t["amount"] for t in m_trips)
    lines = [f"📅 *Monthly Report {year}-{month:02d}*", f"💰 Extra total: *{total:.2f} AED*", "", "📋 Details:"]
    for t in sorted(m_trips, key=lambda x: x["id"]):
        dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
        d_str = dt.strftime("%Y-%m-%d")
        lines.append(
            f"- ID {t['id']}: {d_str} — {t['destination']} — *{t['amount']:.2f} AED* (by {t.get('user_name','?')})"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def year_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update):
        return

    data = load_data()
    trips = data["trips"]

    today = datetime.now(DUBAI_TZ)
    if context.args:
        try:
            year = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Use: `/year 2025`", parse_mode="Markdown")
            return
    else:
        year = today.year

    y_trips = filter_trips_by_year(trips, year)
    if not y_trips:
        await update.message.reply_text(f"No trips in {year}.")
        return

    total = sum(t["amount"] for t in y_trips)
    lines = [f"📅 *Yearly Report {year}*", f"💰 Extra total: *{total:.2f} AED*", "", "📋 Details:"]
    for t in sorted(y_trips, key=lambda x: x["id"]):
        dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
        d_str = dt.strftime("%Y-%m-%d")
        lines.append(
            f"- ID {t['id']}: {d_str} — {t['destination']} — *{t['amount']:.2f} AED* (by {t.get('user_name','?')})"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def delete_trip(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update):
        return

    if not context.args:
        await update.message.reply_text("Use: `/delete <id>`", parse_mode="Markdown")
        return

    try:
        trip_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Trip ID must be a number.")
        return

    data = load_data()
    trips = data["trips"]

    before = len(trips)
    trips = [t for t in trips if t["id"] != trip_id]
    after = len(trips)

    if before == after:
        await update.message.reply_text(f"No trip found with ID {trip_id}.")
        return

    data["trips"] = trips
    save_data(data)

    await update.message.reply_text(f"✅ Trip {trip_id} deleted.")


async def filter_by_date_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update):
        return

    if not context.args:
        await update.message.reply_text("Use: `/filter YYYY-MM-DD`", parse_mode="Markdown")
        return

    try:
        d = parse_date_str(context.args[0])
    except Exception:
        await update.message.reply_text("Invalid date format. Use YYYY-MM-DD.")
        return

    data = load_data()
    trips = filter_trips_by_day(data["trips"], d)

    if not trips:
        await update.message.reply_text(f"No trips on {d}.")
        return

    total = sum(t["amount"] for t in trips)
    lines = [f"📅 Trips on {d}:", f"💰 Total: *{total:.2f} AED*", ""]
    for t in sorted(trips, key=lambda x: x["id"]):
        lines.append(
            f"- ID {t['id']}: {t['destination']} — *{t['amount']:.2f} AED* (by {t.get('user_name','?')})"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def destination_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update):
        return

    if not context.args:
        await update.message.reply_text("Use: `/destination mall`", parse_mode="Markdown")
        return

    keyword = " ".join(context.args)
    data = load_data()
    trips = filter_by_destination(data["trips"], keyword)

    if not trips:
        await update.message.reply_text(f"No trips matching '{keyword}'.")
        return

    total = sum(t["amount"] for t in trips)
    lines = [f"📍 Trips matching '{keyword}':", f"💰 Total: *{total:.2f} AED*", ""]
    for t in sorted(trips, key=lambda x: x["id"]):
        dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
        d_str = dt.strftime("%Y-%m-%d")
        lines.append(
            f"- ID {t['id']}: {d_str} — {t['destination']} — *{t['amount']:.2f} AED* (by {t.get('user_name','?')})"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update):
        return

    data = load_data()
    trips = data["trips"]

    if not trips:
        await update.message.reply_text("No trips to export.")
        return

    filename = "driver_trips_export.csv"
    with open(filename, "w", encoding="utf-8") as f:
        f.write("id,date,amount,destination,user_id,user_name\n")
        for t in sorted(trips, key=lambda x: x["id"]):
            f.write(
                f"{t['id']},{t['date']},{t['amount']},"
                f"\"{t['destination'].replace('\"','\"\"')}\","
                f"{t.get('user_id','')},\"{(t.get('user_name') or '').replace('\"','\"\"')}\"\n"
            )

    await update.message.reply_document(
        document=InputFile(filename),
        filename=filename,
        caption="📄 All trips (shared ledger) exported as CSV.",
    )


async def noschool_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update):
        return

    data = load_data()

    if context.args:
        try:
            d = parse_date_str(context.args[0])
        except Exception:
            await update.message.reply_text("Invalid date format. Use YYYY-MM-DD.")
            return
    else:
        d = today_dubai()

    d_str = d.strftime("%Y-%m-%d")
    if d_str not in data["no_school_dates"]:
        data["no_school_dates"].append(d_str)
        save_data(data)
        await update.message.reply_text(f"✅ Marked {d_str} as no-school day.")
    else:
        await update.message.reply_text(f"ℹ️ {d_str} is already marked as no-school.")


async def holiday_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update):
        return

    if len(context.args) != 2:
        await update.message.reply_text(
            "Use: `/holiday YYYY-MM-DD YYYY-MM-DD`",
            parse_mode="Markdown",
        )
        return

    try:
        start_d = parse_date_str(context.args[0])
        end_d = parse_date_str(context.args[1])
    except Exception:
        await update.message.reply_text("Dates must be in YYYY-MM-DD format.")
        return

    if end_d < start_d:
        await update.message.reply_text("End date must be after or equal to start date.")
        return

    data = load_data()
    no_school_dates = set(data["no_school_dates"])
    cur = start_d
    added = 0
    while cur <= end_d:
        d_str = cur.strftime("%Y-%m-%d")
        if d_str not in no_school_dates:
            no_school_dates.add(d_str)
            added += 1
        cur += timedelta(days=1)

    data["no_school_dates"] = sorted(no_school_dates)
    save_data(data)

    await update.message.reply_text(
        f"✅ Holiday set from {start_d} to {end_d}. Added {added} no-school days."
    )


# --------- Weekly Job (Friday 10:00) ---------

async def weekly_report_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    since, now = week_range_now()
    text = build_weekly_report_text(data, since, now)

    subscribers: List[int] = data.get("subscribers", [])
    for chat_id in subscribers:
        try:
            await context.bot.send_message(chat_id, text, parse_mode="Markdown")
        except Exception:
            # ignore send errors (chat left, blocked, etc.)
            continue


# --------- Main ---------

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

    # JobQueue (requires python-telegram-bot[job-queue])
    if application.job_queue is None:
        jq = JobQueue()
        jq.set_application(application)
    else:
        jq = application.job_queue

    jq.run_daily(
        weekly_report_job,
        time=time(hour=10, minute=0, tzinfo=DUBAI_TZ),
        days=(4,),  # Friday (Mon=0, Tue=1, ... Fri=4)
        name="weekly_report",
    )

    application.run_polling()


if __name__ == "__main__":
    main()
