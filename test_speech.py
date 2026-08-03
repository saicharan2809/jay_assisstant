import pyaudio
import wave
import tempfile
from deepgram import DeepgramClient

DEEPGRAM_API_KEY = "4091c5faff3d0bde0ca3bea9589d235abf7fca8a"
deepgram = DeepgramClient(api_key=DEEPGRAM_API_KEY)

p = pyaudio.PyAudio()
stream = p.open(format=pyaudio.paInt16, channels=1, rate=16000, input=True, frames_per_buffer=1024)
print("Speak now for 5 seconds...")
frames = []
for i in range(0, int(16000 / 1024 * 5)):
    data = stream.read(1024)
    frames.append(data)
stream.stop_stream()
stream.close()
p.terminate()

import wave as wv
temp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
wf = wv.open(temp.name, "wb")
wf.setnchannels(1)
wf.setsampwidth(2)
wf.setframerate(16000)
wf.writeframes(b"".join(frames))
wf.close()

with open(temp.name, "rb") as f:
    data = f.read()

response = deepgram.listen.v1.media.transcribe_file(
    request=data, model="nova-3", language="en-IN", smart_format=True
)
print("Result:", response.results.channels[0].alternatives[0].transcript)