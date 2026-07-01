import os
import asyncio
import re
import json
import base64
import uuid
import time
import subprocess
import shlex
from pathlib import Path
from typing import AsyncGenerator

import httpx
import uvicorn
from fastapi import FastAPI, UploadFile, File, HTTPException, Request, Body, Query
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, StreamingResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

# ── Configuration ──────────────────────────────────────────────────────────
LLAMA_URL = os.getenv("LLAMA_URL", "http://192.168.20.116:8080")
LLAMA_KEY = os.getenv("LLAMA_KEY", "")
STT_URL = os.getenv("STT_URL", "http://192.168.20.116:8091")
TTS_URL = os.getenv("TTS_URL", "http://127.0.0.1:8092")
MEMORY_API_URL = os.getenv("MEMORY_API_URL", "http://192.168.20.116:8000")
QDRANT_URL = os.getenv("QDRANT_URL", "http://192.168.20.116:6333")
OPENWAKEWORD_URL = os.getenv("OPENWAKEWORD_URL", "http://127.0.0.1:8093")
HA_URL = os.getenv("HA_URL", "")
HA_TOKEN = os.getenv("HA_TOKEN", "")
MAX_TOOL_OUTPUT = 4096

def normalize_for_tts(text: str) -> str:
    """Replace X.0 with X so Piper doesn't say point zero."""
    return re.sub(r'\b(\d+)\.0\b', r'\1', text)

SYSTEM_PROMPT = os.getenv(
    "SYSTEM_PROMPT",
    (
        "You are Sophie, a warm and helpful personal voice assistant. "
        "Keep responses concise and natural for spoken delivery. "
        "Use contractions and avoid overly formal language. "
        "When the user asks you to perform an action, use the tools "
        "available to you. Speak naturally about what you're doing."
    ),
)

# ── App ────────────────────────────────────────────────────────────────────
app = FastAPI(title="Sophie Agent")
templates = Jinja2Templates(directory="app/templates")

app.mount("/static", StaticFiles(directory="app/static"), name="static")

# In-memory conversation store: session_id -> list of messages
conversations: dict[str, list[dict]] = {}

# Persistent HTTP client for upstream calls
http_client = httpx.AsyncClient(timeout=120.0)

LLAMA_HEADERS = {
    "Content-Type": "application/json",
}
if LLAMA_KEY:
    LLAMA_HEADERS["Authorization"] = f"Bearer {LLAMA_KEY}"

# ── Pydantic models ───────────────────────────────────────────────────────
class ChatRequest(BaseModel):
    message: str
    session_id: str = ""

class ChatResponse(BaseModel):
    reply: str
    session_id: str

class STTRequest(BaseModel):
    audio_b64: str

class STTResponse(BaseModel):
    text: str

class TTSRequest(BaseModel):
    text: str

class MemoryStoreRequest(BaseModel):
    text: str
    tags: list[str] = []
    user_id: str = "default"

class MemorySearchRequest(BaseModel):
    query: str
    limit: int = 5

class ShellCommandRequest(BaseModel):
    command: str
    timeout: int = 30

class DockerCommandRequest(BaseModel):
    action: str
    name_or_id: str = ""
    flags: str = ""

class FileOperationRequest(BaseModel):
    action: str
    path: str
    content: str = ""

class HomeAssistantRequest(BaseModel):
    action: str
    entity_id: str = ""
    value: str = ""

class TaskRequest(BaseModel):
    steps: list[str]

class TaskStatusResponse(BaseModel):
    task_id: str
    step: int
    total_steps: int
    status: str
    output: str = ""

# ── Tool: Shell ────────────────────────────────────────────────────────────
async def run_shell(cmd: str, timeout: int = 30) -> str:
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            ),
            timeout=2,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        result = stdout.decode().strip()
        if result and len(result) > MAX_TOOL_OUTPUT:
            result = result[:MAX_TOOL_OUTPUT] + "...\n(truncated)"
        if proc.returncode != 0 and stderr:
            err = stderr.decode().strip()[:512]
            result += f"\nError: {err}"
        return result
    except asyncio.TimeoutError:
        return f"Command timed out after {timeout}s"
    except Exception as e:
        return f"Error: {e}"

