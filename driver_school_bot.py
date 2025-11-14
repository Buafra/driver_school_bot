# driver_school_bot.py
# Telegram bot to track driver's extra trips with weekly/monthly/yearly reports,
# holidays, no-school adjustments, and Render-compatible JobQueue.

import os
import json
from datetime import datetime, date, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Optional

from telegram import Update, InputFile
from telegram.ext import (
    Application, CommandHandler, ContextTypes, JobQueue
)

DATA_FILE = Path("driver_trips_data.json")
DEFAULT_BASE_WEEKLY = 725.0  # AED
DUBAI_TZ = ZoneInfo("Asia/Dubai")
SCHOOL_DAYS_PER_WEEK = 5

# ONLY authorized users:
ALLOWED_USERS = [
    7698278415,  # Faisal
    5034920293   # Abdulla
]

# ----------------- Persistence -----------------

def load_data() -> Dict[str, Any]:
    if DATA_FILE.exists():
        try:
            with DATA_FILE.open("r", encoding="utf-8") as f:
                return json.load(f)
        except:
            return {}
    return {}

def save_data(data: Dict[str, Any]) -> None:
    try:
        with DATA_FILE.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except:
        pass

def get_user_record(chat_id: int, data: Dict[str, Any], username: str = "") -> Dict[str, Any]:
    key = str(chat_id)
    if key not in data:
        data[key] = {
            "chat_id": chat_id,
            "base_weekly": DEFAULT_BASE_WEEKLY,
            "trips": [],
            "next_trip_id": 1,
            "no_school_dates": [],
            "last_weekly_report": None,
        }
    if username:
        data[key]["last_username"] = username
    return data[key]

# ----------------- Helpers -----------------

def parse_iso_datetime(dt_str: str) -> datetime:
    return datetime.fromisoformat(dt_str)

def parse_date_str(d: str) -> date:
    return datetime.strptime(d, "%Y-%m-%d").date()

def today_dubai() -> date:
    return datetime.now(DUBAI_TZ).date()

def filter_trips_by_period(trips, start_dt, end_dt):
    out = []
    for t in trips:
        dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
        if start_dt and dt <= start_dt:
            continue
        if end_dt and dt > end_dt:
            continue
        out.append(t)
    return out

def filter_trips_by_month(trips, year, month):
    return [
        t for t in trips
        if parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ).year == year and
           parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ).month == month
    ]

def filter_trips_by_year(trips, year):
    return [
        t for t in trips
        if parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ).year == year
    ]

def filter_trips_by_day(trips, d):
    return [
        t for t in trips
        if parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ).date() == d
    ]

def filter_by_destination(trips, keyword):
    k = keyword.lower()
    return [t for t in trips if k in t["destination"].lower()]

def no_school_days_in_period(no_school, since, until):
    count = 0
    for d_str in no_school:
        d = parse_date_str(d_str)
        d_dt = datetime(d.year, d.month, d.day, 0, 0, tzinfo=DUBAI_TZ)
        if since and d_dt <= since:
            continue
        if d_dt > until:
            continue
        count += 1
    return count

def compute_adjusted_base(base_weekly, no_school_days):
    if SCHOOL_DAYS_PER_WEEK <= 0:
        return base_weekly
    base_per_day = base_weekly / SCHOOL_DAYS_PER_WEEK
    adj = base_weekly - (no_school_days * base_per_day)
    return max(adj, 0.0)

# ----------------- Auth -----------------

