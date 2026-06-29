import subprocess
import tempfile
import os
from fastapi import FastAPI
from fastapi.responses import Response

app = FastAPI(title="Sophie TTS (piper)")

MODEL_PATH = os.getenv("PIPER_MODEL", "/models/amy/en_US-amy-medium.onnx")
ONNX_MODEL_PATH = MODEL_PATH
VOICE_DIR = "/models/amy"

PIPE_SPEED = float(os.getenv("PIPER_SPEED", "1.0"))


def synthesize(text: str) -> bytes:
    """Run piper TTS and return raw PCM/WAV bytes."""
    cmd = [
        "piper",
        "--model", ONNX_MODEL_PATH,
        "--output-raw",
        "--output_file", "-",
        "--speed", str(PIPE_SPEED),
        "--sentence_split",
    ]
    proc = subprocess.run(
        cmd,
        input=text,
        capture_output=True,
        check=True,
    )
    return proc.stdout


@app.post("/tts")
async def tts(text: dict):
    """Accept JSON with 'text' field, return audio stream."""
    t = text.get("text", "")
    if not t.strip():
        return Response(status_code=400, content="Empty text")
    audio = synthesize(t)
    return Response(content=audio, media_type="audio/wav")


@app.post("/tts-raw")
async def tts_raw(text: dict):
    """Same as /tts but returns base64-encoded audio."""
    import base64
    t = text.get("text", "")
    if not t.strip():
        return {"error": "Empty text"}
    audio = synthesize(t)
    return {"audio_b64": base64.b64encode(audio).decode()}