# ── Tool: Docker ───────────────────────────────────────────────────────────
async def docker_action(action: str, name: str = "", flags: str = "") -> str:
    safe_actions = ["ps", "stop", "start", "rm", "logs", "pull", "inspect", "top", "images", "info"]
    if action not in safe_actions:
        return f"Denied: action '{action}' not in allowed list"
    cmd = f"docker {action}"
    if flags:
        cmd += f" {flags}"
    if name:
        cmd += f" {shlex.quote(name)}"
    return await run_shell(cmd, timeout=15)

# ── Tool: Filesystem ───────────────────────────────────────────────────────
async def file_operation(action: str, path: str, content: str = "") -> str:
    p = Path(path).resolve()
    if not (str(p).startswith("/home") or str(p).startswith("/tmp")):
        return f"Denied: path must be under /home or /tmp, got {p}"
    try:
        if action == "list":
            if p.is_dir():
                entries = [str(e) for e in p.iterdir()]
                return "\n".join(entries)
            return f"Not a directory: {p}"
        elif action == "read":
            return p.read_text()[:MAX_TOOL_OUTPUT]
        elif action == "write":
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
            return f"Wrote {len(content)} bytes to {p}"
        else:
            return f"Unknown action: {action}"
    except Exception as e:
        return f"Error: {e}"

# ── Tool: Home Assistant ───────────────────────────────────────────────────
async def ha_action(action: str, entity_id: str, value: str = "") -> str:
    if not HA_URL or not HA_TOKEN:
        return "Home Assistant not configured"
    url = f"{HA_URL}/api/states/{entity_id}" if action == "state" else f"{HA_URL}/api/services/switch/{value}"
    headers = {"Authorization": f"Bearer {HA_TOKEN}", "Content-Type": "application/json"}
    try:
        if action == "state":
            resp = await http_client.get(url, headers=headers, timeout=5)
            return resp.json().get("attributes", {}).get("state", "") or resp.text[:256]
        elif action == "control":
            service_data = {"entity_id": entity_id}
            resp = await http_client.post(
                f"{HA_URL}/api/services/switch/{value}",
                json=service_data,
                headers=headers,
                timeout=5,
            )
            return "OK" if resp.status_code == 200 else resp.text[:256]
        else:
            return f"Unknown HA action: {action}"
    except Exception as e:
        return f"HA error: {e}"

# ── Tool: Memory ───────────────────────────────────────────────────────────
async def memory_search(query: str, limit: int = 5) -> str:
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(
                f"{MEMORY_API_URL}/api/search",
                json={"query": query, "limit": limit},
            )
            if resp.status_code != 200:
                return "Memory search failed"
            results = resp.json()
            return "\n".join(f"- {r.get('content', '')}" for r in results[:limit] if r.get("content"))
        except Exception:
            return "Memory service unavailable"

