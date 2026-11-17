"""
Jay Assistant - Calendar & Reminders Manager
Handles persistent reminders, active background alerting with audio & Windows toast,
natural-language time/date parsing, and optional Google Calendar integration.
"""

import os
import sys
import re
import time
import sqlite3
import logging
import threading
import subprocess
import base64
import datetime
from zoneinfo import ZoneInfo
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple

from dateutil import parser as date_parser

log = logging.getLogger(__name__)
INDIA_TZ = ZoneInfo("Asia/Kolkata")

# Ensure data directory exists
BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "reminders_calendar.db"

CREDENTIALS_FILE = BASE_DIR / "credentials.json"
TOKEN_FILE = BASE_DIR / "token.json"

# Google Calendar OAuth Scopes
GCAL_SCOPES = ["https://www.googleapis.com/auth/calendar"]


# =====================================================================
# TIME & DATE UTILITIES
# =====================================================================

def get_current_time_str() -> str:
    """Returns human-readable time string (e.g. '12:30 PM')."""
    return datetime.datetime.now().strftime("%I:%M %p").lstrip("0")

def get_current_date_str() -> str:
    """Returns human-readable date string (e.g. 'Saturday, November 14, 2026')."""
    return datetime.datetime.now().strftime("%A, %B %d, %Y")

def get_current_datetime_info() -> Dict[str, Any]:
    """Returns a dictionary with comprehensive time and date information."""
    now = datetime.datetime.now()
    return {
        "time_12h": now.strftime("%I:%M %p").lstrip("0"),
        "time_24h": now.strftime("%H:%M"),
        "date": now.strftime("%A, %B %d, %Y"),
        "day": now.strftime("%A"),
        "day_of_month": now.day,
        "month": now.strftime("%B"),
        "year": now.year,
        "iso": now.isoformat(),
        "summary": f"It is currently {now.strftime('%I:%M %p').lstrip('0')} on {now.strftime('%A, %B %d, %Y')}."
    }


# =====================================================================
# NATURAL LANGUAGE DATE & TIME PARSER
# =====================================================================

