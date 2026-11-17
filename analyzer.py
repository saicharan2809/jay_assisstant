import datetime
import actions
from audio_engine import speak

def analyze_and_route_request(command):
    # 0. Clean & normalize command (remove 'Jay,', 'Hey Jay,', 'Please', trailing punctuation)
    cmd = command.lower().strip()
    for prefix in ["hey jay,", "hey jay ", "ok jay,", "ok jay ", "jay,", "jay ", "please ", "can you ", "could you "]:
        if cmd.startswith(prefix):
            cmd = cmd[len(prefix):].strip()
    cmd = cmd.strip(".?!,;:")

    # 1. Direct System Audio Controls
    if any(w in cmd for w in ["volume up", "increase volume", "louder"]):
        actions.volume_up()
        return True, "EXECUTED_SIMPLE"

    elif any(w in cmd for w in ["volume down", "decrease volume", "quieter"]):
        actions.volume_down()
        return True, "EXECUTED_SIMPLE"

    elif any(w in cmd for w in ["mute", "silence"]):
        actions.mute_volume()
        return True, "EXECUTED_SIMPLE"

    # Screen Capture
    elif "screenshot" in cmd or "capture screen" in cmd:
        actions.take_screenshot()
        return True, "EXECUTED_SIMPLE"

    # Hardware & System Telemetry Diagnostics
    elif any(w in cmd for w in ["check battery", "battery level", "battery status", "battery"]):
        actions.check_battery()
        return True, "EXECUTED_SIMPLE"

    elif any(w in cmd for w in ["check cpu", "cpu usage", "processor"]):
        actions.check_cpu()
        return True, "EXECUTED_SIMPLE"

    elif any(w in cmd for w in ["check memory", "ram usage", "memory status"]):
        actions.check_memory()
        return True, "EXECUTED_SIMPLE"

    elif any(w in cmd for w in ["check disk", "disk space", "storage"]):
        actions.check_disk()
        return True, "EXECUTED_SIMPLE"

    elif any(w in cmd for w in ["analyze system", "system analysis", "system status"]):
        actions.analyze_system()
        return True, "EXECUTED_SIMPLE"

    # Power Controls
    elif "shutdown pc" in cmd or "shut down pc" in cmd:
        actions.shutdown_pc()
        return True, "EXECUTED_SIMPLE"

    elif "restart pc" in cmd or "reboot pc" in cmd:
        actions.restart_pc()
        return True, "EXECUTED_SIMPLE"

    elif "sleep pc" in cmd or "hibernate" in cmd:
        actions.sleep_pc()
        return True, "EXECUTED_SIMPLE"

    # Mouse & Screen Controls
    elif cmd.startswith("click on ") or cmd.startswith("click the ") or cmd.startswith("click "):
        target = cmd.replace("click on ", "").replace("click the ", "").replace("click ", "").strip()
        if target in ["", "here", "left"]:
            actions.mouse_click("left")
        elif target == "right":
            actions.mouse_click("right")
        elif target == "double":
            actions.mouse_click("double")
        else:
            actions.click_element_on_screen(target)
        return True, "EXECUTED_SIMPLE"

    elif "double click" in cmd:
        actions.mouse_click("double")
        return True, "EXECUTED_SIMPLE"

    elif "right click" in cmd:
        actions.mouse_click("right")
        return True, "EXECUTED_SIMPLE"

    elif cmd in ["press enter", "hit enter", "submit", "press return"]:
        actions.press_hotkey("enter")
        return True, "EXECUTED_SIMPLE"

    elif cmd in ["press escape", "press esc"]:
        actions.press_hotkey("esc")
        return True, "EXECUTED_SIMPLE"

    elif cmd in ["press tab"]:
        actions.press_hotkey("tab")
        return True, "EXECUTED_SIMPLE"

    elif "scroll down" in cmd:
        actions.scroll_screen("down")
        return True, "EXECUTED_SIMPLE"

    elif "scroll up" in cmd:
        actions.scroll_screen("up")
        return True, "EXECUTED_SIMPLE"

    # Time, Date, Reminders & Calendar
    elif any(w in cmd for w in ["time and date", "date and time", "what is the date and time", "what is the time and date", "tell me the time and date", "tell me the date and time"]):
        actions.tell_current_datetime()
        return True, "EXECUTED_SIMPLE"

    elif any(w in cmd for w in ["what time", "current time", "what's the time", "tell me the time", "time is it", "the time please", "exact time", "what time is it"]) or cmd in ["time", "time now", "tell time"]:
        actions.tell_current_time()
        return True, "EXECUTED_SIMPLE"

    elif any(w in cmd for w in ["what date", "today's date", "todays date", "current date", "what's the date", "what is the date", "tell me the date", "what day is today", "what day of the week", "what day is it", "which day is it"]) or cmd in ["date", "what day", "today's date", "what is today"]:
        actions.tell_current_date()
        return True, "EXECUTED_SIMPLE"

    # Reminders
    elif any(w in cmd for w in ["list my reminders", "show my reminders", "what are my reminders", "do i have any reminders", "check my reminders", "show reminders", "list reminders", "my reminders", "upcoming reminders"]):
        actions.show_reminders("upcoming")
        return True, "EXECUTED_SIMPLE"

    elif any(w in cmd for w in ["clear my reminders", "clear all reminders", "delete all reminders", "clear reminders", "delete my reminders"]):
        actions.clear_all_reminders()
        return True, "EXECUTED_SIMPLE"

    elif cmd.startswith("remind me") or cmd.startswith("set a reminder") or cmd.startswith("set reminder") or "remind me to" in cmd or "remind me in" in cmd:
        actions.create_reminder(cmd)
        return True, "EXECUTED_SIMPLE"

    # Calendar
    elif any(w in cmd for w in ["connect calendar", "connect google calendar", "link calendar", "sync calendar", "connect my calendar", "connect to my calendar"]):
        actions.connect_google_calendar()
        return True, "EXECUTED_SIMPLE"

    elif any(w in cmd for w in ["calendar status", "check calendar status", "calendar overview"]):
        actions.get_calendar_overview()
        return True, "EXECUTED_SIMPLE"

    elif any(w in cmd for w in ["open calendar", "open my calendar", "view calendar", "view my calendar", "launch calendar", "show calendar", "show my calendar", "open google calendar", "open windows calendar"]):
        svc = "windows" if "windows" in cmd else "auto"
        actions.open_calendar_app(svc)
        return True, "EXECUTED_SIMPLE"

    elif any(w in cmd for w in ["what's on my calendar", "whats on my calendar", "what is on my calendar", "check my calendar", "my schedule for today", "my schedule today", "what's my schedule", "calendar today", "calendar tomorrow", "events today", "events tomorrow"]):
        date_q = "tomorrow" if "tomorrow" in cmd else "today"
        actions.show_calendar_schedule(date_q)
        return True, "EXECUTED_SIMPLE"

    elif any(w in cmd for w in ["add event to calendar", "add to calendar", "add an event to calendar", "schedule a meeting", "schedule an event"]):
        actions.create_calendar_event(title=cmd, start_time_str=cmd)
        return True, "EXECUTED_SIMPLE"

    # Screen Vision (EDITH)
    elif any(w in cmd for w in ["look at my screen", "inspect screen", "what is on my screen", "what's on my screen", "read my screen", "analyze screen", "look at screen"]):
        if any(conj in cmd for conj in [" and ", " then ", " after "]):
            return False, "GOAL_BASED_REQUEST"
        actions.inspect_screen(cmd)
        return True, "EXECUTED_SIMPLE"

    # Compound multi-step goal check: if command contains chained actions (play, compare, order, select, click, etc.), route to autonomous agent
    elif any(marker in cmd for marker in [
        " and play", " and watch", " and compare", " and order", " and buy", " and click",
        " and select", " and find", " and check", " and choose", " and add", " then ",
        " after that ", " to play", " to find", " to buy", " to order", " to compare"
    ]) or cmd.count(" and ") >= 2 or (", and " in cmd) or (" latest " in cmd and any(k in cmd for k in ["play", "watch", "open", "show"])):
        return False, "GOAL_BASED_REQUEST"

    # Compound Command: "open [app] and search for [query]"
    elif ("open" in cmd or "launch" in cmd) and ("search for" in cmd or "search" in cmd):
        target_app = "google"
        query = ""
        if "search for " in cmd:
            parts = cmd.split("search for ", 1)
            query = parts[1].strip()
            for app in ["chrome", "youtube", "amazon", "spotify", "browser", "comet", "edge", "google"]:
                if app in parts[0]:
                    target_app = app
                    break
        elif "search " in cmd:
            parts = cmd.split("search ", 1)
            query = parts[1].strip()
            for app in ["chrome", "youtube", "amazon", "spotify", "browser", "comet", "edge", "google"]:
                if app in parts[0]:
                    target_app = app
                    break
        if query:
            actions.search_in_app_or_browser(query, target_app)
            return True, "EXECUTED_SIMPLE"

    # Search on Specific Platform: "search for [query] in/on [app]"
    elif "search" in cmd and (" in " in cmd or " on " in cmd):
        splitter = " in " if " in " in cmd else " on "
        parts = cmd.split(splitter, 1)
        query = parts[0].replace("search for", "").replace("search", "").strip()
        app = parts[1].strip()
        actions.search_in_app_or_browser(query, app)
        return True, "EXECUTED_SIMPLE"

    # Live Knowledge & Encyclopedic Questions (e.g. "who is Einstein", "tell me about Mars")
    elif any(cmd.startswith(prefix) for prefix in ["who is ", "tell me about ", "search web for ", "live search "]):
        actions.search_web_live(cmd)
        return True, "EXECUTED_SIMPLE"

    # General Search: "search for [query]"
    elif cmd.startswith("search for ") or cmd.startswith("search "):
        query = cmd.replace("search for ", "", 1).replace("search ", "", 1).strip()
        actions.search_web_live(query)
        return True, "EXECUTED_SIMPLE"

    # Direct Web Sites
    elif cmd in ["open browser", "open comet", "launch browser"]:
        speak("Opening browser.")
        actions.open_browser_url()
        return True, "EXECUTED_SIMPLE"

    elif cmd in ["open youtube", "launch youtube"]:
        speak("Opening YouTube.")
        actions.open_browser_url("https://www.youtube.com")
        return True, "EXECUTED_SIMPLE"

    elif cmd in ["open google", "launch google"]:
        speak("Opening Google.")
        actions.open_browser_url("https://www.google.com")
        return True, "EXECUTED_SIMPLE"

    elif cmd in ["open amazon", "launch amazon"]:
        speak("Opening Amazon.")
        actions.open_browser_url("https://www.amazon.in")
        return True, "EXECUTED_SIMPLE"
    elif cmd in ["open spotify", "launch spotify"]:
        speak("Opening Spotify.")
        actions.open_browser_url("https://open.spotify.com")
        return True, "EXECUTED_SIMPLE"
    elif cmd in ["open myntra", "launch myntra"]:
        speak("Opening Myntra.")
        actions.open_browser_url("https://www.myntra.com")
        return True, "EXECUTED_SIMPLE"

    # Typing & Input Controls
    elif cmd.startswith("type ") and (" and press enter" in cmd or " and hit enter" in cmd or " and submit" in cmd):
        text = cmd.replace("type ", "", 1).replace(" and press enter", "").replace(" and hit enter", "").replace(" and submit", "").strip()
        actions.type_and_press_enter(text, press_enter=True)
        return True, "EXECUTED_SIMPLE"

    elif cmd.startswith("type ") or cmd.startswith("write "):
        text = cmd.replace("type ", "", 1).replace("write ", "", 1).strip()
        # If it's not a compound creative writing request, type it directly
        if not any(w in text for w in [" poem", " essay", " code", " story", " email to"]):
            actions.type_text_into_active_window(text)
            return True, "EXECUTED_SIMPLE"

    # Application Opening: "open [app]", "launch [app]", "start [app]"
    elif cmd.startswith("open ") or cmd.startswith("launch ") or cmd.startswith("start "):
        app = cmd.replace("open ", "", 1).replace("launch ", "", 1).replace("start ", "", 1).strip()
        # If it doesn't contain compound task conjunctions like "and then", execute directly
        if not any(conj in app for conj in [" and ", " then ", " with "]):
            actions.open_application(app)
            return True, "EXECUTED_SIMPLE"

    # Application Closing
    elif cmd.startswith("close ") or cmd.startswith("kill ") or cmd.startswith("exit "):
        app = cmd.replace("close ", "", 1).replace("kill ", "", 1).replace("exit ", "", 1).strip()
        actions.close_application(app)
        return True, "EXECUTED_SIMPLE"

    # Webcam Hand-Gesture Control (AirTouch)
    elif any(cmd == p or cmd.startswith(p) for p in [
        "enable gesture control", "enable gestures", "turn on gesture control", "turn on gestures",
        "start gesture control", "start gestures", "activate gesture control", "activate gestures",
        "airtouch on", "hand control on", "gesture mode on"
    ]):
        actions.enable_gesture_control()
        return True, "EXECUTED_SIMPLE"

    elif any(cmd == p or cmd.startswith(p) for p in [
        "disable gesture control", "disable gestures", "turn off gesture control", "turn off gestures",
        "stop gesture control", "stop gestures", "deactivate gesture control", "deactivate gestures",
        "airtouch off", "hand control off", "gesture mode off"
    ]):
        actions.disable_gesture_control()
        return True, "EXECUTED_SIMPLE"

    elif any(w in cmd for w in ["gesture status", "check gestures", "status of gestures", "is gesture control on"]):
        actions.check_gesture_status()
        return True, "EXECUTED_SIMPLE"

    elif any(cmd == p for p in ["arm gestures", "arm gesture control"]):
        actions.arm_gesture_control()
        return True, "EXECUTED_SIMPLE"

    elif any(cmd == p for p in ["disarm gestures", "disarm gesture control"]):
        actions.disarm_gesture_control()
        return True, "EXECUTED_SIMPLE"

    # Assistant Modes
    elif any(w in cmd for w in ["voice mode", "switch to voice", "activate voice", "start voice mode"]):
        return True, "VOICE_MODE"

    elif any(w in cmd for w in ["text mode", "switch to text", "activate text", "chat mode", "keyboard mode"]):
        return True, "TEXT_MODE"

    elif any(w in cmd for w in ["sleep", "goodbye", "bye"]):
        return True, "SLEEP"

    elif any(w in cmd for w in ["exit", "shutdown jay", "turn off", "quit"]):
        return True, "EXIT"

    # 2. Goal-Based Request (Requires LLM Planner & Execution Manager)
    return False, "GOAL_BASED_REQUEST"