async def memory_store(text: str, tags: list[str] = None) -> str:
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(
                f"{MEMORY_API_URL}/api/store",
                json={"text": text, "tags": tags or []},
            )
            return "Stored" if resp.status_code == 200 else resp.text[:256]
        except Exception:
            return "Memory service unavailable"

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest):
    session_id = req.session_id or str(uuid.uuid4())
    if session_id not in conversations:
        conversations[session_id] = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Retrieve relevant memory
    try:
        resp = await http_client.post(
            f"{MEMORY_API_URL}/api/search",
            json={"query": req.message, "limit": 5},
            timeout=5.0,
        )
        if resp.status_code == 200:
            memory_results = resp.json()
            if memory_results:
                memory_context = "\n".join(
                    f"- {m.get('content', '')}" for m in memory_results if m.get("content")
                )
                if memory_context.strip():
                    conversations[session_id][0]["content"] += (
                        f"\n\nRelevant memories:\n{memory_context}"
                    )
    except Exception:
        pass

    conversations[session_id].append({"role": "user", "content": req.message})

    async def generate():
        payload = {
            "messages": conversations[session_id],
            "stream": True,
            "max_tokens": 2048,
        }
        async with http_client.stream(
            "POST",
            f"{LLAMA_URL}/v1/chat/completions",
            json=payload,
            headers=LLAMA_HEADERS,
        ) as resp:
            if resp.status_code != 200:
                body = await resp.aread()
                yield json.dumps({"error": body.decode(), "done": True})
                return
            full_reply = ""
            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data = line[6:]
                if data.strip() == "[DONE]":
                    break
                try:
                    chunk = json.loads(data)
                    delta = (
                        chunk.get("choices", [{}])[0]
                        .get("delta", {})
                        .get("content", "")
                    )
                    if delta:
                        full_reply += delta
                        yield json.dumps({"token": delta, "partial": full_reply})
                except json.JSONDecodeError:
                    continue

            conversations[session_id].append(
                {"role": "assistant", "content": full_reply}
            )

            try:
                await http_client.post(
                    f"{MEMORY_API_URL}/api/store",
                    json={
                        "user_message": req.message,
                        "assistant_reply": full_reply,
                        "session_id": session_id,
                        "timestamp": time.time(),
                    },
                    timeout=5.0,
                )
            except Exception:
                pass

            yield json.dumps({"done": True})

    return StreamingResponse(generate(), media_type="text/event-stream")

@app.post("/chat/blocking", response_model=ChatResponse)
async def chat_blocking(req: ChatRequest):
    session_id = req.session_id or str(uuid.uuid4())
    if session_id not in conversations:
        conversations[session_id] = [{"role": "system", "content": SYSTEM_PROMPT}]

    conversations[session_id].append({"role": "user", "content": req.message})

    payload = {
        "messages": conversations[session_id],
        "max_tokens": 2048,
    }
    resp = await http_client.post(
        f"{LLAMA_URL}/v1/chat/completions",
        json=payload,
        headers=LLAMA_HEADERS,
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=resp.text)

    data = resp.json()
    reply = data.get("choices", [{}])[0].get("message", {}).get("content", "")
    conversations[session_id].append({"role": "assistant", "content": reply})
    return ChatResponse(reply=reply, session_id=session_id)

@app.post("/stt", response_model=STTResponse)
async def stt(req: STTRequest):
    resp = await http_client.post(
        f"{STT_URL}/transcribe-b64",
        json={"audio_b64": req.audio_b64, "language": "en"},
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=resp.text)
    return STTResponse(**resp.json())

@app.post("/stt/file", response_model=STTResponse)
async def stt_file(audio: UploadFile = File(...)):
    """Forward audio file to faster-whisper service."""
    audio_bytes = await audio.read()
    async with http_client.stream(
        "POST",
        f"{STT_URL}/transcribe",
        files={"audio": (audio.filename or "audio.wav", audio_bytes, audio.content_type or "audio/webm")},
        data={"language": "en"},
    ) as resp:
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=await resp.aread())
        data = json.loads(await resp.aread())
        text = data.get("text", "")
        if isinstance(text, bytes):
            text = text.decode("utf-8")
        return STTResponse(text=text)

@app.post("/tts")
async def tts(req: TTSRequest):
    cleaned_text = normalize_for_tts(req.text)
    resp = await http_client.post(
        f"{TTS_URL}/tts",
        json={"text": cleaned_text},
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=resp.text)
    return StreamingResponse(
        iter([resp.content]),
        media_type="audio/wav",
        headers={"X-Audio-Length": str(len(resp.content))},
    )

@app.delete("/session/{session_id}")
async def delete_session(session_id: str):
    conversations.pop(session_id, None)
    return {"status": "deleted"}

# ── Phase 3: Memory endpoints ──────────────────────────────────────────────
@app.post("/memory/search")
async def search_memory(req: MemorySearchRequest):
    results = await memory_search(req.query, req.limit)
    return {"results": results}

