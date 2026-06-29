FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN pip install --no-cache-dir \
    fastapi==0.115.8 \
    "uvicorn[standard]==0.34.2" \
    pydantic==2.10.6 \
    httpx==0.28.1 \
    python-multipart==0.0.20 \
    jinja2==3.1.6 \
    websockets==14.2

COPY . /app/

EXPOSE 8090

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
