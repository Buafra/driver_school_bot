# driver_school_bot.py
# DriverSchoolBot 3.0 — multi-driver, per-driver base, per-driver payments, short IDs
#
# Features:
# - Admins vs drivers
# - Multiple drivers; each has:
#       - telegram_id (long)
#       - short_id (small int, e.g. 1,2,3…)
#       - base_weekly (own weekly base, or fallback to global)
#       - payments (list of payment timestamps)
# - Weekly school base with /setbase and /setweekstart
# - Extra trips (REAL vs TEST)
# - /paydriver <driver_code> → close trips for one driver
# - /paid → close trips for ALL drivers
# - Weekly report:
#       - Admin: totals for all drivers, ignoring already-paid trips
#       - Driver: own base + own trips since last payment
# - /setdriverbase <driver_code> <amount>
# - /noschool, /removeschool, /clearnoschool + notifications
# - Short ID usable in commands: /tripfor, /paydriver, /setdriverbase, /removedriver, /setprimarydriver
# - /listunpaid <driver_code> → unpaid trips for one driver
# - /menu shows correct keyboard for admin / driver
# - No Markdown parse issues (plain text messages)

import os
import json
from datetime import datetime, date, timedelta, time
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
BTN_PAID = "💸 Paid (All Drivers)"

# No-school menu buttons
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

    data.setdefault("base_weekly", DEFAULT_BASE_WEEKLY)
    data.setdefault("week_start_date", None)      # "YYYY-MM-DD" or None
    data.setdefault("trips", [])                  # list of trip dicts
    data.setdefault("next_trip_id", 1)
    data.setdefault("no_school_dates", [])        # list of "YYYY-MM-DD"
    data.setdefault("drivers", {})                # {str(telegram_id): {...}}
    data.setdefault("admin_chats", [])            # list of chat_ids
    data.setdefault("test_mode", False)
    data.setdefault("awaiting_noschool_date", []) # list of admin chat_ids awaiting date

    # Upgrade old driver structure with new fields
    drivers = data["drivers"]
    for drv in drivers.values():
        drv.setdefault("active", True)
        drv.setdefault("is_primary", False)
        drv.setdefault("base_weekly", None)   # per-driver base; None -> use global
        drv.setdefault("payments", [])        # list of ISO timestamps
        drv.setdefault("short_id", None)      # small int

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

def get_driver_by_telegram_id(data: Dict[str, Any], telegram_id: int) -> Optional[Dict[str, Any]]:
    return data.get("drivers", {}).get(str(telegram_id))


def get_next_short_driver_id(data: Dict[str, Any]) -> int:
    drivers = data.get("drivers", {})
    max_sid = 0
    for d in drivers.values():
        sid = d.get("short_id")
        if isinstance(sid, int) and sid > max_sid:
            max_sid = sid
    return max_sid + 1


def get_driver_by_any_id(data: Dict[str, Any], code: int) -> Optional[Dict[str, Any]]:
    """
    code can be:
    - Telegram ID (key in data['drivers'])
    - short_id (small int)
    """
    drivers = data.get("drivers", {})

    # 1) Try as Telegram ID key
    drv = drivers.get(str(code))
    if drv:
        return drv

    # 2) Try as short_id
    for d in drivers.values():
        if d.get("short_id") == code:
            return d

    return None