async def ensure_authorized(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else None
    if uid not in ALLOWED_USERS:
        await update.message.reply_text("❌ You are not authorized to use this bot.")
        return False
    return True

# ----------------- Commands -----------------

async def start(update: Update, context):
    if not await ensure_authorized(update): return

    data = load_data()
    user = get_user_record(update.effective_chat.id, data)

    msg = (
        "👋 *DriverSchoolBot Ready*\n\n"
        "Commands:\n"
        "• `/trip <amount> <destination>`\n"
        "• `/list`\n"
        "• `/report`\n"
        "• `/month` or `/month YYYY-MM`\n"
        "• `/year` or `/year YYYY`\n"
        "• `/delete <id>`\n"
        "• `/filter YYYY-MM-DD`\n"
        "• `/destination keyword`\n"
        "• `/export`\n"
        "• `/setbase <amount>`\n"
        "• `/noschool`\n"
        "• `/noschool YYYY-MM-DD`\n"
        "• `/holiday YYYY-MM-DD YYYY-MM-DD`\n\n"
        "📅 Automatic report every Friday 10:00 (Dubai)"
    )

    await update.message.reply_text(msg, parse_mode="Markdown")

async def set_base(update: Update, context):
    if not await ensure_authorized(update): return

    if not context.args:
        await update.message.reply_text("Usage: `/setbase 725`", parse_mode="Markdown")
        return

    try: amount = float(context.args[0])
    except: 
        await update.message.reply_text("Invalid number.")
        return

    data = load_data()
    user = get_user_record(update.effective_chat.id, data)
    user["base_weekly"] = amount
    save_data(data)

    await update.message.reply_text(f"Base updated to {amount} AED")

async def add_trip(update: Update, context):
    if not await ensure_authorized(update): return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: `/trip 20 Dubai Mall`", parse_mode="Markdown")
        return

    try: amount = float(context.args[0])
    except: 
        await update.message.reply_text("Amount must be a number")
        return

    dest = " ".join(context.args[1:])
    now = datetime.now(DUBAI_TZ)

    data = load_data()
    user = get_user_record(update.effective_chat.id, data)

    trip_id = user["next_trip_id"]
    user["next_trip_id"] += 1

    trip = {
        "id": trip_id,
        "date": now.isoformat(),
        "amount": amount,
        "destination": dest,
        "user_id": update.effective_user.id,
        "user_name": update.effective_user.first_name or "User",
    }

    user["trips"].append(trip)
    save_data(data)

    pretty = now.strftime("%Y-%m-%d %H:%M")

    await update.message.reply_text(
        f"✅ Trip added\n"
        f"ID: {trip_id}\n"
        f"{pretty}\n"
        f"{dest}\n"
        f"{amount} AED"
    )

async def list_trips(update: Update, context):
    if not await ensure_authorized(update): return

    data = load_data()
    user = get_user_record(update.effective_chat.id, data)
    trips = user["trips"]

    if not trips:
        await update.message.reply_text("No trips recorded.")
        return

    lines = ["📋 *All trips:*"]
    for t in trips:
        dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
        d_str = dt.strftime("%Y-%m-%d")
        lines.append(
            f"- ID {t['id']}: {d_str} — {t['destination']} — {t['amount']} AED (by {t['user_name']})"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")

def build_weekly_report(user, since, until):
    trips = user["trips"]
    base = user["base_weekly"]
    no_school = user["no_school_dates"]

    period = filter_trips_by_period(trips, since, until)
    total_extra = sum(t["amount"] for t in period)

    n_no = no_school_days_in_period(no_school, since, until)
    adj_base = compute_adjusted_base(base, n_no)
    total = adj_base + total_extra

    if since:
        p = f"{since.date()} → {until.date()}"
    else:
        p = f"Until {until.date()}"

    lines = [
        f"📊 *Weekly Report ({p})*",
        f"Trips: {len(period)}",
        f"Extra total: {total_extra} AED",
        f"No-school days: {n_no}",
        f"Adjusted base: {adj_base} AED",
        "",
        f"💰 *Total to pay: {total} AED*",
        "",
        "📋 Details:"
    ]

    for t in period:
        dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
        d_str = dt.strftime("%Y-%m-%d")
        lines.append(f"- {d_str} {t['destination']} — {t['amount']} AED")

    return "\n".join(lines)

async def report(update: Update, context):
    if not await ensure_authorized(update): return

    data = load_data()
    user = get_user_record(update.effective_chat.id, data)

    now = datetime.now(DUBAI_TZ)
    last = user["last_weekly_report"]
    since = parse_iso_datetime(last) if last else None

    txt = build_weekly_report(user, since, now)
    user["last_weekly_report"] = now.isoformat()
    save_data(data)

    await update.message.reply_text(txt, parse_mode="Markdown")

async def week_job(ctx):
    data = load_data()
    now = datetime.now(DUBAI_TZ)

    for key, user in data.items():
        chat = user["chat_id"]
        last = user.get("last_weekly_report")
        since = parse_iso_datetime(last) if last else None

        # If nothing to report
        if not filter_trips_by_period(user["trips"], since, now) and \
           no_school_days_in_period(user["no_school_dates"], since, now) == 0:
            user["last_weekly_report"] = now.isoformat()
            continue

        msg = build_weekly_report(user, since, now)
        try:
            await ctx.bot.send_message(chat, msg, parse_mode="Markdown")
            user["last_weekly_report"] = now.isoformat()
        except:
            pass

    save_data(data)

async def month_cmd(update: Update, context):
    if not await ensure_authorized(update): return

    data = load_data()
    user = get_user_record(update.effective_chat.id, data)

    today = datetime.now(DUBAI_TZ)
    if context.args:
        y, m = context.args[0].split("-")
        y, m = int(y), int(m)
    else:
        y, m = today.year, today.month

    trips = filter_trips_by_month(user["trips"], y, m)
    total = sum(t["amount"] for t in trips)

    if not trips:
        await update.message.reply_text("No trips in this month.")
        return

    lines = [f"📅 Monthly Report {y}-{m:02d}", f"Total extra: {total} AED", "", "Details:"]
    for t in trips:
        dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
        lines.append(f"- {dt.date()} {t['destination']} — {t['amount']} AED")

    await update.message.reply_text("\n".join(lines))

async def year_cmd(update: Update, context):
    if not await ensure_authorized(update): return

    data = load_data()
    user = get_user_record(update.effective_chat.id, data)

    today = datetime.now(DUBAI_TZ)
    y = int(context.args[0]) if context.args else today.year

    trips = filter_trips_by_year(user["trips"], y)
    total = sum(t["amount"] for t in trips)

    if not trips:
        await update.message.reply_text("No trips in this year.")
        return

    lines = [f"📅 Year Report {y}", f"Total extra: {total} AED", "", "Details:"]
    for t in trips:
        dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
        lines.append(f"- {dt.date()} {t['destination']} — {t['amount']} AED")

    await update.message.reply_text("\n".join(lines))

async def delete_cmd(update: Update, context):
    if not await ensure_authorized(update): return

    if not context.args:
        await update.message.reply_text("Usage: `/delete 3`", parse_mode="Markdown")
        return

    try: tid = int(context.args[0])
    except:
        await update.message.reply_text("Trip ID must be a number.")
        return

    data = load_data()
    user = get_user_record(update.effective_chat.id, data)

    before = len(user["trips"])
    user["trips"] = [t for t in user["trips"] if t["id"] != tid]
    after = len(user["trips"])

    if before == after:
        await update.message.reply_text(f"No trip found with ID {tid}")
    else:
        save_data(data)
        await update.message.reply_text(f"Deleted trip {tid}")

async def filter_cmd(update: Update, context):
    if not await ensure_authorized(update): return
    if not context.args:
        await update.message.reply_text("Usage: `/filter 2025-11-20`", parse_mode="Markdown")
        return

    try: d = parse_date_str(context.args[0])
    except:
        await update.message.reply_text("Invalid date format.")
        return

    data = load_data()
    user = get_user_record(update.effective_chat.id, data)
    trips = filter_trips_by_day(user["trips"], d)

    if not trips:
        await update.message.reply_text("No trips on that day.")
        return

    lines = [f"Trips on {d}:"]
    for t in trips:
        lines.append(f"- {t['destination']} — {t['amount']} AED")

    await update.message.reply_text("\n".join(lines))

async def destination_cmd(update: Update, context):
    if not await ensure_authorized(update): return
    if not context.args:
        await update.message.reply_text("Usage: `/destination mall`", parse_mode="Markdown")
        return

    keyword = " ".join(context.args)
    data = load_data()
    user = get_user_record(update.effective_chat.id, data)
    trips = filter_by_destination(user["trips"], keyword)

    if not trips:
        await update.message.reply_text("No matching trips.")
        return

    total = sum(t["amount"] for t in trips)
    lines = [f"Trips matching '{keyword}':", f"Total: {total} AED", ""]
    for t in trips:
        dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
        lines.append(f"- {dt.date()} {t['destination']} — {t['amount']} AED")

    await update.message.reply_text("\n".join(lines))

async def export_cmd(update: Update, context):
    if not await ensure_authorized(update): return

    data = load_data()
    user = get_user_record(update.effective_chat.id, data)

    if not user["trips"]:
        await update.message.reply_text("No trips recorded.")
        return

    fname = "driver_trips_export.csv"
    with open(fname, "w", encoding="utf-8") as f:
        f.write("id,date,amount,destination,user_id,user_name\n")
        for t in user["trips"]:
            f.write(f"{t['id']},{t['date']},{t['amount']},\"{t['destination']}\",{t['user_id']},\"{t['user_name']}\"\n")

    await update.message.reply_document(InputFile(fname))

async def noschool_cmd(update: Update, context):
    if not await ensure_authorized(update): return

    data = load_data()
    user = get_user_record(update.effective_chat.id, data)

    if context.args:
        try: d = parse_date_str(context.args[0])
        except:
            await update.message.reply_text("Invalid date.")
            return
    else:
        d = today_dubai()

    s = d.strftime("%Y-%m-%d")
    if s not in user["no_school_dates"]:
        user["no_school_dates"].append(s)
        save_data(data)
        await update.message.reply_text(f"Marked {s} as no-school day.")
    else:
        await update.message.reply_text(f"{s} already marked.")

async def holiday_cmd(update: Update, context):
    if not await ensure_authorized(update): return
    if len(context.args) != 2:
        await update.message.reply_text("Usage: `/holiday 2025-12-02 2025-12-05`")
        return

    try:
        start = parse_date_str(context.args[0])
        end = parse_date_str(context.args[1])
    except:
        await update.message.reply_text("Invalid dates.")
        return

    if end < start:
        await update.message.reply_text("End date must be after start date.")
        return

    data = load_data()
    user = get_user_record(update.effective_chat.id, data)

    added = 0
    cur = start
    while cur <= end:
        s = cur.strftime("%Y-%m-%d")
        if s not in user["no_school_dates"]:
            user["no_school_dates"].append(s)
            added += 1
        cur += timedelta(days=1)

    save_data(data)
    await update.message.reply_text(f"Holiday added, {added} days marked.")

# ----------------- MAIN (Render safe) -----------------

def main():
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("BOT_TOKEN is missing.")

    application = Application.builder().token(token).build()

    # Commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("setbase", set_base))
    application.add_handler(CommandHandler("trip", add_trip))
    application.add_handler(CommandHandler("list", list_trips))
    application.add_handler(CommandHandler("report", report))
    application.add_handler(CommandHandler("month", month_cmd))
    application.add_handler(CommandHandler("year", year_cmd))
    application.add_handler(CommandHandler("delete", delete_cmd))
    application.add_handler(CommandHandler("filter", filter_cmd))
    application.add_handler(CommandHandler("destination", destination_cmd))
    application.add_handler(CommandHandler("export", export_cmd))
    application.add_handler(CommandHandler("noschool", noschool_cmd))
    application.add_handler(CommandHandler("holiday", holiday_cmd))

    # JobQueue FIX for Render
    if application.job_queue is None:
        jq = JobQueue()
        jq.set_application(application)
    else:
        jq = application.job_queue

    jq.run_daily(
        week_job,
        time=time(hour=10, minute=0, tzinfo=DUBAI_TZ),
        days=(4,),  # Friday
        name="weekly_report"
    )

    application.run_polling()


if __name__ == "__main__":
    main()
