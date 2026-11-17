import time
import re
import ast
import datetime
import ollama
from config import OLLAMA_MODEL
import actions
import memory
from audio_engine import speak, play_chime

session_messages = []

# ========================================================
# Autonomous Computer-Use Agent Tool Definitions
# ========================================================

def open_app(app_name: str) -> str:
    """Opens any application or program on Windows (e.g. 'chrome', 'vscode', 'notepad', 'calc', 'task manager')."""
    success = actions.open_application(app_name)
    time.sleep(1.0)
    return f"Opened {app_name}" if success else f"Could not find application {app_name}"

def open_url(url: str) -> str:
    """Opens a website URL in the browser (e.g. 'https://www.youtube.com', 'https://www.amazon.in', 'https://www.google.com')."""
    actions.open_browser_url(url)
    time.sleep(1.5)
    return f"Opened {url}"

def click_element(description: str) -> str:
    """Scans the screen at high speed to find a button, search bar, channel card, thumbnail, video, tab, link, or product matching the description and clicks it."""
    success = actions.click_element_on_screen(description, silent=True)
    time.sleep(0.5)
    return f"Clicked on {description}" if success else f"Could not locate element '{description}'. It may require scrolling down or a different query."

def click_coordinates(x: int, y: int, click_type: str = "left") -> str:
    """Clicks at screen pixel coordinates (x, y). click_type can be 'left', 'right', or 'double'."""
    res = actions.click_at_coordinates(x, y, click_type, silent=True)
    time.sleep(0.4)
    return res

def type_text(text: str) -> str:
    """Types text directly into the currently active input field."""
    return actions.type_text_into_active_window(text)

def type_and_submit(text: str, press_enter: bool = True) -> str:
    """Types text into the active search bar, input, or form field and presses Enter to submit."""
    res = actions.type_and_press_enter(text, press_enter, silent=True)
    time.sleep(0.8)
    return res

def press_keys(keys: str) -> str:
    """Presses keyboard shortcuts or keys (e.g. 'enter', 'escape', 'tab', 'ctrl+s', 'alt+f4')."""
    res = actions.press_hotkey(keys, silent=True)
    time.sleep(0.3)
    return res

def scroll_screen(direction: str = "down") -> str:
    """Scrolls down or up to reveal more search results, products, videos, or page content."""
    actions.scroll_screen(direction, silent=True)
    time.sleep(0.5)
    return f"Scrolled {direction}"

def wait_seconds(seconds: int = 2) -> str:
    """Waits for the specified duration (1-5 seconds) for page loading or video buffering."""
    sec = max(1, min(int(seconds), 5))
    time.sleep(sec)
    return f"Waited {sec} seconds"

def create_file(filename: str, content: str) -> str:
    """Creates a file on the user's Desktop with the specified text or code."""
    return actions.create_file_on_desktop(filename, content)

def execute_shell(command: str) -> str:
    """Runs a Windows command prompt / shell command and returns output."""
    return actions.run_shell_command(command)

def search_internet(query: str) -> str:
    """Performs live web search for definitions, answers, or facts."""
    actions.search_web_live(query)
    return f"Searched web for {query}"

def request_confirmation(action_description: str) -> str:
    """MANDATORY SAFETY GUARD: Call immediately before placing orders, making payments, or irreversible actions."""
    speak(f"I am ready to {action_description}. Would you like me to proceed?")
    return f"CONFIRMATION_REQUESTED: {action_description}"

def finish_task(summary: str) -> str:
    """Call ONLY when the user's overall goal is completely achieved (e.g. 'The latest video is now playing', 'Found the product and opened the details')."""
    return f"GOAL_ACCOMPLISHED: {summary}"

def toggle_gesture_control(enable: bool = True) -> str:
    """Enables or disables webcam-based hand-gesture control (AirTouch)."""
    if enable:
        success = actions.enable_gesture_control()
        return "Gesture control enabled." if success else "Failed to enable gesture control."
    else:
        success = actions.disable_gesture_control()
        return "Gesture control disabled."

def get_current_datetime() -> str:
    """Returns the current local date, time, and day of week."""
    return actions.tell_current_datetime()

def set_reminder(prompt: str, due_time: str = "") -> str:
    """Sets a reminder with a task description and trigger time (e.g. 'in 15 minutes', 'tomorrow at 10 AM')."""
    return actions.create_reminder(prompt, due_time)

def list_reminders(filter_type: str = "upcoming") -> str:
    """Lists current pending reminders for today or upcoming."""
    return actions.show_reminders(filter_type)

def add_calendar_event(title: str, start_time: str, end_time: str = "", location: str = "") -> str:
    """Adds an event to the calendar on a given date and time."""
    return actions.create_calendar_event(title, start_time, end_time, location)

def list_calendar_events(date_query: str = "today") -> str:
    """Lists scheduled calendar events for today or tomorrow."""
    return actions.show_calendar_schedule(date_query)

def open_calendar(service: str = "auto") -> str:
    """Opens Google Calendar or Windows Calendar."""
    actions.open_calendar_app(service)
    return "Calendar opened."

