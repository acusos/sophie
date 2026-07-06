import asyncio
import os
import numpy as np
import sounddevice as sd
from fastapi import FastAPI
from pydantic import BaseModel

THRESHOLD = float(os.getenv("WAKE_THRESHOLD", "0.7"))

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
    """Use Silero VAD to detect when someone is speaking,
    then wait for a pause before triggering. This way Sophie
    only responds to actual speech, not random sounds."""
    from openwakeword import VAD

    vad = VAD()
    SAMPLE_RATE = 16000
    CHANNELS = 1
    FRAMES_PER_CHUNK = 256

    speech_active = False
    pause_counter = 0
    PAUSE_THRESHOLD = 15  # ~1.5s of silence before triggering

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
                pause_counter = 0
            else:
                if speech_active:
                    pause_counter += 1
                    if pause_counter >= PAUSE_THRESHOLD:
                        wake_detected.set()
                        speech_active = False
                        pause_counter = 0

        except Exception as e:
            print(f"VAD loop error: {e}")
            await asyncio.sleep(1)
            continue
