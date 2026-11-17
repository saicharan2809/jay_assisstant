import os
import time
import tempfile
import asyncio
import numpy as np
import sounddevice as sd
import scipy.io.wavfile as wavfile
import edge_tts
import pygame
import threading
import re

from config import SAMPLE_RATE, CHUNK_SIZE, state, state_lock, TTS_VOICE, WHISPER_MODEL, INTERRUPT_MULTIPLIER, PLAY_CHIMES

# Initialize Pygame Mixer for low-latency audio playback
try:
    pygame.mixer.init(frequency=44100, size=-16, channels=2)
except Exception as e:
    print(f"Pygame mixer init warning: {e}")

# Procedural Sound FX Synthesizer (0 external audio files needed)
_CHIME_CACHE = {}

def _synthesize_chime(kind="wake"):
    try:
        sr = 44100
        if kind == "wake":
            # Sci-fi 2-tone rising chime (D5 -> A5)
            dur = 0.35
            t = np.linspace(0, dur, int(sr * dur), False)
            freqs = np.where(t < 0.15, 587.33, 880.0)
            env = np.exp(-t * 8)
            wave = (np.sin(2 * np.pi * freqs * t) + 0.3 * np.sin(4 * np.pi * freqs * t)) * env
        elif kind == "success":
            # Ascending confirmation triad (C5 -> E5 -> G5)
            dur = 0.28
            t = np.linspace(0, dur, int(sr * dur), False)
            freqs = np.where(t < 0.08, 523.25, np.where(t < 0.16, 659.25, 783.99))
            env = np.exp(-t * 10)
            wave = (np.sin(2 * np.pi * freqs * t) + 0.2 * np.sin(4 * np.pi * freqs * t)) * env
        elif kind == "error":
            # Subtle low double blip
            dur = 0.25
            t = np.linspace(0, dur, int(sr * dur), False)
            wave = np.sin(2 * np.pi * 320 * t) * np.exp(-t * 12)
        else:
            return None
            
        audio = (wave * 0.35 * 32767).astype(np.int16)
        stereo = np.column_stack((audio, audio))
        return pygame.sndarray.make_sound(stereo)
    except Exception as e:
        return None

def play_chime(kind="wake"):
    if not PLAY_CHIMES:
        return
    try:
        if kind not in _CHIME_CACHE:
            _CHIME_CACHE[kind] = _synthesize_chime(kind)
        snd = _CHIME_CACHE.get(kind)
        if snd:
            snd.play()
    except Exception:
        pass


# Initialize Faster-Whisper (Local Offline STT Engine)
whisper_model = None
try:
    from faster_whisper import WhisperModel
    print(f"Loading local Faster-Whisper STT model ({WHISPER_MODEL})...")
    whisper_model = WhisperModel(WHISPER_MODEL, device="cpu", compute_type="int8")
    print("Faster-Whisper STT engine ready!")
except Exception as e:
    print(f"Faster-Whisper init error: {e}")

def stop_microphone():
    """Immediately stops all active microphone recording streams and releases the audio hardware."""
    try:
        sd.stop()
    except Exception:
        pass

def calibrate_microphone():
    with state_lock:
        if state.get("input_mode") != "voice":
            return
    print("Calibrating microphone sensitivity...")
    try:
        recording = sd.rec(int(SAMPLE_RATE * 2), samplerate=SAMPLE_RATE, channels=1, dtype='int16')
        sd.wait()
        level = float(np.abs(recording).mean())
        with state_lock:
            state["ambient_threshold"] = min(max(level * 1.3, 150.0), 400.0)
            thresh = state["ambient_threshold"]
        print(f"Microphone calibrated. Threshold: {thresh:.1f}")
    except Exception as e:
        print(f"Calibration failed: {e}")

def _sanitize_tts_error(err):
    """Sanitizes error messages to remove sensitive URL tokens or query parameters."""
    err_str = str(err)
    err_str = re.sub(r'TrustedClientToken=[^&\s\'"]+', 'TrustedClientToken=***', err_str)
    err_str = re.sub(r'Sec-MS-GEC=[^&\s\'"]+', 'Sec-MS-GEC=***', err_str)
    err_str = re.sub(r'Sec-MS-GEC-Version=[^&\s\'"]+', 'Sec-MS-GEC-Version=***', err_str)
    err_str = re.sub(r'ConnectionId=[^&\s\'"]+', 'ConnectionId=***', err_str)
    return err_str

_fallback_tts_lock = threading.Lock()

def _speak_fallback(text):
    """
    Offline local Windows TTS fallback (pyttsx3 / Windows SAPI).
    Invoked if Edge TTS network service is unavailable or rate-limited.
    """
    with _fallback_tts_lock:
        # 1. Try pyttsx3
        try:
            import pyttsx3
            engine = pyttsx3.init()
            engine.setProperty('rate', 185)
            engine.say(text)
            engine.runAndWait()
            return True
        except Exception as e_pyttsx3:
            # 2. Try direct Windows SAPI
            try:
                import win32com.client
                speaker = win32com.client.Dispatch("SAPI.SpVoice")
                speaker.Speak(text)
                return True
            except Exception as e_sapi:
                print(f"[TTS Fallback Error] pyttsx3: {e_pyttsx3}, SAPI: {e_sapi}")
                return False

async def _generate_speech(text, file_path):
    communicate = edge_tts.Communicate(text, TTS_VOICE)
    await communicate.save(file_path)