def get_primary_driver(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    drivers = data.get("drivers", {})
    for d in drivers.values():
        if d.get("active", True) and d.get("is_primary", False):
            return d
    for d in drivers.values():
        if d.get("active", True):
            return d
    return None


def drivers_list_text(data: Dict[str, Any]) -> str:
    drivers = data.get("drivers", {})
    if not drivers:
        return "No drivers added yet."
    lines = ["🚕 Drivers list:"]
    for d in drivers.values():
        flag = "⭐ Primary" if d.get("is_primary", False) else ""
        active = "✅ Active" if d.get("active", True) else "❌ Inactive"
        sid = d.get("short_id")
        base = d.get("base_weekly")
        base_str = f"{base:.2f} AED" if base else "default"
        lines.append(
            f"- Name: {d['name']} | ID: {d['id']} | SID: {sid} | Base: {base_str} | {active} {flag}"
        )
    return "\n".join(lines)


def get_driver_base_weekly(data: Dict[str, Any], driver_id: int) -> float:
    d = get_driver_by_telegram_id(data, driver_id) or get_driver_by_any_id(data, driver_id)
    if d and d.get("base_weekly") not in (None, 0):
        return float(d["base_weekly"])
    return float(data.get("base_weekly", DEFAULT_BASE_WEEKLY))


def get_total_base_weekly_all_drivers(data: Dict[str, Any]) -> float:
    total = 0.0
    drivers = data.get("drivers", {})
    for d in drivers.values():
        if not d.get("active", True):
            continue
        base = d.get("base_weekly")
        if base in (None, 0):
            base = data.get("base_weekly", DEFAULT_BASE_WEEKLY)
        total += float(base)
    return total


def get_last_payment_for_driver(data: Dict[str, Any], driver_id: int) -> Optional[datetime]:
    drv = get_driver_by_telegram_id(data, driver_id) or get_driver_by_any_id(data, driver_id)
    if not drv:
        return None
    last_dt = None
    for ts in drv.get("payments", []):
        try:
            dt = parse_iso_datetime(ts)
        except Exception:
            continue
        if last_dt is None or dt > last_dt:
            last_dt = dt
    return last_dt


# ---------- School days & week ranges ----------

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


def weekly_range_now(data: Dict[str, Any]) -> Tuple[Optional[datetime], datetime]:
    """
    Get (start_of_week_for_calc, end_for_calc) for weekly report, respecting week_start_date.

    - If week_start_date is set and not in the future, weeks are 7-day blocks.
    - If week_start_date is in the future, return (None, now).
    - If no week_start_date, use current calendar Monday.
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

    # For calculations: end is min(week_start+4, today)
    calc_end_date = week_start + timedelta(days=4)
    if calc_end_date > today:
        calc_end_date = today

    start_dt = datetime(week_start.year, week_start.month, week_start.day, 0, 0, tzinfo=DUBAI_TZ)
    end_dt = datetime(calc_end_date.year, calc_end_date.month, calc_end_date.day, 23, 59, 59, tzinfo=DUBAI_TZ)
    return start_dt, end_dt


# ---------- Weekly totals ----------

def compute_weekly_totals(
    data: Dict[str, Any],
    start_dt: datetime,
    end_dt: datetime,
    driver_id: Optional[int] = None,
) -> Dict[str, Any]:
    no_school_dates = data["no_school_dates"]
    all_trips = data["trips"]

    start_d = start_dt.date()
    end_d = end_dt.date()
    school_days, noschool_days = school_days_between(start_d, end_d, no_school_dates)

    if driver_id is not None:
        # Per-driver base + payment
        base_weekly = get_driver_base_weekly(data, driver_id)
        last_payment_ts = get_last_payment_for_driver(data, driver_id)
    else:
        # Global base = sum of all active drivers
        base_weekly = get_total_base_weekly_all_drivers(data)
        last_payment_ts = None  # per-trip, see below

    base_per_day = base_weekly / SCHOOL_DAYS_PER_WEEK
    school_base_total = base_per_day * school_days

    real_trips: List[Dict[str, Any]] = []
    for t in all_trips:
        try:
            dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
        except Exception:
            continue

        d = dt.date()
        if d < start_d or d > end_d:
            continue
        if t.get("is_test", False):
            continue

        trip_driver_id = t.get("driver_id")

        if driver_id is not None:
            # Single driver: filter by his ID and his own payments
            if trip_driver_id != driver_id:
                continue
            lp = last_payment_ts
            if lp and dt <= lp:
                continue
        else:
            # Admin global: filter by each trip's own driver's payments
            if trip_driver_id is None:
                continue
            lp = get_last_payment_for_driver(data, trip_driver_id)
            if lp and dt <= lp:
                continue

        real_trips.append(t)

    total_extra = sum(t["amount"] for t in real_trips)
    grand_total = school_base_total + total_extra

    return {
        "base_weekly": base_weekly,
        "base_per_day": base_per_day,
        "school_days": school_days,
        "no_school_days": noschool_days,
        "school_base_total": school_base_total,
        "real_trips": real_trips,
        "total_extra": total_extra,
        "grand_total": grand_total,
        "last_payment_ts": last_payment_ts,
        "end_ts": end_dt,
    }


def format_period_header(start_dt: datetime) -> str:
    """
    For header label: always full week Monday–Friday.
    """
    week_start_d = start_dt.date()
    week_end_d = week_start_d + timedelta(days=4)
    return f"Period: {week_start_d.strftime('%Y-%m-%d')} → {week_end_d.strftime('%Y-%m-%d')}"


def build_admin_weekly_report_text(data: Dict[str, Any], start_dt: datetime, end_dt: datetime) -> str:
    totals = compute_weekly_totals(data, start_dt, end_dt, driver_id=None)
    header = format_period_header(start_dt)
    lines = [
        "📊 Weekly Driver Report (ALL drivers)",
        header,
        "",
        "🎓 School base (daily):",
        f"• Weekly base total for all drivers: {totals['base_weekly']:.2f} AED",
        f"• Base per school day (Mon–Fri): {totals['base_per_day']:.2f} AED",
        f"• School days in this period : {totals['school_days']}",
        f"• No-school / holiday days in this period: {totals['no_school_days']}",
        f"• School base total: {totals['school_base_total']:.2f} AED",
        "",
        "🚗 Extra trips (REAL, unpaid):",
        f"• Count: {len(totals['real_trips'])}",
        f"• Extra total: {totals['total_extra']:.2f} AED",
        "",
        f"✅ Grand total (base + unpaid trips): {totals['grand_total']:.2f} AED",
        "",
        "ℹ️ Trips already paid for each driver (via /paydriver or /paid) are not included here.",
    ]

    # Per-driver extra totals
    if totals["real_trips"]:
        lines.append("")
        lines.append("🚕 Extra trips per driver (unpaid):")
        drivers = data.get("drivers", {})
        extra_by_driver: Dict[str, float] = {}
        for t in totals["real_trips"]:
            did = str(t.get("driver_id"))
            extra_by_driver[did] = extra_by_driver.get(did, 0.0) + t["amount"]
        for did, amount in extra_by_driver.items():
            d = drivers.get(did)
            if d:
                name = d["name"]
                sid = d.get("short_id")
                lines.append(f"- {name} (ID: {d['id']}, SID: {sid}): {amount:.2f} AED")
            else:
                lines.append(f"- Driver {did}: {amount:.2f} AED")

        lines.append("")
        lines.append("📋 Trip details:")
        for t in sorted(totals["real_trips"], key=lambda x: x["id"]):
            dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
            d_str = dt.strftime("%Y-%m-%d")
            drivers = data.get("drivers", {})
            d = drivers.get(str(t.get("driver_id")))
            d_name = d["name"] if d else f"Driver {t.get('driver_id','?')}"
            lines.append(
                f"- ID {t['id']}: {d_str} — {t['destination']} — {t['amount']:.2f} AED (driver: {d_name})"
            )

    return "\n".join(lines)


def build_driver_weekly_report_text(data: Dict[str, Any], driver_telegram_id: int, start_dt: datetime, end_dt: datetime) -> str:
    totals = compute_weekly_totals(data, start_dt, end_dt, driver_id=driver_telegram_id)
    header = format_period_header(start_dt)

    drv = get_driver_by_telegram_id(data, driver_telegram_id) or get_driver_by_any_id(data, driver_telegram_id)
    name = drv["name"] if drv else f"Driver {driver_telegram_id}"
    sid = drv.get("short_id") if drv else None

    # From / until (since last payment for this driver)
    lp = totals["last_payment_ts"]
    if lp is None:
        from_dt = start_dt
    else:
        from_dt = lp

    if from_dt.tzinfo is None:
        from_dt = from_dt.replace(tzinfo=DUBAI_TZ)
    else:
        from_dt = from_dt.astimezone(DUBAI_TZ)

    until_dt = totals["end_ts"]
    if until_dt.tzinfo is None:
        until_dt = until_dt.replace(tzinfo=DUBAI_TZ)
    else:
        until_dt = until_dt.astimezone(DUBAI_TZ)

    fmt = "%d-%m-%Y %I:%M %p"
    from_str = from_dt.strftime(fmt)
    until_str = until_dt.strftime(fmt)

    lines = [
        f"🚕 Weekly Driver Report — {name} (ID: {driver_telegram_id}, SID: {sid})",
        header,
        "",
        "🎓 School base (daily):",
        f"• Weekly base: {totals['base_weekly']:.2f} AED",
        f"• Base per school day (Mon–Fri): {totals['base_per_day']:.2f} AED",
        f"• School days in this period : {totals['school_days']}",
        f"• No-school / holiday days in this period: {totals['no_school_days']}",
        f"• School base total: {totals['school_base_total']:.2f} AED",
        "",
        "🚗 Extra trips (REAL, unpaid):",
        f"• Count: {len(totals['real_trips'])}",
        f"• Extra total: {totals['total_extra']:.2f} AED",
        "",
        f"✅ Grand total (base + unpaid trips): {totals['grand_total']:.2f} AED",
        "",
        "🧾 Trips counted since last payment for this driver:",
        f"🟢 From: {from_str}",
        f"🔵 Until: {until_str}",
    ]

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
            "👋 DriverSchoolBot 3.0 — Admin\n\n"
            "Use /menu or the buttons.\n\n"
            "Main commands:\n"
            "• /setbase <amount> (default weekly base)\n"
            "• /setweekstart <YYYY-MM-DD>\n"
            "• /adddriver <telegram_id> <name>\n"
            "• /setdriverbase <driver_code> <amount>\n"
            "• /trip <amount> <destination>\n"
            "• /tripfor <driver_code> <amount> <destination>\n"
            "• /report (weekly, all drivers)\n"
            "• /paydriver <driver_code> (close trips for one driver)\n"
            "• /paid (close trips for ALL drivers)\n"
            "• /listunpaid <driver_code> (unpaid trips for one driver)\n"
            "• /noschool today|tomorrow|YYYY-MM-DD\n"
            "• /removeschool YYYY-MM-DD\n"
            "• /clearnoschool\n"
            "Note: driver_code can be Telegram ID or SID.\n"
        )
        if update.message:
            await update.message.reply_text(msg, reply_markup=admin_main_keyboard())
        return

    # Driver
    if is_driver_user(data, uid):
        d = data["drivers"].get(str(uid))
        name = d["name"] if d else "driver"
        sid = d.get("short_id") if d else None
        msg = (
            f"🚕 Welcome, {name} (SID: {sid})!\n\n"
            "Use the buttons:\n"
            "• \"📦 My Week\" – short summary\n"
            "• \"🧾 My Weekly Report\" – full details\n"
        )
        if update.message:
            await update.message.reply_text(msg, reply_markup=driver_keyboard())
        return

    # Not authorized
    if update.message:
        await update.message.reply_text("❌ You are not authorized to use this bot.")


async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    data = load_data()
    user = update.effective_user
    if not user:
        return
    uid = user.id

    if is_admin(uid):
        if update.message:
            await update.message.reply_text("👨‍💼 Admin menu:", reply_markup=admin_main_keyboard())
        return

    if is_driver_user(data, uid):
        d = data["drivers"].get(str(uid))
        name = d["name"] if d else "driver"
        sid = d.get("short_id") if d else None
        if update.message:
            await update.message.reply_text(
                f"🚕 Driver menu — {name} (SID: {sid}):",
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
    await update.message.reply_text(f"✅ Global weekly base (default) updated to {amount:.2f} AED")


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
        telegram_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Driver Telegram ID must be a number.")
        return

    name = " ".join(context.args[1:])
    data = load_data()
    drivers = data["drivers"]

    first_driver = len(drivers) == 0
    short_id = get_next_short_driver_id(data)

    drivers[str(telegram_id)] = {
        "id": telegram_id,
        "short_id": short_id,
        "name": name,
        "active": True,
        "is_primary": first_driver,
        "base_weekly": None,  # uses global default until you setdriverbase
        "payments": [],
    }
    save_data(data)

    flag = " (primary)" if first_driver else ""
    await update.message.reply_text(
        f"✅ Driver added: {name} (ID: {telegram_id}, SID: {short_id}){flag}"
    )

    # Try to notify the driver
    welcome_msg = (
        f"🚕 Hello {name}!\n\n"
        "You have been added as a driver in DriverSchoolBot.\n"
        "Your short ID (SID) is "
        f"{short_id}. Use /start to open your driver menu and see your weekly report."
    )
    try:
        await context.bot.send_message(chat_id=telegram_id, text=welcome_msg)
    except Exception:
        pass


async def setdriverbase_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /setdriverbase <driver_code> <amount>
    driver_code can be Telegram ID or SID.
    """
    if not await ensure_admin(update):
        return
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /setdriverbase <driver_code> <amount>")
        return
    try:
        code = int(context.args[0])
        amount = float(context.args[1])
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Driver code and amount must be numbers, amount > 0.")
        return

    data = load_data()
    drv = get_driver_by_any_id(data, code)
    if not drv:
        await update.message.reply_text("Driver not found by this code.")
        return

    drv["base_weekly"] = amount
    save_data(data)
    await update.message.reply_text(
        f"✅ Weekly base for driver {drv['name']} (ID: {drv['id']}, SID: {drv['short_id']}) "
        f"set to {amount:.2f} AED"
    )


async def removedriver_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /removedriver <driver_code>  (Telegram ID or SID)
    """
    if not await ensure_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /removedriver <driver_code>")
        return
    try:
        code = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Driver code must be a number (ID or SID).")
        return

    data = load_data()
    drivers = data["drivers"]
    drv = get_driver_by_any_id(data, code)
    if not drv:
        await update.message.reply_text("Driver not found.")
        return

    name = drv["name"]
    tid = drv["id"]
    # remove by telegram id key
    if str(tid) in drivers:
        del drivers[str(tid)]
    save_data(data)
    await update.message.reply_text(f"🗑 Driver removed: {name} (ID: {tid}, SID: {drv.get('short_id')})")


async def setprimarydriver_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /setprimarydriver <driver_code> (Telegram ID or SID)
    """
    if not await ensure_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /setprimarydriver <driver_code>")
        return
    try:
        code = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Driver code must be a number (ID or SID).")
        return

    data = load_data()
    drv = get_driver_by_any_id(data, code)
    if not drv:
        await update.message.reply_text("Driver not found.")
        return

    drivers = data["drivers"]
    for d in drivers.values():
        d["is_primary"] = False
    drv["is_primary"] = True
    save_data(data)
    await update.message.reply_text(
        f"⭐ Primary driver set to {drv['name']} (ID: {drv['id']}, SID: {drv['short_id']})"
    )


async def drivers_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return
    data = load_data()
    txt = drivers_list_text(data)
    await update.message.reply_text(txt)


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
            f"🚕 Driver: {driver['name']} (ID: {driver['id']}, SID: {driver.get('short_id')})"
        )

    if not is_test:
        # Notify admins
        admin_msg = (
            "🔔 New trip added:\n"
            f"🆔 ID: {trip_id}\n"
            f"📅 {pretty}\n"
            f"📍 {destination}\n"
            f"💰 {amount:.2f} AED\n"
            f"👤 Added by Telegram ID: {trip['user_id']}\n"
            f"🚗 For driver: {driver['name']} (ID: {driver['id']}, SID: {driver.get('short_id')})"
        )
        for chat_id in data.get("admin_chats", []):
            try:
                await context.bot.send_message(chat_id=chat_id, text=admin_msg)
            except Exception:
                continue

        # Notify driver
        driver_msg = (
            "🚗 New extra trip recorded:\n"
            f"🆔 ID: {trip_id}\n"
            f"📅 {pretty}\n"
            f"📍 {destination}\n"
            f"💰 {amount:.2f} AED\n"
            f"👤 Recorded by: {trip['user_name'] or trip['user_id']}"
        )
        try:
            await context.bot.send_message(chat_id=driver["id"], text=driver_msg)
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
    """
    /tripfor <driver_code> <amount> <destination>
    driver_code can be Telegram ID or SID.
    """
    if not await ensure_admin(update):
        return
    if len(context.args) < 3:
        await update.message.reply_text(
            "Usage: /tripfor <driver_code> <amount> <destination>\n"
            "Example: /tripfor 1 40 Dubai Mall (SID 1)\n"
            "Or: /tripfor 981113059 40 Dubai Mall (Telegram ID)"
        )
        return
    try:
        code = int(context.args[0])
        amount = float(context.args[1])
        if amount <= 0:
            raise ValueError
    except ValueError:
        await update.message.reply_text("Driver code must be int, amount positive number.")
        return

    destination = " ".join(context.args[2:])
    data = load_data()
    driver = get_driver_by_any_id(data, code)
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
    lines = ["📋 All trips (REAL + TEST):"]
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
    await update.message.reply_text("\n".join(lines))


