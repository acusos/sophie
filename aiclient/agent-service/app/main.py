import os
import asyncio
import re
import json
import base64
import uuid
import time
import subprocess
import shlex
from app.email_tool import check_email as email_check_tool, init_email
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
        "You are Sophie, a playful and charming personal assistant. You are flirty, "
        "teasing, and warm. You speak like a real person on a phone call. "
        "Keep responses SHORT - one or two sentences max. "
        "No paragraphs, no long explanations, no filler. "
        "Use contractions and casual language. Be brief. Be playful but professional. No terms of endearment (sweetheart, darling, honey, etc.). "
        "When the user asks you to perform an action, use the tools "
        "available to you. Speak naturally about what you are doing. "
        "You have these tools: shell commands, docker commands, file operations, "
        "web search via SearXNG, memory search, and email checking, and time lookup. "
        "Repos on aiclient are under /opt/ai/projects/ (currently only sophie is cloned). "
        "If a tool fails, report the result directly and offer to fix it. "
        "Always use 24-hour time format (e.g. 23:06 not 11:06 PM). When giving time or date, include both date and time in the reply. Never use emojis, emoji descriptions, or emoticons. Speak plainly. "
        "Avoid markdown formatting, asterisks, and code blocks. Speak naturally. "
        "NEVER close a conversation with phrases like anything else I can help with or "
        "let me know if you need anything else. NEVER say hello or introduce yourself. "
        "Just answer the question and stop. "
        "Do not apologize for tool errors - just state what happened and move on."
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
init_email()

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
async def get_time() -> str:
    from datetime import datetime
    now = datetime.now()
    return f"Current time: {now.strftime('%A %B %d at %H:%M')} AEST."
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

# ── Tool Detection ───────────────────────────────────────────────────────────
def detect_tool_use(message: str):
    """Detect if message implies tool use. Returns (tool_name, params) or (None, None)."""
    import re
    msg = message.lower().strip()
    
    # Docker
    m = re.search(r"(?:docker\s+)?(?:list|show|ps|stop|start|rm|remove|logs)\s+(?:container|containers)?(?:\s+(.+))?$", msg)
    if m:
        action = msg.split()[0]
        target = m.group(1) if m.group(1) else ""
        return "docker", f"{action}:{target}"
    m = re.search(r"docker\s+(ps|stop|start|rm|logs)\s*(.*)$", msg)
    if m:
        return "docker", f"{m.group(1)}:{m.group(2).strip()}"
    
    # Shell with repo name: "check acusos git status"
    m = re.search(r"\b(?:check|show)\b\s+(\w+)\s+(git\s+\w+.*)$", msg)
    if m:
        repo_name = m.group(1)
        git_cmd = m.group(2)
        return "shell", f"cd /opt/ai/projects/{repo_name} \\&\\& {git_cmd}"
    # Shell
    # Time: "what time is it", "what day is it", "what date is it"
    m = re.search(r"\b(?:what\s+(?:time|day|date)|time|date)\b", msg)
    if m:
        return "get_time", None
    # Information questions: "what is the price of X", "tell me about X"
    m = re.search(r"\b(?:what\s+is|what\s+are|tell\s+me|find\s+out|look\s+up)\s+(?:the )?(?:current )?(?:price|cost|value|rate|info|details)(?:\s+of|\s+for|\s+about)\s+(.+)$", msg)
    if m:
        return "web_search", m.group(1).strip()

    # General question that might need web search: "what is X", "who is X"
    m = re.search(r"\b(?:what|who|where|how|when|why)\s+(?:is|are|was|were|can|could|should|do|did|does|has|have)\s+(?:the )?(.+)$", msg)
    if m:
        return "web_search", m.group(1).strip()
    # Email
    m = re.search(r"\b(?:check|read|get|list|fetch)\s+(?:my )?(?:email|mail)\b(?:\s+(.+))?$", msg)
    if m:
        return "email_check", m.group(1).strip() if m.group(1) else None
    m = re.search(r"\b(?:run|execute|do|check|show)\b\s+(.+)$", msg)
    if m:
        return "shell", m.group(1).strip()
    
    # Time: "what time is it", "what day is it", "what date is it"
    m = re.search(r"\b(?:what\s+(?:time|day|date)|time|date)\b", msg)
    if m:
        return "get_time", None
    # Information questions: "what is the price of X", "tell me about X"
    m = re.search(r"\b(?:what\s+is|what\s+are|tell\s+me|find\s+out|look\s+up)\s+(?:the )?(?:current )?(?:price|cost|value|rate|info|details)(?:\s+of|\s+for|\s+about)\s+(.+)$", msg)
    if m:
        return "web_search", m.group(1).strip()

    # General question that might need web search: "what is X", "who is X"
    m = re.search(r"\b(?:what|who|where|how|when|why)\s+(?:is|are|was|were|can|could|should|do|did|does|has|have)\s+(?:the )?(.+)$", msg)
    if m:
        return "web_search", m.group(1).strip()
    # Email
    # File
    m = re.search(r"\b(?:read|write|list|open|cat)\b\s+(?:file|the)?\s*(.+)$", msg)
    if m:
        action = "list" if "list" in msg.split()[0] else "read"
        return "file", f"{action}:{m.group(1).strip()}"
    
    # Web search: "search the web for X" or "search: X"
    m = re.search(r"\b(?:search(?: the web)?|web search)\b\s*(?:for|about)?\s*(.+)$", msg)
    if m:
        return "web_search", m.group(1).strip()

    # Task (multi-step): "task: shell:ls, shell:pwd"
    m = re.search(r"\btask\b\s*:\s*(.+)$", msg)
    if m:
        steps_str = m.group(1).strip()
        steps = [s.strip() for s in re.split(r'[;,]', steps_str) if s.strip()]
        if steps:
            return "task", steps

    # Memory
    m = re.search(r"(?:what do you remember|what did i tell you|what do you know)\s+(?:about|on)?\s*(.+)$", msg)
    if m:
        return "memory_search", m.group(1).strip()
    
    return None, None

