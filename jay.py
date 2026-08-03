import ollama
import sounddevice as sd
import scipy.io.wavfile as wavfile
import tempfile
import os
import sys
import numpy as np
import random
import threading
import time
import pickle
import librosa
import asyncio
import edge_tts
import subprocess
import datetime
import json
import platform
import pyautogui
import psutil
from deepgram import DeepgramClient
from urllib.parse import quote
from collections import defaultdict

# ─────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────
DEEPGRAM_API_KEY = "4091c5faff3d0bde0ca3bea9589d235abf7fca8a"
deepgram = DeepgramClient(api_key=DEEPGRAM_API_KEY)

BROWSER_PATH = r"C:\Users\chaga\AppData\Local\Perplexity\Comet\Application\comet.exe"
VLC_PATH = r"C:\Program Files\VideoLAN\VLC\vlc.exe"
PREFERENCES_FILE = os.path.join(os.path.expanduser("~"), ".jay_preferences.json")
MEMORY_FILE = os.path.join(os.path.expanduser("~"), ".jay_memory.json")


# ─────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────
RATE = 16000
CHUNK_DURATION = 0.1  # seconds per chunk
CHUNK_SIZE = int(RATE * CHUNK_DURATION)

# ─────────────────────────────────────────
# STATE
# ─────────────────────────────────────────
awake = False
last_spoken = time.time()
stop_speaking = False
command_history = []
daily_usage = defaultdict(int)
system_monitor_active = True
learning_mode = True
ambient_threshold = 500  # default, will be calibrated


# ─────────────────────────────────────────
# LOAD WAKE WORD MODEL
# ─────────────────────────────────────────
print("Loading your personal wake word detector...")
with open("wake_word_model.pkl", "rb") as f:
    wake_model, wake_scaler = pickle.load(f)
print("Wake word detector loaded!")

# ─────────────────────────────────────────
# PREFERENCES & MEMORY
# ─────────────────────────────────────────
def load_preferences():
    if os.path.exists(PREFERENCES_FILE):
        with open(PREFERENCES_FILE, "r") as f:
            return json.load(f)
    return {
        "favorite_apps": [],
        "work_hours": {"start": "09:00", "end": "18:00"},
        "music_preferences": [],
        "low_battery_alert": 20,
        "username": os.getlogin(),
    }


def save_preferences(prefs):
    with open(PREFERENCES_FILE, "w") as f:
        json.dump(prefs, f, indent=2)


def load_memory():
    if os.path.exists(MEMORY_FILE):
        with open(MEMORY_FILE, "r") as f:
            return json.load(f)
    return {"conversations": [], "facts": {}}


def save_memory(memory):
    with open(MEMORY_FILE, "w") as f:
        json.dump(memory, f, indent=2)


preferences = load_preferences()
memory = load_memory()


# ─────────────────────────────────────────
# BROWSER
# ─────────────────────────────────────────
def open_browser(url):
    subprocess.Popen([BROWSER_PATH, url])


# ─────────────────────────────────────────
# SPEAK
# ─────────────────────────────────────────
async def speak_async(text):
    print(f"Jay: {text}")
    communicate = edge_tts.Communicate(text, "en-GB-RyanNeural")
    await communicate.save("jay_speech.mp3")