async def listunpaid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /listunpaid <driver_code>
    Show all unpaid REAL trips for a driver (after his last payment, and after week_start_date if set).
    driver_code can be Telegram ID or SID.
    """
    if not await ensure_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /listunpaid <driver_code>")
        return
    try:
        code = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Driver code must be a number (ID or SID).")
        return

    data = load_data()
    drv = get_driver_by_any_id(data, code)
    if not drv:
        await update.message.reply_text("Driver not found.")
        return

    driver_id = drv["id"]
    last_payment_ts = get_last_payment_for_driver(data, driver_id)
    week_start_str = data.get("week_start_date")
    floor_date = parse_date_str(week_start_str) if week_start_str else None

    trips = data.get("trips", [])
    unpaid: List[Dict[str, Any]] = []
    for t in trips:
        if t.get("is_test", False):
            continue
        if t.get("driver_id") != driver_id:
            continue
        try:
            dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
        except Exception:
            continue

        # Respect week_start_date as minimum date
        if floor_date and dt.date() < floor_date:
            continue

        if last_payment_ts and dt <= last_payment_ts:
            continue

        unpaid.append(t)

    if not unpaid:
        await update.message.reply_text(
            f"ℹ️ No unpaid REAL trips for {drv['name']} (ID: {drv['id']}, SID: {drv['short_id']})."
        )
        return

    total = sum(t["amount"] for t in unpaid)
    lines = [
        f"📋 Unpaid trips for {drv['name']} (ID: {drv['id']}, SID: {drv['short_id']}):",
        "",
    ]
    for t in sorted(unpaid, key=lambda x: x["id"]):
        dt = parse_iso_datetime(t["date"]).astimezone(DUBAI_TZ)
        d_str = dt.strftime("%Y-%m-%d %H:%M")
        lines.append(
            f"- ID {t['id']}: {d_str} — {t['destination']} — {t['amount']:.2f} AED"
        )
    lines.append("")
    lines.append(f"💰 Total unpaid: {total:.2f} AED")

    await update.message.reply_text("\n".join(lines))


async def report_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Weekly admin report (all drivers, unpaid trips only).
    """
    if not await ensure_admin(update):
        return
    data = load_data()
    start_dt, end_dt = weekly_range_now(data)
    if start_dt is None:
        wd = data.get("week_start_date")
        await update.message.reply_text(f"ℹ️ Work starts from {wd}. No report yet.")
        return
    text = build_admin_weekly_report_text(data, start_dt, end_dt)
    await update.message.reply_text(text)