async def call_tool(tool_name, params):
    """Execute a tool and return result string."""
    if tool_name == "shell":
        return await run_shell(params)
    elif tool_name == "docker":
        parts = params.split(":", 1)
        action = parts[0] if parts[0] in ("ps", "stop", "start", "rm", "logs") else "ps"
        target = parts[1] if len(parts) > 1 else ""
        return await docker_action(action, target)
    elif tool_name == "file":
        parts = params.split(":", 1)
        return await file_operation(parts[0], parts[1] if len(parts) > 1 else "/home")
    elif tool_name == "memory_search":
        return await memory_search(params)
    elif tool_name == "task":
        return await run_task_from_chat(params)
    elif tool_name == "web_search":
        return await web_search(params)
    elif tool_name == "get_time":
        return await get_time()
    elif tool_name == "email_check":
        return await email_check_tool(mailbox_name=params)
    return f"Unknown tool: {tool_name}"

# ── Tool: Task Runner ──────────────────────────────────────────────────────
async def run_task_from_chat(steps: list[str]) -> str:
    """Run a multi-step task and return output."""
    task_id = str(uuid.uuid4())[:8]
    active_tasks[task_id] = {
        "steps": steps,
        "current_step": 0,
        "status": "running",
        "output": "",
    }
    await _execute_task(task_id)
    task = active_tasks.get(task_id, {})
    return f"Task {task_id} {task.get('status', 'unknown')}:\n{task.get('output', '')}"

# ── Tool: Web Search ───────────────────────────────────────────────────────
SEARXNG_URL = os.getenv("SEARXNG_URL", "http://127.0.0.1:8888")

# Simple in-memory cache for web search results (60 second TTL)
_search_cache: dict[str, tuple[str, float]] = {}

