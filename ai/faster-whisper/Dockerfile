FROM nvidia/cuda:12.8.0-runtime-ubuntu24.04

RUN apt-get update && \
    apt-get install -y --no-install-recommends python3 python3-pip python3-venv ffmpeg && \
    rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"

RUN pip install --no-cache-dir \
    faster-whisper==1.1.1 \
    pydantic==2.10.6 \
    fastapi==0.115.8 \
    "uvicorn[standard]==0.34.2" \
    pydub==0.25.1 \
    soundfile==0.13.1 \
    numpy==2.2.3 \
    requests==2.32.3 \
    python-multipart==0.0.20

WORKDIR /app
COPY main.py /app/main.py

EXPOSE 8091
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8091"]
