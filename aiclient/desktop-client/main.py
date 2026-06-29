#!/usr/bin/env python3
"""
Sophie Desktop Voice Client — continuous listening loop

Runs on aiclient (192.168.20.112), captures microphone audio,
detects wake word via openWakeWord, then streams audio to the
agent for STT and chat.

Usage:
    python main.py
"""

import asyncio
import io
import os
import struct
import numpy as np
import sounddevice as sd
import requests

# Configuration
AGENT_URL = os.getenv("AGENT_URL", "http://localhost:8090")
OPENWAKEWORD_URL = os.getenv("OPENWAKEWORD_URL", "http://localhost:8093")
SAMPLE_RATE = 16000
CHUNK = 1024
CHANNELS = 1
SILENCE_THRESHOLD = 0.02  # RMS energy threshold for VAD
SILENCE_DURATION = 1.0  # seconds of silence before stopping

async def main():
    print("Sophie Desktop Client starting...")

    # Start openWakeWord listener
    try:
        resp = requests.post(f"{OPENWAKEWORD_URL}/start", timeout=5)
        print(f"openWakeWord started: {resp.status_code}")
    except Exception as e:
        print(f"Warning: Could not start openWakeWord: {e}")

    # Open microphone stream
    stream = sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=CHANNELS,
        dtype="float32",
        blocksize=CHUNK,
    )

    with stream:
        while True:
            # Poll for wake word
            try:
                resp = requests.post(f"{OPENWAKEWORD_URL}/detect", timeout=2)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get("detected"):
                        print(">>> Wake word detected! Starting recording...")
                        # Record until silence
                        audio_chunks = []
                        silence_count = 0

                        while True:
                            audio_data, overflowed = stream.read(CHUNK)
                            if not overflowed:
                                audio_chunks.append(audio_data.tobytes())
                                # Check for silence
                                rms = np.sqrt(np.mean(audio_data ** 2))
                                if rms < SILENCE_THRESHOLD:
                                    silence_count += 1
                                    if silence_count * (CHUNK / SAMPLE_RATE) >= SILENCE_DURATION:
                                        print(f"Silence detected. Stopping recording.")
                                        break
                                else:
                                    silence_count = 0

                        # Process audio through agent
                        if audio_chunks:
                            await process_audio(audio_chunks)

            except Exception as e:
                print(f"Error: {e}")
                await asyncio.sleep(1)

async def process_audio(audio_chunks: list[bytes]):
    """Send audio to agent for transcription and response."""
    # Create WAV file in memory
    wav_bytes = create_wav(audio_chunks, SAMPLE_RATE, CHANNELS)

    # Send to STT
    try:
        with requests.post(
            f"{AGENT_URL}/stt/file",
            files={"audio": ("audio.wav", wav_bytes, "audio/wav")},
            timeout=30,
        ) as resp:
            if resp.status_code == 200:
                stt_result = resp.json()
                user_text = stt_result.get("text", "")
                if user_text.strip():
                    print(f"STT: {user_text}")

                    # Send to chat
                    with requests.post(
                        f"{AGENT_URL}/chat/blocking",
                        json={"message": user_text},
                        timeout=60,
                    ) as chat_resp:
                        if chat_resp.status_code == 200:
                            reply = chat_resp.json().get("reply", "")
                            print(f"Sophie: {reply}")

                            # Speak response
                            await speak(reply)
            else:
                print(f"STT error: {resp.status_code}")
    except Exception as e:
        print(f"Error processing audio: {e}")

def create_wav(chunks: list[bytes], sample_rate: int, channels: int) -> bytes:
    """Create a WAV file in memory from audio chunks."""
    buf = io.BytesIO()
    buf.write(b"RIFF")
    buf.write(b"\x00\x00\x00\x00")  # placeholder for file size
    buf.write(b"WAVEfmt ")
    buf.write(struct.pack("<i", 16))  # Subchunk1Size
    buf.write(struct.pack("<H", 1))  # AudioFormat (PCM)
    buf.write(struct.pack("<H", channels))  # NumChannels
    buf.write(struct.pack("<I", sample_rate))  # SampleRate
    buf.write(struct.pack("<I", sample_rate * channels * 4))  # ByteRate
    buf.write(struct.pack("<H", channels * 4))  # BlockAlign
    buf.write(struct.pack("<H", 32))  # BitsPerSample
    buf.write(b"data")

    data_bytes = b"".join(chunks)
    buf.write(struct.pack("<I", len(data_bytes)))
    buf.write(data_bytes)

    # Update file size
    file_size = len(buf.getvalue()) - 8
    buf.seek(4)
    buf.write(struct.pack("<I", file_size))

    return buf.getvalue()

async def speak(text: str):
    """Speak text using the TTS service."""
    try:
        resp = requests.post(
            f"{AGENT_URL}/tts",
            json={"text": text},
            timeout=10,
        )
        if resp.status_code == 200:
            # Play audio through speakers
            audio_data = np.frombuffer(resp.content, dtype=np.float32)
            sd.play(audio_data, samplerate=SAMPLE_RATE)
            sd.wait()
    except Exception as e:
        print(f"TTS error: {e}")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nStopping...")
        try:
            requests.post(f"{OPENWAKEWORD_URL}/stop", timeout=2)
        except Exception:
            pass