async def web_search(query: str) -> str:
    """Search the web via SearXNG API and return results. Caches for 60s."""
    import time
    now = time.time()
    cache_key = query.lower().strip()

    # Check cache
    if cache_key in _search_cache:
        result, ts = _search_cache[cache_key]
        if now - ts < 60:
            return result

    async with httpx.AsyncClient(timeout=8.0) as client:
        try:
            resp = await client.get(
                f"{SEARXNG_URL}/search",
                params={
                    "q": query,
                    "format": "json",
                    "language": "en",
                    "engines": "google,duckduckgo",
                },
            )
            if resp.status_code != 200:
                return f"Search failed: HTTP {resp.status_code}"
            data = resp.json()
            results = data.get("results", [])[:5]
            if not results:
                return f"No results found for: {query}"
            output = []
            for i, r in enumerate(results, 1):
                title = r.get("title", "N/A")
                url = r.get("url", "N/A")
                snippet = r.get("content", "")[:200]
                output.append(f"[{i}] {title} - {url}\n{snippet}")
            result_str = "\n\n".join(output)
            _search_cache[cache_key] = (result_str, now)
            return result_str
        except Exception as e:
            return f"Search error: {e}"

# ── Tool: Memory ───────────────────────────────────────────────────────────
async def memory_search(query: str, limit: int = 5) -> str:
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(
                f"{MEMORY_API_URL}/api/enrich",
                json={"prompt": query, "conversation_id": None},
            )
            if resp.status_code != 200:
                return "Memory search failed"
            data = resp.json()
            enriched = data.get("enriched_prompt", "")
            if data.get("context_used"):
                return enriched
            return ""
        except Exception:
            return "Memory service unavailable"

async def memory_store(text: str, tags: list[str] = None) -> str:
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            resp = await client.post(
                f"{MEMORY_API_URL}/api/push",
                json={"text": text, "category": (tags or ["general"])[0]},
            )
            if resp.status_code == 200:
                return "Stored"
            return resp.text[:256]
        except Exception:
            return "Memory service unavailable"

