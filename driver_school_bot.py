# driver_school_bot.py
# DriverSchoolBot — Shared Ledger for Driver Extra Trips
# Features:
# - Shared ledger (all authorized users share one account)
# - Weekly report (Mon → now) + auto report Friday 10:00 (Dubai)
# - No school / holiday / remove no-school
# - Test mode: test trips ignored in real totals
# - Buttons UI (/menu)
# - Clear all trips
#
# Requirements:
#   python-telegram-bot[job-queue]==21.4

import os
import json
from datetime import datetime, date, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Optional

from telegram import (
    Update,
    InputFile,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    JobQueue,
    MessageHandler,
    filters,
)

# --------- Constants & Storage ---------

DATA_FILE = Path("driver_trips_data.json")
DEFAULT_BASE_WEEKLY = 725.0  # AED
DUBAI_TZ = ZoneInfo("Asia/Dubai")
SCHOOL_DAYS_PER_WEEK = 5

# Authorized users
ALLOWED_USERS = [
    7698278415,  # Faisal
    5034920293,  # Abdulla
]

# Buttons labels (for /menu keyboard)
BTN_ADD_TRIP = "➕ Add Trip"
BTN_LIST = "📋 List Trips"
BTN_REPORT = "📊 Weekly Report"
BTN_MONTH = "📅 Month"
BTN_YEAR = "📆 Year"
BTN_NOSCHOOL = "🏫 No School Today"
BTN_REMOVESCHOOL = "❌ Remove No School"
BTN_HOLIDAY = "🎉 Holiday Range"
BTN_EXPORT = "📄 Export CSV"
BTN_TOGGLE_TEST = "🧪 Toggle Test Mode"
BTN_CLEAR_TRIPS = "🧹 Clear All Trips"


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
        data["trips"] = []  # list of {id, date, amount, destination, user_id, user_name, is_test}
    if "next_trip_id" not in data:
        data["next_trip_id"] = 1
    if "no_school_dates" not in data:
        data["no_school_dates"] = []  # list of "YYYY-MM-DD"
    if "subscribers" not in data:
        data["subscribers"] = []  # chat_ids to receive weekly report
    if "test_mode" not in data:
        data["test_mode"] = False  # global test mode flag
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
    since = datetime(
        week_start_date.year, week_start_date.month, week_start_date.day,
        0, 0, tzinfo=DUBAI_TZ
    )
    return since, now


def is_real_trip(trip: Dict[str, Any]) -> bool:
    """True if trip should count in real totals (not test)."""
    return not trip.get("is_test", False)


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
    trips_all = data["trips"]
    base = data["base_weekly"]
    no_school = data["no_school_dates"]

    period_raw = filter_trips_by_period(trips_all, since, until)
    period_trips = [t for t in period_raw if is_real_trip(t)]
    total_extra = sum(t["amount"] for t in period_trips)

    ns_days = no_school_days_in_period(no_school, since, until)
    adjusted_base = compute_adjusted_base(base, ns_days)
    total_to_pay = adjusted_base + total_extra

    period_str = f"{since.date()} → {until.date()}"

    lines = [
        f"📊 *Weekly Driver Report* ({period_str})",
        "",
        f"🧾 Extra trips count (real): *{len(period_trips)}*",
        f"💰 Extra trips total (real): *{total_extra:.2f} AED*",
        "",
        f"🎓 Base weekly (full): *{base:.2f} AED*",
        f"📅 No-school days this week: *{ns_days}*",
        f"🎯 Adjusted base: *{adjusted_base:.2f} AED*",
        "",
        f"✅ *Total to pay this week: {total_to_pay:.2f} AED*",
    ]

    if period_trips:
        lines.append("\n📋 Trip details (real trips only):")
        for t in period_trips:
            dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
            d_str = dt.strftime("%Y-%m-%d")
            lines.append(
                f"- {d_str}: {t['destination']} — *{t['amount']:.2f} AED* (by {t.get('user_name','?')})"
            )

    return "\n".join(lines)


