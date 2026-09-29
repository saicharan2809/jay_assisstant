import sounddevice as sd
import scipy.io.wavfile as wav
import os

fs = 16000
duration = 3

samples = [
    "hey jay",
    "hey jay (louder)",
    "hey jay (softer)",
    "hey jay (faster)",
    "hey jay (slower)",
    "jay",
    "jay (louder)",
    "jai",
    "hey jai",
]

os.makedirs("wake_words", exist_ok=True)

print("We will record your voice in different ways.")
print("Say each phrase naturally when you see RECORDING...\n")

for i, sample in enumerate(samples):
    input(f"Press ENTER and say: '{sample}'")
    print("RECORDING...")
    recording = sd.rec(int(duration * fs), samplerate=fs, channels=1, dtype='int16')
    sd.wait()
    filename = f"wake_words/wake_{i}.wav"
    wav.write(filename, fs, recording)
    print(f"Saved! ({i+1}/{len(samples)})\n")

print("All done! Wake word samples recorded successfully!")