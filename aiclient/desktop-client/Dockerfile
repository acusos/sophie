FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends alsa-utils portaudio19-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir \
    sounddevice==0.5.1 \
    pyaudio==0.2.14 \
    requests==2.32.3 \
    numpy==2.2.3 \
    pydub==0.25.1

COPY main.py /app/main.py

EXPOSE 8094

CMD ["python", "main.py"]