def parse_natural_time(text: str, default_time: Optional[datetime.datetime] = None) -> Tuple[Optional[datetime.datetime], str]:
    """
    Parses a natural-language time string into a datetime object and returns (datetime, remaining_clean_title).
    Handles:
      - "in 10 minutes", "in 1 hour", "in 30 seconds", "in 2 hours and 15 minutes"
      - "at 5 pm", "at 5:30", "at 9 am", "at noon", "at midnight"
      - "tomorrow at 10 am", "tomorrow morning", "tomorrow evening at 6"
      - "tonight at 8 pm", "this evening at 7"
      - "on Monday at 3 pm", "next Friday at 4 pm"
      - "on November 20 at 3 pm", "on 2026-11-20 14:00"
    """
    now = default_time or datetime.datetime.now()
    clean_text = text.strip()
    
    # 1. Relative offsets: "in X minutes/hours/seconds/days"
    rel_match = re.search(r'\bin\s+(\d+)\s*(seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)\b', clean_text, re.IGNORECASE)
    if rel_match:
        qty = int(rel_match.group(1))
        unit = rel_match.group(2).lower()
        delta = datetime.timedelta()
        if "sec" in unit:
            delta = datetime.timedelta(seconds=qty)
        elif "min" in unit:
            delta = datetime.timedelta(minutes=qty)
        elif "hour" in unit or "hr" in unit:
            delta = datetime.timedelta(hours=qty)
        elif "day" in unit:
            delta = datetime.timedelta(days=qty)
        
        target_dt = now + delta
        # Strip matched string from text
        clean_title = re.sub(r'\bin\s+\d+\s*(?:seconds?|secs?|minutes?|mins?|hours?|hrs?|days?)\b', '', clean_text, flags=re.IGNORECASE).strip()
        clean_title = _clean_task_title(clean_title)
        return target_dt, clean_title

    # 2. "tomorrow [at] [time]"
    tomorrow_match = re.search(r'\btomorrow(?:\s+at)?\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?|\bnoon\b|\bmidnight\b|\bmorning\b|\bafternoon\b|\bevening\b|\bnight\b)?', clean_text, re.IGNORECASE)
    if tomorrow_match:
        time_part = tomorrow_match.group(1)
        base_date = (now + datetime.timedelta(days=1)).date()
        target_dt = _parse_time_of_day(time_part, base_date)
        clean_title = re.sub(r'\btomorrow(?:\s+at)?\s*(?:\d{1,2}(?::\d{2})?\s*(?:am|pm)?|\bnoon\b|\bmidnight\b|\bmorning\b|\bafternoon\b|\bevening\b|\bnight\b)?', '', clean_text, flags=re.IGNORECASE).strip()
        clean_title = _clean_task_title(clean_title)
        return target_dt, clean_title

    # 3. "tonight [at] [time]" or "this evening [at] [time]"
    tonight_match = re.search(r'\b(?:tonight|this evening)(?:\s+at)?\s*(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?', clean_text, re.IGNORECASE)
    if tonight_match:
        time_part = tonight_match.group(1)
        base_date = now.date()
        if time_part:
            target_dt = _parse_time_of_day(time_part, base_date)
        else:
            target_dt = datetime.datetime.combine(base_date, datetime.time(20, 0)) # Default 8 PM
        clean_title = re.sub(r'\b(?:tonight|this evening)(?:\s+at)?\s*(?:\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?', '', clean_text, flags=re.IGNORECASE).strip()
        clean_title = _clean_task_title(clean_title)
        return target_dt, clean_title

    # 4. "on [weekday] [at] [time]" or "next [weekday] [at] [time]"
    weekday_match = re.search(r'\b(?:on\s+|next\s+)?(monday|tuesday|wednesday|thursday|friday|saturday|sunday)(?:\s+at)?\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?', clean_text, re.IGNORECASE)
    if weekday_match:
        day_name = weekday_match.group(1).lower()
        time_part = weekday_match.group(2)
        target_date = _get_next_weekday(now, day_name)
        target_dt = _parse_time_of_day(time_part, target_date)
        clean_title = re.sub(r'\b(?:on\s+|next\s+)?(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday)(?:\s+at)?\s*(?:\d{1,2}(?::\d{2})?\s*(?:am|pm)?)?', '', clean_text, flags=re.IGNORECASE).strip()
        clean_title = _clean_task_title(clean_title)
        return target_dt, clean_title

    # 5. "at [time]" e.g. "at 5 pm", "at 4:30 pm", "at 18:00"
    at_match = re.search(r'\bat\s+(\d{1,2}(?::\d{2})?\s*(?:am|pm)?|\bnoon\b|\bmidnight\b)\b', clean_text, re.IGNORECASE)
    if at_match:
        time_part = at_match.group(1)
        target_dt = _parse_time_of_day(time_part, now.date())
        # If the target time today has already passed, schedule for tomorrow
        if target_dt <= now:
            target_dt += datetime.timedelta(days=1)
        clean_title = re.sub(r'\bat\s+(?:\d{1,2}(?::\d{2})?\s*(?:am|pm)?|\bnoon\b|\bmidnight\b)\b', '', clean_text, flags=re.IGNORECASE).strip()
        clean_title = _clean_task_title(clean_title)
        return target_dt, clean_title

    # 6. Fallback: try dateutil parser
    try:
        parsed_dt = date_parser.parse(clean_text, fuzzy=True, default=now)
        if parsed_dt > now:
            clean_title = _clean_task_title(clean_text)
            return parsed_dt, clean_title
    except Exception:
        pass

    return None, _clean_task_title(clean_text)

