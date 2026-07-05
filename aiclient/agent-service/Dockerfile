FROM python:3.12-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg curl && \
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

# Install Docker CLI
RUN curl -fsSL https://download.docker.com/linux/static/stable/x86_64/docker-27.5.1.tgz | tar xz --strip-components=1 -C /usr/local/bin docker/docker && \
    chmod +x /usr/local/bin/docker
