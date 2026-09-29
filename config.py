import os
import json
import threading
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configuration Settings
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "mistral")
WAKE_WORD_MODEL_PATH = os.getenv("WAKE_WORD_MODEL", "wake_word_model.pkl")
BROWSER_PATH = os.getenv("BROWSER_PATH", r"C:\Users\chaga\AppData\Local\Perplexity\Comet\Application\comet.exe")

# Voice & Model Tuning Settings
TTS_VOICE = os.getenv("TTS_VOICE", "en-US-GuyNeural")  # Natural, conversational US English male voice
WHISPER_MODEL = os.getenv("WHISPER_MODEL", "small.en")  # Small.en is much more accurate than base.en
INTERRUPT_MULTIPLIER = float(os.getenv("INTERRUPT_MULTIPLIER", "4.0"))  # Ignores room noise, keyboard, and speaker bleed


# Data File Paths
HOME_DIR = os.path.expanduser("~")
PREFERENCES_FILE = os.path.join(HOME_DIR, ".jay_preferences.json")
MEMORY_FILE = os.path.join(HOME_DIR, ".jay_memory.json")

# Audio Settings
SAMPLE_RATE = 16000
CHUNK_DURATION = 0.1  # seconds
CHUNK_SIZE = int(SAMPLE_RATE * CHUNK_DURATION)

# Global State & Thread Locks
file_lock = threading.Lock()
state_lock = threading.Lock()

state = {
    "awake": False,
    "input_mode": "text",  # "text" by default, "voice" when requested
    "last_spoken": 0,
    "stop_speaking": False,
    "command_history": [],
    "ambient_threshold": 250.0,
    "system_monitor_active": True,
    "learning_mode": True,
}