def _parse_time_of_day(time_str: Optional[str], target_date: datetime.date) -> datetime.datetime:
    """Helper to convert time string into full datetime on given date."""
    if not time_str:
        return datetime.datetime.combine(target_date, datetime.time(9, 0)) # Default 9 AM
    
    t_clean = time_str.strip().lower()
    if t_clean == "noon":
        return datetime.datetime.combine(target_date, datetime.time(12, 0))
    if t_clean == "midnight":
        return datetime.datetime.combine(target_date, datetime.time(0, 0))
    if t_clean == "morning":
        return datetime.datetime.combine(target_date, datetime.time(9, 0))
    if t_clean == "afternoon":
        return datetime.datetime.combine(target_date, datetime.time(14, 0))
    if t_clean == "evening":
        return datetime.datetime.combine(target_date, datetime.time(18, 0))
    if t_clean == "night":
        return datetime.datetime.combine(target_date, datetime.time(20, 0))

    try:
        # Check if am/pm present
        is_pm = "pm" in t_clean
        is_am = "am" in t_clean
        num_part = re.sub(r'[^\d:]', '', t_clean)
        
        if ":" in num_part:
            parts = num_part.split(":")
            h, m = int(parts[0]), int(parts[1])
        else:
            h, m = int(num_part), 0

        if is_pm and h < 12:
            h += 12
        elif is_am and h == 12:
            h = 0
        elif not is_am and not is_pm:
            # Ambiguous single number e.g. "at 5" -> assume PM if 1 <= h <= 6, else AM
            if 1 <= h <= 6:
                h += 12

        return datetime.datetime.combine(target_date, datetime.time(h, m))
    except Exception:
        return datetime.datetime.combine(target_date, datetime.time(9, 0))

def _get_next_weekday(now: datetime.datetime, weekday_name: str) -> datetime.date:
    """Returns the date of the next given weekday (Monday=0 ... Sunday=6)."""
    days = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
    target_idx = days.index(weekday_name.lower())
    current_idx = now.weekday()
    diff = (target_idx - current_idx) % 7
    if diff == 0:
        diff = 7  # Next week
    return (now + datetime.timedelta(days=diff)).date()

def _clean_task_title(text: str) -> str:
    """Strips common prefix and filler words from reminder task title."""
    clean = text.strip()
    # Strip leading prefixes
    prefixes = [
        "add an event to calendar: ", "add an event to calendar ",
        "add event to calendar: ", "add event to calendar ",
        "add to calendar: ", "add to calendar ",
        "add an event: ", "add an event ", "add event: ", "add event ",
        "create an event: ", "create an event ", "create event: ", "create event ",
        "remind me to ", "remind me ", "set a reminder to ", "set a reminder for ",
        "set reminder to ", "set reminder for ", "add reminder to ", "reminder to ",
        "schedule a meeting with ", "schedule a meeting to ", "schedule meeting ",
        "to "
    ]
    for pfx in prefixes:
        if clean.lower().startswith(pfx):
            clean = clean[len(pfx):].strip()
            break

    if clean.lower().startswith("to "):
        clean = clean[3:].strip()
    
    # Strip any leading colon
    clean = clean.lstrip(": ").strip()
    
    # Strip trailing punctuation
    clean = clean.rstrip(".,;!? ")
    return clean if clean else "Reminder"


# =====================================================================
# WINDOWS TOAST NOTIFICATION
# =====================================================================

def show_desktop_notification(title: str, message: str) -> bool:
    """Displays a native Windows toast notification via PowerShell."""
    try:
        # Sanitize single quotes
        safe_title = title.replace("'", "''")
        safe_msg = message.replace("'", "''")
        
        script = f"""[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null
$template = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$textNodes = $template.GetElementsByTagName('text')
$textNodes.Item(0).AppendChild($template.CreateTextNode('{safe_title}')) | Out-Null
$textNodes.Item(1).AppendChild($template.CreateTextNode('{safe_msg}')) | Out-Null
$toast = [Windows.UI.Notifications.ToastNotification]::new($template)
$notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('Jay Assistant')
$notifier.Show($toast)
"""
        encoded = base64.b64encode(script.encode('utf-16le')).decode('utf-8')
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
            capture_output=True,
            timeout=4
        )
        return True
    except Exception as e:
        log.warning(f"Could not show Windows toast notification: {e}")
        return False


