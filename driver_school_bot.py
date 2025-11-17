# driver_school_bot.py
# DriverSchoolBot — Multi-driver Shared Ledger with Driver & Admin Menus
#
# Features:
# - Admins: you (Faisal) + Abdulla
# - Multiple drivers, each with their own trips
# - Primary driver for simple /trip and quick text ("20 Dubai Mall")
# - Weekly / monthly / yearly reports
#   • Calculations start from /setweekstart YYYY-MM-DD
#   • Anything before that date is ignored in totals (but kept in history)
#   • Month/year reports are capped at *today* (no future school days)
# - No-school / holidays with driver notifications
# - Notification to driver when a REAL trip is added
# - Notification to other admins when someone adds a REAL trip
#   • Message includes driver *name* and admin *name*
# - /paid [amount] → payment notification to drivers
# - Driver view commands: /driverview, /driverview_report
# - Driver menu buttons only (no typing needed for drivers):
#   • 📦 My Week
#   • 🧾 My Weekly Report
#
# Requirements:
#   python-telegram-bot==21.4
#
# Environment:
#   BOT_TOKEN must be set (Render / .env)

import os
import json
from datetime import datetime, date, timedelta
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
    MessageHandler,
    filters,
)

# ---------- Constants ----------
DATA_FILE = _Path("driver_trips_data.json")
DUBAI_TZ = ZoneInfo("Asia/Dubai")

DEFAULT_BASE_WEEKLY = 725.0  # AED
SCHOOL_DAYS_PER_WEEK = 5

# Admins (you + Abdulla)
ALLOWED_USERS = [
    7698278415,  # Faisal
    5034920293,  # Abdulla
]

# Admin menu buttons
BTN_ADD_TRIP = "➕ Add Trip"
BTN_LIST = "📋 List Trips"
BTN_REPORT = "📊 Weekly Report"
BTN_MONTH = "📅 Month"
BTN_YEAR = "📆 Year"
BTN_NOSCHOOL = "🏫 No School Today"
BTN_EXPORT = "📄 Export CSV"
BTN_CLEAR_TRIPS = "🧹 Clear All Trips"
BTN_TEST_TOGGLE = "🧪 Toggle Test Mode"
BTN_DRIVERS = "🚕 Drivers"

# Admin sub-menu buttons (Drivers)
BTN_DRV_LIST = "🚕 List Drivers"
BTN_DRV_ADD = "➕ Add Driver"
BTN_DRV_REMOVE = "🗑 Remove Driver"
BTN_DRV_PRIMARY = "⭐ Set Primary Driver"
BTN_BACK_MAIN = "⬅ Back"

# Admin sub-menu buttons (No School)
BTN_NOSCHOOL_TODAY = "🏫 No School Today"
BTN_NOSCHOOL_TOMORROW = "🏫 No School Tomorrow"
BTN_NOSCHOOL_PICKDATE = "📅 No School (Pick Date)"

# Driver menu buttons
DRV_BTN_SUMMARY = "📦 My Week"
DRV_BTN_REPORT = "🧾 My Weekly Report"


# ---------- Storage Helpers ----------

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
        data["trips"] = []  # list of trip dicts
    if "next_trip_id" not in data:
        data["next_trip_id"] = 1
    if "no_school_dates" not in data:
        data["no_school_dates"] = []  # list of YYYY-MM-DD
    if "test_mode" not in data:
        data["test_mode"] = False
    if "weekly_start_date" not in data:
        data["weekly_start_date"] = None  # "YYYY-MM-DD" or None
    if "drivers" not in data:
        # each: {"id": int, "name": str, "primary": bool}
        data["drivers"] = []
    if "payments" not in data:
        data["payments"] = []  # history of /paid
    return data


# ---------- Helper Functions ----------

def parse_iso_datetime(dt_str: str) -> datetime:
    return datetime.fromisoformat(dt_str)