async def driver_week_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Driver weekly report when driver clicks "My Week" or "My Weekly Report".
    """
    data = load_data()
    user = update.effective_user
    if not user:
        return
    if not is_driver_user(data, user.id):
        if update.message:
            await update.message.reply_text("You are not registered as a driver.")
        return
    start_dt, end_dt = weekly_range_now(data)
    if start_dt is None:
        wd = data.get("week_start_date")
        await update.message.reply_text(f"ℹ️ Work starts from {wd}. No report yet.")
        return
    txt = build_driver_weekly_report_text(data, user.id, start_dt, end_dt)
    await update.message.reply_text(txt)


async def paydriver_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /paydriver <driver_code>  (Telegram ID or SID)
    Close trips for one driver up to now.
    """
    if not await ensure_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /paydriver <driver_code>")
        return
    try:
        code = int(context.args[0])
    except ValueError:
        await update.message.reply_text("Driver code must be a number (ID or SID).")
        return

    data = load_data()
    drv = get_driver_by_any_id(data, code)
    if not drv:
        await update.message.reply_text("Driver not found.")
        return

    now = now_dubai()
    drv.setdefault("payments", [])
    drv["payments"].append(now.isoformat())
    save_data(data)

    pretty = now.strftime("%Y-%m-%d %H:%M")
    await update.message.reply_text(
        f"💸 Payment checkpoint for {drv['name']} "
        f"(ID: {drv['id']}, SID: {drv['short_id']}) saved at {pretty}.\n"
        f"Next report for this driver will count trips after this time."
    )