# Map tool names to functions for execution
TOOL_MAP = {
    "open_app": open_app,
    "open_url": open_url,
    "click_element": click_element,
    "click_coordinates": click_coordinates,
    "type_text": type_text,
    "type_and_submit": type_and_submit,
    "press_keys": press_keys,
    "scroll_screen": scroll_screen,
    "wait_seconds": wait_seconds,
    "create_file": create_file,
    "execute_shell": execute_shell,
    "search_internet": search_internet,
    "request_confirmation": request_confirmation,
    "finish_task": finish_task,
    "toggle_gesture_control": toggle_gesture_control,
    "get_current_datetime": get_current_datetime,
    "set_reminder": set_reminder,
    "list_reminders": list_reminders,
    "add_calendar_event": add_calendar_event,
    "list_calendar_events": list_calendar_events,
    "open_calendar": open_calendar,
}

ALL_TOOLS = [
    open_app,
    open_url,
    click_element,
    click_coordinates,
    type_text,
    type_and_submit,
    press_keys,
    scroll_screen,
    wait_seconds,
    create_file,
    execute_shell,
    search_internet,
    request_confirmation,
    finish_task,
    toggle_gesture_control,
    get_current_datetime,
    set_reminder,
    list_reminders,
    add_calendar_event,
    list_calendar_events,
    open_calendar,
]

def extract_inline_tool_names_and_args(answer_text: str):
    """Fallback tool parser for models that emit function calls in text rather than JSON objects."""
    tool_names = list(TOOL_MAP.keys())
    pattern = r'\b(' + '|'.join(tool_names) + r')\s*\((.*?)\)'
    matches = re.findall(pattern, answer_text)
    
    results = []
    for fn_name, raw_args in matches:
        raw_args = raw_args.strip()
        py_args = raw_args.replace('true', 'True').replace('false', 'False').replace('null', 'None')
        args_dict = {}
        try:
            parsed = ast.parse(f"f({py_args})")
            call_node = parsed.body[0].value
            for kw in call_node.keywords:
                if isinstance(kw.value, ast.Constant):
                    args_dict[kw.arg] = kw.value.value
            if not args_dict and call_node.args:
                arg_val = call_node.args[0]
                if isinstance(arg_val, ast.Constant):
                    if fn_name == "open_url":
                        args_dict["url"] = arg_val.value
                    elif fn_name == "open_app":
                        args_dict["app_name"] = arg_val.value
                    elif fn_name == "click_element":
                        args_dict["description"] = arg_val.value
                    elif fn_name in ["type_text", "type_and_submit"]:
                        args_dict["text"] = arg_val.value
                    elif fn_name == "press_keys":
                        args_dict["keys"] = arg_val.value
                    elif fn_name == "scroll_screen":
                        args_dict["direction"] = arg_val.value
                    elif fn_name == "finish_task":
                        args_dict["summary"] = arg_val.value
        except Exception:
            clean_str = raw_args.strip("'\"`")
            if fn_name == "open_url":
                args_dict["url"] = clean_str
            elif fn_name == "open_app":
                args_dict["app_name"] = clean_str
            elif fn_name == "click_element":
                args_dict["description"] = clean_str
            elif fn_name in ["type_text", "type_and_submit"]:
                args_dict["text"] = clean_str
            elif fn_name == "finish_task":
                args_dict["summary"] = clean_str
                
        results.append((fn_name, args_dict))
    return results

def clear_session():
    global session_messages
    session_messages = []
    print("[Memory] Session context reset.")

# ========================================================
# AUTONOMOUS COMPUTER-USE PERCEPTION-ACTION RE-ACT LOOP
# ========================================================