@app.post("/memory/store")
async def store_memory(req: MemoryStoreRequest):
    result = await memory_store(req.text, req.tags)
    return {"status": result}

@app.get("/memory/list")
async def list_memory():
    try:
        resp = await http_client.get(f"{MEMORY_API_URL}/memories", timeout=5)
        return {"memories": resp.json()} if resp.status_code == 200 else {"error": resp.text}
    except Exception as e:
        return {"error": str(e)}

@app.delete("/memory/{memory_id}")
async def delete_memory(memory_id: str):
    try:
        resp = await http_client.delete(f"{MEMORY_API_URL}/memories/{memory_id}", timeout=5)
        return {"status": "deleted"} if resp.status_code == 200 else {"error": resp.text}
    except Exception as e:
        return {"error": str(e)}

# ── Phase 4: Tool endpoints ────────────────────────────────────────────────
@app.post("/tools/shell")
async def tool_shell(req: ShellCommandRequest):
    result = await run_shell(req.command, req.timeout)
    return {"output": result}

@app.post("/tools/docker")
async def tool_docker(req: DockerCommandRequest):
    result = await docker_action(req.action, req.name_or_id, req.flags)
    return {"output": result}

@app.post("/tools/file")
async def tool_file(req: FileOperationRequest):
    result = await file_operation(req.action, req.path, req.content)
    return {"output": result}

@app.post("/tools/homeassistant")
async def tool_ha(req: HomeAssistantRequest):
    result = await ha_action(req.action, req.entity_id, req.value)
    return {"output": result}

@app.post("/tools/memory/search")
async def tool_memory_search(req: MemorySearchRequest):
    result = await memory_search(req.query, req.limit)
    return {"output": result}

# ── Phase 5: Multi-step task execution ─────────────────────────────────────
active_tasks: dict[str, dict] = {}

@app.post("/tasks/run")
async def run_task(req: TaskRequest):
    task_id = str(uuid.uuid4())[:8]
    active_tasks[task_id] = {
        "steps": req.steps,
        "current_step": 0,
        "status": "running",
        "output": "",
    }
    asyncio.create_task(_execute_task(task_id))
    return {"task_id": task_id, "total_steps": len(req.steps)}

async def _execute_task(task_id: str):
    task = active_tasks.get(task_id)
    if not task:
        return
    try:
        for i, step in enumerate(task["steps"]):
            task["current_step"] = i + 1
            action = step.strip().split(":")[0].strip().lower()
            args = step.strip().split(":", 1)[-1].strip() if ":" in step else ""

            if action in ("shell", "exec"):
                result = await run_shell(args, timeout=30)
            elif action.startswith("docker"):
                result = await docker_action(args.strip().split()[0], *args.strip().split()[1:])
            elif action.startswith("file"):
                parts = args.strip().split(None, 2)
                op = parts[0] if parts else "list"
                path = parts[1] if len(parts) > 1 else ""
                content = parts[2] if len(parts) > 2 else ""
                result = await file_operation(op, path, content)
            elif action == "sleep":
                await asyncio.sleep(float(args) if args else 1)
                result = f"Waited {args}s"
            elif action == "speak":
                result = args
            else:
                result = f"Unknown action: {action}"

            task["output"] += f"[Step {i+1}] {step}\n{result}\n\n"
            await asyncio.sleep(0.1)
        task["status"] = "completed"
    except Exception as e:
        task["status"] = f"error: {e}"
        task["output"] += f"\nError: {e}\n"

@app.get("/tasks/{task_id}", response_model=TaskStatusResponse)
async def task_status(task_id: str):
    task = active_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return TaskStatusResponse(
        task_id=task_id,
        step=task["current_step"],
        total_steps=len(task["steps"]),
        status=task["status"],
        output=task["output"],
    )

@app.delete("/tasks/{task_id}")
async def cancel_task(task_id: str):
    task = active_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    task["status"] = "cancelled"
    return {"status": "cancelled"}