@app.get("/")
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.post("/chat")
async def chat(req: ChatRequest):
    session_id = req.session_id or str(uuid.uuid4())
    if session_id not in conversations:
        conversations[session_id] = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Retrieve relevant memory
    memory_context = ""
    try:
        resp = await http_client.post(
            f"{MEMORY_API_URL}/api/enrich",
            json={"prompt": req.message, "conversation_id": session_id},
            timeout=5.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            enriched = data.get("enriched_prompt", "")
            if data.get("context_used") and enriched.strip():
                context_part = enriched.rsplit(req.message, 1)[0].strip() if req.message in enriched else enriched
                if context_part.strip():
                    memory_context = context_part
    except Exception:
        pass

    # Detect and execute tools
    tool_name, params = detect_tool_use(req.message)
    tool_result_str = ""
    if tool_name:
        try:
            tool_result = await call_tool(tool_name, params)
            tool_result_str = f"TOOL_RESULT[{tool_name}]: {tool_result[:500]}"
        except Exception as e:
            tool_result_str = f"TOOL_ERROR[{tool_name}]: {e}"

    # Inject memory context into the user message so the LLM can use it
    user_message = req.message
    if memory_context:
        user_message = f"{memory_context}\n\n{user_message}"
    if tool_result_str:
        user_message = tool_result_str + "\n\n" + user_message

    conversations[session_id].append({"role": "user", "content": user_message})

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
                yield json.dumps({"error": body.decode(), "done": True}) + "\n"
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
                        yield json.dumps({"token": delta, "partial": full_reply}) + "\n"
                except json.JSONDecodeError:
                    continue

            conversations[session_id].append(
                {"role": "assistant", "content": full_reply}
            )

            try:
                # Record user message
                await http_client.post(
                    f"{MEMORY_API_URL}/api/record",
                    json={
                        "conversation_id": session_id,
                        "role": "user",
                        "content": req.message,
                        "model": "sophie",
                        "token_counts": 0,
                        "metadata_": {},
                    },
                    timeout=5.0,
                )
                # Record assistant reply
                await http_client.post(
                    f"{MEMORY_API_URL}/api/record",
                    json={
                        "conversation_id": session_id,
                        "role": "assistant",
                        "content": full_reply,
                        "model": "sophie",
                        "token_counts": 0,
                        "metadata_": {},
                    },
                    timeout=5.0,
                )
                # Push user message to memory
                await http_client.post(
                    f"{MEMORY_API_URL}/api/push",
                    json={"text": req.message, "category": "conversation"},
                    timeout=5.0,
                )
            except Exception:
                pass

            yield json.dumps({"done": True}) + "\n"

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
    resp = await http_client.post(
        f"{STT_URL}/transcribe",
        files={"audio": (audio.filename or "audio.wav", audio_bytes, audio.content_type or "audio/webm")},
        data={"language": "en"},
    )
    if resp.status_code != 200:
        raise HTTPException(status_code=502, detail=resp.text)
    data = resp.json()
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
        resp = await http_client.get(f"{MEMORY_API_URL}/api/history", timeout=5)
        return {"memories": resp.json()} if resp.status_code == 200 else {"error": resp.text}
    except Exception as e:
        return {"error": str(e)}

@app.delete("/memory/{memory_id}")
async def delete_memory(memory_id: str):
    """Memory service doesn't support individual deletion yet."""
    return {"error": "Deletion not supported by memory service"}

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

class WebSearchRequest(BaseModel):
    query: str

@app.post("/tools/web/search")
async def tool_web_search(req: WebSearchRequest):
    result = await web_search(req.query)
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
                f"{MEMORY_API_URL}/api/enrich",
                json={"prompt": "test", "conversation_id": None},
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

# ── VAD Control via webhook ────────────────────────────────────────────────

vad_control_state = {"paused": False}

class VADControlRequest(BaseModel):
    action: str  # "pause" or "resume"

@app.post("/v2/vad/control")
async def vad_control(req: VADControlRequest):
    """Control VAD state from external automation (e.g., iOS proximity sensor)."""
    if req.action == "pause":
        vad_control_state["paused"] = True
        return {"status": "paused"}
    elif req.action == "resume":
        vad_control_state["paused"] = False
        return {"status": "resumed"}
    else:
        return JSONResponse({"error": f"Unknown action: {req.action}"}, status_code=400)

@app.get("/v2/vad/state")
async def vad_state():
    """Return current VAD control state for frontend polling."""
    return {"paused": vad_control_state["paused"]}

@app.get("/debug/memory-test")
async def debug_memory_test():
    """Debug: test what memory service returns for a query"""
    try:
        resp = await http_client.post(
            f"{MEMORY_API_URL}/api/enrich",
            json={"prompt": "Where do I live?", "conversation_id": None},
            timeout=5.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            enriched = data.get("enriched_prompt", "")
            context_used = data.get("context_used", False)
            # Show what context_part would be
            query = "Where do I live?"
            context_part = enriched.rsplit(query, 1)[0].strip() if query in enriched else enriched
            return {
                "raw_response": data,
                "enriched": enriched,
                "context_used": context_used,
                "context_part": context_part,
                "would_inject": bool(context_part.strip()),
            }
        return {"error": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"error": str(e)}

@app.get("/debug/chat-payload")
async def debug_chat_payload():
    """Debug: show what would be sent to LLM for a given query"""
    from unittest.mock import AsyncMock
    import asyncio
    
    async def simulate_chat(message: str, session_id: str):
        # Simulate what /chat does
        if session_id not in conversations:
            conversations[session_id] = [{"role": "system", "content": SYSTEM_PROMPT}]
        
        # Retrieve relevant memory
        try:
            resp = await http_client.post(
                f"{MEMORY_API_URL}/api/enrich",
                json={"prompt": message, "conversation_id": session_id},
                timeout=5.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                enriched = data.get("enriched_prompt", "")
                if data.get("context_used") and enriched.strip():
                    context_part = enriched.rsplit(message, 1)[0].strip() if message in enriched else enriched
                    if context_part.strip():
                        conversations[session_id][0]["content"] += (
                            f"\n{context_part}"
                        )
        except Exception as e:
            print(f"Memory error: {e}")
        
        # Show the system message after memory injection
        return {
            "system_prompt": conversations[session_id][0]["content"],
            "has_memory_context": "Relevant Context" in conversations[session_id][0]["content"],
        }
    
    result = await simulate_chat("Where do I live?", "debug-chat-session")
    return result

@app.get("/debug/llm-payload")
async def debug_llm_payload():
    """Debug: show what would be sent to LLM"""
    test_session = "debug-llm-payload-session"
    
    if test_session not in conversations:
        conversations[test_session] = [{"role": "system", "content": SYSTEM_PROMPT}]
    
    # Retrieve relevant memory
    try:
        resp = await http_client.post(
            f"{MEMORY_API_URL}/api/enrich",
            json={"prompt": "Where do I live?", "conversation_id": test_session},
            timeout=5.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            enriched = data.get("enriched_prompt", "")
            if data.get("context_used") and enriched.strip():
                context_part = enriched.rsplit("Where do I live?", 1)[0].strip() if "Where do I live?" in enriched else enriched
                if context_part.strip():
                    conversations[test_session][0]["content"] += (
                        f"\n{context_part}"
                    )
    except Exception as e:
        pass
    
    conversations[test_session].append({"role": "user", "content": "Where do I live?"})
    
    payload = {
        "messages": conversations[test_session],
        "stream": True,
        "max_tokens": 2048,
    }
    
    return {
        "payload": payload,
        "system_prompt_length": len(conversations[test_session][0]["content"]),
    }

@app.get("/debug/user-message")
async def debug_user_message(query: str = "What is my name?"):
    """Debug: show what user message is constructed for a given query"""
    try:
        resp = await http_client.post(
            f"{MEMORY_API_URL}/api/enrich",
            json={"prompt": query, "conversation_id": None},
            timeout=5.0,
        )
        if resp.status_code == 200:
            data = resp.json()
            enriched = data.get("enriched_prompt", "")
            memory_context = ""
            if data.get("context_used"):
                marker = "--- Relevant Context ---"
                if marker in enriched:
                    memory_context = enriched.split(marker)[0].strip() + enriched.split(marker)[1]
                else:
                    memory_context = enriched
                if enriched.endswith(query):
                    memory_context = memory_context[:-len(query)].strip()
                if memory_context and "Relevant Context" in memory_context:
                    mem_start = memory_context.find("Memories:")
                    if mem_start >= 0:
                        memory_context = memory_context[mem_start:]
            user_message = query
            if memory_context:
                user_message = f"[Remembered about you: {memory_context}]\n\n{query}"
            return {
                "raw_enriched": enriched,
                "memory_context": memory_context,
                "user_message": user_message,
            }
        return {"error": "No response"}
    except Exception as e:
        return {"error": str(e)}

import asyncio
import os

# ── Phase 5: Background scheduler ──────────────────────────────────────────
import subprocess

async def _alert_scheduler():
    """Enhanced background scheduler for proactive alerts."""
    while True:
        await asyncio.sleep(1800)  # Check every 30 minutes

        # Time-based reminders
        hour = datetime.now().hour
        if hour == 9:
            await send_alert({"message": "Good morning! Time to check your schedule.", "priority": "low"})
        elif hour == 18:
            await send_alert({"message": "Good evening! Time to review your day.", "priority": "low"})

        # Disk space check
        try:
            result = subprocess.run(["df", "-h", "/"], capture_output=True, text=True)
            for line in result.stdout.split("\n"):
                if "/ " in line:
                    usage = line.split()[4].replace("%", "")
                    if int(usage) > 85:
                        await send_alert({
                            "message": f"Disk usage is {usage}% on root filesystem",
                            "priority": "high" if int(usage) > 90 else "low"
                        })
                    break
        except Exception:
            pass

        # Container health check
        try:
            result = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}} {{.Status}}"],
                                   capture_output=True, text=True)
            for line in result.stdout.split("\n"):
                if "Exited" in line and "agent-service" in line:
                    await send_alert({
                        "message": f"Agent service appears to have exited: {line.strip()}",
                        "priority": "high"
                    })
        except Exception:
            pass

        # Weather check (via SearXNG)
        try:
            weather_query = "current weather Newington NSW"
            resp = await http_client.post(
                "http://192.168.20.112:8888/search",
                data={"q": weather_query, "format": "json"},
                timeout=10.0,
            )
            if resp.status_code == 200:
                data = resp.json()
                results = data.get("results", [])
                for r in results[:1]:
                    title = r.get("title", "")
                    snippet = r.get("snippet", "")
                    if "rain" in title.lower() or "rain" in snippet.lower():
                        await send_alert({
                            "message": f"Weather update: {title} - {snippet}",
                            "priority": "low"
                        })
                        break
        except Exception:
            pass

# Start the scheduler on startup
@app.on_event("startup")
async def start_scheduler():
    if alert_enabled:
        asyncio.create_task(_alert_scheduler())