def plan_and_execute_goal(prompt: str, max_steps: int = 8) -> bool:
    global session_messages

    if any(w in prompt.lower() for w in ["forget", "reset chat", "clear memory", "new topic"]):
        clear_session()
        speak("Got it, fresh start.")
        return True

    # Check for direct conversational questions
    clean_p = prompt.lower().strip()
    for pfx in ["hey jay,", "hey jay ", "jay,", "jay ", "please ", "can you ", "could you "]:
        if clean_p.startswith(pfx):
            clean_p = clean_p[len(pfx):].strip()

    is_action_goal = any(w in clean_p for w in [
        "open", "launch", "search", "play", "order", "buy", "find", "click", "read",
        "type", "write", "create", "watch", "navigate", "scroll", "go to", "check", "submit", "select"
    ])

    if not is_action_goal:
        try:
            now_str = datetime.datetime.now().strftime("%A, %B %d, %Y at %I:%M %p").lstrip("0")
            res = ollama.chat(
                model=OLLAMA_MODEL,
                messages=[
                    {"role": "system", "content": f"You are Jay, a helpful desktop AI assistant. CURRENT LOCAL TIME & DATE: {now_str}. Answer directly and concisely in 1-2 spoken sentences without markdown."},
                    {"role": "user", "content": prompt}
                ]
            )
            answer = res.get("message", {}).get("content", "").strip()
            speak(answer)
            return True
        except Exception:
            pass

    print(f"\n=======================================================")
    print(f"🤖 [Jay Autonomous Agent] Goal: '{prompt}'")
    print(f"=======================================================")
    speak("Working on it.")
    play_chime("wake")

    action_history = []

    for step in range(1, max_steps + 1):
        print(f"\n--- [Agent Step {step}/{max_steps}] ---")
        
        # 1. OBSERVE: Real-time visual screen perception via native high-speed OCR engine
        print(" -> [Perception] Inspecting current screen state...")
        screen_obs = actions.get_screen_semantic_observation(prompt)
        print(f" -> [Observation]:\n{screen_obs}")

        # 2. REASON: Decide the single next best action based on live state
        history_text = "\n".join(action_history) if action_history else "No actions taken yet."
        now_str = datetime.datetime.now().strftime("%A, %B %d, %Y at %I:%M %p").lstrip("0")
        
        agent_system = f"""You are Jay, an autonomous computer-use agent.
CURRENT LOCAL TIME & DATE: {now_str}
OVERALL GOAL: "{prompt}"

STEP {step} OF {max_steps}
CURRENT SCREEN OBSERVATION:
{screen_obs}

ACTION HISTORY:
{history_text}

CRITICAL EXECUTION RULES:
1. Examine the CURRENT SCREEN OBSERVATION carefully:
   - Look at visible channels, videos, products, tabs, buttons, and search results.
2. Select the SINGLE next best action to advance towards the goal:
   - If you need to search: use open_url or click the search box, type the query, and submit.
   - To open a channel, video, or product: call click_element(description="...") with the exact or key visible name (e.g. click_element("Fashion Verge"), click_element("Videos"), click_element("latest video"), click_element("Add to Cart")).
   - To find more items below the fold: call scroll_screen(direction="down").
   - If comparing products under a price budget: evaluate the visible options, select the best one or summarize the comparison.
3. If the user's overall goal is COMPLETELY ACCOMPLISHED (e.g. the video is playing, or product options were compared):
   IMMEDIATELY call finish_task(summary="..."). Do not continue looping.
4. If about to place a final financial transaction or payment: call request_confirmation(action_description="...").
5. DO NOT output conversational chatter or markdown code. Call ONE tool function directly."""

        messages = [
            {"role": "system", "content": agent_system},
            {"role": "user", "content": f"Based on the screen observation, what is the single next tool to call for '{prompt}'?"}
        ]

        try:
            response = ollama.chat(
                model=OLLAMA_MODEL,
                messages=messages,
                tools=ALL_TOOLS,
            )
            msg = response.get("message", {})
            tool_calls = msg.get("tool_calls", [])
            
            tool_name = None
            tool_args = {}

            if tool_calls:
                tool_name = tool_calls[0].function.name
                tool_args = tool_calls[0].function.arguments
            else:
                content_text = msg.get("content", "").strip()
                inline_matches = extract_inline_tool_names_and_args(content_text)
                if inline_matches:
                    tool_name, tool_args = inline_matches[0]

            if not tool_name or tool_name not in TOOL_MAP:
                content_text = msg.get("content", "").lower()
                if any(w in content_text for w in ["done", "completed", "playing", "finished", "here are the options"]):
                    play_chime("success")
                    final_msg = f"Task completed: {prompt}."
                    speak(final_msg)
                    return True
                else:
                    tool_name = "wait_seconds"
                    tool_args = {"seconds": 2}

            print(f" -> [Action Chosen]: {tool_name}({tool_args})")

            # 3. ACT: Execute the chosen action
            if tool_name == "finish_task":
                summary = tool_args.get("summary", "Task completed.")
                play_chime("success")
                session_messages.append({"role": "user", "content": prompt})
                session_messages.append({"role": "assistant", "content": summary})
                memory.add_conversation_memory(prompt, summary)
                speak(summary)
                return True

            if tool_name == "request_confirmation":
                action_desc = tool_args.get("action_description", "proceed with this transaction")
                speak(f"I am ready to {action_desc}. Please confirm if you would like me to proceed.")
                return True

            pre_fp = actions.get_screen_fingerprint()

            fn = TOOL_MAP[tool_name]
            try:
                res = fn(**tool_args)
            except TypeError:
                try:
                    res = fn(list(tool_args.values())[0]) if tool_args else fn()
                except Exception as err:
                    res = f"Error: {err}"

            # Post-action screen change verification
            time.sleep(0.3)
            post_fp = actions.get_screen_fingerprint()
            screen_changed = not actions.is_screen_identical(pre_fp, post_fp)

            if not screen_changed and tool_name in ["click_element", "type_and_submit"]:
                action_history.append(f"Step {step}: {tool_name}({tool_args}) -> {res} (Page state unchanged; element may be loading or unresponsive)")
            else:
                action_history.append(f"Step {step}: {tool_name}({tool_args}) -> {res}")

        except Exception as e:
            print(f"Agent step error: {e}")
            action_history.append(f"Step {step}: Error {e}")
            time.sleep(0.5)

    play_chime("success")
    concluding_msg = f"I have executed the steps for '{prompt}'."
    speak(concluding_msg)
    return True
