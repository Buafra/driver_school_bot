# driver_school_bot.py
# DriverSchoolBot 2.0 — clean version
#
# Features:
# - Admins vs drivers
# - Weekly school base with /setbase and /setweekstart
# - Extra trips (REAL vs TEST)
# - /paid closes all trips up to that moment
# - Weekly report counts ONLY trips after last payment
# - "Trips counted since last payment" block (Option C)
# - Simple /menu for admin & driver keyboards

import os
import json
from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from zoneinfo import ZoneInfo

from telegram import (
    Update,
    InputFile,
    ReplyKeyboardMarkup,
    KeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------- Constants ----------

DATA_FILE = Path("driver_school_data.json")
DUBAI_TZ = ZoneInfo("Asia/Dubai")

DEFAULT_BASE_WEEKLY = 725.0  # AED
SCHOOL_DAYS_PER_WEEK = 5

# Admins (family) — Telegram user IDs
ALLOWED_ADMINS = [
    7698278415,  # Faisal
    5034920293,  # Abdulla
]

# Buttons — Admin main menu
BTN_ADD_TRIP = "➕ Add Trip"
BTN_LIST_TRIPS = "📋 List Trips"
BTN_WEEKLY_REPORT = "📊 Weekly Report"
BTN_EXPORT_CSV = "📄 Export CSV"
BTN_CLEAR_TRIPS = "🧹 Clear All Trips"
BTN_TOGGLE_TEST = "🧪 Test Mode"
BTN_DRIVERS_MENU = "🚕 Drivers"
BTN_NOSCHOOL_MENU = "🏫 No School"
BTN_PAID = "💸 Paid (Close Week)"

# Buttons — Drivers submenu
BTN_DRIVERS_LIST = "🚕 List Drivers"
BTN_DRIVERS_ADD = "➕ Add Driver"
BTN_DRIVERS_REMOVE = "🗑 Remove Driver"
BTN_DRIVERS_SET_PRIMARY = "⭐ Set Primary Driver"
BTN_BACK_MAIN = "⬅ Back"

# Buttons — Driver menu (for drivers themselves)
BTN_DRIVER_MY_WEEK = "📦 My Week"
BTN_DRIVER_MY_REPORT = "🧾 My Weekly Report"

# ---------- Data helpers ----------

def load_data() -> Dict[str, Any]:
    if DATA_FILE.exists():
        try:
            with DATA_FILE.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            data = {}
    else:
        data = {}

    if not isinstance(data, dict):
        data = {}

    data.setdefault("base_weekly", DEFAULT_BASE_WEEKLY)
    data.setdefault("week_start_date", None)  # "YYYY-MM-DD" or None
    data.setdefault("trips", [])              # list of trip dicts
    data.setdefault("next_trip_id", 1)
    data.setdefault("no_school_dates", [])    # list of "YYYY-MM-DD"
    data.setdefault("drivers", {})            # {str(telegram_id): {...}}
    data.setdefault("admin_chats", [])        # list of chat_ids
    data.setdefault("test_mode", False)
    data.setdefault("payments", [])           # list of {"timestamp": iso, "note": str}

    return data


def save_data(data: Dict[str, Any]) -> None:
    try:
        with DATA_FILE.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def today_dubai() -> date:
    return datetime.now(DUBAI_TZ).date()


def now_dubai() -> datetime:
    return datetime.now(DUBAI_TZ)


def parse_date_str(d: str) -> date:
    return datetime.strptime(d, "%Y-%m-%d").date()


def format_date(d: date) -> str:
    return d.strftime("%Y-%m-%d")


def parse_iso_datetime(dt: str) -> datetime:
    return datetime.fromisoformat(dt)


# ---------- Auth helpers ----------

def is_admin(user_id: Optional[int]) -> bool:
    return user_id in ALLOWED_ADMINS if user_id is not None else False


def is_driver_user(data: Dict[str, Any], user_id: Optional[int]) -> bool:
    if user_id is None:
        return False
    return str(user_id) in data.get("drivers", {})


async def ensure_admin(update: Update) -> bool:
    user = update.effective_user
    if not user or not is_admin(user.id):
        if update.message:
            await update.message.reply_text("❌ You are not authorized to use this bot.")
        return False
    return True


# ---------- Driver helpers ----------

def get_primary_driver(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    drivers = data.get("drivers", {})
    for d in drivers.values():
        if d.get("active", True) and d.get("is_primary", False):
            return d
    for d in drivers.values():
        if d.get("active", True):
            return d
    return None


def get_driver_by_id(data: Dict[str, Any], driver_id: int) -> Optional[Dict[str, Any]]:
    return data.get("drivers", {}).get(str(driver_id))


def drivers_list_text(data: Dict[str, Any]) -> str:
    drivers = data.get("drivers", {})
    if not drivers:
        return "No drivers added yet."
    lines = ["🚕 *Drivers list:*"]
    for d in drivers.values():
        flag = "⭐ Primary" if d.get("is_primary", False) else ""
        active = "✅ Active" if d.get("active", True) else "❌ Inactive"
        lines.append(
            f"- ID: `{d['id']}` — *{d['name']}* ({active}) {flag}"
        )
    return "\n".join(lines)


# ---------- School days & payments ----------

def is_school_day(d: date) -> bool:
    # Monday=0 .. Sunday=6
    return d.weekday() < 5


def school_days_between(start_d: date, end_d: date, no_school_dates: List[str]) -> Tuple[int, int]:
    """
    Returns (school_days, no_school_days) between [start_d, end_d].
    """
    ns_set = set(no_school_dates)
    school = 0
    noschool = 0
    cur = start_d
    while cur <= end_d:
        if is_school_day(cur):
            if format_date(cur) in ns_set:
                noschool += 1
            else:
                school += 1
        cur += timedelta(days=1)
    return school, noschool


def get_last_payment_timestamp(data: Dict[str, Any]) -> Optional[datetime]:
    """
    Returns the timestamp of the last /paid record, or None if no payments yet.
    """
    payments = data.get("payments") or []
    latest: Optional[datetime] = None
    for p in payments:
        ts = p.get("timestamp")
        if not ts:
            continue
        try:
            dt = parse_iso_datetime(ts)
        except Exception:
            continue
        if latest is None or dt > latest:
            latest = dt
    return latest


# ---------- Weekly range & totals ----------

def weekly_range_now(data: Dict[str, Any]) -> Tuple[Optional[datetime], datetime]:
    """
    Get (start_of_week, end_of_week) for weekly report, respecting week_start_date.

    - If week_start_date is set and not in the future, weeks are 7-day blocks.
    - If week_start_date is future, return (None, now).
    - If not set, use current calendar week Monday–Friday.
    """
    now = now_dubai()
    today = now.date()

    floor_str = data.get("week_start_date")
    floor_date = parse_date_str(floor_str) if floor_str else None

    # If start date is in the future, no weekly report yet
    if floor_date and floor_date > today:
        return None, now

    if floor_date:
        days_diff = (today - floor_date).days
        week_index = days_diff // 7
        week_start = floor_date + timedelta(days=7 * week_index)
    else:
        # Calendar Monday
        week_start = today - timedelta(days=today.weekday())

    # Week ends on Friday, capped to today
    week_end_date = week_start + timedelta(days=4)
    if week_end_date > today:
        week_end_date = today

    start_dt = datetime(week_start.year, week_start.month, week_start.day, 0, 0, tzinfo=DUBAI_TZ)
    end_dt = datetime(week_end_date.year, week_end_date.month, week_end_date.day, 23, 59, 59, tzinfo=DUBAI_TZ)
    return start_dt, end_dt


def compute_weekly_totals(
    data: Dict[str, Any],
    start_dt: datetime,
    end_dt: datetime,
    driver_id: Optional[int] = None,
) -> Dict[str, Any]:
    base_weekly = data["base_weekly"]
    base_per_day = base_weekly / SCHOOL_DAYS_PER_WEEK
    no_school_dates = data["no_school_dates"]
    all_trips = data["trips"]

    start_d = start_dt.date()
    end_d = end_dt.date()
    school_days, noschool_days = school_days_between(start_d, end_d, no_school_dates)
    school_base_total = base_per_day * school_days

    floor_str = data.get("week_start_date")
    floor_date = parse_date_str(floor_str) if floor_str else None
    last_payment_ts = get_last_payment_timestamp(data)

    trips: List[Dict[str, Any]] = []
    for t in all_trips:
        try:
            dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
        except Exception:
            continue

        # Skip trips already paid
        if last_payment_ts and dt <= last_payment_ts:
            continue

        d = dt.date()
        if floor_date and d < floor_date:
            continue
        if dt < start_dt or dt > end_dt:
            continue
        if t.get("is_test", False):
            continue
        if driver_id is not None and t.get("driver_id") != driver_id:
            continue
        trips.append(t)

    total_extra = sum(t["amount"] for t in trips)
    grand_total = school_base_total + total_extra

    return {
        "base_weekly": base_weekly,
        "base_per_day": base_per_day,
        "school_days": school_days,
        "no_school_days": noschool_days,
        "school_base_total": school_base_total,
        "real_trips": trips,
        "total_extra": total_extra,
        "grand_total": grand_total,
    }


def format_weekly_report_body(
    data: Dict[str, Any],
    totals: Dict[str, Any],
    start_dt: datetime,
    end_dt: datetime
) -> str:
    """
    Shared weekly report text (admin + driver) with "Trips counted since last payment".
    """
    last_payment_ts = get_last_payment_timestamp(data)
    if last_payment_ts is None:
        from_dt = start_dt
    else:
        from_dt = last_payment_ts

    # Ensure Dubai tz
    if from_dt.tzinfo is None:
        from_dt = from_dt.replace(tzinfo=DUBAI_TZ)
    else:
        from_dt = from_dt.astimezone(DUBAI_TZ)

    if end_dt.tzinfo is None:
        until_dt = end_dt.replace(tzinfo=DUBAI_TZ)
    else:
        until_dt = end_dt.astimezone(DUBAI_TZ)

    fmt = "%d-%m-%Y %I:%M %p"
    from_str = from_dt.strftime(fmt)
    until_str = until_dt.strftime(fmt)

    lines = [
        "📊 Weekly Driver Report",
        "",
        "🎓 School base (daily):",
        f"• Weekly base: {totals['base_weekly']:.2f} AED",
        f"• Base per school day (Mon–Fri): {totals['base_per_day']:.2f} AED",
        f"• School days in this period : {totals['school_days']}",
        f"• No-school / holiday days in this period: {totals['no_school_days']}",
        f"• School base total: {totals['school_base_total']:.2f} AED",
        "",
        "🚗 Extra trips (REAL):",
        f"• Count: {len(totals['real_trips'])}",
        f"• Extra total: {totals['total_extra']:.2f} AED",
        "",
        f"✅ Grand total: {totals['grand_total']:.2f} AED",
        "",
        "🧾 Trips counted since last payment:",
        f"🟢 From: {from_str}",
        f"🔵 Until: {until_str}",
    ]
    return "\n".join(lines)


def build_weekly_report_text(data: Dict[str, Any], start_dt: datetime, end_dt: datetime) -> str:
    totals = compute_weekly_totals(data, start_dt, end_dt)
    lines = format_weekly_report_body(data, totals, start_dt, end_dt).split("\n")
    if totals["real_trips"]:
        lines.append("")
        lines.append("📋 Trip details (REAL):")
        for t in sorted(totals["real_trips"], key=lambda x: x["id"]):
            dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
            d_str = dt.strftime("%Y-%m-%d")
            lines.append(
                f"- ID {t['id']}: {d_str} — {t['destination']} — {t['amount']:.2f} AED "
                f"(driver {t.get('driver_id','?')})"
            )
    return "\n".join(lines)


def build_driver_weekly_report(data: Dict[str, Any], driver_id: int, start_dt: datetime, end_dt: datetime) -> str:
    totals = compute_weekly_totals(data, start_dt, end_dt, driver_id=driver_id)
    lines = format_weekly_report_body(data, totals, start_dt, end_dt).split("\n")
    if totals["real_trips"]:
        lines.append("")
        lines.append("📋 Trip details:")
        for t in sorted(totals["real_trips"], key=lambda x: x["id"]):
            dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
            d_str = dt.strftime("%Y-%m-%d")
            lines.append(
                f"- ID {t['id']}: {d_str} — {t['destination']} — {t['amount']:.2f} AED"
            )
    return "\n".join(lines)


# ---------- Keyboards ----------

def admin_main_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(BTN_ADD_TRIP), KeyboardButton(BTN_LIST_TRIPS)],
        [KeyboardButton(BTN_WEEKLY_REPORT), KeyboardButton(BTN_EXPORT_CSV)],
        [KeyboardButton(BTN_NOSCHOOL_MENU), KeyboardButton(BTN_DRIVERS_MENU)],
        [KeyboardButton(BTN_CLEAR_TRIPS), KeyboardButton(BTN_TOGGLE_TEST)],
        [KeyboardButton(BTN_PAID)],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def drivers_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(BTN_DRIVERS_LIST)],
        [KeyboardButton(BTN_DRIVERS_ADD), KeyboardButton(BTN_DRIVERS_REMOVE)],
        [KeyboardButton(BTN_DRIVERS_SET_PRIMARY)],
        [KeyboardButton(BTN_BACK_MAIN)],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def driver_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(BTN_DRIVER_MY_WEEK)],
        [KeyboardButton(BTN_DRIVER_MY_REPORT)],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


