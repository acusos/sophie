# Sophie Project — Handover Document

Project: https://github.com/acusos/sophie
Branch: master
Date: 2026-07-06

## 1. Current State

| Feature | Status |
|---|---|
| STT (faster-whisper) via /transcribe | Working |
| LLM (Qwopus 3.6 27B Q4_K_S Coder MTP) streaming | Working |
| TTS (Piper) via /tts endpoint | Working |
| Voice chat on PC browsers | Working |
| Voice chat on iPhone Chrome (PTT + VAD) | Working |
| Memory service wired | Working |
| Tool calling (shell, docker, memory_search, web_search, email, time) | Working |
| Web search (SearXNG) | Working |
| Proactive alerts | Working |
| Proactive alerts scheduler | Working |
| Email checking (read-only) | Wired — needs credentials |
| Time/date lookup | Working |
| VAD-based activation (Silero) | Working — replaced openWakeWord with VAD |

## 2. Architecture

Servers:
- aiclient (192.168.20.112) — Agent orchestrator, UI, TTS, VAD, memory service, SearXNG, alerts. SSH: me@aiclient
- ai (192.168.20.116) — LLM (llama.cpp), STT, Qdrant. SSH: me@ai

Ports:
- aiclient:8090 — Agent Service (FastAPI)
- aiclient:8002 — Memory service
- aiclient:8092 — Piper TTS
- aiclient:8093 — openWakeWord (now VAD-based)
- aiclient:8888 — SearXNG
- ai:8080 — llama.cpp (Qwopus 3.6 27B Coder MTP)
- ai:8091 — faster-whisper STT
- ai:6333 — Qdrant
- ai:11434 — Ollama (nomic-embed-text)

## 3. Codebase

/opt/ai/projects/sophie/aiclient/ — aiclient project root (in git)
  docker-compose.yml
  agent-service/Dockerfile — includes ffmpeg, curl, git, docker CLI, timezone (AEST)
  agent-service/app/main.py — FastAPI agent — chat, STT, TTS, tools, alerts, scheduler, time
  agent-service/app/email_tool.py — Email IMAP tool (Gmail, Outlook)
  agent-service/app/HANDOVER.md — this file
  agent-service/app/static/app.js
  agent-service/app/static/style.css
  agent-service/app/templates/index.html
  tts/Dockerfile
  openwakeword/Dockerfile, main.py — now uses Silero VAD instead of wake word
  searxng/settings.yml
  desktop-client/Dockerfile, main.py

/opt/ai/projects/memory-service/ — NOT in git
  app/main.py, api/enrichment.py, api/retrieval.py
  services/qdrant_service.py, services/retrieval_service.py
  database/models.py, database/session.py
  workers/extraction_worker.py
  docker/Dockerfile

/data/projects/sophie/ai/ — ai server, NOT in git
  docker-compose.yml
  faster-whisper/Dockerfile, main.py

## 4. Services (systemd)

| Server | Service | Path |
|---|---|---|
| aiclient | sophie.service | /etc/systemd/system/sophie.service |
| ai | sophie.service | /etc/systemd/system/sophie.service |

## 5. Tool Detection Order

detect_tool_use matches in this order (first match wins):
1. Time — "what time", "what day", "what date", "time", "date"
2. Email — "check my email", "read email from gmail", "fetch mail"
3. Docker — "docker ps", "docker stop X"
4. Shell with repo — "check acusos git status" -> cd /opt/ai/projects/acusos && git status
5. Shell — "run X", "check X", "show X", "execute X", "do X"
6. Web search — "search for X", "web search X"
7. Task — "task: shell:ls, shell:pwd"
8. Memory — "what do you remember about X"
9. File — "read file X", "list file X"

## 6. Email Configuration

Email tool uses IMAP via email_tool.py. Configure via env vars in docker-compose.yml:

EMAIL_GMAIL_ENABLED: "true"
EMAIL_GMAIL_USER: "your-gmail@gmail.com"
EMAIL_GMAIL_PASS: "app-password"
EMAIL_OUTLOOK_ENABLED: "true"
EMAIL_OUTLOOK_USER: "your-outlook@outlook.com"
EMAIL_OUTLOOK_PASS: "app-password"

