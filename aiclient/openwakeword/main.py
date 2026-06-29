import asyncio
import io
import os
import struct
import numpy as np
import sounddevice as sd
from fastapi import FastAPI
from pydantic import BaseModel

# openWakeWord model for "sophie"
# This requires the model to be downloaded first
WAKE_WORD = os.getenv("WAKE_WORD", "sophie")
THRESHOLD = float(os.getenv("WAKE_THRESHOLD", "0.6"))

app = FastAPI(title="Sophie openWakeWord")

# Track wake word detection state
wake_detected = asyncio.Event()
wake_detected.clear()
listening = asyncio.Event()
listening.clear()

class WakeWordStatus(BaseModel):
    detected: bool
    threshold: float

@app.post("/detect", response_model=WakeWordStatus)
async def detect():
    """Check if wake word was detected (polling endpoint)."""
    status = wake_detected.is_set()
    if status:
        wake_detected.clear()
    return WakeWordStatus(detected=status, threshold=THRESHOLD)

@app.post("/start")
async def start_listening():
    """Start the continuous wake word listener."""
    if not listening.is_set():
        asyncio.create_task(wake_word_loop())
        listening.set()
    return {"status": "listening"}

@app.post("/stop")
async def stop_listening():
    """Stop the wake word listener."""
    listening.clear()
    return {"status": "stopped"}

@app.get("/status", response_model=WakeWordStatus)
async def status():
    """Get current wake word detection status."""
    return WakeWordStatus(detected=wake_detected.is_set(), threshold=THRESHOLD)

@app.get("/health")
async def health():
    return {"status": "ok", "listening": listening.is_set()}

async def wake_word_loop():
    """Continuous loop to detect wake word from microphone."""
    # Initialize openWakeWord detector
    try:
        from openwakeword import Detector
        detector = Detector(wake_word_phrases=[WAKE_WORD], threshold=THRESHOLD)
    except ImportError:
        # Fallback: basic audio energy detection if openwakeword not available
        detector = None

    SAMPLE_RATE = 16000
    CHUNK = 1024
    CHANNELS = 1

    while listening.is_set():
        try:
            # Read audio chunk from microphone
            audio_data, _ = sd.read(CHUNK, samplerate=SAMPLE_RATE, channels=CHANNELS, dtype='float32')
            
            if detector is not None:
                # Use openWakeWord detector
                result = detector.predict(audio_data)
                if result.get("probability", 0) >= THRESHOLD:
                    wake_detected.set()
            else:
                # Fallback: simple energy-based detection
                # This is a very basic approach - just detect speech presence
                energy = np.mean(np.abs(audio_data))
                if energy > 0.1:  # Simple threshold
                    wake_detected.set()
            
        except Exception as e:
            print(f"Wake word loop error: {e}")
            break

    # Clean up
    detector = None