async def paid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /paid — close trips for ALL drivers up to now.
    """
    if not await ensure_admin(update):
        return
    data = load_data()
    now = now_dubai()
    drivers = data.get("drivers", {})
    for drv in drivers.values():
        drv.setdefault("payments", [])
        drv["payments"].append(now.isoformat())
    save_data(data)
    pretty = now.strftime("%Y-%m-%d %H:%M")
    await update.message.reply_text(
        f"💸 Payment checkpoint saved for ALL drivers at {pretty}.\n"
        f"Next weekly reports will count trips after this time for each driver."
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


# ---------- No-school ----------

async def noschool_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await ensure_admin(update):
        return

    data = load_data()

    if context.args:
        arg = context.args[0].lower()
    else:
        arg = "today"

    if arg == "today":
        d = today_dubai()
    elif arg == "tomorrow":
        d = today_dubai() + timedelta(days=1)
    else:
        try:
            d = parse_date_str(context.args[0])
        except Exception:
            await update.message.reply_text("Invalid date. Use: today, tomorrow, or YYYY-MM-DD.")
            return

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
        msg = f"🏫 No school on {d_str}. No pickup needed that day."
        for drv in drivers:
            try:
                await context.bot.send_message(chat_id=drv["id"], text=msg)
            except Exception:
                continue


async def removeschool_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /removeschool YYYY-MM-DD
    Remove one no-school date (no driver notification).
    """
    if not await ensure_admin(update):
        return
    if not context.args:
        await update.message.reply_text("Usage: /removeschool YYYY-MM-DD")
        return
    try:
        d = parse_date_str(context.args[0])
    except Exception:
        await update.message.reply_text("Invalid date. Use YYYY-MM-DD.")
        return

    d_str = format_date(d)
    data = load_data()
    if d_str in data["no_school_dates"]:
        data["no_school_dates"] = [x for x in data["no_school_dates"] if x != d_str]
        save_data(data)
        await update.message.reply_text(f"✅ {d_str} removed from no-school dates.")
    else:
        await update.message.reply_text(f"ℹ️ {d_str} was not in no-school dates.")


