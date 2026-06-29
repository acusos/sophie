import os
import tempfile
import base64

from fastapi import FastAPI, UploadFile, File, Form
from pydantic import BaseModel
from faster_whisper import WhisperModel

MODEL_NAME = os.getenv("WHISPER_MODEL", "large-v3-turbo")
MODEL_DEVICE = os.getenv("WHISPER_DEVICE", "cuda")
MODEL_COMPUTE = os.getenv("WHISPER_COMPUTE", "float16")

model = WhisperModel(
    MODEL_NAME,
    device=MODEL_DEVICE,
    compute_type=MODEL_COMPUTE,
)

app = FastAPI(title="Sophie STT")


class STTResponse(BaseModel):
    text: str
    segments: list[dict] = []


@app.post("/transcribe", response_model=STTResponse)
async def transcribe(
    audio: UploadFile = File(...),
    language: str = Form("en"),
):
    # Save uploaded audio to a temp file
    with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as f:
        f.write(await audio.read())
        path = f.name

    try:
        segments, info = model.transcribe(
            path,
            language=language,
            beam_size=5,
            vad_filter=True,
        )
        segments_list = []
        full_text = ""
        for seg in segments:
            segments_list.append({
                "start": seg.start,
                "end": seg.end,
                "text": seg.text.strip(),
                "probability": seg.probability,
            })
            full_text += seg.text.strip() + " "
        full_text = full_text.strip()
        return STTResponse(text=full_text, segments=segments_list)
    finally:
        os.unlink(path)


@app.post("/transcribe-b64", response_model=STTResponse)
async def transcribe_b64(
    audio_b64: str,
    language: str = "en",
):
    with tempfile.NamedTemporaryFile(delete=False, suffix=".wav") as f:
        f.write(base64.b64decode(audio_b64))
        path = f.name

    try:
        segments, info = model.transcribe(
            path,
            language=language,
            beam_size=5,
            vad_filter=True,
        )
        segments_list = []
        full_text = ""
        for seg in segments:
            segments_list.append({
                "start": seg.start,
                "end": seg.end,
                "text": seg.text.strip(),
                "probability": seg.probability,
            })
            full_text += seg.text.strip() + " "
        full_text = full_text.strip()
        return STTResponse(text=full_text, segments=segments_list)
    finally:
        os.unlink(path)