def _listen_for_interrupt():
    with state_lock:
        # Microphone must be completely OFF when not in active voice mode
        if state.get("input_mode") != "voice":
            return
        # Require a clear speaking volume (minimum floor 1200.0) to prevent minor room noise triggers
        threshold = max(state["ambient_threshold"] * INTERRUPT_MULTIPLIER, 1200.0)
        
    while pygame.mixer.get_init() and pygame.mixer.music.get_busy():
        with state_lock:
            if state.get("input_mode") != "voice":
                break
        try:
            chunk = sd.rec(CHUNK_SIZE, samplerate=SAMPLE_RATE, channels=1, dtype='int16')
            sd.wait()
            level = float(np.abs(chunk).mean())
            if level > threshold:
                print("\n[!] Interrupt detected! Stopping speech...")
                with state_lock:
                    state["stop_speaking"] = True
                pygame.mixer.music.stop()
                break
        except Exception:
            break

def _speak_single_chunk(text_chunk):
    with state_lock:
        if state["stop_speaking"]:
            return False

    temp_speech_file = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False).name
    try:
        # Attempt primary Edge TTS with timeout to prevent hanging on network stalls
        asyncio.run(asyncio.wait_for(_generate_speech(text_chunk, temp_speech_file), timeout=10.0))
        
        if pygame.mixer.get_init():
            pygame.mixer.music.load(temp_speech_file)
            pygame.mixer.music.play()
            
            with state_lock:
                current_mode = state.get("input_mode", "text")
            if current_mode == "voice":
                interrupt_thread = threading.Thread(target=_listen_for_interrupt, daemon=True)
                interrupt_thread.start()
            
            while pygame.mixer.music.get_busy():
                with state_lock:
                    if state["stop_speaking"]:
                        pygame.mixer.music.stop()
                        return False
                time.sleep(0.05)
            pygame.mixer.music.unload()
            return True
        else:
            return _speak_fallback(text_chunk)
    except Exception as e:
        clean_err = _sanitize_tts_error(e)
        print(f"TTS Speech error: {clean_err}")
        print("[TTS] Falling back to local offline speech synthesizer...")
        return _speak_fallback(text_chunk)
    finally:
        try:
            if os.path.exists(temp_speech_file):
                os.remove(temp_speech_file)
        except Exception:
            pass

def speak(text):
    print(f"Jay: {text}")
    with state_lock:
        state["stop_speaking"] = False

    sentences = [s.strip() for s in re.split(r'(?<=[.!?]) +', text) if s.strip()]
    if not sentences:
        sentences = [text]

    for sentence in sentences:
        with state_lock:
            if state["stop_speaking"]:
                break
        try:
            success = _speak_single_chunk(sentence)
            if not success and state.get("stop_speaking"):
                break
        except Exception as e:
            # Absolute guard: TTS failure MUST NEVER crash or interrupt caller
            print(f"[TTS Critical Guard] Speak execution error: {e}")
            break

    with state_lock:
        state["last_spoken"] = time.time()

def listen(duration=8):
    with state_lock:
        if state.get("input_mode") != "voice":
            print("[Microphone] OFF (Text Mode active).")
            return ""
        threshold = state["ambient_threshold"]

    print("Listening...")
        
    pre_buffer = []
    start_time = time.time()
    
    # Wait for voice activity (Max 6s timeout)
    while True:
        if time.time() - start_time > 6.0:
            print("Listening timed out (no speech detected).")
            return ""
            
        try:
            chunk = sd.rec(CHUNK_SIZE, samplerate=SAMPLE_RATE, channels=1, dtype='int16')
            sd.wait()
            pre_buffer.append(chunk.copy())
            if len(pre_buffer) > 18:
                pre_buffer.pop(0)
            level = float(np.abs(chunk).mean())
            if level > threshold * 0.4:
                print("Recording speech...")
                break
        except Exception as e:
            print(f"Audio read error: {e}")
            return ""

    chunks = list(pre_buffer)
    silence_count = 0
    max_silence = 14

    while True:
        try:
            chunk = sd.rec(CHUNK_SIZE, samplerate=SAMPLE_RATE, channels=1, dtype='int16')
            sd.wait()
            chunks.append(chunk.copy())
            level = float(np.abs(chunk).mean())
            if level < threshold:
                silence_count += 1
            else:
                silence_count = 0
            if silence_count > max_silence:
                break
            if len(chunks) > int(SAMPLE_RATE / CHUNK_SIZE * duration):
                break
        except Exception:
            break

    full_recording = np.concatenate(chunks, axis=0)
    
    # Volume normalization
    max_val = float(np.max(np.abs(full_recording)))
    if max_val > 100.0:
        scaling_factor = min(26000.0 / max_val, 10.0)
        full_recording = (full_recording.astype(np.float32) * scaling_factor).astype(np.int16)

    duration_sec = len(full_recording) / SAMPLE_RATE
    print(f"Captured {duration_sec:.1f}s of audio.")

    temp_file = tempfile.NamedTemporaryFile(suffix=".wav", delete=False).name
    wavfile.write(temp_file, SAMPLE_RATE, full_recording)

    command = ""
    try:
        if whisper_model:
            segments, info = whisper_model.transcribe(
                temp_file,
                beam_size=5,
                language="en",
                vad_filter=True,
                vad_parameters=dict(min_silence_duration_ms=500),
            )
            command = " ".join([segment.text for segment in segments]).strip().lower()
            if command:
                print(f"You said (Whisper): {command}")
        else:
            print("[!] Faster-Whisper model is not available.")
    except Exception as e:
        print(f"STT Error: {e}")
    finally:
        try:
            os.remove(temp_file)
        except Exception:
            pass

    return command
