import os
import pickle
import time
import numpy as np
import sounddevice as sd
import librosa
from config import WAKE_WORD_MODEL_PATH, SAMPLE_RATE

wake_model = None
wake_scaler = None

if os.path.exists(WAKE_WORD_MODEL_PATH):
    try:
        with open(WAKE_WORD_MODEL_PATH, "rb") as f:
            wake_model, wake_scaler = pickle.load(f)
        print("Wake word detector model loaded successfully.")
    except Exception as e:
        print(f"Error loading wake word model: {e}")

# Rolling buffer of 2.2 seconds for smooth, continuous detection
BUFFER_SECONDS = 2.2
BUFFER_SIZE = int(SAMPLE_RATE * BUFFER_SECONDS)
STEP_SECONDS = 0.4
STEP_SIZE = int(SAMPLE_RATE * STEP_SECONDS)

_rolling_buffer = np.zeros(BUFFER_SIZE, dtype='float32')

def is_wake_word(audio, sr=SAMPLE_RATE):
    if wake_model is None or wake_scaler is None:
        return False
    try:
        max_val = float(np.max(np.abs(audio)))
        if max_val < 0.03:
            return False  # Pure silence

        # Peak normalization ensures consistent MFCC features regardless of mic volume
        audio_norm = audio / max_val
        audio_norm = np.nan_to_num(audio_norm, nan=0.0, posinf=0.0, neginf=0.0)

        mfcc = librosa.feature.mfcc(y=audio_norm, sr=sr, n_mfcc=13)
        features = np.mean(mfcc.T, axis=0).reshape(1, -1)
        features = np.nan_to_num(features, nan=0.0, posinf=0.0, neginf=0.0)
        features_scaled = wake_scaler.transform(features)
        prob = wake_model.predict_proba(features_scaled)[0][1]

        if prob >= 0.88:
            print(f"Wake word detected! (Confidence: {prob*100:.1f}%)")
            return True
        return False
    except Exception as e:
        print(f"Wake word prediction exception: {e}")
        return False

def listen_for_wake_word():
    global _rolling_buffer
    try:
        # Record a 0.4-second audio step
        chunk = sd.rec(STEP_SIZE, samplerate=SAMPLE_RATE, channels=1, dtype='float32')
        sd.wait()
        chunk = chunk.flatten()

        # Slide into rolling circular buffer
        _rolling_buffer = np.roll(_rolling_buffer, -STEP_SIZE)
        _rolling_buffer[-STEP_SIZE:] = chunk

        # Check if rolling window matches wake word
        if is_wake_word(_rolling_buffer, SAMPLE_RATE):
            # Clear buffer so it doesn't double-trigger
            _rolling_buffer = np.zeros(BUFFER_SIZE, dtype='float32')
            return True

        return False
    except Exception as e:
        print(f"Wake word listening error: {e}")
        time.sleep(0.1)
        return False
