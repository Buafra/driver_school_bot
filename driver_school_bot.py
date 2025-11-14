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
