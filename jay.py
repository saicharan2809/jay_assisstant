import time
import random
import datetime
import threading
from config import state, state_lock
import audio_engine
import wake_word
import analyzer
import planner

idle_phrases = [
    "Hey, let me know if you need anything.",
    "Standing by whenever you need me.",
    "Ready when you are.",
    "Let me know if I can help with something.",
]

greeting_phrases = [
    "Hey! What's on your mind?",
    "Hey there! How can I help today?",
    "Ready to go. What are we working on?",
]

def idle_talker():
    while True:
        time.sleep(45)
        with state_lock:
            mode = state.get("input_mode", "text")
            is_awake = state["awake"]
            last_spk = state["last_spoken"]
        
        # Only talk when in active voice mode and awake
        if mode == "voice" and is_awake and (time.time() - last_spk > 90):
            audio_engine.speak(random.choice(idle_phrases))

def run_text_mode():
    print("\n" + "=" * 50)
    print("💬 Jay Terminal Chat Mode Active")
    print("• Type your message or system command directly below.")
    print("• Type 'voice mode' to switch to Voice Activation.")
    print("• Type 'exit' or 'quit' to close Jay.")
    print("=" * 50 + "\n")

    while True:
        with state_lock:
            if state["input_mode"] != "text":
                break

        try:
            user_input = input("You > ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nExiting Jay...")
            audio_engine.speak("Goodbye!")
            return "EXIT"

        if not user_input:
            continue

        is_simple, status = analyzer.analyze_and_route_request(user_input)

        if status == "VOICE_MODE":
            print("\n[🎤] Switching to Voice Mode...")
            audio_engine.speak("Switching to voice mode.")
            with state_lock:
                state["input_mode"] = "voice"
                state["awake"] = False
            break

        elif status == "TEXT_MODE":
            print("[i] You are already in Text Chat Mode.")
            continue

        elif status == "EXIT":
            audio_engine.speak("Goodbye! Shutting down completely.")
            return "EXIT"

        elif status == "SLEEP":
            print("[i] Chat mode is idle. Type 'exit' to quit or 'voice mode' to switch.")
            continue

        elif is_simple:
            # Simple command was executed by actions module
            pass
        else:
            # LLM Planner goal execution
            planner.plan_and_execute_goal(user_input)

    return "CONTINUE"

def run_voice_mode(mic_calibrated):
    if not mic_calibrated:
        print("\n[🎤] Calibrating microphone for Voice Mode...")
        audio_engine.calibrate_microphone()
        mic_calibrated = True

    print("\n" + "=" * 50)
    print("🎤 Jay Voice Mode Active")
    print("• Say 'Hey Jay' to activate listening.")
    print("• Say 'text mode' to switch back to Terminal Chat.")
    print("• Say 'sleep' to put Jay on standby.")
    print("=" * 50 + "\n")

    audio_engine.speak("Voice mode active. Say Hey Jay to activate me.")

    while True:
        with state_lock:
            if state["input_mode"] != "voice":
                break

        try:
            # 1. Wake Word Loop
            while True:
                with state_lock:
                    if state["input_mode"] != "voice":
                        break
                    is_awake = state["awake"]

                if not is_awake:
                    if wake_word.listen_for_wake_word():
                        with state_lock:
                            state["awake"] = True
                        planner.clear_session()
                        audio_engine.speak(random.choice(greeting_phrases))
                        break
                else:
                    break

            with state_lock:
                if state["input_mode"] != "voice":
                    break

            # 2. Speech-to-Text
            command = audio_engine.listen(duration=8)
            if not command:
                continue

            # 3. Analyze & Route
            is_simple, status = analyzer.analyze_and_route_request(command)

            if status == "TEXT_MODE":
                audio_engine.speak("Switching back to text mode.")
                with state_lock:
                    state["input_mode"] = "text"
                    state["awake"] = False
                break

            elif status == "VOICE_MODE":
                audio_engine.speak("Voice mode is already active.")
                continue

            elif status == "SLEEP":
                audio_engine.speak("I shall retire for now. Say Hey Jay whenever you need me.")
                with state_lock:
                    state["awake"] = False
                continue

            elif status == "EXIT":
                audio_engine.speak("Goodbye! Shutting down completely.")
                return "EXIT", mic_calibrated

            elif is_simple:
                # Executed via actions
                pass
            else:
                planner.plan_and_execute_goal(command)

        except Exception as e:
            print(f"Error in voice loop: {e}")
            audio_engine.speak("I encountered an unexpected issue. Standing by.")
            time.sleep(1)

    return "CONTINUE", mic_calibrated

def run_jay():
    # Start background idle thread
    threading.Thread(target=idle_talker, daemon=True).start()

    mic_calibrated = False

    # Main Mode Switcher Loop
    while True:
        with state_lock:
            current_mode = state.get("input_mode", "text")

        if current_mode == "text":
            result = run_text_mode()
            if result == "EXIT":
                break
        elif current_mode == "voice":
            result, mic_calibrated = run_voice_mode(mic_calibrated)
            if result == "EXIT":
                break

if __name__ == "__main__":
    run_jay()