# ---------- Commands ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return
    uid = user.id

    # Admin
    if is_admin(uid):
        if chat.id not in data["admin_chats"]:
            data["admin_chats"].append(chat.id)
            save_data(data)
        msg = (
            "👋 *DriverSchoolBot 2.0 — Admin*\n\n"
            "Use /menu or the buttons.\n\n"
            "Main commands:\n"
            "• /setbase <amount>\n"
            "• /setweekstart <YYYY-MM-DD>\n"
            "• /trip <amount> <destination>\n"
            "• /tripfor <driver_id> <amount> <destination>\n"
            "• /report — weekly\n"
            "• /paid — close all trips up to now\n"
        )
        if update.message:
            await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=admin_main_keyboard())
        return

    # Driver
    if is_driver_user(data, uid):
        d = data["drivers"].get(str(uid))
        name = d["name"] if d else "driver"
        msg = (
            f"🚕 *Welcome, {name}!* \n\n"
            "Use the buttons:\n"
            "• \"📦 My Week\" – short summary\n"
            "• \"🧾 My Weekly Report\" – full details\n"
        )
        if update.message:
            await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=driver_keyboard())
        return

    # Not authorized
    if update.message:
        await update.message.reply_text("❌ You are not authorized to use this bot.")


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /menu — show correct keyboard based on user type.
    """
    data = load_data()
    user = update.effective_user
    if not user:
        return
    uid = user.id

    if is_admin(uid):
        if update.message:
            await update.message.reply_text(
                "👨‍💼 Admin menu:",
                reply_markup=admin_main_keyboard(),
            )
        return

    if is_driver_user(data, uid):
        d = data["drivers"].get(str(uid))
        name = d["name"] if d else "driver"
        if update.message:
            await update.message.reply_text(
                f"🚕 Driver menu — {name}:",
                reply_markup=driver_keyboard(),
            )
        return

    if update.message:
        await update.message.reply_text("❌ You are not authorized to use this bot.")


async def setbase_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /setbase 725")
        return
    try:
        amount = float(context.args[0])
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Amount must be a positive number.")
        return
    data = load_data()
    data["base_weekly"] = amount
    save_data(data)
    await update.message.reply_text(f"✅ Weekly base updated to {amount:.2f} AED")


async def setweekstart_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /setweekstart YYYY-MM-DD")
        return
    try:
        d = parse_date_str(context.args[0])
    except Exception:
        await update.message.reply_text("Invalid date. Use YYYY-MM-DD.")
        return
    data = load_data()
    data["week_start_date"] = format_date(d)
    save_data(data)
    await update.message.reply_text(f"✅ Weekly calculations start from {format_date(d)}.")


async def adddriver_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /adddriver <telegram_id> <name>")
        return
    try:
        driver_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Driver Telegram ID must be a number.")
        return
    name = " ".join(context.args[1:])
    data = load_data()
    drivers = data["drivers"]
    first_driver = len(drivers) == 0
    drivers[str(driver_id)] = {
        "id": driver_id,
        "name": name,
        "active": True,
        "is_primary": first_driver,
    }
    save_data(data)
    flag = " (primary)" if first_driver else ""
    await update.message.reply_text(f"✅ Driver added: {name} ({driver_id}){flag}")


async def removedriver_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /removedriver <telegram_id>")
        return
    try:
        driver_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Driver Telegram ID must be a number.")
        return
    data = load_data()
    drivers = data["drivers"]
    key = str(driver_id)
    if key not in drivers:
        await update.message.reply_text("Driver not found.")
        return
    name = drivers[key]["name"]
    del drivers[key]
    save_data(data)
    await update.message.reply_text(f"🗑 Driver removed: {name} ({driver_id})")


async def setprimarydriver_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /setprimarydriver <telegram_id>")
        return
    try:
        driver_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Driver Telegram ID must be a number.")
        return
    data = load_data()
    drivers = data["drivers"]
    key = str(driver_id)
    if key not in drivers:
        await update.message.reply_text("Driver not found.")
        return
    for d in drivers.values():
        d["is_primary"] = False
    drivers[key]["is_primary"] = True
    save_data(data)
    await update.message.reply_text(
        f"⭐ Primary driver set to {drivers[key]['name']} ({driver_id})"
    )


async def drivers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    data = load_data()
    txt = drivers_list_text(data)
    await update.message.reply_text(txt, parse_mode="Markdown")


async def add_trip_common(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    amount: float,
    destination: str,
    driver: Dict[str, Any],
) -> None:
    data = load_data()
    now = now_dubai()
    trip_id = data["next_trip_id"]
    data["next_trip_id"] += 1

    is_test = data.get("test_mode", False)
    user = update.effective_user
    trip = {
        "id": trip_id,
        "date": now.isoformat(),
        "amount": amount,
        "destination": destination,
        "user_id": user.id if user else None,
        "user_name": user.first_name if user else "",
        "driver_id": driver["id"],
        "driver_name": driver["name"],
        "is_test": is_test,
    }
    data["trips"].append(trip)
    save_data(data)

    pretty = now.strftime("%Y-%m-%d %H:%M")
    test_label = "🧪 [TEST] " if is_test else ""
    if update.message:
        await update.message.reply_text(
            f"✅ {test_label}Trip added\n"
            f"🆔 ID: {trip_id}\n"
            f"📅 {pretty}\n"
            f"📍 {destination}\n"
            f"💰 {amount:.2f} AED\n"
            f"🚕 Driver: {driver['name']} ({driver['id']})"
        )


async def trip_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text(
            "Usage: /trip <amount> <destination>\nExample: /trip 35 Dubai Mall"
        )
        return
    try:
        amount = float(context.args[0])
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Amount must be a positive number.")
        return
    destination = " ".join(context.args[1:])
    data = load_data()
    driver = get_primary_driver(data)
    if not driver:
        await update.message.reply_text("No driver found. Use /adddriver first.")
        return
    await add_trip_common(update, context, amount, destination, driver)


async def tripfor_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    if len(context.args) < 3:
        await update.message.reply_text(
            "Usage: /tripfor <driver_id> <amount> <destination>\n"
            "Example: /tripfor 981113059 40 Dubai Mall"
        )
        return
    try:
        driver_id = int(context.args[0])
        amount = float(context.args[1])
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Driver ID must be int, amount positive number.")
        return
    destination = " ".join(context.args[2:])
    data = load_data()
    driver = get_driver_by_id(data, driver_id)
    if not driver or not driver.get("active", True):
        await update.message.reply_text("Driver not found or inactive.")
        return
    await add_trip_common(update, context, amount, destination, driver)


async def list_trips_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    data = load_data()
    trips = data["trips"]
    if not trips:
        await update.message.reply_text("No trips recorded yet.")
        return
    real_total = 0.0
    test_total = 0.0
    lines = ["📋 *All trips (REAL + TEST):*"]
    for t in sorted(trips, key=lambda x: x["id"]):
        dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
        d_str = dt.strftime("%Y-%m-%d")
        test_flag = t.get("is_test", False)
        tag = " 🧪[TEST]" if test_flag else ""
        if test_flag:
            test_total += t["amount"]
        else:
            real_total += t["amount"]
        driver_name = t.get("driver_name") or f"Driver {t.get('driver_id','?')}"
        by = t.get("user_name") or f"ID {t.get('user_id','?')}"
        lines.append(
            f"- ID {t['id']}: {d_str} — {t['destination']} — {t['amount']:.2f} AED{tag} "
            f"(by {by}, driver: {driver_name})"
        )
    lines.append("")
    lines.append(f"💰 REAL trips total: {real_total:.2f} AED")
    lines.append(f"🧪 TEST trips total (ignored in weekly totals): {test_total:.2f} AED")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    data = load_data()
    start_dt, end_dt = weekly_range_now(data)
    if start_dt is None:
        wd = data.get("week_start_date")
        await update.message.reply_text(f"ℹ️ Work starts from {wd}. No report yet.")
        return
    text = build_weekly_report_text(data, start_dt, end_dt)
    await update.message.reply_text(text)


async def driver_week_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    user = update.effective_user
    if not user or not is_driver_user(data, user.id):
        return
    start_dt, end_dt = weekly_range_now(data)
    if start_dt is None:
        wd = data.get("week_start_date")
        await update.message.reply_text(f"ℹ️ Work starts from {wd}. No report yet.")
        return
    txt = build_driver_weekly_report(data, user.id, start_dt, end_dt)
    await update.message.reply_text(txt)


async def paid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /paid — close all trips up to now.
    Next weekly reports will only count trips after this moment.
    """
    if not await ensure_admin(update):
        return
    data = load_data()
    now = now_dubai()
    data.setdefault("payments", [])
    data["payments"].append(
        {
            "timestamp": now.isoformat(),
            "note": "manual /paid",
        }
    )
    save_data(data)
    pretty = now.strftime("%Y-%m-%d %H:%M")
    await update.message.reply_text(
        f"💸 Payment checkpoint saved at {pretty}.\n"
        f"Next weekly report will count trips after this time."
    )


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
        f.write("id,date,amount,destination,user_id,user_name,driver_id,driver_name,is_test\n")
        for t in sorted(trips, key=lambda x: x["id"]):
            f.write(
                f"{t['id']},{t['date']},{t['amount']},"
                f"\"{t['destination'].replace('\"','\"\"')}\","
                f"{t.get('user_id','')},"
                f"\"{(t.get('user_name') or '').replace('\"','\"\"')}\","
                f"{t.get('driver_id','')},"
                f"\"{(t.get('driver_name') or '').replace('\"','\"\"')}\","
                f"{1 if t.get('is_test', False) else 0}\n"
            )
    await update.message.reply_document(
        document=InputFile(filename),
        filename=filename,
        caption="📄 All trips exported as CSV (REAL + TEST).",
    )