def speak(text):
    global stop_speaking, last_spoken
    stop_speaking = False
    asyncio.run(speak_async(text))
    try:
        vlc_proc = subprocess.Popen(
            [VLC_PATH, "--intf", "dummy", "--play-and-exit", "jay_speech.mp3"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        timeout = time.time() + 60  # max 60 seconds
        while vlc_proc.poll() is None:
            if stop_speaking or time.time() > timeout:
                vlc_proc.terminate()
                # use subprocess to kill lingering vlc processes on Windows
                try:
                    subprocess.call(["taskkill", "/f", "/im", "vlc.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except Exception:
                    pass
                break
            time.sleep(0.1)
    except Exception as e:
        print(f"Error occurred while trying to play audio: {e}")
    last_spoken = time.time()
# ─────────────────────────────────────────
# MICROPHONE CALIBRATION
# ───────────────────────────────────────── 
def calibrate_microphone():
    global ambient_threshold
    print("Calibrating microphone...")
    recording = sd.rec(int(RATE * 2), samplerate=RATE, channels=1, dtype='int16')
    sd.wait()
    level = np.abs(recording).mean()
    ambient_threshold = level * 2.5
    print(f"Calibrated. Threshold: {ambient_threshold:.1f}")
#────────────────────────────────────────
# WAKE WORD DETECTION       
#────────────────────────────────────────
def record_for_wake_word(duration=3):
    recording = sd.rec(int(RATE * duration), samplerate=RATE, channels=1, dtype='float32')
    sd.wait()
    return recording.flatten(), RATE

def is_wake_word(audio, sr):
    try:
        audio = np.nan_to_num(audio, nan=0.0, posinf=0.0, neginf=0.0)
        mfcc = librosa.feature.mfcc(y=audio, sr=sr, n_mfcc=13)
        features = np.mean(mfcc.T, axis=0).reshape(1, -1)
        features = np.nan_to_num(features, nan=0.0, posinf=0.0, neginf=0.0)
        features_scaled = wake_scaler.transform(features)
        prob = wake_model.predict_proba(features_scaled)[0][1]
        print(f"Wake word probability: {prob:.2f}")
        return prob > 0.85
    except:
        return False





# ─────────────────────────────────────────
# LISTEN
# ─────────────────────────────────────────
def listen(duration=8):
    print("Waiting for you to speak...")
    
    pre_buffer = []
    
    while True:
        chunk = sd.rec(CHUNK_SIZE, samplerate=RATE, channels=1, dtype='int16')
        sd.wait()
        pre_buffer.append(chunk.copy())
        if len(pre_buffer) > 10:
            pre_buffer.pop(0)
        level = np.abs(chunk).mean()
        if level > ambient_threshold * 0.5:
            print("Recording...")
            break

    chunks = list(pre_buffer)
    silence_count = 0
    max_silence = 8

    while True:
        chunk = sd.rec(CHUNK_SIZE, samplerate=RATE, channels=1, dtype='int16')
        sd.wait()
        chunks.append(chunk.copy())
        level = np.abs(chunk).mean()
        if level < ambient_threshold:
            silence_count += 1
        else:
            silence_count = 0
        if silence_count > max_silence:
            break
        if len(chunks) > int(RATE / CHUNK_SIZE * duration):
            break

    full_recording = np.concatenate(chunks, axis=0)
    temp_file = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    wavfile.write(temp_file, RATE, full_recording)

    try:
        with open(temp_file, "rb") as audio_file:
            buffer_data = audio_file.read()
        for attempt in range(3):
            try:
                response = deepgram.listen.v1.media.transcribe_file(
                    request=buffer_data,
                    model="nova-3",
                    language="en-IN",
                    smart_format=True,
                    punctuate=True,
                    numerals=True,
                )
                command = response.results.channels[0].alternatives[0].transcript.lower()
                print(f"You said: {command}")
                break
            except Exception as e:
                print(f"Attempt {attempt+1} failed: {e}")
                if attempt < 2:
                    time.sleep(1)
                else:
                    command = ""
    except Exception as e:
        print(f"Deepgram error: {e}")
        command = ""
    finally:
        os.remove(temp_file)

    return command
# ─────────────────────────────────────────
# INTERRUPT LISTENER
# ─────────────────────────────────────────
def interrupt_listener():
    global stop_speaking
    while True:
        try:
            chunk = sd.rec(CHUNK_SIZE, samplerate=RATE, channels=1, dtype='int16')
            sd.wait()
            level = np.abs(chunk).mean()
            if level > ambient_threshold * 1.5:
                stop_speaking = True
        except:
            pass

# ─────────────────────────────────────────
# AI BRAIN (Self-Thinking)
# ─────────────────────────────────────────
def ask_ollama(prompt, context=""):
    recent_memory = memory["conversations"][-5:] if memory["conversations"] else []
    memory_context = "\n".join([f"Previous: {m}" for m in recent_memory])

    system = f"""You are Jay, a highly intelligent and self-thinking AI butler.
You address your master as Sir at all times.
You don't just answer questions — you analyze, think deeply, and proactively suggest what Sir might need.
You are eloquent, witty, and always helpful.
You speak naturally — no bullet points or lists in responses.
Always think about what Sir actually needs, not just what they literally said.
Keep responses concise and natural for speaking aloud.

{f"Recent context: {memory_context}" if memory_context else ""}
{f"Additional context: {context}" if context else ""}
"""
    response = ollama.chat(
        model="mistral",
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ],
    )
    result = response["message"]["content"]

    # Save to memory
    memory["conversations"].append(f"User: {prompt} | Jay: {result[:100]}")
    if len(memory["conversations"]) > 50:
        memory["conversations"] = memory["conversations"][-50:]
    save_memory(memory)

    return result


def understand_intent(command):
    """Let AI figure out what Sir actually needs"""
    intent_prompt = f"""Analyze this voice command and determine what the user actually needs.
Command: "{command}"
Respond with ONLY one of these intents and nothing else:
- SEARCH_WEB: needs information from internet
- OPEN_APP: wants to open an application
- SYSTEM_INFO: wants system status (battery/cpu/memory)
- VOLUME: wants to change volume
- SCREENSHOT: wants to take screenshot
- WEATHER: wants weather info
- MOUSE_CONTROL: wants mouse/keyboard control
- GENERAL_QUESTION: general question for AI
- REMINDER: wants to set a reminder
- CLOSE_APP: wants to close an application
- SHUTDOWN: wants to shutdown/restart/sleep PC
- MUSIC: wants to play music
- FILE: wants file operations"""

    response = ollama.chat(
        model="mistral", messages=[{"role": "user", "content": intent_prompt}]
    )
    return response["message"]["content"].strip()


# ─────────────────────────────────────────
# SYSTEM FEATURES (from JarvisAI)
# ─────────────────────────────────────────
def check_battery():
    battery = psutil.sensors_battery()
    if battery:
        plugged = "plugged in" if battery.power_plugged else "not plugged in"
        speak(f"Battery is at {battery.percent}% and is {plugged} Sir.")
    else:
        speak("Battery information is not available Sir.")


def check_cpu():
    cpu = psutil.cpu_percent(interval=1)
    cores = psutil.cpu_count()
    speak(f"CPU usage is at {cpu}% across {cores} cores Sir.")


def check_memory():
    mem = psutil.virtual_memory()
    used = mem.used / (1024**3)
    total = mem.total / (1024**3)
    speak(
        f"Memory usage is {mem.percent}%, with {used:.1f} GB used out of {total:.1f} GB Sir."
    )


def check_disk():
    disk = psutil.disk_usage("/")
    free = disk.free / (1024**3)
    speak(f"Disk is {disk.percent}% full with {free:.1f} GB free Sir.")


def analyze_system():
    speak("Performing a full system analysis Sir. One moment.")
    cpu = psutil.cpu_percent(interval=1)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    battery = psutil.sensors_battery()
    bat_str = f"{battery.percent}%" if battery else "unavailable"
    report = f"CPU at {cpu}%, memory at {mem.percent}%, disk at {disk.percent}%, battery at {bat_str}."
    recommendations = []
    if cpu > 80:
        recommendations.append(
            "CPU is running high — consider closing some applications."
        )
    if mem.percent > 85:
        recommendations.append(
            "Memory is running low — you may want to restart some apps."
        )
    if disk.percent > 90:
        recommendations.append(
            "Disk space is critically low — please free up some space."
        )
    full_report = report + (
        " " + " ".join(recommendations)
        if recommendations
        else " All systems are running within normal parameters."
    )
    speak(full_report)


def take_screenshot():
    screenshot = pyautogui.screenshot()
    path = os.path.join(os.path.expanduser("~"),"Pictures","Screenshots",f"jay_ {datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.png")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    screenshot.save(path)
    speak(f"Screenshot saved to your Pictures folder Sir.")


def volume_up():
    pyautogui.press("volumeup")
    speak("Volume increased Sir.")


def volume_down():
    pyautogui.press("volumedown")
    speak("Volume decreased Sir.")


def mute_volume():
    pyautogui.press("volumemute")
    speak("Volume muted Sir.")


def shutdown_pc():
    speak("Shutting down the system Sir. Goodbye.")
    subprocess.run(["shutdown", "/s", "/t", "10"])


def restart_pc():
    speak("Restarting the system Sir.")
    subprocess.run(["shutdown", "/r", "/t", "10"])


def sleep_pc():
    speak("Putting the system to sleep Sir.")
    subprocess.run(["rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"])


def open_application(app_name):
    apps = {
        "notepad": "notepad.exe",
        "calculator": "calc.exe",
        "command prompt": "cmd.exe",
        "powershell": "powershell.exe",
        "file explorer": "explorer.exe",
        "word": "WINWORD.EXE",
        "excel": "EXCEL.EXE",
        "powerpoint": "POWERPNT.EXE",
        "paint": "mspaint.exe",
        "task manager": "taskmgr.exe",
    }
    if app_name in apps:
        try:
            subprocess.Popen(apps[app_name])
            speak(f"Opening {app_name} Sir.")
        except:
            speak(f"Sorry Sir, I could not open {app_name}.")
    else:
        try:
            subprocess.run(["cmd", "/c", "start", "", app_name], check=True)
            speak(f"Opening {app_name} Sir.")
        except:
            speak(f"Sorry Sir, I could not find {app_name}.")


def close_application(app_name):
    try:
        subprocess.run(["taskkill", "/f", "/im", f"{app_name}.exe"], check=True)
        speak(f"Closing {app_name} Sir.")
    except:
        speak(f"Sorry Sir, I could not close {app_name}.")


def mouse_click(click_type="left"):
    if "double" in click_type:
        pyautogui.doubleClick()
        speak("Double clicked Sir.")
    elif "right" in click_type:
        pyautogui.rightClick()
        speak("Right clicked Sir.")
    else:
        pyautogui.click()
        speak("Clicked Sir.")


def scroll_screen(direction="up"):
    if "down" in direction:
        pyautogui.scroll(-300)
        speak("Scrolled down Sir.")
    else:
        pyautogui.scroll(300)
        speak("Scrolled up Sir.")


def predict_needs():
    hour = datetime.datetime.now().hour
    work_start = int(preferences["work_hours"]["start"].split(":")[0])
    work_end = int(preferences["work_hours"]["end"].split(":")[0])
    if work_start <= hour < work_end:
        speak(
            "You are in your work hours Sir. Shall I open any applications or assist with anything specific?"
        )
    elif hour >= 22:
        speak(
            "It is getting late Sir. Would you like me to save your work and prepare for shutdown?"
        )
    elif hour == 12:
        speak(
            "It is lunchtime Sir. Would you like me to set a reminder or look something up?"
        )
    else:
        speak("I am here and ready Sir. What would you like to accomplish?")


# ─────────────────────────────────────────
# SYSTEM MONITOR (Background)
# ─────────────────────────────────────────
def system_monitor():
    while system_monitor_active:
        try:
            battery = psutil.sensors_battery()
            if (
                battery
                and battery.percent <= preferences["low_battery_alert"]
                and not battery.power_plugged
            ):
                speak(
                    f"Alert Sir. Battery is at {battery.percent}%. Please connect to power.")
            mem = psutil.virtual_memory()
            if mem.percent > 90:
                speak(f"Warning Sir. Memory usage is critically high at {mem.percent}%.")
        except:
            pass
        time.sleep(300)




# ─────────────────────────────────────────
# IDLE TALKER
# ─────────────────────────────────────────
idle_phrases = [
    "Sir, shall I assist you with anything?",
    "I am at your service Sir. Do you require anything?",
    "Whenever you are ready Sir, I am here.",
    "Shall I look something up for you Sir?",
    "I am standing by Sir. Just say the word.",
]

greeting_phrases = [
    "Good to have you back Sir. How may I assist you today?",
    "Welcome Sir. I trust your day is going well. How can I be of service?",
    "At your service Sir. What shall we accomplish today?",
]


def idle_talker():
    global awake, last_spoken
    while True:
        time.sleep(30)
        if awake and (time.time() - last_spoken > 30):
            speak(random.choice(idle_phrases))


# ─────────────────────────────────────────
# COMMAND PROCESSOR
# ─────────────────────────────────────────
def process_command(command):
    command_history.append(command)
    daily_usage[datetime.datetime.now().hour] += 1

    # Learn preferences
    if learning_mode and "prefer" in command:
        memory["facts"][f"preference_{len(memory['facts'])}"] = command
        save_memory(memory)

    # ── System commands ──
    if any(w in command for w in ["volume up", "increase volume", "louder"]):
        volume_up()

    elif any(w in command for w in ["volume down", "decrease volume", "quieter"]):
        volume_down()

    elif any(w in command for w in ["mute", "silence"]):
        mute_volume()

    elif "screenshot" in command or "capture screen" in command:
        take_screenshot()

    elif any(w in command for w in ["check battery", "battery level", "battery status"]):
        check_battery()

    elif any(w in command for w in ["check cpu", "cpu usage", "processor"]):
        check_cpu()

    elif any(w in command for w in ["check memory", "ram usage", "memory status"]):
        check_memory()

    elif any(w in command for w in ["check disk", "disk space", "storage"]):
        check_disk()

    elif any(w in command for w in ["analyze system", "system analysis", "system status"]):
        analyze_system()

    elif any(w in command for w in ["predict", "what do i need", "suggest"]):
        predict_needs()

    elif "shutdown" in command or "shut down" in command:
        shutdown_pc()

    elif "restart" in command or "reboot" in command:
        restart_pc()

    elif "sleep pc" in command or "hibernate" in command:
        sleep_pc()

    elif "double click" in command:
        pyautogui.doubleClick()
        mouse_click("double")

    elif "right click" in command:
        pyautogui.rightClick()
        mouse_click("right")

    elif "click" in command:
        pyautogui.click()
        mouse_click("left")

    elif "scroll down" in command:
        pyautogui.scroll(-300)
        scroll_screen("down")

    elif "scroll up" in command:
        pyautogui.scroll(300)
        scroll_screen("up")

    # ── Close app ──
    elif "close" in command:
        app = command.replace("close", "").strip()
        close_application(app)

    # ── Sleep/wake ──
    elif any(w in command for w in ["sleep", "goodbye", "bye"]):
        return "SLEEP"

    # ── Exit ──
    elif any(w in command for w in ["exit", "shutdown jay", "turn off"]):
        return "EXIT"

    # ── Time/Date ──
    elif "what time" in command or "current time" in command:
        current_time = datetime.datetime.now().strftime("%I:%M %p")
        speak(f"The current time is {current_time} IST Sir.")
    elif "what day" in command or "what date" in command or "today's date" in command:
        current_date = datetime.datetime.now().strftime("%A, %B %d %Y")
        speak(f"Today is {current_date} Sir.")

    # ── Website searches ──
    elif "search" in command and " in " in command:
        parts = command.split(" in ")
        query = parts[0].replace("search", "").strip()
        app = parts[1].strip()
        website_searches = {
            "amazon": f"https://www.amazon.in/s?k={quote(query)}",
            "youtube": f"https://www.youtube.com/results?search_query={quote(query)}",
            "flipkart": f"https://www.flipkart.com/search?q={quote(query)}",
            "email": f"https://mail.google.com/mail/u/1/#inbox/search?q={quote(query)}",
            "spotify": f"https://open.spotify.com/search/{quote(query)}",
            "twitter": f"https://twitter.com/search?q={quote(query)}",
            "github": f"https://github.com/saicharan2809/search/?q={quote(query)}",
            "wikipedia": f"https://en.wikipedia.org/wiki/{quote(query)}",
            "chatgpt": f"https://chatgpt.com/search?q={quote(query)}",
            "linkedin": f"https://www.linkedin.com/in/sai-charan-47b419380/?keywords={quote(query)}",
            "instagram": f"https://www.instagram.com/explore/tags/{quote(query)}/",
        }
        if app.lower() in website_searches:
            speak(f"Searching for {query} in {app} Sir.")
            subprocess.Popen([BROWSER_PATH, website_searches[app.lower()]])
        else:
            speak(f"Searching for {query} Sir.")
            subprocess.Popen([BROWSER_PATH, f"https://www.google.com/search?q={quote(query)}"])

    elif "search" in command:
        query = command.replace("search", "").strip()
        speak(f"Right away Sir. Searching for {query}.")
        subprocess.Popen([BROWSER_PATH, f"https://www.google.com/search?q={quote(query)}"])

    # ── Open browser/apps ──
    elif "open browser" in command or "open comet" in command:
        speak("Opening Comet browser Sir.")
        subprocess.Popen([BROWSER_PATH])
    elif "open youtube" in command:
        speak("Opening YouTube Sir.")
        subprocess.Popen([BROWSER_PATH, "https://www.youtube.com"])
    elif "open google" in command:
        speak("Opening Google Sir.")
        subprocess.Popen([BROWSER_PATH, "https://www.google.com"])
    elif "open amazon" in command:
        speak("Opening Amazon Sir.")
        subprocess.Popen([BROWSER_PATH, "https://www.amazon.in"])
    elif "open whatsapp" in command:
        speak("Opening WhatsApp Sir.")
        os.startfile("whatsapp:")
    elif "open" in command:
        app = command.replace("open", "").strip()
        open_application(app)

    # ── Self-thinking AI response ──
    else:
        speak("Allow me a moment Sir...")
        response = ask_ollama(command)
        speak(response)

    return "CONTINUE"


# ─────────────────────────────────────────
# MAIN RUN LOOP
# ─────────────────────────────────────────
def run_jay():
    global awake
    calibrate_microphone()
 
    # Start background threads
    threading.Thread(target=interrupt_listener, daemon=True).start()
    threading.Thread(target=system_monitor, daemon=True).start()
    threading.Thread(target=idle_talker, daemon=True).start()
 
    speak("Jay standing by Sir. Say Hey Jay to activate me.")
    
    # Wake word loop
    while True:
        print("Waiting for wake word...")
        audio, sr = record_for_wake_word(duration=3)
        if is_wake_word(audio, sr):
            time.sleep(0.2)
            audio2, sr2 = record_for_wake_word(duration=2)
            if is_wake_word(audio2, sr2):
                awake = True
                speak(random.choice(greeting_phrases))
                break

    # Time-based greeting
    hour = datetime.datetime.now().hour
    if hour < 12:
        speak("It is a fine morning Sir. I am fully at your disposal.")
    elif hour < 17:
        speak("Good afternoon Sir. Ready to assist you.")
    else:
        speak("Good evening Sir. How may I serve you tonight?")

    # Main command loop
    while True:
        try:
            command = listen(duration=8)
            if not command:
                continue
            result = process_command(command)
            if result == "SLEEP":
                speak("Very well Sir. I shall retire for now. Say Hey Jay whenever you need me.")
                awake = False
                while True:
                    print("Sleeping... say Hey Jay to wake me up!")
                    audio, sr = record_for_wake_word(duration=3)
                    if is_wake_word(audio, sr):
                        time.sleep(0.2)
                        audio2, sr2 = record_for_wake_word(duration=2)
                        if is_wake_word(audio2, sr2):
                            awake = True
                            speak(random.choice(greeting_phrases))
                            break
            elif result == "EXIT":
                speak("Goodbye Sir. It was a pleasure serving you. Shutting down completely.")
                break
        except Exception as e:
            print(f"Error in main loop: {e}")
            speak("I apologize Sir, I encountered an issue. I am back now.")
            continue

if __name__ == "__main__":
    run_jay()