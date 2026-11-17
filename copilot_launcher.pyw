import os
import time
import subprocess
import threading
from pynput import keyboard

# Path to start Jay
JAY_BATCH_PATH = r"C:\Users\chaga\OneDrive\Desktop\jay_assisstant\start_jay.bat"
JAY_DIR = r"C:\Users\chaga\OneDrive\Desktop\jay_assisstant"

last_launch_time = 0
launch_lock = threading.Lock()

def launch_jay():
    global last_launch_time
    with launch_lock:
        now = time.time()
        # 1.5 second debounce to prevent double-spawning
        if now - last_launch_time < 1.5:
            return
        last_launch_time = now

    try:
        subprocess.Popen(
            ["cmd.exe", "/c", "start", "", JAY_BATCH_PATH],
            cwd=JAY_DIR,
            creationflags=subprocess.CREATE_NEW_CONSOLE
        )
    except Exception as e:
        print(f"Error launching Jay: {e}")

# Track active modifier keys
current_keys = set()

def on_press(key):
    current_keys.add(key)
    
    # Check for dedicated Windows 11 Copilot Key: Win + Shift + F23
    has_win = any(k in current_keys for k in [keyboard.Key.cmd, keyboard.Key.cmd_l, keyboard.Key.cmd_r])
    has_shift = any(k in current_keys for k in [keyboard.Key.shift, keyboard.Key.shift_l, keyboard.Key.shift_r])
    
    # Check for F23 key or custom virtual key code
    is_f23 = False
    try:
        if hasattr(key, 'vk') and key.vk == 134:  # VK_F23 = 0x86 (134)
            is_f23 = True
        elif hasattr(key, 'char') and key.char is None and str(key) == '<134>':
            is_f23 = True
        elif key == keyboard.Key.f23 if hasattr(keyboard.Key, 'f23') else False:
            is_f23 = True
    except Exception:
        pass

    # Check for legacy Copilot shortcut: Win + C
    is_c = False
    try:
        if hasattr(key, 'char') and key.char and key.char.lower() == 'c':
            is_c = True
    except Exception:
        pass

    # 1. Hardware Copilot Key: Win + Shift + F23
    if has_win and has_shift and is_f23:
        launch_jay()
    # 2. Legacy Copilot: Win + C
    elif has_win and is_c:
        launch_jay()

def on_release(key):
    try:
        current_keys.discard(key)
    except KeyError:
        pass

if __name__ == "__main__":
    with keyboard.Listener(on_press=on_press, on_release=on_release) as listener:
        listener.join()