Both Gmail and Outlook require App Passwords (not regular passwords) if 2FA is enabled.

## 7. Time Tool

The get_time() function returns current date and time in AEST (Australia/Sydney).
Always uses 24-hour format (e.g. 23:06 not 11:06 PM).
Returns both date and time in replies.

## 8. Key Decisions / History

- Qwopus 3.6 27B Coder MTP (Q4_K_S) — current LLM. Known to be verbose and sometimes ignore tool results.
- faster-whisper model at /models/gguf/whisper/faster-whisper-large-v3-turbo/ on ai server
- GPU memory issue resolved by --gpu-layers 1 on llama-cpp
- docker-compose.yml — removed ports from agent-service (incompatible with network_mode: host)
- desktop-client Dockerfile — added build-essential for pyaudio
- Agent container now includes: ffmpeg, curl, git, docker CLI, timezone AEST
- Email tool added (read-only Gmail + Outlook via IMAP)
- Shell detection expanded to include "check", "show" keywords
- System prompt updated with tool descriptions, repo paths, failure handling
- openWakeWord replaced with Silero VAD for speech detection (no wake word needed)
- Time tool added with 24-hour format
- System prompt enforces: no greetings, no sign-offs, no emojis, no markdown, 24-hour time

## 9. What Needs to Be Done

High Priority:
- Replace Qwopus with two models: one instruct (conversation) and one coder (tool calling)
- Fix SearXNG weather check retry mechanism (may fail on startup)
- Configure email credentials (App Passwords for Gmail/Outlook)

Medium Priority:
- Extend alert scheduler — more triggers (reminders, news, etc.)
- Improve email tool — add reply capability
- Auto-clone repos on request when user asks about missing repos
- Calendar integration (Google Calendar, Outlook Calendar)

## 10. Files for Quick Review (Batch)

cat /opt/ai/projects/sophie/aiclient/agent-service/app/main.py
cat /opt/ai/projects/sophie/aiclient/agent-service/app/email_tool.py
cat /opt/ai/projects/sophie/aiclient/agent-service/Dockerfile
cat /opt/ai/projects/sophie/aiclient/docker-compose.yml
cat /opt/ai/projects/sophie/aiclient/openwakeword/main.py
cat /opt/ai/projects/sophie/aiclient/searxng/settings.yml
cat /opt/ai/projects/sophie/aiclient/desktop-client/Dockerfile
cat /opt/ai/projects/sophie/aiclient/agent-service/app/templates/index.html
cat /opt/ai/projects/sophie/aiclient/agent-service/app/static/app.js
cat /opt/ai/projects/sophie/aiclient/agent-service/app/static/style.css
cat /etc/systemd/system/sophie.service
cat /opt/ai/projects/memory-service/app/main.py
cat /opt/ai/projects/memory-service/app/api/enrichment.py
cat /opt/ai/projects/memory-service/app/api/retrieval.py
cat /opt/ai/projects/memory-service/app/services/qdrant_service.py
cat /opt/ai/projects/memory-service/app/services/retrieval_service.py
cat /opt/ai/projects/memory-service/app/database/models.py
cat /opt/ai/projects/memory-service/app/database/session.py
cat /opt/ai/projects/memory-service/app/workers/extraction_worker.py
cat /data/projects/sophie/ai/faster-whisper/main.py
cat /data/projects/sophie/ai/docker-compose.yml
ls -la /models/gguf/
ls -la /models/gguf/whisper/
docker ps
git status

## 11. SSH / Access

ssh me@aiclient -> aiclient (192.168.20.112)
ssh me@ai -> ai server (192.168.20.116)

DO NOT use acusos@192.168.20.112 or acusos@192.168.20.116

## 12. Known Issues

- SearXNG weather check may fail on startup — retry mechanism needed
- Qwopus ignores context — known limitation of Coder model
- Qwopus is verbose with tool results — reports them rather than processing them
- Email tool needs App Passwords configured (Gmail + Outlook both require them with 2FA)

## 13. Golden Rule

PC browsers are working with the current code — any change to /chat or the VAD loop must be tested against PC Chrome first.