# --------- Commands ---------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Always reply to /start and show the user's Telegram ID + auth status."""
    user = update.effective_user
    chat_id = update.effective_chat.id
    data = load_data()

    # Register chat as subscriber for weekly reports
    if chat_id not in data["subscribers"]:
        data["subscribers"].append(chat_id)
        save_data(data)

    user_id = user.id if user else None
    is_auth = user_id in ALLOWED_USERS

    msg = (
        "👋 *DriverSchoolBot — Shared Ledger*\n\n"
        "All trips from you and Abdulla go into *one* account.\n\n"
        f"👤 Your Telegram ID: `{user_id}`\n"
        f"🔐 Authorized: *{'YES ✅' if is_auth else 'NO ❌'}*\n\n"
        "Main commands:\n"
        "• `/menu` — show buttons menu\n"
        "• `/trip <amount> <destination>` – add extra trip\n"
        "• `/list` – list all trips (you + Abdulla)\n"
        "• `/report` – this week’s report (Mon → now)\n"
        "• `/month [YYYY-MM]` – monthly summary\n"
        "• `/year [YYYY]` – yearly summary\n"
        "• `/delete <id>` – delete a trip by ID\n"
        "• `/filter YYYY-MM-DD` – trips on a specific day\n"
        "• `/destination <keyword>` – filter by destination\n"
        "• `/export` – export all trips as CSV\n"
        "• `/setbase <amount>` – change weekly base (default 725 AED)\n"
        "• `/cleartrips` – delete *all* trips (real + test)\n\n"
        "Holidays / No school:\n"
        "• `/noschool` — mark *today* as no-school\n"
        "• `/noschool YYYY-MM-DD` — mark specific day\n"
        "• `/holiday YYYY-MM-DD YYYY-MM-DD` — mark range as no-school\n"
        "• `/removeschool` or `/removeschool YYYY-MM-DD` — unmark a no-school day\n\n"
        "Test mode:\n"
        "• `/test_on` — new trips become TEST (ignored in totals)\n"
        "• `/test_off` — back to normal\n\n"
        "🔔 Auto weekly report every *Friday 10:00 (Dubai)*."
    )

    if update.message:
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

    is_test = data.get("test_mode", False)

    trip = {
        "id": trip_id,
        "date": now.isoformat(),
        "amount": amount,
        "destination": destination,
        "user_id": update.effective_user.id,
        "user_name": update.effective_user.first_name or "User",
        "is_test": is_test,
    }
    data["trips"].append(trip)
    save_data(data)

    pretty = now.strftime("%Y-%m-%d %H:%M")
    test_label = "🧪 [TEST] " if is_test else ""

    await update.message.reply_text(
        f"✅ {test_label}Trip added\n"
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
    real_total = 0.0
    test_total = 0.0

    for t in sorted(trips, key=lambda x: x["id"]):
        dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
        d_str = dt.strftime("%Y-%m-%d")
        is_test = t.get("is_test", False)
        tag = " 🧪[TEST]" if is_test else ""
        if is_test:
            test_total += t["amount"]
        else:
            real_total += t["amount"]

        lines.append(
            f"- ID {t['id']}: {d_str} — {t['destination']} — *{t['amount']:.2f} AED*{tag} "
            f"(by {t.get('user_name','?')})"
        )

    lines.append(f"\n💰 Real trips total: *{real_total:.2f} AED*")
    lines.append(f"🧪 Test trips total (ignored in reports): *{test_total:.2f} AED*")

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
    trips_all = data["trips"]

    today = datetime.now(DUBAI_TZ)
    if context.args:
        try:
            ym = context.args[0]
            year_str, month_str = ym.split("-")
            year = int(year_str)
            month = int(month_str)
        except Exception:
            await update.message.reply_text(
                "Use: `/month YYYY-MM` e.g. `/month 2025-11`",
                parse_mode="Markdown",
            )
            return
    else:
        year, month = today.year, today.month

    raw = filter_trips_by_month(trips_all, year, month)
    m_trips = [t for t in raw if is_real_trip(t)]

    if not m_trips:
        await update.message.reply_text(f"No *real* trips in {year}-{month:02d}.", parse_mode="Markdown")
        return

    total = sum(t["amount"] for t in m_trips)
    lines = [
        f"📅 *Monthly Report {year}-{month:02d}*",
        f"💰 Extra total (real trips): *{total:.2f} AED*",
        "",
        "📋 Details:",
    ]
    for t in sorted(m_trips, key=lambda x: x["id"]):
        dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
        d_str = dt.strftime("%Y-%m-%d")
        lines.append(
            f"- ID {t['id']}: {d_str} — {t['destination']} — *{t['amount']:.2f} AED* "
            f"(by {t.get('user_name','?')})"
        )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def year_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update):
        return

    data = load_data()
    trips_all = data["trips"]

    today = datetime.now(DUBAI_TZ)
    if context.args:
        try:
            year = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Use: `/year 2025`", parse_mode="Markdown")
            return
    else:
        year = today.year

    raw = filter_trips_by_year(trips_all, year)
    y_trips = [t for t in raw if is_real_trip(t)]

    if not y_trips:
        await update.message.reply_text(f"No *real* trips in {year}.", parse_mode="Markdown")
        return

    total = sum(t["amount"] for t in y_trips)
    lines = [
        f"📅 *Yearly Report {year}*",
        f"💰 Extra total (real trips): *{total:.2f} AED*",
        "",
        "📋 Details:",
    ]
    for t in sorted(y_trips, key=lambda x: x["id"]):
        dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
        d_str = dt.strftime("%Y-%m-%d")
        lines.append(
            f"- ID {t['id']}: {d_str} — {t['destination']} — *{t['amount']:.2f} AED* "
            f"(by {t.get('user_name','?')})"
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


async def clear_trips_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete ALL trips (real + test)."""
    if not await ensure_authorized(update):
        return

    data = load_data()
    count = len(data["trips"])
    data["trips"] = []
    save_data(data)

    await update.message.reply_text(f"🧹 Cleared all trips. Removed {count} records.")


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
    raw = filter_trips_by_day(data["trips"], d)
    trips = [t for t in raw if is_real_trip(t)]

    if not trips:
        await update.message.reply_text(f"No *real* trips on {d}.", parse_mode="Markdown")
        return

    total = sum(t["amount"] for t in trips)
    lines = [f"📅 Trips on {d} (real only):", f"💰 Total: *{total:.2f} AED*", ""]
    for t in sorted(trips, key=lambda x: x["id"]):
        lines.append(
            f"- ID {t['id']}: {t['destination']} — *{t['amount']:.2f} AED* "
            f"(by {t.get('user_name','?')})"
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
    raw = filter_by_destination(data["trips"], keyword)
    trips = [t for t in raw if is_real_trip(t)]

    if not trips:
        await update.message.reply_text(f"No *real* trips matching '{keyword}'.", parse_mode="Markdown")
        return

    total = sum(t["amount"] for t in trips)
    lines = [f"📍 Trips matching '{keyword}' (real only):", f"💰 Total: *{total:.2f} AED*", ""]
    for t in sorted(trips, key=lambda x: x["id"]):
        dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
        d_str = dt.strftime("%Y-%m-%d")
        lines.append(
            f"- ID {t['id']}: {d_str} — {t['destination']} — *{t['amount']:.2f} AED* "
            f"(by {t.get('user_name','?')})"
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
        f.write("id,date,amount,destination,user_id,user_name,is_test\n")
        for t in sorted(trips, key=lambda x: x["id"]):
            f.write(
                f"{t['id']},{t['date']},{t['amount']},"
                f"\"{t['destination'].replace('\"','\"\"')}\","
                f"{t.get('user_id','')},\"{(t.get('user_name') or '').replace('\"','\"\"')}\","
                f"{1 if t.get('is_test', False) else 0}\n"
            )

    await update.message.reply_document(
        document=InputFile(filename),
        filename=filename,
        caption="📄 All trips (real + test) exported as CSV.",
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


async def removeschool_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove a date from the no-school list."""
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
    if d_str in data["no_school_dates"]:
        data["no_school_dates"].remove(d_str)
        save_data(data)
        await update.message.reply_text(f"✅ {d_str} removed from no-school days.")
    else:
        await update.message.reply_text(f"ℹ️ {d_str} was not marked as no-school.")


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


# --------- Test Mode Commands ---------

async def test_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update):
        return

    data = load_data()
    data["test_mode"] = True
    save_data(data)

    await update.message.reply_text(
        "🧪 *Test Mode is ON*\n"
        "New `/trip` entries will be marked as TEST and *ignored* in reports.",
        parse_mode="Markdown",
    )