# ── Phase 5: Proactive alerts ──────────────────────────────────────────────
alert_queue: list[dict] = []
alert_enabled = os.getenv("ALERTS_ENABLED", "false").lower() == "true"

@app.post("/alerts/send")
async def send_alert(message: dict = Body(...)):
    alert = {
        "message": message.get("message", ""),
        "priority": message.get("priority", "normal"),
        "scheduled_at": time.time(),
    }
    alert_queue.append(alert)
    if alert_enabled and alert["priority"] == "high":
        asyncio.create_task(_speak_alert(alert["message"]))
    return {"queued": True}

async def _speak_alert(text: str):
    try:
        await http_client.post(f"{TTS_URL}/tts", json={"text": text}, timeout=10)
    except Exception:
        pass

@app.get("/alerts/pending")
async def pending_alerts():
    return {"alerts": alert_queue}

@app.post("/alerts/clear")
async def clear_alerts():
    alert_queue.clear()
    return {"cleared": True}

@app.get("/debug/memory")
async def debug_memory():
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{MEMORY_API_URL}/api/search",
                json={"query": "test", "limit": 5},
            )
            return {"status": "success", "code": resp.status_code, "data": resp.json()}
    except Exception as e:
        return {"status": "error", "type": type(e).__name__, "message": str(e)}

@app.post("/voice")
async def voice_endpoint(audio: UploadFile = File(...)):
    try:
        audio_bytes = await audio.read()

        resp = await http_client.post(
            f"{STT_URL}/transcribe-b64",
            json={"audio_b64": base64.b64encode(audio_bytes).decode(), "language": "en"},
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code != 200:
            return JSONResponse({"error": resp.text}, status_code=502)
        stt_data = resp.json()
        user_text = stt_data.get("text", "")
        if not user_text:
            return JSONResponse({"error": "No speech detected"}, status_code=400)

        resp = await http_client.post(
            f"{LLAMA_URL}/v1/chat/completions",
            json={
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_text},
                ],
                "max_tokens": 2048,
            },
            headers=LLAMA_HEADERS,
        )
        if resp.status_code != 200:
            return JSONResponse({"error": resp.text}, status_code=502)
        reply = resp.json().get("choices", [{}])[0].get("message", {}).get("content", "")
        if not reply:
            return JSONResponse({"error": "No reply"}, status_code=500)

        cleaned_reply = normalize_for_tts(reply)
        resp = await http_client.post(f"{TTS_URL}/tts", json={"text": cleaned_reply})
        if resp.status_code != 200:
            return JSONResponse({"error": resp.text}, status_code=502)

        return StreamingResponse(
            iter([resp.content]),
            media_type="audio/wav",
            headers={"X-Audio-Length": str(len(resp.content))},
        )
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/health")
async def health():
    status = {"status": "ok", "services": {}}
    for name, url in [
        ("llama", LLAMA_URL),
        ("stt", STT_URL),
        ("tts", TTS_URL),
        ("memory", MEMORY_API_URL),
        ("qdrant", QDRANT_URL),
        ("openwakeword", OPENWAKEWORD_URL),
    ]:
        try:
            r = await http_client.get(f"{url}/health", timeout=3.0)
            status["services"][name] = "up" if r.status_code == 200 else f"http {r.status_code}"
        except Exception:
            status["services"][name] = "unreachable"
    status["alerts_enabled"] = alert_enabled
    return status

# ── iOS Shortcut endpoints ────────────────────────────────────────────────
class VoiceB64Request(BaseModel):
    audio_b64: str

