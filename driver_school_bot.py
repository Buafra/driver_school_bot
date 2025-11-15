# driver_school_bot.py
# DriverSchoolBot — multi-driver shared ledger with school base, holidays, and notifications
#
# Features:
# - Admins: you (Faisal) + Abdulla by Telegram ID
# - Multiple drivers (add/remove/set primary)
# - Start date for calculations (/setweekstart)
# - Weekly / monthly / yearly reports with school base & extra trips
# - No-school days & holiday ranges
# - Holiday notifications to drivers (3 per holiday):
#     1) When holiday is set
#     2) One day before holiday starts
#     3) One day before holiday ends (resume next day)
# - Notifications:
#     - To admins when any REAL trip is added
#     - To driver when trip is added for him
#     - To drivers on no-school days
# - Quick trip (e.g. "70 Dubai Mall") for primary driver
# - Admin menu + No-School submenu + Drivers submenu
# - Driver menu (buttons only, no typing)

import os
import json
from datetime import datetime, date, time, timedelta
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
ContextTypes,
    MessageHandler,
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
BTN_MONTH_REPORT = "📅 Month"
BTN_YEAR_REPORT = "📆 Year"
BTN_NOSCHOOL_MENU = "🏫 No School"
BTN_EXPORT_CSV = "📄 Export CSV"
BTN_CLEAR_TRIPS = "🧹 Clear All Trips"
BTN_TOGGLE_TEST = "🧪 Test Mode"
BTN_DRIVERS_MENU = "🚕 Drivers"

# Buttons — No-school submenu
BTN_NOSCHOOL_TODAY = "🏫 No School Today"
BTN_NOSCHOOL_TOMORROW = "🏫 No School Tomorrow"
BTN_NOSCHOOL_PICKDATE = "📅 No School (Pick Date)"
BTN_BACK_MAIN = "⬅ Back"

# Buttons — Drivers submenu
BTN_DRIVERS_LIST = "🚕 List Drivers"
BTN_DRIVERS_ADD = "➕ Add Driver"
BTN_DRIVERS_REMOVE = "🗑 Remove Driver"
BTN_DRIVERS_SET_PRIMARY = "⭐ Set Primary Driver"

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

    if "base_weekly" not in data:
        data["base_weekly"] = DEFAULT_BASE_WEEKLY

    if "week_start_date" not in data:
        # string "YYYY-MM-DD" or None — where to start counting school & trips
        data["week_start_date"] = None

    if "trips" not in data or not isinstance(data["trips"], list):
        data["trips"] = []  # list of trip dicts

    if "next_trip_id" not in data:
        data["next_trip_id"] = 1

    if "no_school_dates" not in data or not isinstance(data["no_school_dates"], list):
        data["no_school_dates"] = []  # list of "YYYY-MM-DD"

    if "holiday_ranges" not in data or not isinstance(data["holiday_ranges"], list):
        # each: {"start": "YYYY-MM-DD", "end": "YYYY-MM-DD",
        #        "notified_on_create": bool,
        #        "notified_before_start": bool,
        #        "notified_before_end": bool}
        data["holiday_ranges"] = []

    if "drivers" not in data or not isinstance(data["drivers"], dict):
        # drivers keyed by telegram_id string
        # value: {"id": int, "name": str, "active": bool, "is_primary": bool}
        data["drivers"] = {}
    for d in data["drivers"].values():
        if "start_date" not in d:
            d["start_date"] = None


    if "admin_chats" not in data or not isinstance(data["admin_chats"], list):
        data["admin_chats"] = []  # chat_ids of admins who used /start

    if "test_mode" not in data:
        data["test_mode"] = False

    if "pending_preview_monday" not in data:
        data["pending_preview_monday"] = None  # for Sunday preview -> /confirmdrivers

    if "awaiting_noschool_date" not in data:
        data["awaiting_noschool_date"] = []  # list of admin chat_ids waiting for date input

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


def clamp_period_to_active(start_d: date, end_d: date, data: Dict[str, Any]) -> Optional[Tuple[date, date]]:
    """
    Clamp [start_d, end_d] by:
    - start at max(week_start_date, start_d)
    - end at min(today, end_d)
    If the range is invalid, return None.
    """
    wd_str = data.get("week_start_date")
    if wd_str:
        try:
            wd = parse_date_str(wd_str)
            if wd > start_d:
                start_d = wd
        except Exception:
            pass

    today = today_dubai()
    if end_d > today:
        end_d = today

    if start_d > end_d:
        return None
    return (start_d, end_d)


# ---------- Authorization ----------

def is_admin(user_id: Optional[int]) -> bool:
    return user_id in ALLOWED_ADMINS if user_id is not None else False


def is_driver_user(data: Dict[str, Any], user_id: Optional[int]) -> bool:
    if user_id is None:
        return False
    return str(user_id) in data.get("drivers", {})


