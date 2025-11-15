# driver_school_bot.py
# DriverSchoolBot — Shared Ledger for Driver Extra Trips
#
# Features:
# - Shared ledger (you + Abdulla share one account)
# - Weekly report (Mon → now) + auto report Friday 10:00 (Dubai)
# - Monthly & yearly reports include:
#     * School base on daily basis (Mon–Fri, excluding no-school/holiday)
#     * Extra trips (REAL only)
#     * Grand total = school base + REAL trips
# - No school / holiday / remove no-school
# - Test mode: test trips ignored in real totals
# - Buttons UI (/menu)
# - Quick Trip: send "20 Dubai Mall" without /trip
# - Clear all trips
# - Drivers management: add/remove drivers
# - Driver view & driver weekly report
# - Notifications to driver:
#       • No school / holidays
#       • Payment notification with amount (with optional override/tip)
#       • Sunday preview for next week (to admins only) + /confirmdriver to send
#
# Requirements:
#   python-telegram-bot[job-queue]==21.4

import os
import json
from datetime import datetime, date, time, timedelta
from pathlib import Path as _Path
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

DATA_FILE = _Path("driver_trips_data.json")
DEFAULT_BASE_WEEKLY = 725.0  # AED
DUBAI_TZ = ZoneInfo("Asia/Dubai")
SCHOOL_DAYS_PER_WEEK = 5

# Authorized admins (you & Abdulla for full control)
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
    if "drivers" not in data:
        # list of {"id": int, "name": str}
        data["drivers"] = []
    if "payments" not in data:
        # list of {"timestamp", "amount", "period_from", "period_to", "computed_total", "difference"}
        data["payments"] = []
    if "weekly_start_date" not in data:
        # Optional: minimum date for weekly calculations (string YYYY-MM-DD)
        data["weekly_start_date"] = None
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


def week_range_now(data: Dict[str, Any]) -> Tuple[datetime, datetime]:
    """
    Return (start_of_week_start_00:00, now) in Dubai time,
    clamped by weekly_start_date if set.
    """
    now = datetime.now(DUBAI_TZ)
    week_start_date = now.date() - timedelta(days=now.weekday())  # Monday of this week
    floor_str = data.get("weekly_start_date")
    if floor_str:
        try:
            floor_date = parse_date_str(floor_str)
            if floor_date > week_start_date:
                week_start_date = floor_date
        except Exception:
            pass
    since = datetime(
        week_start_date.year, week_start_date.month, week_start_date.day,
        0, 0, tzinfo=DUBAI_TZ
    )
    return since, now


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


def is_full_holiday_week(monday_date: date, data: Dict[str, Any]) -> bool:
    """
    Return True if all Mon–Fri of that week are marked as no-school.
    Used to skip driver reminders when it's full holiday.
    """
    ns_set = set(data.get("no_school_dates", []))
    for i in range(5):
        d = monday_date + timedelta(days=i)
        d_str = d.strftime("%Y-%m-%d")
        if d_str not in ns_set:
            return False
    return True


def get_driver_ids(data: Dict[str, Any]) -> List[int]:
    ids: List[int] = []
    for d in data.get("drivers", []):
        try:
            did = int(d.get("id"))
            ids.append(did)
        except Exception:
            continue
    return ids


def compute_weekly_totals(data: Dict[str, Any],
                          since: datetime,
                          until: datetime) -> Dict[str, Any]:
    """Compute totals for a weekly period (Mon→now) using REAL trips only."""
    trips_all = data["trips"]
    base = data["base_weekly"]
    no_school = data["no_school_dates"]

    period_raw = filter_trips_by_period(trips_all, since, until)
    period_trips = [t for t in period_raw if is_real_trip(t)]
    total_extra = sum(t["amount"] for t in period_trips)

    ns_days = no_school_days_in_period(no_school, since, until)
    adjusted_base = compute_adjusted_base(base, ns_days)
    total_to_pay = adjusted_base + total_extra

    return {
        "period_trips": period_trips,
        "total_extra": total_extra,
        "no_school_days": ns_days,
        "adjusted_base": adjusted_base,
        "base_weekly": base,
        "total_to_pay": total_to_pay,
        "since": since,
        "until": until,
    }