async def test_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update):
        return

    data = load_data()
    data["test_mode"] = False
    save_data(data)

    await update.message.reply_text(
        "✅ *Test Mode is OFF*\n"
        "New `/trip` entries will be counted as REAL in reports.",
        parse_mode="Markdown",
    )


async def toggle_test_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Toggle test_mode (for menu button)."""
    if not await ensure_authorized(update):
        return

    data = load_data()
    current = data.get("test_mode", False)
    data["test_mode"] = not current
    save_data(data)

    if data["test_mode"]:
        msg = (
            "🧪 *Test Mode is now ON*\n"
            "New trips will be TEST and ignored in totals."
        )
    else:
        msg = (
            "✅ *Test Mode is now OFF*\n"
            "New trips will be REAL and counted in all reports."
        )

    await update.message.reply_text(msg, parse_mode="Markdown")


# --------- Buttons UI (/menu) ---------

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update):
        return

    keyboard = [
        [KeyboardButton(BTN_ADD_TRIP), KeyboardButton(BTN_LIST)],
        [KeyboardButton(BTN_REPORT), BTN_MONTH, BTN_YEAR],
        [KeyboardButton(BTN_NOSCHOOL), KeyboardButton(BTN_REMOVESCHOOL)],
        [KeyboardButton(BTN_HOLIDAY), KeyboardButton(BTN_EXPORT)],
        [KeyboardButton(BTN_TOGGLE_TEST), KeyboardButton(BTN_CLEAR_TRIPS)],
    ]

    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    await update.message.reply_text(
        "📱 Menu — choose an action:",
        reply_markup=reply_markup,
    )


async def menu_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle presses on the ReplyKeyboard buttons."""
    if not await ensure_authorized(update):
        return

    text = (update.message.text or "").strip()

    # Only react to our known buttons; ignore normal text
    if text == BTN_ADD_TRIP:
        await update.message.reply_text(
            "To add a trip, send:\n`/trip <amount> <destination>`\n"
            "Example: `/trip 25 Dubai Mall`",
            parse_mode="Markdown",
        )
        return

    if text == BTN_LIST:
        context.args = []
        await list_trips(update, context)
        return

    if text == BTN_REPORT:
        context.args = []
        await report(update, context)
        return

    if text == BTN_MONTH:
        # current month by default
        context.args = []
        await month_report(update, context)
        return

    if text == BTN_YEAR:
        # current year by default
        context.args = []
        await year_report(update, context)
        return

    if text == BTN_NOSCHOOL:
        context.args = []
        await noschool_cmd(update, context)
        return

    if text == BTN_REMOVESCHOOL:
        context.args = []
        await removeschool_cmd(update, context)
        return

    if text == BTN_HOLIDAY:
        await update.message.reply_text(
            "To set a holiday range, send:\n"
            "`/holiday YYYY-MM-DD YYYY-MM-DD`\n"
            "Example: `/holiday 2025-12-02 2025-12-05`",
            parse_mode="Markdown",
        )
        return

    if text == BTN_EXPORT:
        context.args = []
        await export_cmd(update, context)
        return

    if text == BTN_TOGGLE_TEST:
        await toggle_test_cmd(update, context)
        return

    if text == BTN_CLEAR_TRIPS:
        await clear_trips_cmd(update, context)
        return

    # Any other text (not a button we know) — ignore
    return


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
            continue


