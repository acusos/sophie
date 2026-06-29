# Sophie — Personal Voice Assistant

Sophie is a distributed personal voice assistant running across two servers:

- **aiclient** (192.168.20.112) — Intel i9-12900HK — Agent service, TTS, wake word detection, tools
- **ai** (192.168.20.116) — AMD Ryzen 9 9950X3D + GPU — STT (faster-whisper), LLM (llama.cpp), memory (Qdrant)

## Architecture

```
┌─────────────────────────────────────────────────────────────────────┐
│ aiclient (192.168.20.112)                                         │
│                                                                     │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────────┐   │
│  │openWake  │  │piper TTS │  │desktop   │  │agent-service     │   │
│  │Word:8093 │  │:8092     │  │client    │  │:8090 (web UI)    │   │
│  └────┬─────┘  └─────┬────┘  └────┬─────┘  └──────┬─────▲─────┘   │
│       │               │            │               │       │         │
└───────┼───────────────┼────────────┼───────────────┼───────┼────────┘
        │               │            │               │       │
        │               │            │               │       │
┌───────┼───────────────┼────────────┼───────────────┼───────┼────────┐
│ ai (192.168.20.116)                                         │         │
│                                                               │         │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────┐ │         │
│  │faster-wh │  │llama.cpp │  │qdrant    │  │memory-api    │ │         │
│  │isper:8091│  │:8080     │  │:6333-34  │  │:8002         │ │         │
│  └─────┬────┘  └─────┬────┘  └─────┬────┘  └──────┬───────┘ │         │
└────────┼─────────────┼────────────┼─────────────────┼────────┘
         │             │            │                 │
         │             │            │                 │
    STT  │         LLM  │        RAG  │          Memory │
         │             │            │                 │
```

## Services

| Service | Host | Port | Purpose |
|---------|------|------|---------|
| faster-whisper | ai | 8091 | Speech-to-text (large-v3-turbo, CUDA) |
| llama.cpp | ai | 8080 | LLM (Qwopus3.6-27B-Coder-MTP) |
| qdrant | ai | 6333-6334 | Vector database for RAG |
| memory-api | ai | 8002 | Memory service (embedding + retrieval) |
| agent-service | aiclient | 8090 | Orchestrator, chat, STT/TTS proxy, web UI, tools |
| piper-tts | aiclient | 8092 | Text-to-speech (local, fast) |
| openwakeword | aiclient | 8093 | Wake word detection ("sophie") |

## Phase 1: Voice Chat Loop

- **STT**: faster-whisper on ai with CUDA, receives base64 audio or WAV files
- **TTS**: Piper TTS on aiclient, local and fast
- **LLM**: llama.cpp on ai with 131k context
- **Web UI**: Push-to-talk interface optimized for iPhone Safari

## Phase 2: Hands-Free

- **openWakeWord**: Detects "sophie" wake word via microphone on aiclient
- **Desktop client**: Continuous listening loop that captures audio after wake word and sends to agent

## Phase 3: Memory (RAG)

- **Memory search**: `/memory/search` — retrieves relevant memories before LLM response
- **Memory store**: `/memory/store` — saves conversation pairs to Qdrant via memory-api
- **Memory list**: `/memory/list` — lists all stored memories
- **Memory delete**: `/memory/{id}` — removes a memory

## Phase 4: Tools

| Endpoint | Purpose |
|----------|---------|
| `POST /tools/shell` | Execute shell commands on aiclient |
| `POST /tools/docker` | Run Docker commands (ps, stop, start, rm, logs, pull, inspect, top, images, info) |
| `POST /tools/file` | Read, write, or list files (restricted to /home and /tmp) |
| `POST /tools/homeassistant` | Query state or control entities via Home Assistant API |
| `POST /tools/memory/search` | Search memory via tool endpoint |

## Phase 5: Autonomy

- **Multi-step tasks**: `POST /tasks/run` with list of steps (shell, docker, file, sleep, speak)
- **Task tracking**: `GET /tasks/{task_id}` for progress, `DELETE /tasks/{task_id}` to cancel
- **Proactive alerts**: `POST /alerts/send` to queue spoken alerts, `GET /alerts/pending` to list

## Deployment

### 1. Deploy STT on ai (192.168.20.116)

```bash
# On ai server
cd sophie/ai
docker compose up -d --build
```

### 2. Deploy Agent Services on aiclient (192.168.20.112)

```bash
# On aiclient server
cd sophie/aiclient
docker compose up -d --build
```

### 3. Start Desktop Voice Client (optional)

```bash
# On aiclient, run manually (not as a daemon)
docker compose run --rm desktop-client
```

### 4. Configure Environment Variables

Set in `.env` or as environment variables on aiclient:

| Variable | Default | Purpose |
|----------|---------|---------|
| LLAMA_URL | http://192.168.20.116:8080 | LLM endpoint |
| STT_URL | http://192.168.20.116:8091 | STT endpoint |
| TTS_URL | http://127.0.0.1:8092 | TTS endpoint |
| MEMORY_API_URL | http://192.168.20.116:8002 | Memory service |
| QDRANT_URL | http://192.168.20.116:6333 | Qdrant vector DB |
| OPENWAKEWORD_URL | http://127.0.0.1:8093 | Wake word service |
| HA_URL | | Home Assistant URL |
| HA_TOKEN | | Home Assistant token |
| ALERTS_ENABLED | false | Enable proactive alerts |

## API Quick Start

```bash
# Chat
curl -X POST http://192.168.20.112:8090/chat/blocking \
  -H "Content-Type: application/json" \
  -d '{"message": "Hello Sophie", "session_id": "test"}'

# STT (base64 audio)
curl -X POST http://192.168.20.112:8090/stt \
  -H "Content-Type: application/json" \
  -d '{"audio_b64": "..."}'

# TTS
curl -X POST http://192.168.20.112:8090/tts \
  -H "Content-Type: application/json" \
  -d '{"text": "Hello!"}' --output hello.wav

# Shell tool
curl -X POST http://192.168.20.112:8090/tools/shell \
  -H "Content-Type: application/json" \
  -d '{"command": "uptime"}'

# Run a multi-step task
curl -X POST http://192.168.20.112:8090/tasks/run \
  -H "Content-Type: application/json" \
  -d '{"steps": ["shell: uptime", "docker: ps"]}'
```

## Web UI

Open `http://192.168.20.112:8090` in your browser. The interface supports:
- Push-to-talk voice recording
- Text chat input
- Audio playback of Sophie's responses
- Session management