@app.post("/voice-b64")
async def voice_b64(req: VoiceB64Request):
    try:
        audio_bytes = base64.b64decode(req.audio_b64)

        resp = await http_client.post(
            f"{STT_URL}/transcribe-b64",
            json={"audio_b64": req.audio_b64, "language": "en"},
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code != 200:
            return JSONResponse({"error": resp.text}, status_code=502)
        stt_data = resp.json()
        user_text = stt_data.get("text", "")
        if not user_text:
            return JSONResponse({"error": "No speech detected"}, status_code=400)

        resp = await http_client.post(
            f"{LLAMA_URL}/v1/chat/completions",
            json={
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_text},
                ],
                "max_tokens": 2048,
            },
            headers=LLAMA_HEADERS,
        )
        if resp.status_code != 200:
            return JSONResponse({"error": resp.text}, status_code=502)
        assistant_text = resp.json()["choices"][0]["message"]["content"]

        cleaned_text = normalize_for_tts(assistant_text)
        tts_resp = await http_client.post(TTS_URL, json={"text": cleaned_text})
        if tts_resp.status_code != 200:
            return JSONResponse({"error": tts_resp.text}, status_code=502)

        return Response(content=tts_resp.content, media_type="audio/wav")
    except Exception as e:
        import traceback
        print(f"[VOICE-B64] Error: {e}\n{traceback.format_exc()}")
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/voice-shortcut")
async def voice_shortcut(req: Request):
    try:
        audio_b64 = await req.body()
        audio_b64 = audio_b64.decode('utf-8').strip()

        resp = await http_client.post(
            f"{STT_URL}/transcribe-b64",
            json={"audio_b64": audio_b64, "language": "en"},
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code != 200:
            return JSONResponse({"error": resp.text}, status_code=502)
        stt_data = resp.json()
        user_text = stt_data.get("text", "")
        if not user_text:
            return JSONResponse({"error": "No speech detected"}, status_code=400)

        resp = await http_client.post(
            f"{LLAMA_URL}/v1/chat/completions",
            json={
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_text},
                ],
                "max_tokens": 2048,
            },
            headers=LLAMA_HEADERS,
        )
        if resp.status_code != 200:
            return JSONResponse({"error": resp.text}, status_code=502)
        assistant_text = resp.json()["choices"][0]["message"]["content"]

        cleaned_text = normalize_for_tts(assistant_text)
        tts_resp = await http_client.post(TTS_URL, json={"text": cleaned_text})
        if tts_resp.status_code != 200:
            return JSONResponse({"error": tts_resp.text}, status_code=502)

        return Response(content=tts_resp.content, media_type="audio/wav")
    except Exception as e:
        import traceback
        print(f"[VOICE-SHORTCUT] Error: {e}\n{traceback.format_exc()}")
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/voice-get")
async def voice_get(audio_b64: str = Query(..., min_length=10)):
    try:
        resp = await http_client.post(
            f"{STT_URL}/transcribe-b64",
            json={"audio_b64": audio_b64, "language": "en"},
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code != 200:
            return JSONResponse({"error": resp.text}, status_code=502)
        stt_data = resp.json()
        user_text = stt_data.get("text", "")
        if not user_text:
            return JSONResponse({"error": "No speech detected"}, status_code=400)

        resp = await http_client.post(
            f"{LLAMA_URL}/v1/chat/completions",
            json={
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_text},
                ],
                "max_tokens": 2048,
            },
            headers=LLAMA_HEADERS,
        )
        if resp.status_code != 200:
            return JSONResponse({"error": resp.text}, status_code=502)
        assistant_text = resp.json()["choices"][0]["message"]["content"]

        cleaned_text = normalize_for_tts(assistant_text)
        tts_resp = await http_client.post(TTS_URL, json={"text": cleaned_text})
        if tts_resp.status_code != 200:
            return JSONResponse({"error": tts_resp.text}, status_code=502)

        return Response(content=tts_resp.content, media_type="audio/wav")
    except Exception as e:
        import traceback
        print(f"[VOICE-GET] Error: {e}\n{traceback.format_exc()}")
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/voice-b64-get")
async def voice_b64_get(q: str = Query(..., min_length=10)):
    try:
        resp = await http_client.post(
            f"{STT_URL}/transcribe-b64",
            json={"audio_b64": q, "language": "en"},
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code != 200:
            return JSONResponse({"error": resp.text}, status_code=502)
        stt_data = resp.json()
        user_text = stt_data.get("text", "")
        if not user_text:
            return JSONResponse({"error": "No speech detected"}, status_code=400)

        resp = await http_client.post(
            f"{LLAMA_URL}/v1/chat/completions",
            json={
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_text},
                ],
                "max_tokens": 2048,
            },
            headers=LLAMA_HEADERS,
        )
        if resp.status_code != 200:
            return JSONResponse({"error": resp.text}, status_code=502)
        assistant_text = resp.json()["choices"][0]["message"]["content"]

        cleaned_text = normalize_for_tts(assistant_text)
        tts_resp = await http_client.post(TTS_URL, json={"text": cleaned_text})
        if tts_resp.status_code != 200:
            return JSONResponse({"error": tts_resp.text}, status_code=502)

        return Response(content=tts_resp.content, media_type="audio/wav")
    except Exception as e:
        import traceback
        print(f"[VOICE-B64-GET] Error: {e}\n{traceback.format_exc()}")
        return JSONResponse({"error": str(e)}, status_code=500)

