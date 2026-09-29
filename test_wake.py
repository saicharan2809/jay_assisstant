import pyaudio
import numpy as np
from openwakeword.model import Model

print("Loading wake word model...")
model = Model(wakeword_models=['hey_jarvis'], inference_framework='onnx')
print("Say 'Hey Jarvis' to test...")

RATE = 16000
CHUNK = 1280

p = pyaudio.PyAudio()
stream = p.open(format=pyaudio.paInt16, channels=1, rate=RATE, input=True, frames_per_buffer=CHUNK)

while True:
    chunk = stream.read(CHUNK, exception_on_overflow=False)
    chunk = np.frombuffer(chunk, dtype=np.int16)
    prediction = model.predict(chunk)
    score = prediction['hey_jarvis']
    if score > 0.5:
        print(f"Wake word detected! Score: {score:.2f}")