async def clearnoschool_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    /clearnoschool
    Clear ALL no-school dates and notify drivers that schedule is back to normal.
    """
    if not await ensure_admin(update):
        return

    data = load_data()
    existing = data.get("no_school_dates", [])
    if not existing:
        await update.message.reply_text("ℹ️ There are no no-school dates to clear.")
        return

    count = len(existing)
    data["no_school_dates"] = []
    save_data(data)

    await update.message.reply_text(f"✅ All no-school dates cleared. ({count} days removed)")

    # Notify drivers
    drivers = [drv for drv in data.get("drivers", {}).values() if drv.get("active", True)]
    if drivers:
        msg = (
            "📚 All previous no-school days were cleared.\n"
            "🚗 Please follow the normal school schedule."
        )
        for drv in drivers:
            try:
                await context.bot.send_message(chat_id=drv["id"], text=msg)
            except Exception:
                continue


# ---------- Menu handlers ----------

async def admin_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Handle admin text buttons & quick trip (e.g., '70 Dubai Mall'),
    and no-school pick date input.
    """
    data = load_data()
    user = update.effective_user
    chat = update.effective_chat
    if not user or not is_admin(user.id):
        return

    txt = (update.message.text or "").strip()

    # Are we waiting for a no-school date from this admin?
    if chat and chat.id in data.get("awaiting_noschool_date", []):
        try:
            d = parse_date_str(txt)
        except Exception:
            await update.message.reply_text("Please send date as YYYY-MM-DD.\nExample: 2025-12-02")
            return

        # Stop waiting
        data["awaiting_noschool_date"] = [
            cid for cid in data["awaiting_noschool_date"] if cid != chat.id
        ]
        save_data(data)

        context.args = [format_date(d)]
        await noschool_cmd(update, context)

        await update.message.reply_text("Back to main No School menu.", reply_markup=noschool_keyboard())
        return

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
        await update.message.reply_text(drivers_list_text(data))
        return
    if txt == BTN_DRIVERS_ADD:
        await update.message.reply_text("Use /adddriver <telegram_id> <name>")
        return
    if txt == BTN_DRIVERS_REMOVE:
        await update.message.reply_text("Use /removedriver <driver_code> (ID or SID)")
        return
    if txt == BTN_DRIVERS_SET_PRIMARY:
        await update.message.reply_text("Use /setprimarydriver <driver_code> (ID or SID)")
        return
    if txt == BTN_NOSCHOOL_MENU:
        await update.message.reply_text("🏫 No School:", reply_markup=noschool_keyboard())
        return
    if txt == BTN_NOSCHOOL_TODAY:
        context.args = ["today"]
        await noschool_cmd(update, context)
        return
    if txt == BTN_NOSCHOOL_TOMORROW:
        context.args = ["tomorrow"]
        await noschool_cmd(update, context)
        return
    if txt == BTN_NOSCHOOL_PICKDATE:
        if chat and chat.id not in data["awaiting_noschool_date"]:
            data["awaiting_noschool_date"].append(chat.id)
            save_data(data)
        await update.message.reply_text(
            "📅 Send the date as YYYY-MM-DD for no school.\nExample: 2025-12-02"
        )
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