@app.post("/voice-raw")
async def voice_raw(req: Request):
    try:
        audio_bytes = await req.body()
        audio_b64 = base64.b64encode(audio_bytes).decode()

        resp = await http_client.post(
            f"{STT_URL}/transcribe-b64",
            json={"audio_b64": audio_b64, "language": "en"},
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code != 200:
            return JSONResponse({"error": resp.text}, status_code=502)
        stt_data = resp.json()
        user_text = stt_data.get("text", "")
        if not user_text:
            return JSONResponse({"error": "No speech detected"}, status_code=400)

        resp = await http_client.post(
            f"{LLAMA_URL}/v1/chat/completions",
            json={
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_text},
                ],
                "max_tokens": 2048,
            },
            headers=LLAMA_HEADERS,
        )
        if resp.status_code != 200:
            return JSONResponse({"error": resp.text}, status_code=502)
        assistant_text = resp.json()["choices"][0]["message"]["content"]

        cleaned_text = normalize_for_tts(assistant_text)
        tts_resp = await http_client.post(TTS_URL, json={"text": cleaned_text})
        if tts_resp.status_code != 200:
            return JSONResponse({"error": tts_resp.text}, status_code=502)

        return Response(content=tts_resp.content, media_type="audio/wav")
    except Exception as e:
        import traceback
        print(f"[VOICE-RAW] Error: {e}\n{traceback.format_exc()}")
        return JSONResponse({"error": str(e)}, status_code=500)

@app.get("/voice-b64-query")
async def voice_b64_query(audio_b64: str = Query(..., min_length=10)):
    try:
        resp = await http_client.post(
            f"{STT_URL}/transcribe-b64",
            json={"audio_b64": audio_b64, "language": "en"},
            headers={"Content-Type": "application/json"},
        )
        if resp.status_code != 200:
            return JSONResponse({"error": resp.text}, status_code=502)
        stt_data = resp.json()
        user_text = stt_data.get("text", "")
        if not user_text:
            return JSONResponse({"error": "No speech detected"}, status_code=400)

        resp = await http_client.post(
            f"{LLAMA_URL}/v1/chat/completions",
            json={
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_text},
                ],
                "max_tokens": 2048,
            },
            headers=LLAMA_HEADERS,
        )
        if resp.status_code != 200:
            return JSONResponse({"error": resp.text}, status_code=502)
        assistant_text = resp.json()["choices"][0]["message"]["content"]

        cleaned_text = normalize_for_tts(assistant_text)
        tts_resp = await http_client.post(TTS_URL, json={"text": cleaned_text})
        if tts_resp.status_code != 200:
            return JSONResponse({"error": tts_resp.text}, status_code=502)

        return Response(content=tts_resp.content, media_type="audio/wav")
    except Exception as e:
        import traceback
        print(f"[VOICE-B64-QUERY] Error: {e}\n{traceback.format_exc()}")
        return JSONResponse({"error": str(e)}, status_code=500)