async def ensure_admin(update: Update) -> bool:
    user = update.effective_user
    if not user or not is_admin(user.id):
        msg = "❌ You are not authorized to use this bot."
        if update.message:
            await update.message.reply_text(msg)
        return False
    return True


# ---------- Driver helpers ----------

def get_primary_driver(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    drivers = data.get("drivers", {})
    # try primary
    for d in drivers.values():
        if d.get("active", True) and d.get("is_primary", False):
            return d
    # fallback: any active driver
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


# ---------- School days / base computation ----------

def is_school_day(d: date) -> bool:
    # Monday=0 .. Sunday=6; school Mon–Fri
    return d.weekday() < 5


def school_days_between(start_d: date, end_d: date, no_school_dates: List[str]) -> int:
    ns_set = set(no_school_dates)
    count = 0
    cur = start_d
    while cur <= end_d:
        if is_school_day(cur) and format_date(cur) not in ns_set:
            count += 1
        cur += timedelta(days=1)
    return count


def filter_trips_by_date_range(
    trips: List[Dict[str, Any]],
    start_d: date,
    end_d: date,
    include_test: bool = False,
) -> List[Dict[str, Any]]:
    out = []
    for t in trips:
        try:
            dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
        except Exception:
            continue
        d = dt.date()
        if d < start_d or d > end_d:
            continue
        if not include_test and t.get("is_test", False):
            continue
        out.append(t)
    return out


def filter_trips_by_month(trips: List[Dict[str, Any]], year: int, month: int, data: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[Tuple[date, date]]]:
    start_d = date(year, month, 1)
    if month == 12:
        end_d = date(year, 12, 31)
    else:
        end_d = date(year, month + 1, 1) - timedelta(days=1)
    clamped = clamp_period_to_active(start_d, end_d, data)
    if not clamped:
        return [], None
    s, e = clamped
    trips_filtered = filter_trips_by_date_range(trips, s, e, include_test=False)
    return trips_filtered, (s, e)


def filter_trips_by_year(trips: List[Dict[str, Any]], year: int, data: Dict[str, Any]) -> Tuple[List[Dict[str, Any]], Optional[Tuple[date, date]]]:
    start_d = date(year, 1, 1)
    end_d = date(year, 12, 31)
    clamped = clamp_period_to_active(start_d, end_d, data)
    if not clamped:
        return [], None
    s, e = clamped
    trips_filtered = filter_trips_by_date_range(trips, s, e, include_test=False)
    return trips_filtered, (s, e)


# ---------- Text builders ----------

def build_period_report(
    data: Dict[str, Any],
    start_d: date,
    end_d: date,
    title: str,
) -> str:
    """Full admin report for [start_d, end_d] (already clamped)."""
    base_weekly = data["base_weekly"]
    no_school_dates = data["no_school_dates"]
    trips = data["trips"]
    drivers = data["drivers"]

    school_days = school_days_between(start_d, end_d, no_school_dates)
    base_per_day = base_weekly / SCHOOL_DAYS_PER_WEEK
    school_base_total = base_per_day * school_days

    period_trips = filter_trips_by_date_range(trips, start_d, end_d, include_test=False)
    extra_total = sum(t["amount"] for t in period_trips)

    grand_total = school_base_total + extra_total

    lines = [
        f"{title} (from {format_date(start_d)} to {format_date(end_d)})",
        "",
        "🎓 School base (daily):",
        f"• Weekly base: {base_weekly:.2f} AED",
        f"• Base per school day: {base_per_day:.2f} AED",
        f"• School days (excluding no-school): {school_days}",
        f"• School base total: {school_base_total:.2f} AED",
        "",
        "🚗 Extra trips (REAL):",
        f"• Count: {len(period_trips)}",
        f"• Extra total: {extra_total:.2f} AED",
        "",
        f"✅ Grand total: {grand_total:.2f} AED",
    ]

    # Per-driver breakdown
    if period_trips:
        lines.append("")
        lines.append("🚕 *Per-driver extra totals:*")
        extra_by_driver: Dict[str, float] = {}
        for t in period_trips:
            did = str(t.get("driver_id", "unknown"))
            extra_by_driver[did] = extra_by_driver.get(did, 0.0) + t["amount"]
        for did, amount in extra_by_driver.items():
            d = drivers.get(did)
            d_name = d["name"] if d else f"Driver {did}"
            lines.append(f"- {d_name} ({did}): {amount:.2f} AED")

        lines.append("")
        lines.append("📋 Trip details:")
        for t in sorted(period_trips, key=lambda x: x["id"]):
            dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
            d_str = dt.strftime("%Y-%m-%d")
            by = t.get("user_name") or f"ID {t.get('user_id','?')}"
            d = drivers.get(str(t.get("driver_id")))
            d_name = d["name"] if d else f"Driver {t.get('driver_id','?')}"
            lines.append(
                f"- ID {t['id']}: {d_str} — {t['destination']} — {t['amount']:.2f} AED "
                f"(by {by}, driver: {d_name})"
            )

    return "\n".join(lines)


def build_driver_weekly_report(
    data: Dict[str, Any],
    driver_id: int,
) -> Optional[str]:
    driver = get_driver_by_id(data, driver_id)
    if not driver or not driver.get("active", True):
        return None

    today = today_dubai()
    monday = today - timedelta(days=today.weekday())  # Monday of this week
    clamped = clamp_period_to_active(monday, today, data)
    if not clamped:
        return None
    start_d, end_d = clamped
    drv_start_str = driver.get("start_date")
    if drv_start_str:
        try:
            drv_start = parse_date_str(drv_start_str)
            if drv_start > start_d:
                start_d = drv_start
        except Exception:
            pass


    base_weekly = data["base_weekly"]
    no_school_dates = data["no_school_dates"]
    trips = data["trips"]

    school_days = school_days_between(start_d, end_d, no_school_dates)
    base_per_day = base_weekly / SCHOOL_DAYS_PER_WEEK
    school_base_total = base_per_day * school_days

    # Only this driver's trips
    all_period = filter_trips_by_date_range(trips, start_d, end_d, include_test=False)
    d_trips = [t for t in all_period if str(t.get("driver_id")) == str(driver_id)]
    extra_total = sum(t["amount"] for t in d_trips)
    grand_total = school_base_total + extra_total

    lines = [
        f"🚕 Driver Weekly Report — {driver['name']} ({driver_id})",
        f"Period: {format_date(start_d)} → {format_date(end_d)}",
        "",
        "🎓 School base (daily):",
        f"• Weekly base: {base_weekly:.2f} AED",
        f"• Base per school day: {base_per_day:.2f} AED",
        f"• School days (excluding no-school): {school_days}",
        f"• School base total: {school_base_total:.2f} AED",
        "",
        "🚗 Extra trips (REAL):",
        f"• Count: {len(d_trips)}",
        f"• Extra total: {extra_total:.2f} AED",
        "",
        f"✅ Grand total: {grand_total:.2f} AED",
    ]

    if d_trips:
        lines.append("")
        lines.append("📋 Trip details:")
        for t in sorted(d_trips, key=lambda x: x["id"]):
            dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
            d_str = dt.strftime("%Y-%m-%d")
            by = t.get("user_name") or f"ID {t.get('user_id','?')}"
            lines.append(
                f"- ID {t['id']}: {d_str} — {t['destination']} — {t['amount']:.2f} AED (by {by})"
            )

    return "\n".join(lines)


# ---------- Menus ----------

def admin_main_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(BTN_ADD_TRIP), KeyboardButton(BTN_LIST_TRIPS)],
        [KeyboardButton(BTN_WEEKLY_REPORT), KeyboardButton(BTN_MONTH_REPORT), KeyboardButton(BTN_YEAR_REPORT)],
        [KeyboardButton(BTN_NOSCHOOL_MENU), KeyboardButton(BTN_DRIVERS_MENU)],
        [KeyboardButton(BTN_EXPORT_CSV), KeyboardButton(BTN_CLEAR_TRIPS)],
        [KeyboardButton(BTN_TOGGLE_TEST)],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def noschool_keyboard() -> ReplyKeyboardMarkup:
    keyboard = [
        [KeyboardButton(BTN_NOSCHOOL_TODAY), KeyboardButton(BTN_NOSCHOOL_TOMORROW)],
        [KeyboardButton(BTN_NOSCHOOL_PICKDATE)],
        [KeyboardButton(BTN_BACK_MAIN)],
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


# ---------- Commands: Start ----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    user = update.effective_user
    chat = update.effective_chat
    if not user or not chat:
        return

    user_id = user.id

    # Admin view
    if is_admin(user_id):
        if chat.id not in data["admin_chats"]:
            data["admin_chats"].append(chat.id)
            save_data(data)

        msg = (
            "👋 *DriverSchoolBot – Admin Mode*\n\n"
            "You can track extra trips, school base, holidays and notify drivers.\n\n"
            "Main commands:\n"
            "• /setbase <amount> – change weekly base (default 725)\n"
            "• /setweekstart <YYYY-MM-DD> – when driver calculations start\n"
            "• /getweekstart – show current start date\n"
            "• /setdriverstart <driver_id> <YYYY-MM-DD> – start date for this driver\n"
            "• /removedriverstart <driver_id> – clear driver start date\n"
            "• /trip <amount> <destination> – add trip for primary driver\n"
            "• /tripfor <driver_id> <amount> <destination> – for specific driver\n"
            "• /report – weekly report\n"
            "• /month [YYYY-MM] – monthly report\n"
            "• /year [YYYY] – yearly report\n"
            "• /noschool [today|tomorrow|YYYY-MM-DD]\n"
            "• /holiday YYYY-MM-DD YYYY-MM-DD\n"
            "• /adddriver <telegram_id> <name>\n"
            "• /removedriver <telegram_id>\n"
            "• /setprimarydriver <telegram_id>\n"
            "• /drivers – list drivers\n"
            "• /export – export CSV\n"
            "• /test_on /test_off – test mode\n\n"
            "You can also use the buttons below."
        )
        save_data(data)
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=admin_main_keyboard())
        return

    # Driver view
    data = load_data()
    if is_driver_user(data, user_id):
        d = data["drivers"][str(user_id)]
        msg = (
            f"🚕 *Welcome, {d['name']}!* \n\n"
            "Use the buttons below:\n"
            "• \"📦 My Week\" – short summary\n"
            "• \"🧾 My Weekly Report\" – full details\n"
        )
        await update.message.reply_text(msg, parse_mode="Markdown", reply_markup=driver_keyboard())
        return

    # Not admin, not driver
    await update.message.reply_text("❌ You are not authorized to use this bot.")