def is_real_trip(trip: Dict[str, Any]) -> bool:
    """True if trip should count in real totals (not test)."""
    return not trip.get("is_test", False)


# --------- Authorization ---------

def is_admin(user_id: Optional[int]) -> bool:
    return user_id in ALLOWED_USERS if user_id is not None else False


def is_driver_user(user_id: Optional[int], data: Dict[str, Any]) -> bool:
    if user_id is None:
        return False
    for d in data.get("drivers", []):
        try:
            if int(d.get("id")) == user_id:
                return True
        except Exception:
            continue
    return False


async def ensure_authorized(update: Update) -> bool:
    """Admin-only authorization for management commands."""
    user = update.effective_user
    if not user or not is_admin(user.id):
        if update.message:
            await update.message.reply_text("❌ You are not authorized to use this command.")
        return False
    return True


# --------- Report Builder ---------

def build_weekly_report_text(data: Dict[str, Any],
                             since: datetime,
                             until: datetime) -> str:
    totals = compute_weekly_totals(data, since, until)
    period_trips = totals["period_trips"]
    total_extra = totals["total_extra"]
    ns_days = totals["no_school_days"]
    adjusted_base = totals["adjusted_base"]
    base = totals["base_weekly"]
    total_to_pay = totals["total_to_pay"]

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
    Always reply to /start and show the user's Telegram ID + status.
    Admins are also added as weekly-report subscribers and see the menu.
    Drivers can use driver view commands.
    """
    user = update.effective_user
    chat_id = update.effective_chat.id
    data = load_data()

    user_id = user.id if user else None
    admin = is_admin(user_id)
    driver = is_driver_user(user_id, data)

    # Only subscribe admins for weekly admin report
    if admin and chat_id not in data["subscribers"]:
        data["subscribers"].append(chat_id)
        save_data(data)

    role = "Admin ✅" if admin else ("Driver 🚕" if driver else "Guest ❌")

    msg = (
        "👋 *DriverSchoolBot — Shared Ledger*\n\n"
        "All trips from you and Abdulla go into *one* shared account.\n\n"
        f"👤 Your Telegram ID: `{user_id}`\n"
        f"🔐 Role: *{role}*\n\n"
        "Admin main commands:\n"
        "• `/trip <amount> <destination>` – add extra trip\n"
        "   Or just type: `20 Dubai Mall` (quick trip)\n"
        "• `/list` – list all trips (REAL + TEST)\n"
        "• `/report` – weekly report (Mon → now)\n"
        "• `/month [YYYY-MM]` – monthly report\n"
        "• `/year [YYYY]` – yearly report\n"
        "• `/filter YYYY-MM-DD` – trips on a specific day\n"
        "• `/destination <keyword>` – filter by destination\n"
        "• `/export` – export all trips as CSV\n"
        "• `/setbase <amount>` – change weekly base amount\n"
        "• `/setweekstart YYYY-MM-DD` – from which date weekly totals start\n"
        "• `/cleartrips` – delete *all* trips\n"
        "• `/noschool [YYYY-MM-DD]` – mark no-school day\n"
        "• `/removeschool [YYYY-MM-DD]` – remove no-school day\n"
        "• `/holiday YYYY-MM-DD YYYY-MM-DD` – holiday range\n"
        "• `/test_on` / `/test_off` – toggle test mode\n"
        "• `/adddriver <id> [name]` – add driver\n"
        "• `/removedriver <id>` – remove driver\n"
        "• `/paid [amount]` – send payment notification to driver\n"
        "• `/confirmdriver [YYYY-MM-DD]` – send weekly reminder to driver (after Sunday preview)\n\n"
        "Driver commands:\n"
        "• `/driverview` – quick weekly view\n"
        "• `/driverview_report` – full weekly report view\n\n"
        "🔔 Auto weekly report every *Friday 10:00 (Dubai)* for admins.\n"
        "🔔 Every *Sunday 18:00* you get a preview of next week's driver reminder; you confirm with `/confirmdriver`."
    )

    if update.message:
        await update.message.reply_text(msg, parse_mode="Markdown")

    # Auto-open menu only for admins
    if admin and update.message:
        await menu_cmd(update, context)


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


async def set_weekstart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: /setweekstart YYYY-MM-DD — define from which date weekly totals start counting."""
    if not await ensure_authorized(update):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: `/setweekstart YYYY-MM-DD`\n"
            "Example: `/setweekstart 2025-11-24`",
            parse_mode="Markdown",
        )
        return

    try:
        d = parse_date_str(context.args[0])
    except Exception:
        await update.message.reply_text("Invalid date format. Use YYYY-MM-DD.")
        return

    d_str = d.strftime("%Y-%m-%d")
    data = load_data()
    data["weekly_start_date"] = d_str
    save_data(data)

    await update.message.reply_text(
        f"✅ Weekly totals will not go before *{d_str}*.",
        parse_mode="Markdown",
    )


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
    """Manual weekly report: always this week (Mon → now, clamped by weekly_start_date)."""
    if not await ensure_authorized(update):
        return

    data = load_data()
    since, now = week_range_now(data)

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
        f"• Count: *{len(real_trips)}*",
        f"• Extra total (REAL): *{extra_real_total:.2f} AED*",
        "",
        f"✅ *Grand total (school base + REAL trips): {grand_total:.2f} AED*",
    ]

    if real_trips:
        lines.append("\n📋 REAL trip details:")
        for t in sorted(real_trips, key=lambda x: x["id"]):
            dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
            d_str = dt.strftime("%Y-%m-%d")
            lines.append(
                f"- ID {t['id']}: {d_str} — {t['destination']} — *{t['amount']:.2f} AED* "
                f"(by {t.get('user_name','?')})"
            )

    if test_trips:
        total_test = sum(t["amount"] for t in test_trips)
        lines.extend([
            "",
            f"🧪 TEST trips in this year (ignored in totals): *{len(test_trips)}*",
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
        await update.message.reply_text(f"No *REAL* trips on {d}.", parse_mode="Markdown")
        return

    total = sum(t["amount"] for t in trips)
    lines = [f"📅 Trips on {d} (REAL only):", f"💰 Total: *{total:.2f} AED*", ""]
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
        await update.message.reply_text(f"No *REAL* trips matching '{keyword}'.", parse_mode="Markdown")
        return

    total = sum(t["amount"] for t in trips)
    lines = [f"📍 Trips matching '{keyword}' (REAL only):", f"💰 Total: *{total:.2f} AED*", ""]
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
        caption="📄 All trips (REAL + TEST) exported as CSV.",
    )


# --------- No School / Holiday Commands ---------

async def noschool_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Admin-only
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
        # Notify drivers
        driver_ids = get_driver_ids(data)
        msg = f"🏫 *No school* on *{d_str}*."
        for did in driver_ids:
            try:
                await context.bot.send_message(did, msg, parse_mode="Markdown")
            except Exception:
                continue
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
        # Notify drivers that school is back on that day
        driver_ids = get_driver_ids(data)
        msg = f"📚 *School day restored* for *{d_str}*."
        for did in driver_ids:
            try:
                await context.bot.send_message(did, msg, parse_mode="Markdown")
            except Exception:
                continue
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

    # Notify drivers
    driver_ids = get_driver_ids(data)
    msg = (
        f"🎉 *School holiday* set from *{start_d}* to *{end_d}*.\n"
        f"No school on these days."
    )
    for did in driver_ids:
        try:
            await context.bot.send_message(did, msg, parse_mode="Markdown")
        except Exception:
            continue


# --------- Test Mode Commands ---------

async def test_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_authorized(update):
        return

    data = load_data()
    data["test_mode"] = True
    save_data(data)

    await update.message.reply_text(
        "🧪 *Test Mode is ON*\n"
        "New `/trip` entries (and quick trips) will be marked as TEST and *ignored* in reports.",
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
        "New `/trip` entries (and quick trips) will be counted as REAL.",
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


# --------- Quick Trip (plain text "20 Dubai Mall") ---------

async def quick_trip_from_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Try to parse a plain text message like:
      "20 Dubai Mall"
    as a new trip.
    Returns True if a trip was created, False otherwise.
    """
    user = update.effective_user
    if not user or not is_admin(user.id):
        # Only admins can add trips
        return False

    text = (update.message.text or "").strip()
    parts = text.split()
    if len(parts) < 2:
        return False

    # First part must be amount
    try:
        amount = float(parts[0])
    except ValueError:
        return False

    destination = " ".join(parts[1:])
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
        "user_id": user.id,
        "user_name": (user.first_name or "User"),
        "is_test": is_test,
    }
    data["trips"].append(trip)
    save_data(data)

    pretty = now.strftime("%Y-%m-%d %H:%M")
    test_label = "🧪 [TEST] " if is_test else ""

    await update.message.reply_text(
        f"✅ {test_label}Trip added (quick)\n"
        f"🆔 ID: {trip_id}\n"
        f"📅 {pretty}\n"
        f"📍 {destination}\n"
        f"💰 {amount:.2f} AED"
    )

    return True


# --------- Driver Management (Add / Remove / View) ---------

async def add_driver_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: /adddriver <telegram_id> [name]"""
    if not await ensure_authorized(update):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: `/adddriver <telegram_id> [name]`\n"
            "Example: `/adddriver 123456789 Ahmed`",
            parse_mode="Markdown",
        )
        return

    try:
        driver_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Driver ID must be a number (Telegram user ID).")
        return

    name = " ".join(context.args[1:]) if len(context.args) > 1 else "Driver"

    data = load_data()
    drivers = data.get("drivers", [])

    updated = False
    for d in drivers:
        try:
            if int(d.get("id")) == driver_id:
                d["name"] = name
                updated = True
                break
        except Exception:
            continue

    if not updated:
        drivers.append({"id": driver_id, "name": name})

    data["drivers"] = drivers
    save_data(data)

    await update.message.reply_text(
        f"✅ Driver added/updated:\nID: `{driver_id}`\nName: *{name}*",
        parse_mode="Markdown",
    )


async def remove_driver_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Admin: /removedriver <telegram_id>"""
    if not await ensure_authorized(update):
        return

    if not context.args:
        await update.message.reply_text(
            "Usage: `/removedriver <telegram_id>`",
            parse_mode="Markdown",
        )
        return

    try:
        driver_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Driver ID must be a number (Telegram user ID).")
        return

    data = load_data()
    drivers = data.get("drivers", [])

    new_drivers = []
    removed = False
    for d in drivers:
        try:
            if int(d.get("id")) == driver_id:
                removed = True
                continue
        except Exception:
            pass
        new_drivers.append(d)

    data["drivers"] = new_drivers
    save_data(data)

    if removed:
        await update.message.reply_text(f"✅ Driver `{driver_id}` removed.", parse_mode="Markdown")
    else:
        await update.message.reply_text(f"ℹ️ Driver `{driver_id}` not found.", parse_mode="Markdown")


async def driver_view_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Driver View:
    - Available to admins AND drivers
    - Shows quick weekly summary (Mon→now)
    """
    user = update.effective_user
    data = load_data()
    if not user or (not is_admin(user.id) and not is_driver_user(user.id, data)):
        await update.message.reply_text("❌ You are not allowed to view driver reports.")
        return

    since, now = week_range_now(data)
    totals = compute_weekly_totals(data, since, now)

    period_str = f"{since.date()} → {now.date()}"
    lines = [
        f"🚕 *Driver View — This Week* ({period_str})",
        "",
        f"🧾 Extra trips (REAL): *{len(totals['period_trips'])}*",
        f"💰 Extra total: *{totals['total_extra']:.2f} AED*",
        f"🎓 Adjusted school base: *{totals['adjusted_base']:.2f} AED*",
        "",
        f"✅ *Total to pay (this week): {totals['total_to_pay']:.2f} AED*",
    ]

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def driver_view_report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Driver View Report:
    - Available to admins AND drivers
    - Shows full weekly report text
    """
    user = update.effective_user
    data = load_data()
    if not user or (not is_admin(user.id) and not is_driver_user(user.id, data)):
        await update.message.reply_text("❌ You are not allowed to view driver reports.")
        return

    since, now = week_range_now(data)
    text = build_weekly_report_text(data, since, now)
    await update.message.reply_text(text, parse_mode="Markdown")


# --------- Payment Notification to Driver ---------

async def paid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Admin: /paid [amount]
    - Computes this week's total (Mon→now, clamped by weekly_start_date)
    - If amount given, overrides computed total (e.g. tip, manual adjust)
    - Sends notification to all drivers with final paid amount
    - Stores record in data["payments"]
    """
    if not await ensure_authorized(update):
        return

    data = load_data()
    since, now = week_range_now(data)
    totals = compute_weekly_totals(data, since, now)
    computed_total = totals["total_to_pay"]

    if context.args:
        try:
            paid_amount = float(context.args[0])
        except ValueError:
            await update.message.reply_text("Amount must be a number, e.g. `/paid 800`", parse_mode="Markdown")
            return
    else:
        paid_amount = computed_total

    diff = paid_amount - computed_total

    # Store payment record
    payments = data.get("payments", [])
    payment = {
        "timestamp": datetime.now(DUBAI_TZ).isoformat(),
        "amount": paid_amount,
        "period_from": str(since.date()),
        "period_to": str(now.date()),
        "computed_total": computed_total,
        "difference": diff,
    }
    payments.append(payment)
    data["payments"] = payments
    save_data(data)

    # Message back to admin
    lines_admin = [
        f"💵 *Payment recorded* for week {payment['period_from']} → {payment['period_to']}",
        f"🧮 Computed total: *{computed_total:.2f} AED*",
        f"✅ Paid amount: *{paid_amount:.2f} AED*",
    ]
    if abs(diff) > 0.009:
        lines_admin.append(f"➕ Tip/adjustment: *{diff:+.2f} AED*")
    await update.message.reply_text("\n".join(lines_admin), parse_mode="Markdown")

    # Message to drivers
    driver_ids = get_driver_ids(data)
    if driver_ids:
        msg_lines = [
            "💵 *Payment Notification*",
            f"🗓 Period: *{payment['period_from']} → {payment['period_to']}*",
            f"✅ Amount paid: *{paid_amount:.2f} AED*",
        ]
        if abs(diff) > 0.009:
            if diff > 0:
                msg_lines.append(f"Includes extra tip/adjustment: *{diff:.2f} AED* 😊")
            else:
                msg_lines.append(f"(Adjusted from computed total by *{diff:.2f} AED*.)")
        msg = "\n".join(msg_lines)
        for did in driver_ids:
            try:
                await context.bot.send_message(did, msg, parse_mode="Markdown")
            except Exception:
                continue


async def confirmdriver_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Admin: /confirmdriver [YYYY-MM-DD]
    - Sends a weekly reminder to all drivers for the given week (Monday start).
    - If no date is given, assumes *next Monday* from today.
    - If the whole week is marked as holiday (Mon–Fri all no-school), nothing is sent.
    """
    if not await ensure_authorized(update):
        return

    data = load_data()
    today = today_dubai()

    # Determine Monday of the week to confirm
    if context.args:
        try:
            monday = parse_date_str(context.args[0])
        except Exception:
            await update.message.reply_text("Invalid date format. Use YYYY-MM-DD.", parse_mode="Markdown")
            return
    else:
        # Next Monday from today
        days_ahead = (7 - today.weekday()) % 7
        if days_ahead == 0:
            days_ahead = 7
        monday = today + timedelta(days=days_ahead)

    friday = monday + timedelta(days=4)

    # Check if full holiday
    if is_full_holiday_week(monday, data):
        await update.message.reply_text(
            f"ℹ️ Week {monday} → {friday} is full holiday (all Mon–Fri are no-school).\n"
            "No reminder sent to driver.",
            parse_mode="Markdown",
        )
        return

    # Build message for drivers
    msg = (
        "🚐 *School Pickup Reminder*\n"
        f"🗓 Week: *{monday} → {friday}*\n\n"
        "Please be ready to pick up the kids on school days this week.\n"
        "If there are any changes, we will inform you."
    )

    driver_ids = get_driver_ids(data)
    sent_count = 0
    for did in driver_ids:
        try:
            await context.bot.send_message(did, msg, parse_mode="Markdown")
            sent_count += 1
        except Exception:
            continue

    await update.message.reply_text(
        f"✅ Driver reminder sent for week {monday} → {friday} to {sent_count} driver(s).",
        parse_mode="Markdown",
    )


# --------- Sunday Preview Job ---------

async def sunday_preview_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Runs every Sunday. Sends admins a preview of the next week's driver reminder.
    They must manually confirm with /confirmdriver before it goes to the driver.
    If the whole week is holiday (Mon–Fri all no-school), nothing is sent.
    """
    data = load_data()
    today = today_dubai()

    # Next Monday
    days_ahead = (7 - today.weekday()) % 7
    if days_ahead == 0:
        days_ahead = 7
    monday = today + timedelta(days=days_ahead)
    friday = monday + timedelta(days=4)

    # Skip if full holiday
    if is_full_holiday_week(monday, data):
        return

    driver_msg = (
        "🚐 *School Pickup Reminder*\n"
        f"🗓 Week: *{monday} → {friday}*\n\n"
        "Please be ready to pick up the kids on school days this week.\n"
        "If there are any changes, we will inform you."
    )

    preview = (
        "🕒 *Driver Reminder Preview (Next Week)*\n"
        f"Week: *{monday} → {friday}*\n\n"
        "This is the message that can be sent to the driver:\n\n"
        f"{driver_msg}\n\n"
        "To send it now, run:\n"
        f"`/confirmdriver {monday}`"
    )

    for admin_id in ALLOWED_USERS:
        try:
            await context.bot.send_message(admin_id, preview, parse_mode="Markdown")
        except Exception:
            continue


# --------- Buttons UI (/menu) ---------

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # Only admins see the full menu
    user = update.effective_user
    if not user or not is_admin(user.id):
        await update.message.reply_text("❌ Menu is only for admins.")
        return

    keyboard = [
        [KeyboardButton(BTN_ADD_TRIP), KeyboardButton(BTN_LIST)],
        [KeyboardButton(BTN_REPORT), KeyboardButton(BTN_MONTH), KeyboardButton(BTN_YEAR)],
        [KeyboardButton(BTN_NOSCHOOL), KeyboardButton(BTN_REMOVESCHOOL)],
        [KeyboardButton(BTN_HOLIDAY), KeyboardButton(BTN_EXPORT)],
        [KeyboardButton(BTN_TOGGLE_TEST), KeyboardButton(BTN_CLEAR_TRIPS)],
    ]

    reply_markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    await update.message.reply_text(
        "📱 Menu — choose an action or just type `20 Dubai Mall` to add a trip quickly:",
        reply_markup=reply_markup,
        parse_mode="Markdown",
    )


async def menu_button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle presses on the ReplyKeyboard buttons and also quick-trip text.
    Any non-command text comes here.
    """
    text = (update.message.text or "").strip()

    # First, check if it's one of our buttons
    if text == BTN_ADD_TRIP:
        if not await ensure_authorized(update):
            return
        await update.message.reply_text(
            "To add a trip, send:\n`/trip <amount> <destination>`\n"
            "Example: `/trip 25 Dubai Mall`\n\n"
            "Or just type: `25 Dubai Mall`.",
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
        context.args = []
        await month_report(update, context)
        return

    if text == BTN_YEAR:
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
        if not await ensure_authorized(update):
            return
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

    # Not a button → try quick-trip format: "<amount> <destination>" (admin only)
    if await quick_trip_from_text(update, context):
        return

    # Not a known button, not a quick trip → ignore
    return


# --------- Weekly Job (Friday 10:00) ---------

async def weekly_report_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    since, now = week_range_now(data)
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
    application.add_handler(CommandHandler("setweekstart", set_weekstart_cmd))
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

    # Driver management & view
    application.add_handler(CommandHandler("adddriver", add_driver_cmd))
    application.add_handler(CommandHandler("removedriver", remove_driver_cmd))
    application.add_handler(CommandHandler("driverview", driver_view_cmd))
    application.add_handler(CommandHandler("driverview_report", driver_view_report_cmd))

    # Payment & driver confirmation
    application.add_handler(CommandHandler("paid", paid_cmd))
    application.add_handler(CommandHandler("confirmdriver", confirmdriver_cmd))

    # Menu buttons + quick trips handler (all non-command text)
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

    # Friday weekly report
    jq.run_daily(
        weekly_report_job,
        time=time(hour=10, minute=0, tzinfo=DUBAI_TZ),
        days=(4,),  # Friday
        name="weekly_report",
    )

    # Sunday preview for next week driver reminder (requires admin confirmation)
    jq.run_daily(
        sunday_preview_job,
        time=time(hour=18, minute=0, tzinfo=DUBAI_TZ),
        days=(6,),  # Sunday
        name="sunday_preview",
    )

    application.run_polling()


if __name__ == "__main__":
    main()
