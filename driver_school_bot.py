# driver_school_bot.py
# DriverSchoolBot — Shared Ledger for Driver Extra Trips
#
# Features:
# - Shared ledger (all authorized users share one account)
# - Weekly report (Mon → now) + auto report Friday 10:00 (Dubai)
# - Monthly & yearly reports include:
#     * School base on daily basis (Mon–Fri, excluding no-school/holiday)
#     * Extra trips (REAL)
#     * Grand total = school base + extra trips
# - No school / holiday / remove no-school
# - Test mode: test trips ignored in real totals
# - Buttons UI (/menu)
# - Quick Trip: send "20 Dubai Mall" without /trip
# - Clear all trips
#
# Requirements:
#   python-telegram-bot[job-queue]==21.4

import os
import json
from datetime import datetime, date, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from typing import Dict, Any, List, Optional, Tuple

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
        # Silent fail is fine for our use
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


def filter_trips_by_period(
    trips: List[Dict[str, Any]],
    start_dt: Optional[datetime],
    end_dt: Optional[datetime],
) -> List[Dict[str, Any]]:
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


def no_school_days_in_period(
    no_school_dates: List[str],
    since: Optional[datetime],
    until: datetime,
) -> int:
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


def week_range_now() -> Tuple[datetime, datetime]:
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


def count_school_days_in_range(
    start_date: date,
    end_date: date,
    no_school_dates: List[str],
) -> int:
    """
    Count how many SCHOOL days (Mon–Fri) between start_date and end_date
    that are NOT in no_school_dates.
    """
    ns_set = set(no_school_dates)
    count = 0
    cur = start_date
    while cur <= end_date:
        # Monday=0 ... Sunday=6 → school days = Mon–Fri
        if cur.weekday() < 5:
            if cur.strftime("%Y-%m-%d") not in ns_set:
                count += 1
        cur += timedelta(days=1)
    return count


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
        f"🧾 Extra trips count (REAL): *{len(period_trips)}*",
        f"💰 Extra trips total (REAL): *{total_extra:.2f} AED*",
        "",
        f"🎓 Weekly school base (full): *{base:.2f} AED*",
        f"📅 No-school days this week: *{ns_days}*",
        f"🎯 Adjusted base: *{adjusted_base:.2f} AED*",
        "",
        f"✅ *Total to pay this week: {total_to_pay:.2f} AED*",
    ]

    if period_trips:
        lines.append("\n📋 REAL trip details (this week):")
        for t in period_trips:
            dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
            d_str = dt.strftime("%Y-%m-%d")
            lines.append(
                f"- {d_str}: {t['destination']} — *{t['amount']:.2f} AED* (by {t.get('user_name','?')})"
            )

    return "\n".join(lines)