# =====================================================================
# GOOGLE CALENDAR CLIENT
# =====================================================================

class GoogleCalendarClient:
    """Handles OAuth authentication, event listing, and creation with Google Calendar."""
    
    def __init__(self, credentials_path: Path = CREDENTIALS_FILE, token_path: Path = TOKEN_FILE):
        self.credentials_path = credentials_path
        self.token_path = token_path
        self._service = None
        self._lock = threading.Lock()

    def is_configured(self) -> bool:
        """Returns True if credentials.json or token.json is present."""
        return self.credentials_path.exists() or self.token_path.exists()

    def is_connected(self) -> bool:
        """Returns True if authenticated with a valid token."""
        return self.token_path.exists()

    def get_service(self):
        """Returns authenticated Google Calendar service resource, or None."""
        with self._lock:
            if self._service is not None:
                return self._service
            
            try:
                from google.oauth2.credentials import Credentials
                from google.auth.transport.requests import Request
                from googleapiclient.discovery import build

                creds = None
                if self.token_path.exists():
                    creds = Credentials.from_authorized_user_file(str(self.token_path), GCAL_SCOPES)
                
                if not creds or not creds.valid:
                    if creds and creds.expired and creds.refresh_token:
                        creds.refresh(Request())
                        with open(self.token_path, "w", encoding="utf-8") as token_f:
                            token_f.write(creds.to_json())
                    elif self.credentials_path.exists():
                        from google_auth_oauthlib.flow import InstalledAppFlow
                        flow = InstalledAppFlow.from_client_secrets_file(str(self.credentials_path), GCAL_SCOPES)
                        creds = flow.run_local_server(port=0)
                        with open(self.token_path, "w", encoding="utf-8") as token_f:
                            token_f.write(creds.to_json())
                    else:
                        return None
                
                self._service = build("calendar", "v3", credentials=creds)
                return self._service
            except Exception as e:
                log.warning(f"Google Calendar service initialization failed: {e}")
                return None

    def list_upcoming_events(self, max_results: int = 10, time_min: Optional[datetime.datetime] = None) -> List[Dict[str, Any]]:
        """Fetches upcoming events from the primary calendar."""
        service = self.get_service()
        if not service:
            return []
        
        try:
            t_min = (time_min or datetime.datetime.now()).isoformat() + "Z"
            events_result = service.events().list(
                calendarId="primary",
                timeMin=t_min,
                maxResults=max_results,
                singleEvents=True,
                orderBy="startTime"
            ).execute()
            return events_result.get("items", [])
        except Exception as e:
            log.error(f"Error fetching Google Calendar events: {e}")
            return []

    def create_event(self, title: str, start_dt: datetime.datetime, end_dt: Optional[datetime.datetime] = None,
                     description: str = "", location: str = "") -> Optional[Dict[str, Any]]:
        """Creates an event on the user's primary Google Calendar."""
        service = self.get_service()
        if not service:
            return None

        if end_dt is None:
            end_dt = start_dt + datetime.timedelta(hours=1)

        # Treat naive values as India local time; normalize aware values to India.
        start_dt = start_dt.replace(tzinfo=INDIA_TZ) if start_dt.tzinfo is None else start_dt.astimezone(INDIA_TZ)
        end_dt = end_dt.replace(tzinfo=INDIA_TZ) if end_dt.tzinfo is None else end_dt.astimezone(INDIA_TZ)
        if end_dt <= start_dt:
            raise ValueError("Event end time must be later than its start time.")

        event_body = {
            "summary": title,
            "description": description or f"Created by Jay Assistant on {datetime.datetime.now().strftime('%Y-%m-%d')}",
            "location": location,
            "start": {
                "dateTime": start_dt.isoformat(timespec="seconds"),
                "timeZone": "Asia/Kolkata"
            },
            "end": {
                "dateTime": end_dt.isoformat(timespec="seconds"),
                "timeZone": "Asia/Kolkata"
            },
            "reminders": {
                "useDefault": True
            }
        }
        
        try:
            created = service.events().insert(calendarId="primary", body=event_body).execute()
            return created
        except Exception as e:
            log.error(f"Error creating Google Calendar event: {e}")
            return None