# ---------- Admin Commands ----------

async def set_base(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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


async def set_week_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
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
    await update.message.reply_text(f"✅ Calculations will start from {format_date(d)}.")


async def get_week_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    data = load_data()
    wd = data.get("week_start_date")
    if wd:
        await update.message.reply_text(f"📅 Current start date for calculations: {wd}")
    else:
        await update.message.reply_text("No start date set yet. Use /setweekstart YYYY-MM-DD.")




async def set_driver_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    if len(context.args) < 2:
        await update.message.reply_text("Usage: /setdriverstart <driver_id> <YYYY-MM-DD>")
        return
    try:
        driver_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Driver ID must be a number.")
        return
    try:
        d = parse_date_str(context.args[1])
    except Exception:
        await update.message.reply_text("Invalid date. Use YYYY-MM-DD.")
        return
    data = load_data()
    drivers = data.get("drivers", {})
    key = str(driver_id)
    drv = drivers.get(key)
    if not drv:
        await update.message.reply_text(f"Driver {driver_id} not found. Use /adddriver first.")
        return
    drv["start_date"] = format_date(d)
    save_data(data)
    name = drv.get("name") or str(driver_id)
    await update.message.reply_text(f"✅ Start date for driver {name} set to {format_date(d)}.")


async def remove_driver_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /removedriverstart <driver_id>")
        return
    try:
        driver_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Driver ID must be a number.")
        return
    data = load_data()
    drivers = data.get("drivers", {})
    key = str(driver_id)
    drv = drivers.get(key)
    if not drv:
        await update.message.reply_text(f"Driver {driver_id} not found.")
        return
    if not drv.get("start_date"):
        await update.message.reply_text("This driver has no start date set.")
        return
    drv["start_date"] = None
    save_data(data)
    name = drv.get("name") or str(driver_id)
    await update.message.reply_text(f"🗑️ Start date removed for driver {name}. Counting will use the global start date (if any).")

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
        "is_primary": first_driver,  # first added becomes primary by default
        "start_date": None,
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
    text = drivers_list_text(data)
    await update.message.reply_text(text, parse_mode="Markdown")


async def add_trip_common(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    amount: float,
    destination: str,
    driver: Dict[str, Any],
) -> None:
    """Create a trip, notify admins and driver."""
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

    # Confirm to who added
    await update.message.reply_text(
        f"✅ {test_label}Trip added\n"
        f"🆔 ID: {trip_id}\n"
        f"📅 {pretty}\n"
        f"📍 {destination}\n"
        f"💰 {amount:.2f} AED\n"
        f"🚕 Driver: {driver['name']} ({driver['id']})"
    )

    if not is_test:
        # Notify admins
        admin_msg = (
            "🔔 New trip added by someone in the family:\n"
            f"🆔 ID: {trip_id}\n"
            f"📅 {pretty}\n"
            f"📍 {destination}\n"
            f"💰 {amount:.2f} AED\n"
            f"👤 Added by Telegram ID: {trip['user_id']}\n"
            f"🚗 For driver: {driver['name']} ({driver['id']})"
        )
        for chat_id in data.get("admin_chats", []):
            try:
                await context.bot.send_message(chat_id=chat_id, text=admin_msg)
            except Exception:
                continue

        # Notify driver
        try:
            await context.bot.send_message(
                chat_id=driver["id"],
                text=(
                    "🚗 New extra trip recorded:\n"
                    f"🆔 ID: {trip_id}\n"
                    f"📅 {pretty}\n"
                    f"📍 {destination}\n"
                    f"💰 {amount:.2f} AED\n"
                    f"👤 Recorded by: {trip['user_name'] or trip['user_id']}"
                )
            )
        except Exception:
            pass


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
    lines.append(f"🧪 TEST trips total (ignored in reports): {test_total:.2f} AED")
    await update.message.reply_text("\n".join(lines), parse_mode="Markdown")


async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    data = load_data()
    today = today_dubai()
    monday = today - timedelta(days=today.weekday())
    clamped = clamp_period_to_active(monday, today, data)
    if not clamped:
        await update.message.reply_text("Driver calculations have not started yet.")
        return
    s, e = clamped
    text = build_period_report(data, s, e, "📊 Weekly Report")
    await update.message.reply_text(text)


async def month_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    data = load_data()
    today = today_dubai()
    if context.args:
        try:
            year_str, month_str = context.args[0].split("-")
            year = int(year_str)
            month = int(month_str)
        except Exception:
            await update.message.reply_text("Usage: /month YYYY-MM")
            return
    else:
        year, month = today.year, today.month

    trips = data["trips"]
    month_trips, period = filter_trips_by_month(trips, year, month, data)
    if not period:
        await update.message.reply_text("Driver calculations have not started yet for that period.")
        return
    s, e = period
    text = build_period_report(data, s, e, f"📅 Monthly Report {year}-{month:02d}")
    await update.message.reply_text(text)


async def year_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    data = load_data()
    today = today_dubai()
    if context.args:
        try:
            year = int(context.args[0])
        except Exception:
            await update.message.reply_text("Usage: /year YYYY")
            return
    else:
        year = today.year

    trips = data["trips"]
    year_trips, period = filter_trips_by_year(trips, year, data)
    if not period:
        await update.message.reply_text("Driver calculations have not started yet for that period.")
        return
    s, e = period
    text = build_period_report(data, s, e, f"📅 Yearly Report {year}")
    await update.message.reply_text(text)


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
            dest = str(t.get("destination", "")).replace("\"", "\"\"")
            user_name = (t.get("user_name") or "").replace("\"", "\"\"")
            driver_name = (t.get("driver_name") or "").replace("\"", "\"\"")
            f.write(
                f"{t['id']},{t['date']},{t['amount']},"
                f"\"{dest}\","
                f"{t.get('user_id','')},"
                f"\"{user_name}\","
                f"{t.get('driver_id','')},"
                f"\"{driver_name}\","
                f"{1 if t.get('is_test', False) else 0}\n"
            )

    await update.message.reply_document(
        document=InputFile(filename),
        filename=filename,
        caption="📄 All trips exported as CSV (REAL + TEST).",
    )


async def clear_trips_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    data = load_data()
    count = len(data["trips"])
    data["trips"] = []
    data["next_trip_id"] = 1
    save_data(data)
    await update.message.reply_text(f"🧹 Cleared all trips. Removed {count} records.")


# ---------- No-school & Holiday ----------

async def noschool_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    data = load_data()

    # Parse "today" / "tomorrow" / date
    if context.args:
        arg = context.args[0].lower()
        if arg == "today":
            d = today_dubai()
        elif arg == "tomorrow":
            d = today_dubai() + timedelta(days=1)
        else:
            try:
                d = parse_date_str(context.args[0])
            except Exception:
                await update.message.reply_text("Use /noschool today, /noschool tomorrow or /noschool YYYY-MM-DD.")
                return
    else:
        d = today_dubai()

    d_str = format_date(d)
    if d_str not in data["no_school_dates"]:
        data["no_school_dates"].append(d_str)
        data["no_school_dates"].sort()
        save_data(data)
        await update.message.reply_text(f"✅ Marked {d_str} as no-school day.")
    else:
        await update.message.reply_text(f"ℹ️ {d_str} is already no-school.")

    # Notify drivers about this no-school date
    drivers = [drv for drv in data.get("drivers", {}).values() if drv.get("active", True)]
    if drivers:
        text = f"🏫 No school on {d_str}. No pickup needed that day."
        for drv in drivers:
            try:
                await context.bot.send_message(chat_id=drv["id"], text=text)
            except Exception:
                continue


async def holiday_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    if len(context.args) != 2:
        await update.message.reply_text(
            "Use: /holiday YYYY-MM-DD YYYY-MM-DD\nExample: /holiday 2025-12-02 2025-12-05"
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

    # Mark all days as no-school
    no_school_set = set(data["no_school_dates"])
    cur = start_d
    added = 0
    while cur <= end_d:
        d_str = format_date(cur)
        if d_str not in no_school_set:
            no_school_set.add(d_str)
            added += 1
        cur += timedelta(days=1)
    data["no_school_dates"] = sorted(no_school_set)

    # Store holiday range with notification flags
    holiday_ranges = data.get("holiday_ranges", [])
    holiday_ranges.append(
        {
            "start": format_date(start_d),
            "end": format_date(end_d),
            "notified_on_create": False,
            "notified_before_start": False,
            "notified_before_end": False,
        }
    )
    data["holiday_ranges"] = holiday_ranges
    save_data(data)

    # Notify admins
    msg_admin = (
        "🎉 *Holiday set*\n\n"
        f"From: *{format_date(start_d)}*\n"
        f"To:   *{format_date(end_d)}*\n"
        f"No-school days added: *{added}*"
    )
    for chat_id in data.get("admin_chats", []):
        try:
            await context.bot.send_message(chat_id=chat_id, text=msg_admin, parse_mode="Markdown")
        except Exception:
            continue

    # Notify drivers immediately (notification #1)
    drivers = [drv for drv in data.get("drivers", {}).values() if drv.get("active", True)]
    if drivers:
        msg_driver = (
            "🎉 Holiday has been set.\n\n"
            f"📅 From: {format_date(start_d)}\n"
            f"📅 To:   {format_date(end_d)}\n"
            "🏫 No school during this period. No pickup needed."
        )
        for drv in drivers:
            try:
                await context.bot.send_message(chat_id=drv["id"], text=msg_driver)
            except Exception:
                continue

    # Mark notified_on_create = True
    holiday_ranges[-1]["notified_on_create"] = True
    save_data(data)

    await update.message.reply_text(
        f"✅ Holiday set from {format_date(start_d)} to {format_date(end_d)}. Added {added} no-school days."
    )


# ---------- Test mode ----------

async def test_on_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    data = load_data()
    data["test_mode"] = True
    save_data(data)
    await update.message.reply_text(
        "🧪 Test Mode is ON. New trips will be marked as TEST and ignored in totals."
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


# ---------- Admin menu button handler ----------

async def admin_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text from admins: menu buttons + quick trip + no-school date input."""
    data = load_data()
    user = update.effective_user
    chat = update.effective_chat
    if not user or not is_admin(user.id):
        return

    text = (update.message.text or "").strip()

    # If admin is awaiting a no-school date
    if chat.id in data.get("awaiting_noschool_date", []):
        try:
            d = parse_date_str(text)
        except Exception:
            await update.message.reply_text("Please send date as YYYY-MM-DD.")
            return
        # Remove from awaiting list
        data["awaiting_noschool_date"] = [cid for cid in data["awaiting_noschool_date"] if cid != chat.id]
        save_data(data)
        # Reuse noschool_cmd logic
        context.args = [format_date(d)]
        await noschool_cmd(update, context)
        # Return to main menu keyboard
        await update.message.reply_text("Back to main menu.", reply_markup=admin_main_keyboard())
        return

    # Handle specific buttons
    if text == BTN_ADD_TRIP:
        await update.message.reply_text(
            "To add a trip, either:\n"
            "• Use /trip <amount> <destination>\n"
            "• Or simply type: \"70 Dubai Mall\"",
        )
        return

    if text == BTN_LIST_TRIPS:
        await list_trips_cmd(update, context)
        return

    if text == BTN_WEEKLY_REPORT:
        await report_cmd(update, context)
        return

    if text == BTN_MONTH_REPORT:
        await month_cmd(update, context)
        return

    if text == BTN_YEAR_REPORT:
        await year_cmd(update, context)
        return

    if text == BTN_EXPORT_CSV:
        await export_cmd(update, context)
        return

    if text == BTN_CLEAR_TRIPS:
        await clear_trips_cmd(update, context)
        return

    if text == BTN_TOGGLE_TEST:
        if load_data().get("test_mode", False):
            await test_off_cmd(update, context)
        else:
            await test_on_cmd(update, context)
        return

    # No-school submenu
    if text == BTN_NOSCHOOL_MENU:
        await update.message.reply_text("🏫 No School Options:", reply_markup=noschool_keyboard())
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
        # Add this chat to awaiting_noschool_date
        if chat.id not in data["awaiting_noschool_date"]:
            data["awaiting_noschool_date"].append(chat.id)
            save_data(data)
        await update.message.reply_text(
            "📅 Send the date as YYYY-MM-DD for no school.\nExample: 2025-12-02"
        )
        return

    # Drivers submenu
    if text == BTN_DRIVERS_MENU:
        await update.message.reply_text("🚕 Drivers Management:", reply_markup=drivers_keyboard())
        return

    if text == BTN_DRIVERS_LIST:
        await update.message.reply_text(drivers_list_text(data), parse_mode="Markdown")
        return

    if text == BTN_DRIVERS_ADD:
        await update.message.reply_text(
            "To add a driver, use:\n/adddriver <telegram_id> <name>\nExample: /adddriver 981113059 faisal"
        )
        return

    if text == BTN_DRIVERS_REMOVE:
        await update.message.reply_text(
            "To remove a driver, use:\n/removedriver <telegram_id>"
        )
        return

    if text == BTN_DRIVERS_SET_PRIMARY:
        await update.message.reply_text(
            "To set primary driver, use:\n/setprimarydriver <telegram_id>"
        )
        return

    if text == BTN_BACK_MAIN:
        await update.message.reply_text("Back to main menu.", reply_markup=admin_main_keyboard())
        return

    # Quick trip: text like "70 dubai mall"
    import re
    m = re.match(r"^\s*(\d+(?:\.\d+)?)\s+(.+)$", text)
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

    # Otherwise ignore
    return


# ---------- Driver menu handler ----------

async def driver_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    user = update.effective_user
    if not user or not is_driver_user(data, user.id):
        return

    text = (update.message.text or "").strip()
    driver = data["drivers"][str(user.id)]

    if text == BTN_DRIVER_MY_WEEK or text == BTN_DRIVER_MY_REPORT:
        report_text = build_driver_weekly_report(data, driver["id"])
        if not report_text:
            await update.message.reply_text("No data for this week yet.")
            return
        await update.message.reply_text(report_text)
        return

    # Ignore other text from driver
    return


# ---------- Jobs ----------

async def weekly_report_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Send weekly admin report every Friday 10:00."""
    data = load_data()
    today = today_dubai()
    monday = today - timedelta(days=today.weekday())
    clamped = clamp_period_to_active(monday, today, data)
    if not clamped:
        return
    s, e = clamped
    text = build_period_report(data, s, e, "📊 Weekly Driver Report")
    for chat_id in data.get("admin_chats", []):
        try:
            await context.bot.send_message(chat_id=chat_id, text=text)
        except Exception:
            continue


async def sunday_preview_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Every Sunday 10:00 — prepare next week's Monday preview for admins.
    They must confirm with /confirmdrivers to notify drivers.
    """
    data = load_data()
    today = today_dubai()
    # If today is Sunday (weekday=6)
    if today.weekday() != 6:
        return

    next_monday = today + timedelta(days=(7 - today.weekday()) % 7 or 1)
    # Actually: from Sunday, next Monday is tomorrow
    if today.weekday() == 6:
        next_monday = today + timedelta(days=1)

    data["pending_preview_monday"] = format_date(next_monday)
    save_data(data)

    msg = (
        "📅 Sunday Weekly Preview\n\n"
        f"Upcoming week starts on {format_date(next_monday)}.\n"
        "Use /confirmdrivers to send the plan to drivers."
    )
    for chat_id in data.get("admin_chats", []):
        try:
            await context.bot.send_message(chat_id=chat_id, text=msg)
        except Exception:
            continue


async def confirmdrivers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    data = load_data()
    monday_str = data.get("pending_preview_monday")
    if context.args:
        monday_str = context.args[0]

    if not monday_str:
        await update.message.reply_text("No pending preview. It will be prepared on Sunday.")
        return

    try:
        monday = parse_date_str(monday_str)
    except Exception:
        await update.message.reply_text("Invalid Monday date stored. Set manually: /confirmdrivers YYYY-MM-DD")
        return

    # Upcoming week Monday–Friday
    start_d = monday
    end_d = monday + timedelta(days=4)

    no_school = set(data.get("no_school_dates", []))
    school_days = [d for d in (start_d + timedelta(days=i) for i in range(5))
                   if is_school_day(d)]
    real_school_days = [d for d in school_days if format_date(d) not in no_school]

    if not real_school_days:
        await update.message.reply_text("Upcoming week is fully no-school/holiday. No notifications sent.")
        data["pending_preview_monday"] = None
        save_data(data)
        return

    ns_days = [d for d in school_days if format_date(d) in no_school]
    sd_str = ", ".join(format_date(d) for d in real_school_days)
    ns_str = ", ".join(format_date(d) for d in ns_days) if ns_days else "None"

    msg_driver = (
        "📅 Upcoming school week:\n\n"
        f"Week start: {format_date(start_d)}\n"
        f"School days: {sd_str}\n"
        f"No-school days: {ns_str}\n"
        "🚗 Please be ready for pickups on school days."
    )

    drivers = [drv for drv in data.get("drivers", {}).values() if drv.get("active", True)]
    for drv in drivers:
        try:
            await context.bot.send_message(chat_id=drv["id"], text=msg_driver)
        except Exception:
            continue

    data["pending_preview_monday"] = None
    save_data(data)
    await update.message.reply_text("✅ Weekly plan sent to all active drivers.")


async def holiday_notification_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Runs daily.
    For each holiday range:
      - Notify drivers when created (already done in /holiday).
      - 1 day before start -> notify drivers that holiday starts tomorrow.
      - 1 day before end   -> notify drivers that holiday ends tomorrow (resume next day).
    """
    data = load_data()
    today = today_dubai()
    changed = False

    holiday_ranges = data.get("holiday_ranges", [])
    drivers = [d for d in data.get("drivers", {}).values() if d.get("active", True)]

    for hr in holiday_ranges:
        try:
            start_d = parse_date_str(hr["start"])
            end_d = parse_date_str(hr["end"])
        except Exception:
            continue

        if "notified_on_create" not in hr:
            hr["notified_on_create"] = True  # assume done to avoid duplicates
        if "notified_before_start" not in hr:
            hr["notified_before_start"] = False
        if "notified_before_end" not in hr:
            hr["notified_before_end"] = False

        # 1 day before start
        if not hr["notified_before_start"] and today == (start_d - timedelta(days=1)):
            text = (
                "🎉 Holiday starts tomorrow.\n\n"
                f"📅 Period: {hr['start']} → {hr['end']}\n"
                "🏫 No school during this period. No pickup needed."
            )
            for drv in drivers:
                try:
                    await context.bot.send_message(chat_id=drv["id"], text=text)
                except Exception:
                    continue
            hr["notified_before_start"] = True
            changed = True

        # 1 day before end
        if not hr["notified_before_end"] and today == (end_d - timedelta(days=1)):
            resume_d = end_d + timedelta(days=1)
            text = (
                "📚 Holiday ends tomorrow.\n\n"
                f"📅 Period: {hr['start']} → {hr['end']}\n"
                f"🚗 Please resume pickups from {format_date(resume_d)}."
            )
            for drv in drivers:
                try:
                    await context.bot.send_message(chat_id=drv["id"], text=text)
                except Exception:
                    continue
            hr["notified_before_end"] = True
            changed = True

    if changed:
        data["holiday_ranges"] = holiday_ranges
        save_data(data)


# ---------- Main ----------

def main() -> None:
    token = os.getenv("BOT_TOKEN")
    if not token:
        raise RuntimeError("Please set BOT_TOKEN environment variable.")

    application = Application.builder().token(token).build()

    # Admin & driver commands
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("setbase", set_base))
    application.add_handler(CommandHandler("setweekstart", set_week_start))
    application.add_handler(CommandHandler("getweekstart", get_week_start))
    application.add_handler(CommandHandler("setdriverstart", set_driver_start))
    application.add_handler(CommandHandler("removedriverstart", remove_driver_start))
    application.add_handler(CommandHandler("adddriver", adddriver_cmd))
    application.add_handler(CommandHandler("removedriver", removedriver_cmd))
    application.add_handler(CommandHandler("setprimarydriver", setprimarydriver_cmd))
    application.add_handler(CommandHandler("drivers", drivers_cmd))
    application.add_handler(CommandHandler("trip", trip_cmd))
    application.add_handler(CommandHandler("tripfor", tripfor_cmd))
    application.add_handler(CommandHandler("list", list_trips_cmd))
    application.add_handler(CommandHandler("report", report_cmd))
    application.add_handler(CommandHandler("month", month_cmd))
    application.add_handler(CommandHandler("year", year_cmd))
    application.add_handler(CommandHandler("export", export_cmd))
    application.add_handler(CommandHandler("cleartrips", clear_trips_cmd))
    application.add_handler(CommandHandler("noschool", noschool_cmd))
    application.add_handler(CommandHandler("holiday", holiday_cmd))
    application.add_handler(CommandHandler("test_on", test_on_cmd))
    application.add_handler(CommandHandler("test_off", test_off_cmd))
    application.add_handler(CommandHandler("confirmdrivers", confirmdrivers_cmd))

    # Message handlers:
    #   - Admin menu & quick trip
    #   - Driver buttons
    application.add_handler(
        MessageHandler(
            filters.TEXT & (~filters.COMMAND),
            admin_menu_handler,
        )
    )
    application.add_handler(
        MessageHandler(
            filters.TEXT & (~filters.COMMAND),
            driver_menu_handler,
        )
    )

    # Jobs
    jq = application.job_queue
    # Weekly admin report – Friday
    jq.run_daily(
        weekly_report_job,
        time=time(hour=10, minute=0, tzinfo=DUBAI_TZ),
        days=(4,),  # Friday (Mon=0)
        name="weekly_report",
    )
    # Sunday preview
    jq.run_daily(
        sunday_preview_job,
        time=time(hour=10, minute=0, tzinfo=DUBAI_TZ),
        days=(6,),  # Sunday
        name="sunday_preview",
    )
    # Holiday reminders
    jq.run_daily(
        holiday_notification_job,
        time=time(hour=9, minute=0, tzinfo=DUBAI_TZ),
        days=(0, 1, 2, 3, 4, 5, 6),
        name="holiday_notifier",
    )

    application.run_polling()


if __name__ == "__main__":
    main()