# ---------- AI ----------
from openai import OpenAI
client = OpenAI()

async def ai_chat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_text = update.message.text.replace("/ai", "").strip()

    if not user_text:
        await update.message.reply_text(
            "🧠 Send your question after /ai.\nExample:\n/ai explain my weekly report"
        )
        return

    completion = client.chat.completions.create(
        model="gpt-5.1-mini",
        messages=[
            {"role": "system",
             "content": "You are an assistant for Faisal's driver bot. "
                        "Explain things clearly and simply."},
            {"role": "user", "content": user_text}
        ],
    )

    await update.message.reply_text(completion.choices[0].message.content)

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
    app.add_handler(CommandHandler("setdriverbase", setdriverbase_cmd))
    app.add_handler(CommandHandler("removedriver", removedriver_cmd))
    app.add_handler(CommandHandler("drivers", drivers_cmd))
    app.add_handler(CommandHandler("setprimarydriver", setprimarydriver_cmd))

    app.add_handler(CommandHandler("trip", trip_cmd))
    app.add_handler(CommandHandler("tripfor", tripfor_cmd))
    app.add_handler(CommandHandler("list", list_trips_cmd))
    app.add_handler(CommandHandler("listunpaid", listunpaid_cmd))
    app.add_handler(CommandHandler("report", report_cmd))
    app.add_handler(CommandHandler("paydriver", paydriver_cmd))
    app.add_handler(CommandHandler("paid", paid_cmd))
    app.add_handler(CommandHandler("export", export_cmd))
    app.add_handler(CommandHandler("cleartrips", cleartrips_cmd))
    app.add_handler(CommandHandler("test_on", test_on_cmd))
    app.add_handler(CommandHandler("test_off", test_off_cmd))
    app.add_handler(CommandHandler("noschool", noschool_cmd))
    app.add_handler(CommandHandler("removeschool", removeschool_cmd))
    app.add_handler(CommandHandler("clearnoschool", clearnoschool_cmd))
    application.add_handler(CommandHandler("ai", ai_chat))

    # Text handlers
    # Admin buttons / quick trips
    app.add_handler(
        MessageHandler(
            filters.TEXT & (~filters.COMMAND) & filters.User(user_id=ALLOWED_ADMINS),
            admin_menu_handler,
        )
    )
    # Driver buttons
    app.add_handler(
        MessageHandler(
            filters.TEXT & (~filters.COMMAND),
            driver_menu_handler,
        )
    )

    # Disable signal handling (for Render)
    app.run_polling(stop_signals=())


if __name__ == "__main__":
    main()
