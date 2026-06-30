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
from fastapi import FastAPI, UploadFile, File, HTTPException, Request, Body
from fastapi.staticfiles import StaticFiles
from fastapi.responses import JSONResponse, StreamingResponse
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
    action: str  # ps, stop, start, rm, logs, pull
    name_or_id: str = ""
    flags: str = ""


class FileOperationRequest(BaseModel):
    action: str  # read, write, list
    path: str
    content: str = ""


class HomeAssistantRequest(BaseModel):
    action: str  # state, control
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
    """Execute a shell command and return output."""
    try:
        proc = await asyncio.wait_for(
            asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            ),
            timeout=2,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
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
    """Run a Docker command."""
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
    """Read, write, or list files. Only absolute paths under /home and /tmp."""
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
    """Interact with Home Assistant."""
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
    """Search the memory service."""
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
    """Store a fact in memory."""
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

    # Retrieve relevant memory for this conversation
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
        pass  # Continue without memory if unavailable

    conversations[session_id].append({"role": "user", "content": req.message})

    # Stream from llama.cpp
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

            # Save assistant reply
            conversations[session_id].append(
                {"role": "assistant", "content": full_reply}
            )

            # Store conversation in memory service for long-term recall
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
    """Non-streaming chat endpoint."""
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
    """Forward base64 audio to faster-whisper service."""
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
    async with http_client.stream(
        "POST",
        f"{STT_URL}/transcribe",
        files={"audio": (audio.filename or "audio.wav", await audio.read(), "audio/wav")},
        data={"language": "en"},
    ) as resp:
        if resp.status_code != 200:
            raise HTTPException(status_code=502, detail=await resp.aread())
        data = json.loads(await resp.aread())
        return STTResponse(**data)


@app.post("/tts")
async def tts(req: TTSRequest):
    """Forward text to piper TTS service, return audio stream."""
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
    """Clear conversation history for a session."""
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
    """List stored memories via memory-api."""
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
    """Execute a shell command on aiclient."""
    result = await run_shell(req.command, req.timeout)
    return {"output": result}


@app.post("/tools/docker")
async def tool_docker(req: DockerCommandRequest):
    """Execute a Docker command."""
    result = await docker_action(req.action, req.name_or_id, req.flags)
    return {"output": result}


@app.post("/tools/file")
async def tool_file(req: FileOperationRequest):
    """Read, write, or list files."""
    result = await file_operation(req.action, req.path, req.content)
    return {"output": result}


@app.post("/tools/homeassistant")
async def tool_ha(req: HomeAssistantRequest):
    """Interact with Home Assistant."""
    result = await ha_action(req.action, req.entity_id, req.value)
    return {"output": result}


@app.post("/tools/memory/search")
async def tool_memory_search(req: MemorySearchRequest):
    """Search memory via tool endpoint."""
    result = await memory_search(req.query, req.limit)
    return {"output": result}


# ── Phase 5: Multi-step task execution ─────────────────────────────────────
# Active tasks: task_id -> {steps, current_step, status, output}
active_tasks: dict[str, dict] = {}


@app.post("/tasks/run")
async def run_task(req: TaskRequest):
    """Execute a multi-step task."""
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
    """Background task executor."""
    task = active_tasks.get(task_id)
    if not task:
        return

    try:
        for i, step in enumerate(task["steps"]):
            task["current_step"] = i + 1
            # Parse step: assume format "action: args"
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
                result = args  # Just record the speech, don't auto-speak
            else:
                result = f"Unknown action: {action}"

            task["output"] += f"[Step {i+1}] {step}\n{result}\n\n"
            await asyncio.sleep(0.1)  # Yield

        task["status"] = "completed"
    except Exception as e:
        task["status"] = f"error: {e}"
        task["output"] += f"\nError: {e}\n"


@app.get("/tasks/{task_id}", response_model=TaskStatusResponse)
async def task_status(task_id: str):
    """Get task status."""
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
    """Cancel a running task."""
    task = active_tasks.get(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    task["status"] = "cancelled"
    return {"status": "cancelled"}


# ── Phase 5: Proactive alerts ──────────────────────────────────────────────
# Alert queue: list of {message, priority, scheduled_at}
alert_queue: list[dict] = []
alert_enabled = os.getenv("ALERTS_ENABLED", "false").lower() == "true"


@app.post("/alerts/send")
async def send_alert(message: dict = Body(...)):
    """Queue an alert to be spoken."""
    alert = {
        "message": message.get("message", ""),
        "priority": message.get("priority", "normal"),
        "scheduled_at": time.time(),
    }
    alert_queue.append(alert)
    if alert_enabled:
        # Auto-speak high priority immediately
        if alert["priority"] == "high":
            asyncio.create_task(_speak_alert(alert["message"]))
    return {"queued": True}


async def _speak_alert(text: str):
    """Speak an alert through TTS."""
    try:
        resp = await http_client.post(
            f"{TTS_URL}/tts",
            json={"text": text},
            timeout=10,
        )
        if resp.status_code == 200:
            # Audio would be played via desktop client or notification
            pass
    except Exception:
        pass


@app.get("/alerts/pending")
async def pending_alerts():
    """List pending alerts."""
    return {"alerts": alert_queue}


@app.post("/alerts/clear")
async def clear_alerts():
    alert_queue.clear()
    return {"cleared": True}



@app.get("/debug/memory")
async def debug_memory():
    """Debug endpoint for memory service."""
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
    """Accept audio, transcribe, chat, return TTS audio."""
    try:
        audio_bytes = await audio.read()
        print(f"[VOICE] Received {len(audio_bytes)} bytes from {audio.filename}")
        
        # 1. Transcribe - send as base64 via /stt-b64 or as file
        resp = await http_client.post(
            f"{STT_URL}/transcribe-b64",
            json={"audio_b64": base64.b64encode(audio_bytes).decode(), "language": "en"},
            headers={"Content-Type": "application/json"},
        )
        print(f"[VOICE] STT response: {resp.status_code} {resp.text[:200]}")
        if resp.status_code != 200:
            return JSONResponse({"error": resp.text}, status_code=502)
        stt_data = resp.json()
        user_text = stt_data.get("text", "")
        if not user_text:
            return JSONResponse({"error": "No speech detected"}, status_code=400)

        # 2. Chat (blocking)
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

        # 3. TTS
        cleaned_reply = normalize_for_tts(reply)
        resp = await http_client.post(
            f"{TTS_URL}/tts",
            json={"text": cleaned_reply},
        )
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
    """Health check with upstream status."""
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
