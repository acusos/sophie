FROM debian:bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends wget unzip python3 python3-pip libsndfile1 && \
    rm -rf /var/lib/apt/lists/* && \
    python3 -m pip install --break-system-packages \
    fastapi==0.115.8 \
    "uvicorn[standard]==0.34.2" \
    piper-tts==1.4.2

# Download the amy voice model (female English)
RUN mkdir -p /models/amy && \
    wget -qO /models/amy/en_US-amy-medium.onnx https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/amy/medium/en_US-amy-medium.onnx && \
    wget -qO /models/amy/en_US-amy-medium.onnx.json https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/amy/medium/en_US-amy-medium.onnx.json

COPY main.py /app/main.py
WORKDIR /app
EXPOSE 8092
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8092"]