async def cleartrips_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    data = load_data()
    count = len(data["trips"])
    data["trips"] = []
    data["next_trip_id"] = 1
    save_data(data)
    await update.message.reply_text(f"🧹 Cleared all trips. Removed {count} records.")


async def test_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    data = load_data()
    data["test_mode"] = True
    save_data(data)
    await update.message.reply_text(
        "🧪 Test Mode is ON. New trips will be marked as TEST and ignored in weekly totals."
    )


async def test_off_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    data = load_data()
    data["test_mode"] = False
    save_data(data)
    await update.message.reply_text(
        "✅ Test Mode is OFF. New trips will be REAL and counted in all reports."
    )


async def noschool_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    data = load_data()
    if not context.args:
        await update.message.reply_text("Usage: /noschool YYYY-MM-DD")
        return
    try:
        d = parse_date_str(context.args[0])
    except Exception:
        await update.message.reply_text("Invalid date. Use YYYY-MM-DD.")
        return
    d_str = format_date(d)
    if d_str not in data["no_school_dates"]:
        data["no_school_dates"].append(d_str)
        data["no_school_dates"].sort()
        save_data(data)
        await update.message.reply_text(f"✅ Marked {d_str} as no-school day.")
    else:
        await update.message.reply_text(f"ℹ️ {d_str} is already no-school.")


