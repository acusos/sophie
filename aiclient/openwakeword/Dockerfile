FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential portaudio19-dev ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir \
    openwakeword==0.4.0 \
    fastapi==0.115.8 \
    "uvicorn[standard]==0.34.2" \
    sounddevice==0.5.1 \
    pyaudio==0.2.14

COPY main.py /app/main.py

EXPOSE 8093

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8093"]