# =====================================================================
# CALENDAR & REMINDER MANAGER (DATABASE & ACTIVE WORKER)
# =====================================================================

class CalendarManager:
    """Manages persistent SQLite storage, background alert worker, and Google Calendar sync."""
    
    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self.gcal = GoogleCalendarClient()
        self._db_lock = threading.Lock()
        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None
        self._init_db()

    def _init_db(self):
        """Initializes tables in the SQLite database."""
        with self._db_lock:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            
            cur.execute("""
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT DEFAULT '',
                    due_time TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    notified INTEGER DEFAULT 0,
                    synced_to_gcal INTEGER DEFAULT 0,
                    gcal_event_id TEXT DEFAULT ''
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    end_time TEXT,
                    location TEXT DEFAULT '',
                    description TEXT DEFAULT '',
                    created_at TEXT NOT NULL,
                    synced_to_gcal INTEGER DEFAULT 0,
                    gcal_event_id TEXT DEFAULT ''
                )
            """)
            
            conn.commit()
            conn.close()

    # -----------------------------------------------------------------
    # Background Reminder Worker
    # -----------------------------------------------------------------

    def start_reminder_daemon(self):
        """Starts the active background thread that checks for due reminders."""
        if self._worker_thread and self._worker_thread.is_alive():
            return
        
        self._stop_event.clear()
        self._worker_thread = threading.Thread(target=self._reminder_loop, name="JayReminderDaemon", daemon=True)
        self._worker_thread.start()
        log.info("Jay Reminder Daemon active")

    def stop_reminder_daemon(self):
        """Stops the reminder daemon."""
        self._stop_event.set()
        if self._worker_thread and self._worker_thread.is_alive():
            self._worker_thread.join(timeout=2.0)
        self._worker_thread = None

    def _reminder_loop(self):
        """Continuous background loop checking every 2 seconds for due reminders."""
        while not self._stop_event.is_set():
            try:
                self._check_and_trigger_due_reminders()
            except Exception as e:
                log.error(f"Error in reminder loop: {e}")
            self._stop_event.wait(timeout=2.0)

    def _check_and_trigger_due_reminders(self):
        """Finds reminders that have reached their due_time and alerts the user."""
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        due_items = []

        with self._db_lock:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("""
                SELECT id, title, description, due_time FROM reminders
                WHERE status = 'pending' AND notified = 0 AND due_time <= ?
            """, (now_str,))
            due_items = cur.fetchall()

            if due_items:
                ids = [item[0] for item in due_items]
                cur.execute(f"""
                    UPDATE reminders SET notified = 1, status = 'completed'
                    WHERE id IN ({','.join(['?']*len(ids))})
                """, ids)
                conn.commit()
            conn.close()

        for item_id, title, desc, due_t in due_items:
            log.info(f"[REMINDER ALERT] Triggering reminder {item_id}: {title}")
            # 1. Desktop Toast Notification
            show_desktop_notification("Jay Reminder", f"{title}")
            
            # 2. Spoken Alert in background thread so it never blocks the loop
            threading.Thread(target=self._speak_reminder, args=(title,), daemon=True).start()

    def _speak_reminder(self, title: str):
        """Asynchronously plays chime and speaks the reminder."""
        try:
            from audio_engine import speak, play_chime
            play_chime("notification")
            speak(f"Excuse me, here is your reminder: {title}!")
        except Exception as e:
            log.warning(f"Could not speak reminder: {e}")

    # -----------------------------------------------------------------
    # Reminder CRUD Operations
    # -----------------------------------------------------------------

    def add_reminder(self, title: str, due_datetime: datetime.datetime, description: str = "") -> Tuple[bool, str, int]:
        """
        Adds a reminder to the persistent database.
        Optionally syncs to Google Calendar if connected.
        """
        if not self.gcal.is_connected():
            return False, "Reminder was not saved because Google Calendar is not connected.", -1
        try:
            gcal_event = self.gcal.create_event(
                title=f"[Reminder] {title}",
                start_dt=due_datetime,
                end_dt=due_datetime + datetime.timedelta(minutes=15),
                description=description or "Scheduled reminder via Jay Assistant"
            )
        except Exception as e:
            log.warning(f"Failed to sync reminder to Google Calendar: {e}")
            gcal_event = None
        if not gcal_event or "id" not in gcal_event:
            return False, "Reminder was not saved because Google Calendar could not create it.", -1

        due_str = due_datetime.strftime("%Y-%m-%d %H:%M:%S")
        created_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with self._db_lock:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO reminders (title, description, due_time, created_at, status, notified,
                                      synced_to_gcal, gcal_event_id)
                VALUES (?, ?, ?, ?, 'pending', 0, 1, ?)
            """, (title, description, due_str, created_str, gcal_event["id"]))
            reminder_id = cur.lastrowid
            conn.commit()
            conn.close()

        readable_time = due_datetime.strftime("%I:%M %p").lstrip("0")
        readable_date = due_datetime.strftime("%A, %B %d")
        
        msg = f"Reminder set for {title} at {readable_time} on {readable_date} and synced to your Google Calendar."
        return True, msg, reminder_id

    def list_reminders(self, filter_type: str = "upcoming") -> List[Dict[str, Any]]:
        """
        Lists reminders from the database.
        filter_type: 'upcoming' (pending & due in future), 'today', or 'all'
        """
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        today_date = datetime.date.today().strftime("%Y-%m-%d")
        
        query = "SELECT id, title, description, due_time, status, notified, synced_to_gcal FROM reminders"
        params = []
        
        if filter_type == "upcoming":
            query += " WHERE status = 'pending' AND due_time >= ? ORDER BY due_time ASC"
            params.append(now_str)
        elif filter_type == "today":
            query += " WHERE due_time LIKE ? ORDER BY due_time ASC"
            params.append(f"{today_date}%")
        else:
            query += " ORDER BY due_time DESC"

        results = []
        with self._db_lock:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute(query, params)
            rows = cur.fetchall()
            conn.close()

            for r in rows:
                results.append({
                    "id": r[0],
                    "title": r[1],
                    "description": r[2],
                    "due_time": r[3],
                    "status": r[4],
                    "notified": bool(r[5]),
                    "synced_gcal": bool(r[6])
                })
        return results

    def clear_all_reminders(self) -> Tuple[bool, str]:
        """Clears all completed or pending reminders."""
        with self._db_lock:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("DELETE FROM reminders")
            conn.commit()
            conn.close()
        return True, "All reminders have been cleared."

    def delete_reminder_by_title_or_id(self, query: str) -> Tuple[bool, str]:
        """Deletes a reminder matching an ID or keyword."""
        with self._db_lock:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            
            # Check if query is integer ID
            if query.strip().isdigit():
                cur.execute("DELETE FROM reminders WHERE id = ?", (int(query.strip()),))
            else:
                cur.execute("DELETE FROM reminders WHERE title LIKE ?", (f"%{query.strip()}%",))
            
            deleted_count = cur.rowcount
            conn.commit()
            conn.close()

        if deleted_count > 0:
            return True, f"Deleted {deleted_count} reminder(s) matching '{query}'."
        return False, f"No reminders found matching '{query}'."

    # -----------------------------------------------------------------
    # Calendar Event Operations
    # -----------------------------------------------------------------

    def add_event(self, title: str, start_dt: datetime.datetime, end_dt: Optional[datetime.datetime] = None,
                  location: str = "", description: str = "") -> Tuple[bool, str]:
        """Adds a calendar event to local DB and Google Calendar if connected."""
        if end_dt is None:
            end_dt = start_dt + datetime.timedelta(hours=1)

        start_str = start_dt.strftime("%Y-%m-%d %H:%M:%S")
        end_str = end_dt.strftime("%Y-%m-%d %H:%M:%S")
        created_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        event_id = -1
        with self._db_lock:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            cur.execute("""
                INSERT INTO events (title, start_time, end_time, location, description, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (title, start_str, end_str, location, description, created_str))
            event_id = cur.lastrowid
            conn.commit()
            conn.close()

        gcal_synced = False
        if self.gcal.is_connected():
            try:
                gcal_res = self.gcal.create_event(title, start_dt, end_dt, description, location)
                if gcal_res and "id" in gcal_res:
                    gcal_synced = True
                    with self._db_lock:
                        conn = sqlite3.connect(self.db_path)
                        cur = conn.cursor()
                        cur.execute("UPDATE events SET synced_to_gcal = 1, gcal_event_id = ? WHERE id = ?",
                                    (gcal_res["id"], event_id))
                        conn.commit()
                        conn.close()
            except Exception as e:
                log.warning(f"Could not add event to Google Calendar: {e}")

        readable_start = start_dt.strftime("%I:%M %p").lstrip("0")
        readable_date = start_dt.strftime("%A, %B %d")
        sync_note = " and added to your Google Calendar" if gcal_synced else ""
        msg = f"Event '{title}' scheduled for {readable_start} on {readable_date}{sync_note}."
        return True, msg

    def list_events(self, date_query: str = "today") -> List[Dict[str, Any]]:
        """
        Lists events for a given period ('today', 'tomorrow', 'upcoming', 'all').
        Merges local events and live Google Calendar events.
        """
        results = []
        now = datetime.datetime.now()
        
        # 1. Fetch from Google Calendar if connected
        if self.gcal.is_connected():
            gcal_items = self.gcal.list_upcoming_events(max_results=15)
            for item in gcal_items:
                start_val = item.get("start", {}).get("dateTime") or item.get("start", {}).get("date")
                results.append({
                    "id": item.get("id"),
                    "title": item.get("summary", "Untitled Event"),
                    "start_time": start_val,
                    "location": item.get("location", ""),
                    "source": "Google Calendar"
                })

        # 2. Fetch from local database
        with self._db_lock:
            conn = sqlite3.connect(self.db_path)
            cur = conn.cursor()
            
            if date_query == "today":
                d_str = now.strftime("%Y-%m-%d")
                cur.execute("SELECT id, title, start_time, end_time, location FROM events WHERE start_time LIKE ? ORDER BY start_time ASC", (f"{d_str}%",))
            elif date_query == "tomorrow":
                d_str = (now + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
                cur.execute("SELECT id, title, start_time, end_time, location FROM events WHERE start_time LIKE ? ORDER BY start_time ASC", (f"{d_str}%",))
            else:
                cur.execute("SELECT id, title, start_time, end_time, location FROM events ORDER BY start_time ASC LIMIT 20")
            
            rows = cur.fetchall()
            conn.close()

            for r in rows:
                results.append({
                    "id": r[0],
                    "title": r[1],
                    "start_time": r[2],
                    "end_time": r[3],
                    "location": r[4],
                    "source": "Local Calendar"
                })

        return results

    def get_status(self) -> Dict[str, Any]:
        """Returns structured status of calendar and reminders."""
        upcoming_reminders = self.list_reminders("upcoming")
        today_events = self.list_events("today")
        return {
            "gcal_configured": self.gcal.is_configured(),
            "gcal_connected": self.gcal.is_connected(),
            "pending_reminders_count": len(upcoming_reminders),
            "today_events_count": len(today_events),
            "daemon_active": self._worker_thread is not None and self._worker_thread.is_alive()
        }


# Global singleton instance
calendar_manager = CalendarManager()
