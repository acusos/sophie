# Sophie Project — Handover Document

Project: https://github.com/acusos/sophie
Branch: master
Last updated: 2026-07-06

## 1. Current State

| Feature | Status |
|---|---|
| STT (faster-whisper) via /transcribe | Working |
| LLM (Qwopus 3.6 27B Q4_K_S Coder MTP) streaming | Working — but ignores tool results (known issue) |
| TTS (Piper) via /tts endpoint | Working |
| Voice chat on PC browsers | Working |
| Voice chat on iPhone Chrome (PTT + VAD) | Working |
| Memory service wired | Working |
| Tool calling (shell, docker, memory_search, web_search, email, time) | Working |
| Web search (SearXNG) | Working — Google + DuckDuckGo, 60s cache, 8s timeout |
| Proactive alerts | Working — queue + TTS delivery |
| Proactive alerts scheduler | Working — every 30 min |
| Email checking (read-only) | Wired — needs App Passwords |
| Time/date lookup | Working — 24-hour format, AEST |
| VAD-based activation (Silero) | Working |

## 2. Architecture

Servers:
- aiclient (192.168.20.112) — Agent orchestrator, UI, TTS, VAD, memory, SearXNG, alerts. SSH: me@aiclient
- ai (192.168.20.116) — LLM (llama.cpp), STT, Qdrant. SSH: me@ai

Ports:
- aiclient:8090 — Agent Service (FastAPI)
- aiclient:8002 — Memory service
- aiclient:8092 — Piper TTS
- aiclient:8093 — openWakeWord (VAD-based)
- aiclient:8888 — SearXNG
- ai:8080 — llama.cpp
- ai:8091 — faster-whisper STT
- ai:6333 — Qdrant
- ai:11434 — Ollama

## 3. Codebase

/opt/ai/projects/sophie/aiclient/ — in git
  docker-compose.yml
  agent-service/Dockerfile — ffmpeg, curl, git, docker CLI, TZ=AEST
  agent-service/app/main.py — FastAPI agent
  agent-service/app/email_tool.py — Email IMAP tool
  agent-service/app/HANDOVER.md — this file
  agent-service/app/static/app.js
  agent-service/app/static/style.css
  agent-service/app/templates/index.html
  tts/Dockerfile
  openwakeword/Dockerfile, main.py — Silero VAD
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

## 4. Tool Detection Order

1. Time — what time, what day, what date
2. Information questions — what is the price of X, tell me about X
3. General questions — what is X, who is X, how to X -> web_search
4. Email — check my email, read email
5. Docker — docker ps, docker stop X
6. Shell with repo — check acusos git status
7. Shell — run X, check X, show X
8. Web search — search for X
9. Task — task: shell:ls
10. Memory — what do you remember about X
11. File — read file X

## 5. Email Configuration

Email tool uses IMAP. Env vars in docker-compose.yml:
EMAIL_GMAIL_ENABLED: true, EMAIL_GMAIL_USER, EMAIL_GMAIL_PASS
EMAIL_OUTLOOK_ENABLED: true, EMAIL_OUTLOOK_USER, EMAIL_OUTLOOK_PASS
Both require App Passwords if 2FA enabled.

## 6. Time Tool

get_time() returns AEST, 24-hour format, date and time.

## 7. Web Search

Engines: Google + DuckDuckGo only. Timeout: 8s. Cache: 60s TTL. Locale: en-AU.

## 8. System Prompt Enforces

- Playful but professional personality
- Brevity — 1-2 sentences
- No greetings, no sign-offs
- No terms of endearment
- No emojis, no markdown
- 24-hour time
- No apologies for tool errors

## 9. VAD Configuration

Silero VAD. Min speech: 30 frames. Pause: 40 frames. Cooldown: 60 frames.
Frontend threshold: 0.005. Silence threshold: 60 frames.

## 10. What Needs to Be Done

High Priority:
- Replace Qwopus with instruct model (root cause of tool result ignoring)
- Fix SearXNG weather check retry
- Configure email App Passwords

Medium Priority:
- Telegram integration (repo: https://github.com/acusos/telegrambot)
- iOS Shortcut Automation for VAD/Siri conflict
- Auto-clone repos
- Email reply capability
- Calendar integration

Low Priority:
- Extend alert scheduler
- SMS via Twilio

## 11. Known Issues

- Qwopus ignores TOOL_RESULT injection
- Qwopus is verbose with tool results
- SearXNG weather check may fail on startup
- Email needs App Passwords

## 12. Golden Rule

PC browsers working — test /chat and VAD changes against PC Chrome first.

## 13. Files for Quick Review

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

## 14. SSH

ssh me@aiclient -> aiclient (192.168.20.112)
ssh me@ai -> ai server (192.168.20.116)

## 15. Session Summary

1. Email tool added (email_tool.py)
2. Time tool added (get_time())
3. Shell detection expanded (check, show)
4. Tool detection order fixed
5. System prompt updated (playful, brief, no endearments)
6. Git and docker CLI added to agent container
7. TTS restarted
8. openWakeWord replaced with Silero VAD
9. Web search improved (cache, reduced engines, timeout)
10. Timezone AEST added to agent container
11. Information question detection added for natural queries