# ---------- Menu handlers ----------

async def admin_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle admin text buttons & quick trip (e.g., '70 Dubai Mall').
    """
    data = load_data()
    user = update.effective_user
    if not user or not is_admin(user.id):
        return

    txt = (update.message.text or "").strip()

    # Buttons
    if txt == BTN_ADD_TRIP:
        await update.message.reply_text(
            "Use /trip <amount> <destination>\nOr type: \"70 Dubai Mall\""
        )
        return
    if txt == BTN_LIST_TRIPS:
        await list_trips_cmd(update, context)
        return
    if txt == BTN_WEEKLY_REPORT:
        await report_cmd(update, context)
        return
    if txt == BTN_EXPORT_CSV:
        await export_cmd(update, context)
        return
    if txt == BTN_CLEAR_TRIPS:
        await cleartrips_cmd(update, context)
        return
    if txt == BTN_TOGGLE_TEST:
        if data.get("test_mode", False):
            await test_off_cmd(update, context)
        else:
            await test_on_cmd(update, context)
        return
    if txt == BTN_DRIVERS_MENU:
        await update.message.reply_text("🚕 Drivers:", reply_markup=drivers_keyboard())
        return
    if txt == BTN_DRIVERS_LIST:
        await update.message.reply_text(drivers_list_text(data), parse_mode="Markdown")
        return
    if txt == BTN_DRIVERS_ADD:
        await update.message.reply_text("Use /adddriver <telegram_id> <name>")
        return
    if txt == BTN_DRIVERS_REMOVE:
        await update.message.reply_text("Use /removedriver <telegram_id>")
        return
    if txt == BTN_DRIVERS_SET_PRIMARY:
        await update.message.reply_text("Use /setprimarydriver <telegram_id>")
        return
    if txt == BTN_NOSCHOOL_MENU:
        await update.message.reply_text("🏫 No School:", reply_markup=ReplyKeyboardMarkup(
            [
                [KeyboardButton("🏫 No School Today"), KeyboardButton("🏫 No School Tomorrow")],
                [KeyboardButton(BTN_BACK_MAIN)],
            ],
            resize_keyboard=True,
        ))
        return
    if txt == BTN_BACK_MAIN:
        await update.message.reply_text("Back to main menu.", reply_markup=admin_main_keyboard())
        return
    if txt == BTN_PAID:
        await paid_cmd(update, context)
        return

    # Quick trip: "70 dubai mall"
    import re as _re
    m = _re.match(r"^\s*(\d+(?:\.\d+)?)\s+(.+)$", txt)
    if m:
        try:
            amount = float(m.group(1))
            destination = m.group(2).strip()
        except ValueError:
            return
        driver = get_primary_driver(data)
        if not driver:
            await update.message.reply_text("No driver found. Use /adddriver first.")
            return
        await add_trip_common(update, context, amount, destination, driver)
        return


async def driver_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    user = update.effective_user
    if not user or not is_driver_user(data, user.id):
        return
    txt = (update.message.text or "").strip()
    if txt in (BTN_DRIVER_MY_WEEK, BTN_DRIVER_MY_REPORT):
        await driver_week_cmd(update, context)


# ---------- Main ----------

def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Please set BOT_TOKEN environment variable.")

    app = Application.builder().token(token).build()

    # Commands
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_cmd))

    app.add_handler(CommandHandler("setbase", setbase_cmd))
    app.add_handler(CommandHandler("setweekstart", setweekstart_cmd))

    app.add_handler(CommandHandler("adddriver", adddriver_cmd))
    app.add_handler(CommandHandler("removedriver", removedriver_cmd))
    app.add_handler(CommandHandler("drivers", drivers_cmd))
    app.add_handler(CommandHandler("setprimarydriver", setprimarydriver_cmd))

    app.add_handler(CommandHandler("trip", trip_cmd))
    app.add_handler(CommandHandler("tripfor", tripfor_cmd))
    app.add_handler(CommandHandler("list", list_trips_cmd))
    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(CommandHandler("paid", paid_cmd))
    app.add_handler(CommandHandler("export", export_cmd))
    app.add_handler(CommandHandler("cleartrips", cleartrips_cmd))
    app.add_handler(CommandHandler("test_on", test_on_cmd))
    app.add_handler(CommandHandler("test_off", test_off_cmd))
    app.add_handler(CommandHandler("noschool", noschool_cmd))

    # Text handlers
    app.add_handler(
        MessageHandler(
            filters.TEXT & (~filters.COMMAND),
            admin_menu_handler,
        )
    )
    app.add_handler(
        MessageHandler(
            filters.TEXT & (~filters.COMMAND),
            driver_menu_handler,
        )
    )

    app.run_polling(stop_signals=())


if __name__ == "__main__":
    main()