def parse_date_str(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def today_dubai() -> date:
    return datetime.now(DUBAI_TZ).date()


def clamp_start_by_weekstart(data: Dict[str, Any], start_date: date) -> Optional[date]:
    """
    If weekly_start_date is set, ensure we don't count before it.
    Returns adjusted start_date.
    """
    floor_str = data.get("weekly_start_date")
    if floor_str:
        try:
            floor_date = parse_date_str(floor_str)
        except Exception:
            return start_date
        if floor_date > start_date:
            return floor_date
    return start_date


def is_admin(user_id: Optional[int]) -> bool:
    return user_id in ALLOWED_USERS if user_id is not None else False


async def ensure_admin(update: Update) -> bool:
    user = update.effective_user
    if not user or not is_admin(user.id):
        if update.message:
            await update.message.reply_text("❌ You are not authorized to use this command.")
        return False
    return True


def get_driver_by_id(data: Dict[str, Any], driver_id: int) -> Optional[Dict[str, Any]]:
    for d in data.get("drivers", []):
        try:
            if int(d.get("id")) == driver_id:
                return d
        except Exception:
            continue
    return None


def get_primary_driver_id(data: Dict[str, Any]) -> Optional[int]:
    drivers = data.get("drivers", [])
    if not drivers:
        return None
    for d in drivers:
        if d.get("primary"):
            try:
                return int(d["id"])
            except Exception:
                continue
    try:
        return int(drivers[0]["id"])
    except Exception:
        return None


def is_driver_user(data: Dict[str, Any], user_id: Optional[int]) -> bool:
    if user_id is None:
        return False
    return get_driver_by_id(data, user_id) is not None


def is_real_trip(trip: Dict[str, Any]) -> bool:
    return not trip.get("is_test", False)


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


def filter_trips_by_year(trips: List[Dict[str, Any]], year: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for t in trips:
        dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
        if dt.year == year:
            out.append(t)
    return out


def filter_trips_by_month(trips: List[Dict[str, Any]], year: int, month: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for t in trips:
        dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
        if dt.year == year and dt.month == month:
            out.append(t)
    return out


def count_school_days_between(
    start_date: date,
    end_date: date,
    no_school_dates: List[str],
) -> int:
    ns = set(no_school_dates)
    d = start_date
    count = 0
    while d <= end_date:
        if d.weekday() < 5:  # Mon–Fri
            if d.strftime("%Y-%m-%d") not in ns:
                count += 1
        d += timedelta(days=1)
    return count


def weekly_range_now(data: Dict[str, Any]) -> Tuple[Optional[datetime], datetime]:
    """
    Get (start_of_week, now) for weekly report,
    respecting weekly_start_date if set.
    Returns (None, now) if start is in future.
    """
    now = datetime.now(DUBAI_TZ)
    today = now.date()

    floor_str = data.get("weekly_start_date")
    floor_date = parse_date_str(floor_str) if floor_str else None

    if floor_date and floor_date > today:
        return None, now

    week_start = today - timedelta(days=today.weekday())  # Monday
    if floor_date and floor_date > week_start:
        week_start = floor_date

    start_dt = datetime(
        week_start.year, week_start.month, week_start.day, 0, 0, tzinfo=DUBAI_TZ
    )
    return start_dt, now


def compute_weekly_totals(
    data: Dict[str, Any],
    start_dt: datetime,
    end_dt: datetime,
    driver_id: Optional[int] = None,
) -> Dict[str, Any]:
    """
    Compute weekly totals for all drivers or a specific driver.
    School base is shared (one driver now; in future, can be per driver).
    """
    all_trips = data["trips"]
    floor_str = data.get("weekly_start_date")
    floor_date = parse_date_str(floor_str) if floor_str else None

    trips: List[Dict[str, Any]] = []
    for t in all_trips:
        dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
        d = dt.date()
        if floor_date and d < floor_date:
            continue
        if dt < start_dt or dt > end_dt:
            continue
        if driver_id is not None:
            if t.get("driver_id") != driver_id:
                continue
        trips.append(t)

    real_trips = [t for t in trips if is_real_trip(t)]
    total_extra = sum(t["amount"] for t in real_trips)

    base_weekly = data["base_weekly"]
    no_school = data["no_school_dates"]

    start_date = start_dt.date()
    end_date = end_dt.date()
    school_days = count_school_days_between(start_date, end_date, no_school)

    # Total possible Mon–Fri school days in this period
    total_school_weekdays = 0
    d_iter = start_date
    while d_iter <= end_date:
        if d_iter.weekday() < 5:  # Mon–Fri
            total_school_weekdays += 1
        d_iter += timedelta(days=1)

    no_school_days = total_school_weekdays - school_days
    if no_school_days < 0:
        no_school_days = 0

    base_per_day = base_weekly / SCHOOL_DAYS_PER_WEEK
    school_base_total = base_per_day * school_days
    grand_total = school_base_total + total_extra

    return {
        "real_trips": real_trips,
        "total_extra": total_extra,
        "base_weekly": base_weekly,
        "base_per_day": base_per_day,
        "school_days": school_days,
        "no_school_days": no_school_days,
        "school_base_total": school_base_total,
        "grand_total": grand_total,
        "start_date": start_date,
        "end_date": end_date,
    }


# ---------- Notifications ----------

async def notify_drivers(
    context: ContextTypes.DEFAULT_TYPE,
    data: Dict[str, Any],
    text: str,
) -> None:
    for d in data.get("drivers", []):
        try:
            driver_id = int(d["id"])
        except Exception:
            continue
        try:
            await context.bot.send_message(chat_id=driver_id, text=text)
        except Exception:
            continue


async def notify_driver_for_trip(
    context: ContextTypes.DEFAULT_TYPE,
    data: Dict[str, Any],
    trip: Dict[str, Any],
) -> None:
    driver_id = trip.get("driver_id")
    if not driver_id:
        return
    if trip.get("is_test"):
        return
    d = get_driver_by_id(data, driver_id)
    driver_name = d["name"] if d and d.get("name") else "Driver"

    dt = parse_iso_datetime(trip["date"]).astimezone(DUBAI_TZ)
    d_str = dt.strftime("%Y-%m-%d %H:%M")

    text = (
        f"🚗 New trip added for you, {driver_name}:\n"
        f"📅 {d_str}\n"
        f"📍 {trip['destination']}\n"
        f"💰 {trip['amount']:.2f} AED"
    )

    try:
        await context.bot.send_message(chat_id=driver_id, text=text)
    except Exception:
        pass


async def notify_admins_for_trip(
    context: ContextTypes.DEFAULT_TYPE,
    data: Dict[str, Any],
    trip: Dict[str, Any],
) -> None:
    """
    Notify other admins when a REAL trip is added.
    Example: when Abdulla adds a trip, Faisal gets a message.
    """
    if trip.get("is_test"):
        return

    added_by_id = trip.get("user_id")
    added_by_name = trip.get("added_by_name") or "Unknown"
    driver_id = trip.get("driver_id")
    driver_name = trip.get("driver_name") or (f"{driver_id}" if driver_id else "No driver")

    dt = parse_iso_datetime(trip["date"]).astimezone(DUBAI_TZ)
    d_str = dt.strftime("%Y-%m-%d %H:%M")

    text = (
        "🔔 New trip added by someone in the family:\n"
        f"🆔 ID: {trip['id']}\n"
        f"📅 {d_str}\n"
        f"📍 {trip['destination']}\n"
        f"💰 {trip['amount']:.2f} AED\n"
        f"👤 Added by: {added_by_name} (ID: {added_by_id})\n"
        f"🚗 For driver: {driver_name} (ID: {driver_id})"
    )

    for admin_id in set(ALLOWED_USERS):
        if admin_id == added_by_id:
            continue
        try:
            await context.bot.send_message(chat_id=admin_id, text=text)
        except Exception:
            continue




async def holiday_reminder_start_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Job: remind drivers one day before the holiday starts.
    """
    job = context.job
    data_dict = getattr(job, "data", {}) or {}
    start_str = data_dict.get("start")
    end_str = data_dict.get("end")

    data = load_data()

    if start_str and end_str:
        text = (
            f"⏰ Reminder: Holiday starts tomorrow ({start_str}).\n"
            f"🏫 No school from {start_str} to {end_str}."
        )
    elif start_str:
        text = f"⏰ Reminder: Holiday starts tomorrow ({start_str})."
    else:
        text = "⏰ Reminder: Holiday starts tomorrow."

    await notify_drivers(context, data, text)


async def holiday_reminder_end_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Job: remind drivers one day before the holiday ends (to resume work).
    """
    job = context.job
    data_dict = getattr(job, "data", {}) or {}
    start_str = data_dict.get("start")
    end_str = data_dict.get("end")

    data = load_data()

    if end_str:
        text = (
            f"🔔 Reminder: Holiday will end tomorrow ({end_str}).\n"
            "🚗 Please get ready to resume school drop-off after the holiday."
        )
    else:
        text = "🔔 Reminder: Holiday will end tomorrow. Please get ready to resume school drop-off."

    await notify_drivers(context, data, text)


# ---------- /start ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    data = load_data()

    uid = user.id if user else None
    admin = is_admin(uid)
    driver_flag = is_driver_user(data, uid)

    role = "Admin ✅" if admin else ("Driver 🚕" if driver_flag else "Guest ❌")

    msg = (
        "👋 *DriverSchoolBot — Multi-driver Shared Ledger*\n\n"
        f"👤 Your Telegram ID: `{uid}`\n"
        f"🔐 Role: *{role}*\n\n"
    )

    if admin:
        msg += (
            "Admin commands:\n"
            "• `/trip <amount> <destination>` – add trip for primary driver\n"
            "• `/tripd <driver_id> <amount> <destination>` – add trip for specific driver\n"
            "• Just send: `20 Dubai Mall` – quick trip for primary driver\n"
            "• `/list` – list all trips\n"
            "• `/report` – weekly report (all drivers)\n"
            "• `/month [YYYY-MM]` – monthly report\n"
            "• `/year [YYYY]` – yearly report\n"
            "• `/setbase <amount>` – set weekly base (e.g. 725)\n"
            "• `/setweekstart YYYY-MM-DD` – calculations start from this date\n"
            "• `/noschool [today|tomorrow|YYYY-MM-DD]` – no school + notify drivers\n"
            "• `/removeschool YYYY-MM-DD` – remove no-school\n"
            "• `/holiday YYYY-MM-DD YYYY-MM-DD` – holiday range + notify drivers\n"
            "• `/adddriver <id> <name>` – add driver\n"
            "• `/removedriver <id>` – remove driver\n"
            "• `/drivers` – list drivers\n"
            "• `/setprimarydriver <id>` – primary driver\n"
            "• `/paid [amount]` – payment notification\n"
            "• `/driverview_report <driver_id>` – weekly report for that driver\n"
            "• `/test_on`, `/test_off` – toggle test mode\n"
            "• `/export` – export CSV\n"
            "\nUse /menu to show admin buttons.\n"
        )
    if driver_flag:
        msg += (
            "\nDriver options (no typing needed):\n"
            "Use the buttons:\n"
            "• 📦 My Week – summary for this week\n"
            "• 🧾 My Weekly Report – detailed trips + school base\n"
        )

    if update.message:
        await update.message.reply_text(msg, parse_mode="Markdown")

    if update.message:
        await menu_cmd(update, context)


# ---------- Admin: driver management ----------

async def adddriver_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    if len(context.args) < 1:
        await update.message.reply_text("Usage: `/adddriver <telegram_id> [name]`", parse_mode="Markdown")
        return
    try:
        did = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Driver Telegram ID must be a number.")
        return

    name = " ".join(context.args[1:]) if len(context.args) > 1 else ""

    data = load_data()
    for d in data["drivers"]:
        if int(d["id"]) == did:
            d["name"] = name
            save_data(data)
            await update.message.reply_text(f"✅ Updated driver {did} name to '{name}'.")
            return
    data["drivers"].append({"id": did, "name": name, "primary": False})
    save_data(data)
    await update.message.reply_text(f"✅ Added driver {did} ({name}).")


async def removedriver_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: `/removedriver <telegram_id>`", parse_mode="Markdown")
        return
    try:
        did = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Driver Telegram ID must be a number.")
        return

    data = load_data()
    before = len(data["drivers"])
    data["drivers"] = [d for d in data["drivers"] if int(d["id"]) != did]
    after = len(data["drivers"])
    save_data(data)

    if before == after:
        await update.message.reply_text(f"No driver found with ID {did}.")
    else:
        await update.message.reply_text(f"✅ Removed driver {did}.")


async def drivers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    data = load_data()
    drivers = data.get("drivers", [])
    if not drivers:
        await update.message.reply_text("No drivers configured yet.")
        return
    lines = ["🚕 *Drivers:*"]
    for d in drivers:
        mark = "⭐" if d.get("primary") else ""
        lines.append(f"- {d['id']} — {d.get('name','(no name)')} {mark}")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def setprimarydriver_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: `/setprimarydriver <telegram_id>`", parse_mode="Markdown")
        return
    try:
        did = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Driver Telegram ID must be a number.")
        return
    data = load_data()
    found = False
    for d in data["drivers"]:
        d["primary"] = (int(d["id"]) == did)
        if d["primary"]:
            found = True
    save_data(data)
    if not found:
        await update.message.reply_text(f"No driver found with ID {did}.")
    else:
        await update.message.reply_text(f"✅ Driver {did} set as primary.")


# ---------- Admin: base / week start ----------

async def setbase_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
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
    await update.message.reply_text(f"✅ Weekly base set to {amount:.2f} AED")


async def setweekstart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    if not context.args:
        await update.message.reply_text(
            "Usage: `/setweekstart YYYY-MM-DD`\n"
            "This date is when calculations start (driver start date).",
            parse_mode="Markdown",
        )
        return
    try:
        d = parse_date_str(context.args[0])
    except Exception:
        await update.message.reply_text("Invalid date format. Use YYYY-MM-DD.")
        return
    data = load_data()
    data["weekly_start_date"] = d.strftime("%Y-%m-%d")
    save_data(data)
    await update.message.reply_text(
        f"✅ Calculations (weekly/monthly/yearly) will ignore anything before *{d}*.",
        parse_mode="Markdown",
    )


# ---------- Admin: add trips ----------

async def add_trip_internal(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    driver_id: Optional[int],
    amount: float,
    destination: str,
) -> None:
    data = load_data()
    now = datetime.now(DUBAI_TZ)

    if driver_id is None:
        await update.message.reply_text(
            "⚠ No driver selected and no primary driver set.\n"
            "Use `/adddriver <id> <name>` then `/setprimarydriver <id>` or `/tripd <id> ...`",
        )
        return

    driver_info = get_driver_by_id(data, driver_id)
    if not driver_info:
        await update.message.reply_text(
            f"⚠ Driver {driver_id} not found. Add with `/adddriver {driver_id} <name>`.",
            parse_mode="Markdown",
        )
        return

    trip_id = data["next_trip_id"]
    data["next_trip_id"] = trip_id + 1

    is_test = data.get("test_mode", False)
    user = update.effective_user
    added_by_name = user.first_name if user and user.first_name else "User"

    driver_name = driver_info.get("name") or str(driver_id)

    trip = {
        "id": trip_id,
        "driver_id": driver_id,
        "driver_name": driver_name,
        "date": now.isoformat(),
        "amount": amount,
        "destination": destination,
        "user_id": user.id if user else None,
        "user_name": added_by_name,
        "added_by_name": added_by_name,
        "is_test": is_test,
    }
    data["trips"].append(trip)
    save_data(data)

    tag = "🧪 [TEST] " if is_test else ""
    when = now.strftime("%Y-%m-%d %H:%M")

    await update.message.reply_text(
        f"✅ {tag}Trip added for driver {driver_name} ({driver_id})\n"
        f"🆔 ID: {trip_id}\n"
        f"📅 {when}\n"
        f"📍 {destination}\n"
        f"💰 {amount:.2f} AED"
    )

    # Notify driver (if REAL) and other admins
    await notify_driver_for_trip(context, data, trip)
    await notify_admins_for_trip(context, data, trip)


async def trip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: `/trip <amount> <destination>`\n"
            "Example: `/trip 25 Dubai Mall`",
            parse_mode="Markdown",
        )
        return
    try:
        amount = float(context.args[0])
    except ValueError:
        await update.message.reply_text("Amount must be a number.")
        return
    destination = " ".join(context.args[1:])
    data = load_data()
    driver_id = get_primary_driver_id(data)
    await add_trip_internal(update, context, driver_id, amount, destination)


async def tripd_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    if len(context.args) < 3:
        await update.message.reply_text(
            "Usage: `/tripd <driver_id> <amount> <destination>`\n"
            "Example: `/tripd 123456789 20 Dubai Mall`",
            parse_mode="Markdown",
        )
        return
    try:
        driver_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Driver ID must be a number.")
        return
    try:
        amount = float(context.args[1])
    except ValueError:
        await update.message.reply_text("Amount must be a number.")
        return
    destination = " ".join(context.args[2:])
    await add_trip_internal(update, context, driver_id, amount, destination)


# ---------- Admin: list / delete / clear ----------

async def list_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    data = load_data()
    trips = data["trips"]
    if not trips:
        await update.message.reply_text("No trips recorded yet.")
        return
    drivers = {int(d["id"]): d.get("name","") for d in data.get("drivers", [])}

    real_total = 0.0
    test_total = 0.0
    lines = ["📋 *All trips (all drivers):*"]
    for t in sorted(trips, key=lambda x: x["id"]):
        dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
        d_str = dt.strftime("%Y-%m-%d")
        is_test = t.get("is_test", False)
        if is_test:
            test_total += t["amount"]
        else:
            real_total += t["amount"]
        tag = "🧪" if is_test else ""
        did = t.get("driver_id")
        dname = drivers.get(did, "") if did is not None else ""
        driver_label = f"(Driver {did} {dname})" if did else "(No driver)"
        lines.append(
            f"- ID {t['id']}: {d_str} — {t['destination']} — *{t['amount']:.2f} AED* {tag} {driver_label}"
        )
    lines.append(f"\n💰 REAL total: *{real_total:.2f} AED*")
    lines.append(f"🧪 TEST total (ignored in reports): *{test_total:.2f} AED*")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def delete_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: `/delete <trip_id>`", parse_mode="Markdown")
        return
    try:
        tid = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Trip ID must be a number.")
        return
    data = load_data()
    trips = data["trips"]
    before = len(trips)
    trips = [t for t in trips if t["id"] != tid]
    after = len(trips)
    data["trips"] = trips
    save_data(data)
    if before == after:
        await update.message.reply_text(f"No trip found with ID {tid}.")
    else:
        await update.message.reply_text(f"✅ Trip {tid} deleted.")


async def cleartrips_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    data = load_data()
    count = len(data["trips"])
    data["trips"] = []
    save_data(data)
    await update.message.reply_text(f"🧹 Cleared all trips. Removed {count} records.")


# ---------- Admin: weekly / month / year ----------

def build_weekly_report_text(data: Dict[str, Any], start_dt: datetime, end_dt: datetime) -> str:
    totals = compute_weekly_totals(data, start_dt, end_dt)
    lines = [
        f"📊 *Weekly Driver Report* ({totals['start_date']} → {totals['end_date']})",
        "",
        "🎓 School base (daily):",
        f"• Weekly base: *{totals['base_weekly']:.2f} AED*",
        f"• Base per school day (Mon–Fri): *{totals['base_per_day']:.2f} AED*",
        f"• School days in this period (excluding no-school/holidays): *{totals['school_days']}*",
        f"• No-school / holiday days in this period: *{totals.get('no_school_days', 0)}*",
        f"• School base total: *{totals['school_base_total']:.2f} AED*",
        "",
        "🚗 Extra trips (REAL):",
        f"• Count: *{len(totals['real_trips'])}*",
        f"• Extra total: *{totals['total_extra']:.2f} AED*",
        "",
        f"✅ *Grand total: {totals['grand_total']:.2f} AED*",
    ]
    if totals["real_trips"]:
        lines.append("\n📋 Trip details (REAL):")
        for t in sorted(totals["real_trips"], key=lambda x: x["id"]):
            dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
            d_str = dt.strftime("%Y-%m-%d")
            lines.append(
                f"- ID {t['id']}: {d_str} — {t['destination']} — *{t['amount']:.2f} AED* "
                f"(driver {t.get('driver_id','?')})"
            )
    return "\n".join(lines)


async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    data = load_data()
    start_dt, end_dt = weekly_range_now(data)
    if start_dt is None:
        start_str = data.get("weekly_start_date")
        await update.message.reply_text(
            f"ℹ️ Driver starts from *{start_str}*.\n"
            "Weekly report will be available after that date.",
            parse_mode="Markdown",
        )
        return
    text = build_weekly_report_text(data, start_dt, end_dt)
    await update.message.reply_text(text, parse_mode="Markdown")


async def month_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    data = load_data()
    trips_all = data["trips"]
    base_weekly = data["base_weekly"]
    no_school = data["no_school_dates"]

    today = today_dubai()
    now = datetime.now(DUBAI_TZ)
    if context.args:
        try:
            ym = context.args[0]
            y_s, m_s = ym.split("-")
            year = int(y_s)
            month = int(m_s)
        except Exception:
            await update.message.reply_text("Use: `/month YYYY-MM`", parse_mode="Markdown")
            return
    else:
        year, month = now.year, now.month

    start_date = date(year, month, 1)
    if month == 12:
        next_month = date(year + 1, 1, 1)
    else:
        next_month = date(year, month + 1, 1)
    end_date = next_month - timedelta(days=1)

    # Cap at today (no future school days)
    if end_date > today:
        end_date = today

    # Adjust by weekly_start_date
    start_date = clamp_start_by_weekstart(data, start_date)

    if end_date < start_date:
        await update.message.reply_text(
            f"No data for {year}-{month:02d} (before driver start or in the future).",
            parse_mode="Markdown",
        )
        return

    month_trips = filter_trips_by_month(trips_all, year, month)
    filtered = []
    for t in month_trips:
        dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
        d = dt.date()
        if d < start_date or d > end_date:
            continue
        filtered.append(t)
    real_trips = [t for t in filtered if is_real_trip(t)]
    test_trips = [t for t in filtered if not is_real_trip(t)]

    base_per_day = base_weekly / SCHOOL_DAYS_PER_WEEK
    school_days = count_school_days_between(start_date, end_date, no_school)
    school_base_total = base_per_day * school_days
    extra_real = sum(t["amount"] for t in real_trips)
    grand_total = school_base_total + extra_real

    if not real_trips and not test_trips and school_days == 0:
        await update.message.reply_text(
            f"No data in {year}-{month:02d}.",
            parse_mode="Markdown",
        )
        return

    lines = [
        f"📅 *Monthly Report {year}-{month:02d}* (from {start_date} to {end_date})",
        "",
        "🎓 School base (daily):",
        f"• Weekly base: *{base_weekly:.2f} AED*",
        f"• Base per school day: *{base_per_day:.2f} AED*",
        f"• School days (excluding no-school): *{school_days}*",
        f"• School base total: *{school_base_total:.2f} AED*",
        "",
        "🚗 Extra trips (REAL):",
        f"• Count: *{len(real_trips)}*",
        f"• Extra total: *{extra_real:.2f} AED*",
        "",
        f"✅ *Grand total: {grand_total:.2f} AED*",
    ]

    if real_trips:
        lines.append("\n📋 REAL trip details:")
        for t in sorted(real_trips, key=lambda x: x["id"]):
            dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
            d_str = dt.strftime("%Y-%m-%d")
            lines.append(
                f"- ID {t['id']}: {d_str} — {t['destination']} — *{t['amount']:.2f} AED* "
                f"(driver {t.get('driver_id','?')})"
            )

    if test_trips:
        total_test = sum(t["amount"] for t in test_trips)
        lines.extend([
            "",
            f"🧪 TEST trips (ignored in totals): *{len(test_trips)}* — *{total_test:.2f} AED*",
        ])

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def year_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    data = load_data()
    trips_all = data["trips"]
    base_weekly = data["base_weekly"]
    no_school = data["no_school_dates"]

    today = today_dubai()
    now = datetime.now(DUBAI_TZ)
    if context.args:
        try:
            year = int(context.args[0])
        except Exception:
            await update.message.reply_text("Use: `/year 2025`", parse_mode="Markdown")
            return
    else:
        year = now.year

    start_date = date(year, 1, 1)
    end_date = date(year, 12, 31)

    # Cap at today (no future school days)
    if end_date > today:
        end_date = today

    # Adjust by weekly_start_date
    start_date = clamp_start_by_weekstart(data, start_date)

    if end_date < start_date:
        await update.message.reply_text(
            f"No data for {year} (before driver start or in the future).",
            parse_mode="Markdown",
        )
        return

    year_trips = filter_trips_by_year(trips_all, year)
    filtered = []
    for t in year_trips:
        dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
        d = dt.date()
        if d < start_date or d > end_date:
            continue
        filtered.append(t)
    real_trips = [t for t in filtered if is_real_trip(t)]
    test_trips = [t for t in filtered if not is_real_trip(t)]

    base_per_day = base_weekly / SCHOOL_DAYS_PER_WEEK
    school_days = count_school_days_between(start_date, end_date, no_school)
    school_base_total = base_per_day * school_days
    extra_real = sum(t["amount"] for t in real_trips)
    grand_total = school_base_total + extra_real

    if not real_trips and not test_trips and school_days == 0:
        await update.message.reply_text(
            f"No data in {year}.",
            parse_mode="Markdown",
        )
        return

    lines = [
        f"📅 *Yearly Report {year}* (from {start_date} to {end_date})",
        "",
        "🎓 School base (daily):",
        f"• Weekly base: *{base_weekly:.2f} AED*",
        f"• Base per school day: *{base_per_day:.2f} AED*",
        f"• School days (excluding no-school): *{school_days}*",
        f"• School base total: *{school_base_total:.2f} AED*",
        "",
        "🚗 Extra trips (REAL):",
        f"• Count: *{len(real_trips)}*",
        f"• Extra total: *{extra_real:.2f} AED*",
        "",
        f"✅ *Grand total: {grand_total:.2f} AED*",
    ]

    if real_trips:
        lines.append("\n📋 REAL trip details:")
        for t in sorted(real_trips, key=lambda x: x["id"]):
            dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
            d_str = dt.strftime("%Y-%m-%d")
            lines.append(
                f"- ID {t['id']}: {d_str} — {t['destination']} — *{t['amount']:.2f} AED* "
                f"(driver {t.get('driver_id','?')})"
            )

    if test_trips:
        total_test = sum(t["amount"] for t in test_trips)
        lines.extend([
            "",
            f"🧪 TEST trips (ignored in totals): *{len(test_trips)}* — *{total_test:.2f} AED*",
        ])

    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ---------- No-school / holidays ----------

async def noschool_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return

    data = load_data()

    if not context.args:
        d = today_dubai()
    else:
        arg = context.args[0].lower()
        if arg in ("today", "td"):
            d = today_dubai()
        elif arg in ("tomorrow", "tm"):
            d = today_dubai() + timedelta(days=1)
        else:
            try:
                d = parse_date_str(context.args[0])
            except Exception:
                await update.message.reply_text(
                    "Use: `/noschool [today|tomorrow|YYYY-MM-DD]`",
                    parse_mode="Markdown",
                )
                return

    d_str = d.strftime("%Y-%m-%d")
    if d_str not in data["no_school_dates"]:
        data["no_school_dates"].append(d_str)
        data["no_school_dates"].sort()
        save_data(data)
        await update.message.reply_text(f"✅ {d_str} marked as no-school day.")
        text = f"🏫 No school on {d_str}. No pickup needed that day."
        await notify_drivers(context, data, text)
    else:
        await update.message.reply_text(f"ℹ️ {d_str} is already marked as no-school.")


async def removeschool_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: `/removeschool YYYY-MM-DD`", parse_mode="Markdown")
        return
    try:
        d = parse_date_str(context.args[0])
    except Exception:
        await update.message.reply_text("Invalid date format. Use YYYY-MM-DD.")
        return
    d_str = d.strftime("%Y-%m-%d")
    data = load_data()
    if d_str in data["no_school_dates"]:
        data["no_school_dates"].remove(d_str)
        save_data(data)
        await update.message.reply_text(f"✅ {d_str} removed from no-school days.")
    else:
        await update.message.reply_text(f"ℹ️ {d_str} was not marked as no-school.")


async def holiday_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    if len(context.args) != 2:
        await update.message.reply_text(
            "Usage: `/holiday YYYY-MM-DD YYYY-MM-DD`",
            parse_mode="Markdown",
        )
        return
    try:
        start_d = parse_date_str(context.args[0])
        end_d = parse_date_str(context.args[1])
    except Exception:
        await update.message.reply_text("Dates must be YYYY-MM-DD.", parse_mode="Markdown")
        return
    if end_d < start_d:
        await update.message.reply_text("End date must be after or equal to start date.")
        return
    data = load_data()
    ns = set(data["no_school_dates"])
    cur = start_d
    added = 0
    while cur <= end_d:
        s = cur.strftime("%Y-%m-%d")
        if s not in ns:
            ns.add(s)
            added += 1
        cur += timedelta(days=1)
    data["no_school_dates"] = sorted(ns)
    save_data(data)
    await update.message.reply_text(
        f"✅ Holiday set from {start_d} to {end_d}. Added {added} no-school days."
    )
    text = f"🎉 Holiday announced from {start_d} to {end_d}.\n🏫 No school on these days."
    await notify_drivers(context, data, text)

    # Extra driver notifications for holidays:
    # 1) When we set the holiday (above)
    # 2) One day before the holiday starts
    # 3) One day before the holiday ends (reminder to resume work)
    try:
        app = context.application
        job_queue = app.job_queue if app else None
    except Exception:
        job_queue = None

    if job_queue:
        now_dt = datetime.now(DUBAI_TZ)

        # 1 day before holiday start
        start_dt = datetime.combine(start_d, datetime.min.time(), tzinfo=DUBAI_TZ)
        remind_start_dt = start_dt - timedelta(days=1)
        if remind_start_dt > now_dt:
            job_queue.run_once(
                holiday_reminder_start_job,
                when=remind_start_dt,
                name=f"holiday_start_{start_d.isoformat()}_{end_d.isoformat()}",
                data={"start": start_d.isoformat(), "end": end_d.isoformat()},
            )

        # 1 day before holiday end
        end_dt = datetime.combine(end_d, datetime.min.time(), tzinfo=DUBAI_TZ)
        remind_end_dt = end_dt - timedelta(days=1)
        if remind_end_dt > now_dt:
            job_queue.run_once(
                holiday_reminder_end_job,
                when=remind_end_dt,
                name=f"holiday_end_{start_d.isoformat()}_{end_d.isoformat()}",
                data={"start": start_d.isoformat(), "end": end_d.isoformat()},
            )



# ---------- Test mode ----------

async def test_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    data = load_data()
    data["test_mode"] = True
    save_data(data)
    await update.message.reply_text(
        "🧪 *Test mode ON*\nNew trips are marked as TEST and ignored in totals.\n"
        "Drivers will NOT be notified.",
        parse_mode="Markdown",
    )


async def test_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    data = load_data()
    data["test_mode"] = False
    save_data(data)
    await update.message.reply_text(
        "✅ *Test mode OFF*\nNew trips are REAL and counted in totals.\n"
        "Drivers will receive notifications.",
        parse_mode="Markdown",
    )


# ---------- Export ----------

async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    data = load_data()
    trips = data["trips"]
    if not trips:
        await update.message.reply_text("No trips to export.")
        return
    filename = "driver_trips_export.csv"
    with open(filename, "w", encoding="utf-8") as f:
        f.write("id,driver_id,driver_name,date,amount,destination,user_id,user_name,is_test\n")
        for t in sorted(trips, key=lambda x: x["id"]):
            dest = t["destination"].replace('"', '""')
            uname = (t.get("user_name") or "").replace('"', '""')
            dname = (t.get("driver_name") or "").replace('"', '""')
            f.write(
                f"{t['id']},{t.get('driver_id','')},\"{dname}\",{t['date']},{t['amount']},"
                f"\"{dest}\",{t.get('user_id','')},\"{uname}\",{1 if t.get('is_test') else 0}\n"
            )
    await update.message.reply_document(
        document=InputFile(filename),
        filename=filename,
        caption="📄 All trips (all drivers) exported as CSV.",
    )


# ---------- /paid ----------

async def paid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /paid [amount]
    - Calculates this week grand total (school base + all REAL trips)
    - If amount argument given, use that as paid amount (tip/rounding)
    - Sends notification to all drivers
    - Saves payment record
    """
    if not await ensure_admin(update):
        return
    data = load_data()
    start_dt, end_dt = weekly_range_now(data)
    if start_dt is None:
        start_str = data.get("weekly_start_date")
        await update.message.reply_text(
            f"ℹ️ Driver starts from *{start_str}*.\nNo payment calculation yet.",
            parse_mode="Markdown",
        )
        return
    totals = compute_weekly_totals(data, start_dt, end_dt)
    computed = totals["grand_total"]

    if context.args:
        try:
            paid_amount = float(context.args[0])
        except ValueError:
            await update.message.reply_text("Amount must be a number.")
            return
    else:
        paid_amount = computed

    diff = paid_amount - computed
    record = {
        "timestamp": datetime.now(DUBAI_TZ).isoformat(),
        "period_from": totals["start_date"].isoformat(),
        "period_to": totals["end_date"].isoformat(),
        "computed_total": computed,
        "paid_amount": paid_amount,
        "difference": diff,
    }
    data["payments"].append(record)
    save_data(data)

    summary = (
        f"💵 *Payment Summary (This Week)*\n"
        f"Period: {totals['start_date']} → {totals['end_date']}\n"
        f"Calculated total: *{computed:.2f} AED*\n"
        f"Paid amount: *{paid_amount:.2f} AED*\n"
        f"Difference (tip/adjustment): *{diff:+.2f} AED*"
    )
    await update.message.reply_text(summary, parse_mode="Markdown")

    notify_text = (
        f"💵 Payment confirmed for this week:\n"
        f"Period: {totals['start_date']} → {totals['end_date']}\n"
        f"Amount: {paid_amount:.2f} AED"
    )
    await notify_drivers(context, data, notify_text)


# ---------- Driver views ----------

async def driverview_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if not user or not user.id:
        return
    data = load_data()
    if not is_driver_user(data, user.id):
        await update.message.reply_text("❌ You are not registered as a driver in this bot.")
        return
    start_dt, end_dt = weekly_range_now(data)
    if start_dt is None:
        start_str = data.get("weekly_start_date")
        await update.message.reply_text(
            f"ℹ️ Work starts from *{start_str}*.\nNo trips yet.",
            parse_mode="Markdown",
        )
        return
    totals = compute_weekly_totals(data, start_dt, end_dt, driver_id=user.id)
    text = (
        f"🚕 *Your Week Summary* ({totals['start_date']} → {totals['end_date']})\n\n"
        f"Trips (REAL): *{len(totals['real_trips'])}*\n"
        f"Extra total: *{totals['total_extra']:.2f} AED*"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def driverview_report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    user = update.effective_user
    if not user:
        return

    driver_id: Optional[int] = None
    if is_admin(user.id) and context.args:
        try:
            driver_id = int(context.args[0])
        except ValueError:
            await update.message.reply_text("Driver ID must be a number.")
            return
    else:
        driver_id = user.id

    if not get_driver_by_id(data, driver_id):
        await update.message.reply_text("❌ This driver is not registered.")
        return

    start_dt, end_dt = weekly_range_now(data)
    if start_dt is None:
        start_str = data.get("weekly_start_date")
        await update.message.reply_text(
            f"ℹ️ Work starts from *{start_str}*.\nNo trips yet.",
            parse_mode="Markdown",
        )
        return
    totals = compute_weekly_totals(data, start_dt, end_dt, driver_id=driver_id)
    driver = get_driver_by_id(data, driver_id)
    name = driver["name"] if driver and driver.get("name") else str(driver_id)

    lines = [
        f"🚕 *Driver Weekly Report* — {name} ({driver_id})",
        f"Period: {totals['start_date']} → {totals['end_date']}",
        "",
        "🎓 School base for this week:",
        f"• Weekly base: *{totals['base_weekly']:.2f} AED*",
        f"• Base per school day: *{totals['base_per_day']:.2f} AED*",
        f"• School days (excluding no-school): *{totals['school_days']}*",
        f"• School base total: *{totals['school_base_total']:.2f} AED*",
        "",
        "🚗 Your extra trips (REAL):",
        f"• Count: *{len(totals['real_trips'])}*",
        f"• Extra total: *{totals['total_extra']:.2f} AED*",
        "",
        f"✅ *Grand total for you this week: {totals['grand_total']:.2f} AED*",
    ]
    if totals["real_trips"]:
        lines.append("\n📋 Trip details:")
        for t in sorted(totals["real_trips"], key=lambda x: x["id"]):
            dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
            d_str = dt.strftime("%Y-%m-%d")
            lines.append(
                f"- ID {t['id']}: {d_str} — {t['destination']} — *{t['amount']:.2f} AED*"
            )
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


# ---------- Menus & Text Handler ----------

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Smart /menu:
    - If admin → admin keyboard
    - If driver → driver keyboard
    - Else → small info
    """
    user = update.effective_user
    data = load_data()
    uid = user.id if user else None

    if is_admin(uid):
        keyboard = [
            [KeyboardButton(BTN_ADD_TRIP), KeyboardButton(BTN_LIST)],
            [KeyboardButton(BTN_REPORT), KeyboardButton(BTN_MONTH), KeyboardButton(BTN_YEAR)],
            [KeyboardButton(BTN_NOSCHOOL), KeyboardButton(BTN_EXPORT)],
            [KeyboardButton(BTN_CLEAR_TRIPS), KeyboardButton(BTN_TEST_TOGGLE)],
            [KeyboardButton(BTN_DRIVERS)],
        ]
        markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text("📱 Admin menu:", reply_markup=markup)
        return

    if is_driver_user(data, uid):
        keyboard = [
            [KeyboardButton(DRV_BTN_SUMMARY), KeyboardButton(DRV_BTN_REPORT)],
        ]
        markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
        await update.message.reply_text("🚕 Driver menu:", reply_markup=markup)
        return

    await update.message.reply_text("ℹ️ You are not registered as admin or driver.")


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handles:
    - Admin menu buttons
    - Admin quick trips: "20 Dubai Mall"
    - Driver buttons (no typing logic for drivers)
    """
    if not update.message:
        return
    text = (update.message.text or "").strip()
    if not text:
        return

    user = update.effective_user
    if not user:
        return

    data = load_data()


    # ----- Admin branch -----
    if is_admin(user.id):
        # Admin menu buttons
        if text == BTN_ADD_TRIP:
            await update.message.reply_text(
                "Use `/trip <amount> <destination>` or just send: `20 Dubai Mall`.",
                parse_mode="Markdown",
            )
            return
        if text == BTN_LIST:
            await list_cmd(update, context)
            return
        if text == BTN_REPORT:
            await report_cmd(update, context)
            return
        if text == BTN_MONTH:
            await month_cmd(update, context)
            return
        if text == BTN_YEAR:
            await year_cmd(update, context)
            return

        # ---- No School sub-menu ----
        if text == BTN_NOSCHOOL:
            # Show sub menu for no school: today / tomorrow / pick date
            keyboard = [
                [KeyboardButton(BTN_NOSCHOOL_TODAY), KeyboardButton(BTN_NOSCHOOL_TOMORROW)],
                [KeyboardButton(BTN_NOSCHOOL_PICKDATE), KeyboardButton(BTN_BACK_MAIN)],
            ]
            markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
            await update.message.reply_text("🏫 No school options:", reply_markup=markup)
            return
        if text == BTN_NOSCHOOL_TODAY:
            context.args = ["today"]
            await noschool_cmd(update, context)
            return
        if text == BTN_NOSCHOOL_TOMORROW:
            context.args = ["tomorrow"]
            await noschool_cmd(update, context)
            return
        if text == BTN_NOSCHOOL_PICKDATE:
            # Ask for a specific date; we will parse the next text as YYYY-MM-DD
            context.user_data["await_noschool_date"] = True
            await update.message.reply_text(
                "📅 Send the date as `YYYY-MM-DD` for no school.\n"
                "Example: `2025-11-20`",
                parse_mode="Markdown",
            )
            return

        # If we are waiting for a no-school date, try to parse this text as the date
        if context.user_data.get("await_noschool_date"):
            context.user_data["await_noschool_date"] = False
            context.args = [text]
            await noschool_cmd(update, context)
            # After handling, return to main menu keyboard
            await menu_cmd(update, context)
            return

        if text == BTN_EXPORT:
            await export_cmd(update, context)
            return
        if text == BTN_CLEAR_TRIPS:
            await cleartrips_cmd(update, context)
            return
        if text == BTN_TEST_TOGGLE:
            if data.get("test_mode"):
                await test_off_cmd(update, context)
            else:
                await test_on_cmd(update, context)
            return

        # ---- Drivers sub-menu ----
        if text == BTN_DRIVERS:
            # Show driver management sub-menu
            keyboard = [
                [KeyboardButton(BTN_DRV_LIST)],
                [KeyboardButton(BTN_DRV_ADD), KeyboardButton(BTN_DRV_REMOVE)],
                [KeyboardButton(BTN_DRV_PRIMARY), KeyboardButton(BTN_BACK_MAIN)],
            ]
            markup = ReplyKeyboardMarkup(keyboard, resize_keyboard=True)
            await update.message.reply_text("🚕 Driver management:", reply_markup=markup)
            return
        if text == BTN_DRV_LIST:
            await drivers_cmd(update, context)
            return
        if text == BTN_DRV_ADD:
            await update.message.reply_text(
                "➕ To add driver, send:\n"
                "`/adddriver <telegram_id> <name>`\n"
                "Example: `/adddriver 981113059 faisal`",
                parse_mode="Markdown",
            )
            return
        if text == BTN_DRV_REMOVE:
            await update.message.reply_text(
                "🗑 To remove driver, send:\n"
                "`/removedriver <telegram_id>`",
                parse_mode="Markdown",
            )
            return
        if text == BTN_DRV_PRIMARY:
            await update.message.reply_text(
                "⭐ To set primary driver, send:\n"
                "`/setprimarydriver <telegram_id>`",
                parse_mode="Markdown",
            )
            return

        # Back to main admin menu from any sub-menu
        if text == BTN_BACK_MAIN:
            await menu_cmd(update, context)
            return

        # Admin quick trip: "20 Dubai Mall"
        parts = text.split()
        if len(parts) >= 2:
            try:
                amount = float(parts[0])
            except ValueError:
                return
            destination = " ".join(parts[1:])
            driver_id = get_primary_driver_id(data)
            await add_trip_internal(update, context, driver_id, amount, destination)
        return

    # ----- Driver branch -----
    if is_driver_user(data, user.id):
        # Only respond to button texts. No free typing logic.
        if text == DRV_BTN_SUMMARY:
            await driverview_cmd(update, context)
            return
        if text == DRV_BTN_REPORT:
            await driverview_report_cmd(update, context)
            return
        # Any other text from driver is ignored (no need for typing)
        return

    # Guests / anything else: ignore
    return


# ---------- Main ----------

def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Please set BOT_TOKEN environment variable.")

    app = Application.builder().token(token).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_cmd))

    app.add_handler(CommandHandler("adddriver", adddriver_cmd))
    app.add_handler(CommandHandler("removedriver", removedriver_cmd))
    app.add_handler(CommandHandler("drivers", drivers_cmd))
    app.add_handler(CommandHandler("setprimarydriver", setprimarydriver_cmd))

    app.add_handler(CommandHandler("setbase", setbase_cmd))
    app.add_handler(CommandHandler("setweekstart", setweekstart_cmd))

    app.add_handler(CommandHandler("trip", trip_cmd))
    app.add_handler(CommandHandler("tripd", tripd_cmd))
    app.add_handler(CommandHandler("list", list_cmd))
    app.add_handler(CommandHandler("delete", delete_cmd))
    app.add_handler(CommandHandler("cleartrips", cleartrips_cmd))

    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(CommandHandler("month", month_cmd))
    app.add_handler(CommandHandler("year", year_cmd))

    app.add_handler(CommandHandler("noschool", noschool_cmd))
    app.add_handler(CommandHandler("removeschool", removeschool_cmd))
    app.add_handler(CommandHandler("holiday", holiday_cmd))

    app.add_handler(CommandHandler("test_on", test_on_cmd))
    app.add_handler(CommandHandler("test_off", test_off_cmd))

    app.add_handler(CommandHandler("export", export_cmd))
    app.add_handler(CommandHandler("paid", paid_cmd))

    app.add_handler(CommandHandler("driverview", driverview_cmd))
    app.add_handler(CommandHandler("driverview_report", driverview_report_cmd))

    # Text handler (menus + quick trip + driver buttons)
    app.add_handler(
        MessageHandler(
            filters.TEXT & (~filters.COMMAND),
            text_handler,
        )
    )

    app.run_polling()


if __name__ == "__main__":
    main()