# --------- Main ---------

def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Please set BOT_TOKEN environment variable.")

    application = Application.builder().token(token).build()

    # Commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("menu", menu_cmd))
    application.add_handler(CommandHandler("setbase", set_base))
    application.add_handler(CommandHandler("trip", add_trip))
    application.add_handler(CommandHandler("list", list_trips))
    application.add_handler(CommandHandler("report", report))
    application.add_handler(CommandHandler("month", month_report))
    application.add_handler(CommandHandler("year", year_report))
    application.add_handler(CommandHandler("delete", delete_trip))
    application.add_handler(CommandHandler("cleartrips", clear_trips_cmd))
    application.add_handler(CommandHandler("filter", filter_by_date_cmd))
    application.add_handler(CommandHandler("destination", destination_cmd))
    application.add_handler(CommandHandler("export", export_cmd))
    application.add_handler(CommandHandler("noschool", noschool_cmd))
    application.add_handler(CommandHandler("removeschool", removeschool_cmd))
    application.add_handler(CommandHandler("holiday", holiday_cmd))
    application.add_handler(CommandHandler("test_on", test_on_cmd))
    application.add_handler(CommandHandler("test_off", test_off_cmd))

    # Menu buttons handler (must be after command handlers)
    application.add_handler(
        MessageHandler(
            filters.TEXT & (~filters.COMMAND),
            menu_button_handler,
        )
    )

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