# --------- Commands ---------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Always reply to /start and show the user's Telegram ID + auth status.
    Authorized users are also added as weekly-report subscribers.
    """
    user = update.effective_user
    chat_id = update.effective_chat.id
    data = load_data()

    user_id = user.id if user else None
    is_auth = user_id in ALLOWED_USERS

    # Only subscribe authorized chats
    if is_auth and chat_id not in data["subscribers"]:
        data["subscribers"].append(chat_id)
        save_data(data)

    msg = (
        "👋 *DriverSchoolBot — Shared Ledger*\n\n"
        "All trips from you and Abdulla go into *one* shared account.\n\n"
        f"👤 Your Telegram ID: `{user_id}`\n"
        f"🔐 Authorized: *{'YES ✅' if is_auth else 'NO ❌'}*\n\n"
        "Main commands:\n"
        "• `/menu` — show buttons menu\n"
        "• `/trip <amount> <destination>` – add extra trip\n"
        "• Or just type: `20 Dubai Mall` (amount + destination)\n"
        "• `/list` – list all trips (REAL + TEST)\n"
        "• `/report` – this week’s report (Mon → now, REAL only)\n"
        "• `/month [YYYY-MM]` – monthly summary (school base + real trips)\n"
        "• `/year [YYYY]` – yearly summary (school base + real trips)\n"
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
        "🔔 Auto weekly report every *Friday 10:00 (Dubai)* for authorized subscribers."
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
    """
    Monthly report:
    - School base on daily basis (Mon–Fri, excluding no-school/holiday)
    - Extra REAL trips
    - Grand total = school base + real extra trips
    - Test trips listed separately (ignored in totals)
    """
    if not await ensure_authorized(update):
        return

    data = load_data()
    trips_all = data["trips"]
    base_weekly = data["base_weekly"]
    no_school = data["no_school_dates"]

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

    # Month date range
    start_date = date(year, month, 1)
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    end_date = next_month - timedelta(days=1)

    # Trips in this month
    raw = filter_trips_by_month(trips_all, year, month)
    real_trips = [t for t in raw if is_real_trip(t)]
    test_trips = [t for t in raw if not is_real_trip(t)]

    # School base (daily basis)
    base_per_day = base_weekly / SCHOOL_DAYS_PER_WEEK
    school_days = count_school_days_in_range(start_date, end_date, no_school)
    school_base_total = base_per_day * school_days

    extra_real_total = sum(t["amount"] for t in real_trips)
    grand_total = school_base_total + extra_real_total

    if not real_trips and not test_trips and school_days == 0:
        await update.message.reply_text(
            f"No data for {year}-{month:02d}.",
            parse_mode="Markdown",
        )
        return

    lines = [
        f"📅 *Monthly Report {year}-{month:02d}*",
        "",
        "🎓 *School base (daily basis)*",
        f"• Weekly base: *{base_weekly:.2f} AED*",
        f"• Base per school day (Mon–Fri): *{base_per_day:.2f} AED*",
        f"• School days in this month (excluding holidays/no-school): *{school_days}*",
        f"• School base total this month: *{school_base_total:.2f} AED*",
        "",
        "🚗 *Extra trips (REAL)*",
        f"• Count: *{len(real_trips)}*",
        f"• Extra total (REAL): *{extra_real_total:.2f} AED*",
        "",
        f"✅ *Grand total (school base + REAL trips): {grand_total:.2f} AED*",
    ]

    # Real trips details
    if real_trips:
        lines.append("\n📋 REAL trip details:")
        for t in sorted(real_trips, key=lambda x: x["id"]):
            dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
            d_str = dt.strftime("%Y-%m-%d")
            lines.append(
                f"- ID {t['id']}: {d_str} — {t['destination']} — *{t['amount']:.2f} AED* "
                f"(by {t.get('user_name','?')})"
            )

    # Test trips info
    if test_trips:
        total_test = sum(t["amount"] for t in test_trips)
        lines.extend([
            "",
            f"🧪 TEST trips in this month (ignored in totals): *{len(test_trips)}*",
            f"🧪 TEST amount total: *{total_test:.2f} AED*",
            "",
            "📋 Test trip details:",
        ])
        for t in sorted(test_trips, key=lambda x: x["id"]):
            dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
            d_str = dt.strftime("%Y-%m-%d")
            lines.append(
                f"- ID {t['id']}: {d_str} — {t['destination']} — *{t['amount']:.2f} AED* 🧪 "
                f"(by {t.get('user_name','?')})"
            )

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def year_report(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Yearly report:
    - School base on daily basis (Mon–Fri, excluding no-school/holiday)
    - Extra REAL trips
    - Grand total = school base + real extra trips
    - Test trips listed separately
    """
    if not await ensure_authorized(update):
        return

    data = load_data()
    trips_all = data["trips"]
    base_weekly = data["base_weekly"]
    no_school = data["no_school_dates"]

    today = datetime.now(DUBAI_TZ)
    if context.args:
        try:
            year = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Use: `/year 2025`", parse_mode="Markdown")
            return
    else:
        year = today.year

    # Year date range
    start_date = date(year, 1, 1)
    end_date = date(year, 12, 31)

    raw = filter_trips_by_year(trips_all, year)
    real_trips = [t for t in raw if is_real_trip(t)]
    test_trips = [t for t in raw if not is_real_trip(t)]

    base_per_day = base_weekly / SCHOOL_DAYS_PER_WEEK
    school_days = count_school_days_in_range(start_date, end_date, no_school)
    school_base_total = base_per_day * school_days

    extra_real_total = sum(t["amount"] for t in real_trips)
    grand_total = school_base_total + extra_real_total

    if not real_trips and not test_trips and school_days == 0:
        await update.message.reply_text(
            f"No data for {year}.",
            parse_mode="Markdown",
        )
        return

    lines = [
        f"📅 *Yearly Report {year}*",
        "",
        "🎓 *School base (daily basis)*",
        f"• Weekly base: *{base_weekly:.2f} AED*",
        f"• Base per school day (Mon–Fri): *{base_per_day:.2f} AED*",
        f"• School days in this year (excluding holidays/no-school): *{school_days}*",
        f"• School base total this year: *{school_base_total:.2f} AED*",
        "",
        "🚗 *Extra trips (REAL)*",
        f"• Count: *{
