import asyncio
import os
import numpy as np
import sounddevice as sd
from fastapi import FastAPI
from pydantic import BaseModel

THRESHOLD = float(os.getenv("WAKE_THRESHOLD", "0.8"))

app = FastAPI(title="Sophie VAD")

wake_detected = asyncio.Event()
wake_detected.clear()
listening = asyncio.Event()
listening.clear()

class WakeWordStatus(BaseModel):
    detected: bool
    threshold: float

@app.post("/detect", response_model=WakeWordStatus)
async def detect():
    status = wake_detected.is_set()
    if status:
        wake_detected.clear()
    return WakeWordStatus(detected=status, threshold=THRESHOLD)

@app.post("/start")
async def start_listening():
    if not listening.is_set():
        asyncio.create_task(vad_loop())
        listening.set()
    return {"status": "listening"}

@app.post("/stop")
async def stop_listening():
    listening.clear()
    return {"status": "stopped"}

@app.get("/status", response_model=WakeWordStatus)
async def status():
    return WakeWordStatus(detected=wake_detected.is_set(), threshold=THRESHOLD)

@app.get("/health")
async def health():
    return {"status": "ok", "listening": listening.is_set()}

async def vad_loop():
    """Use Silero VAD to detect when someone is speaking.
    Must detect speech for at least 1s before triggering."""
    from openwakeword import VAD

    vad = VAD()
    SAMPLE_RATE = 16000
    CHANNELS = 1
    FRAMES_PER_CHUNK = 256

    speech_active = False
    speech_frames = 0
    pause_counter = 0
    PAUSE_THRESHOLD = 40  # ~3s of silence before triggering
    MIN_SPEECH_FRAMES = 30  # ~3s minimum speech detection
    COOLDOWN_FRAMES = 60  # ~6s cooldown after each trigger

    cooldown = 0

    while listening.is_set():
        try:
            audio_data, _ = sd.read(
                FRAMES_PER_CHUNK,
                samplerate=SAMPLE_RATE,
                channels=CHANNELS,
                dtype='float32'
            )
            is_speech = vad.is_speech(audio_data)

            if is_speech:
                speech_active = True
                speech_frames += 1
                pause_counter = 0
            else:
                if speech_active:
                    pause_counter += 1
                    if pause_counter >= PAUSE_THRESHOLD and speech_frames >= MIN_SPEECH_FRAMES and cooldown <= 0:
                        wake_detected.set()
                        speech_active = False
                        speech_frames = 0
                        pause_counter = 0
                        cooldown = COOLDOWN_FRAMES

            if cooldown > 0:
                cooldown -= 1

        except Exception as e:
            print(f"VAD loop error: {e}")
            await asyncio.sleep(1)
            continue
