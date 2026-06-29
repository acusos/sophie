FROM debian:bookworm-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends wget unzip python3 python3-pip libsndfile1 && \
    rm -rf /var/lib/apt/lists/* && \
    python3 -m pip install --break-system-packages \
    fastapi==0.115.8 \
    "uvicorn[standard]==0.34.2" \
    piper-tts==1.2.2

# Download the amy voice model (female English)
RUN wget -qO /tmp/amy.zip https://huggingface.co/rhasspy/piper-voices/resolve/v1.0.0/en/en_US/amy/medium/en_US-amy-medium.zip && \
    mkdir -p /models/amy && \
    unzip -o /tmp/amy.zip -d /models/amy && \
    rm -f /tmp/amy.zip

COPY main.py /app/main.py
WORKDIR /app
EXPOSE 8092
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8092"]
