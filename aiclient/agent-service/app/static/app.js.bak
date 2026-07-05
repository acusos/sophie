/* ── Sophie Web UI ─────────────────────────────────────────── */

const messageArea = document.getElementById("messageArea");
const statusBar = document.getElementById("statusBar");
const pttBtn = document.getElementById("pttBtn");
const textInput = document.getElementById("textInput");
const sendBtn = document.getElementById("sendBtn");
const canvas = document.getElementById("canvas");
const ctx = canvas.getContext("2d");

let sessionId = sessionStorage.getItem("sophie_session") || "";
if (!sessionId) {
    sessionId = Math.random().toString(36).substring(2, 10);
    sessionStorage.setItem("sophie_session", sessionId);
}

let audioCtx = null;
let mediaRecorder = null;
let chunks = [];
let isRecording = false;
let animFrame = null;
let audioStream = null;
let recordingTimer = null;

function resizeCanvas() {
    canvas.width = canvas.offsetWidth * (window.devicePixelRatio || 1);
    canvas.height = canvas.offsetHeight * (window.devicePixelRatio || 1);
}
window.addEventListener("resize", resizeCanvas);
resizeCanvas();

function drawIdle() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.beginPath();
    ctx.moveTo(0, canvas.height / 2);
    ctx.lineTo(canvas.width, canvas.height / 2);
    ctx.strokeStyle = "rgba(192,132,252,0.15)";
    ctx.lineWidth = 1;
    ctx.stroke();
}
drawIdle();

let analyser = null;

function startAnalyser(stream) {
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const source = audioCtx.createMediaStreamSource(stream);
    analyser = audioCtx.createAnalyser();
    analyser.fftSize = 256;
    source.connect(analyser);
    function tick() {
        if (!analyser || !isRecording) return;
        const bufferLength = analyser.frequencyBinCount;
        const dataArray = new Uint8Array(bufferLength);
        analyser.getByteTimeDomainData(dataArray);
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        ctx.lineWidth = 2;
        ctx.strokeStyle = "#c084fc";
        ctx.beginPath();
        const sliceWidth = canvas.width / bufferLength;
        let x = 0;
        for (let i = 0; i < bufferLength; i++) {
            const v = dataArray[i] / 128.0;
            const y = (v * canvas.height) / 2;
            if (i === 0) ctx.moveTo(x, y);
            else ctx.lineTo(x, y);
            x += sliceWidth;
        }
        ctx.lineTo(canvas.width, canvas.height / 2);
        ctx.stroke();
        animFrame = requestAnimationFrame(tick);
    }
    tick();
}

function stopAnalyser() {
    if (animFrame) { cancelAnimationFrame(animFrame); animFrame = null; }
    analyser = null;
    drawIdle();
}

function setStatus(text, className) {
    statusBar.textContent = text;
    statusBar.className = "status-bar " + (className || "");
}

function addMessage(role, text) {
    const msg = document.createElement("div");
    msg.className = `message ${role}`;
    const avatar = document.createElement("div");
    avatar.className = "avatar";
    avatar.textContent = role === "user" ? "U" : "S";
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = text;
    msg.appendChild(avatar);
    msg.appendChild(bubble);
    messageArea.appendChild(msg);
    messageArea.scrollTop = messageArea.scrollHeight;
    return bubble;
}

function addStreamingMessage(role) {
    const msg = document.createElement("div");
    msg.className = `message ${role}`;
    const avatar = document.createElement("div");
    avatar.className = "avatar";
    avatar.textContent = role === "user" ? "U" : "S";
    const bubble = document.createElement("div");
    bubble.className = "bubble";
    bubble.textContent = "";
    msg.appendChild(avatar);
    msg.appendChild(bubble);
    messageArea.appendChild(msg);
    messageArea.scrollTop = messageArea.scrollHeight;
    return bubble;
}

async function speak(text) {
    if (!text || !text.trim()) return;
    console.log("[SPEAK] text:", JSON.stringify(text));
    setStatus("Speaking", "speaking");
    vadEnabled = false;
    try {
        if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        if (audioCtx.state === "suspended") await audioCtx.resume();
        const resp = await fetch("/tts", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ text }),
        });
        if (!resp.ok) throw new Error("TTS HTTP " + resp.status);
        const buffer = await resp.arrayBuffer();
        const audioBuffer = await audioCtx.decodeAudioData(buffer);
        const source = audioCtx.createBufferSource();
        source.buffer = audioBuffer;
        source.connect(audioCtx.destination);
        source.start();
        source.onended = () => { setStatus("Ready"); setTimeout(() => { vadEnabled = true; }, 500); };
        return;
    } catch (ctxErr) {
        console.log("AudioContext decodeAudioData failed, trying Audio fallback:", ctxErr.message);
    }
    try {
        const resp2 = await fetch("/tts", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ text }),
        });
        if (!resp2.ok) throw new Error("TTS HTTP " + resp2.status);
        const blob = await resp2.blob();
        const url = URL.createObjectURL(blob);
        const audio = new Audio(url);
        audio.onended = () => {
            URL.revokeObjectURL(url);
            setStatus("Ready");
            setTimeout(() => { vadEnabled = true; }, 500);
        };
        audio.onerror = () => {
            console.error("Audio element playback error");
            URL.revokeObjectURL(url);
            setStatus("Ready");
            vadEnabled = true;
        };
        try {
            await audio.play();
        } catch (playErr) {
            console.error("Audio.play() failed:", playErr);
            URL.revokeObjectURL(url);
            setStatus("Ready");
            vadEnabled = true;
        }
    } catch (fallbackErr) {
        console.error("TTS fallback also failed:", fallbackErr);
        setStatus("Ready");
        vadEnabled = true;
    }
}

async function transcribe(blob) {
    setStatus("Transcribing…", "thinking");
    const formData = new FormData();
    formData.append("audio", blob, "audio.webm");
    const resp = await fetch("/stt/file", {
        method: "POST",
        body: formData,
    });
    if (!resp.ok) throw new Error("STT failed: " + resp.statusText);
    const data = await resp.json();
    return data.text;
}

async function chatStream(userText, bubble) {
    setStatus("Thinking…", "thinking");
    const resp = await fetch("/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message: userText, session_id: sessionId }),
    });
    if (!resp.ok) throw new Error("Chat failed: " + resp.statusText);
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let fullText = "";
    while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        const chunk = decoder.decode(value, { stream: true });
        for (const line of chunk.split("\n")) {
            if (!line.trim()) continue;
            try {
                const data = JSON.parse(line);
                if (data.token) {
                    fullText += data.token;
                    bubble.textContent = fullText;
                    messageArea.scrollTop = messageArea.scrollHeight;
                }
                if (data.error) throw new Error(data.error);
            } catch (e) {
                if (!e.message.includes("Unexpected token")) throw e;
            }
        }
    }
    return fullText;
}

async function runConversation(userAudioBlob) {
    try {
        const userText = await transcribe(userAudioBlob);
        if (!userText || !userText.trim()) {
            setStatus("Ready");
            return;
        }
        addMessage("user", userText);
        const bubble = addStreamingMessage("assistant");
        const reply = await chatStream(userText, bubble);
        if (reply && reply.trim()) {
            await speak(reply);
        } else {
            setStatus("Ready");
        }
    } catch (err) {
        console.error("Conversation error:", err);
        setStatus("Error: " + err.message, "error");
    }
}

async function startRecording() {
    if (isRecording) return;
    try {
        audioStream = await navigator.mediaDevices.getUserMedia({ audio: true });
        chunks = [];
        const mimeTypes = [
            "audio/webm;codecs=opus",
            "audio/webm",
            "audio/ogg;codecs=opus",
            "audio/mp4",
        ];
        let selectedMime = "";
        for (const m of mimeTypes) {
            if (MediaRecorder.isTypeSupported(m)) { selectedMime = m; break; }
        }
        mediaRecorder = new MediaRecorder(audioStream, selectedMime ? { mimeType: selectedMime } : {});
        mediaRecorder.ondataavailable = (e) => {
            if (e.data && e.data.size > 0) chunks.push(e.data);
        };
        mediaRecorder.onstop = () => {
            const blob = new Blob(chunks, { type: selectedMime || "audio/webm" });
            isRecording = false;
            pttBtn.classList.remove("active");
            pttBtn.querySelector(".ptt-label").textContent = "Hold to Speak";
            stopAnalyser();
            audioStream.getTracks().forEach(t => t.stop());
            audioStream = null;
            if (recordingTimer) { clearInterval(recordingTimer); recordingTimer = null; }
            runConversation(blob);
        };
        mediaRecorder.start(100);
        isRecording = true;
        pttBtn.classList.add("active");
        setStatus("Listening…", "listening");
        startAnalyser(audioStream);
        let elapsed = 0;
        const label = pttBtn.querySelector(".ptt-label");
        label.textContent = "Release";
        recordingTimer = setInterval(() => {
            elapsed++;
            label.textContent = elapsed + "s";
        }, 1000);
    } catch (err) {
        console.error("Recording error:", err);
        setStatus(err.name === "NotAllowedError" ? "Mic access denied" : "Mic error: " + err.name, "error");
    }
}

function stopRecording() {
    if (!isRecording) return;
    if (mediaRecorder && mediaRecorder.state !== "inactive") mediaRecorder.stop();
    mediaRecorder = null;
}

pttBtn.addEventListener("touchstart", (e) => { e.preventDefault(); startRecording(); }, { passive: false });
pttBtn.addEventListener("touchend", (e) => { e.preventDefault(); stopRecording(); }, { passive: false });
pttBtn.addEventListener("touchcancel", (e) => { e.preventDefault(); stopRecording(); }, { passive: false });

pttBtn.addEventListener("mousedown", (e) => { e.preventDefault(); startRecording(); });
pttBtn.addEventListener("mouseup", (e) => { e.preventDefault(); stopRecording(); });
pttBtn.addEventListener("mouseleave", () => { if (isRecording) stopRecording(); });

async function sendTextMessage() {
    const userText = textInput.value.trim();
    if (!userText) return;
    textInput.value = "";
    addMessage("user", userText);
    const bubble = addStreamingMessage("assistant");
    const reply = await chatStream(userText, bubble);
    if (reply && reply.trim()) {
        await speak(reply);
    } else {
        setStatus("Ready");
    }
}

sendBtn.addEventListener("click", sendTextMessage);
textInput.addEventListener("keydown", (e) => { if (e.key === "Enter") sendTextMessage(); });

/* ── Hands-Free Voice Activity Detection ──────────────────────────────── */

let handsFreeActive = false;
let handsFreeStream = null;
let handsFreeStreamForRec = null;
let handsFreeMediaRecorder = null;
let handsFreeChunks = [];
let handsFreeAnalyser = null;
let handsFreeAnimFrame = null;
let isSpeechDetected = false;
let vadEnabled = true;
let silenceFrameCount = 0;

const VAD_THRESHOLD = 0.001;        // energy threshold for speech detection
const VAD_SILENCE_MS = 1000;       // ms of silence to consider speech ended
const VAD_FRAME_MS = 20;           // analysis interval

async function startHandsFree() {
    if (handsFreeActive) return;
    try {
        if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        if (audioCtx.state === "suspended") await audioCtx.resume();
        handsFreeStream = await navigator.mediaDevices.getUserMedia({ audio: true });
        handsFreeAnalyser = audioCtx.createAnalyser();
        handsFreeAnalyser.fftSize = 512;
        const source = audioCtx.createMediaStreamSource(handsFreeStream);
        source.connect(handsFreeAnalyser);
        // Clone stream for MediaRecorder - iOS can't share stream between AnalyserNode and MediaRecorder
        handsFreeStreamForRec = handsFreeStream.clone();
        handsFreeActive = true;
        document.getElementById("handsFreeToggle").textContent = "Hands-Free: ON";
        document.getElementById("handsFreeToggle").classList.add("active");
        setStatus("Hands-free listening…", "listening");
        vadLoop();
    } catch (err) {
        console.error("Hands-free mic error:", err);
        addMessage("assistant", "Error: " + String(err));
        setStatus("Mic denied", "error");
    }
}

function stopHandsFree() {
    handsFreeActive = false;
    if (handsFreeAnimFrame) { cancelAnimationFrame(handsFreeAnimFrame); handsFreeAnimFrame = null; }
    if (handsFreeMediaRecorder) {
        if (handsFreeMediaRecorder.state !== "inactive") handsFreeMediaRecorder.stop();
        handsFreeMediaRecorder = null;
    }
    if (handsFreeStream) {
        handsFreeStream.getTracks().forEach(t => t.stop());
        handsFreeStream = null;
    }
    isSpeechDetected = false;
    handsFreeChunks = [];
    document.getElementById("handsFreeToggle").textContent = "Hands-Free: OFF";
    document.getElementById("handsFreeToggle").classList.remove("active");
    setStatus("Ready");
}

function vadLoop() {
    if (!handsFreeActive) return;
    if (!vadEnabled) {
        handsFreeAnimFrame = setTimeout(vadLoop, 30);
        return;
    }

    const dataArray = new Float32Array(handsFreeAnalyser.fftSize);
    handsFreeAnalyser.getFloatTimeDomainData(dataArray);

    let energy = 0;
    for (let i = 0; i < dataArray.length; i++) {
        energy += dataArray[i] * dataArray[i];
    }
    energy = energy / dataArray.length;

    if (energy > VAD_THRESHOLD) {
        if (!isSpeechDetected) {
            isSpeechDetected = true;
            silenceFrameCount = 0;
            setStatus("Recording...", "listening");
            startHandsFreeRecording();
        } else {
            silenceFrameCount = 0;
        }
    } else {
        if (isSpeechDetected) {
            silenceFrameCount++;
            if (silenceFrameCount >= 30) {
                isSpeechDetected = false;
                silenceFrameCount = 0;
                setStatus("Processing...", "thinking");
                stopHandsFreeRecording();
            }
        }
    }

    handsFreeAnimFrame = setTimeout(vadLoop, 30);
}
function startHandsFreeRecording() {
    if (handsFreeMediaRecorder && handsFreeMediaRecorder.state !== "inactive") {
        return;
    }

    chunks = [];  // reuse the existing chunks array
    const mimeTypes = ["audio/webm;codecs=opus", "audio/webm", "audio/ogg;codecs=opus", "audio/mp4"];
    let selectedMime = "";
    for (const m of mimeTypes) {
        if (MediaRecorder.isTypeSupported(m)) { selectedMime = m; break; }
    }

    handsFreeMediaRecorder = new MediaRecorder(handsFreeStreamForRec, selectedMime ? { mimeType: selectedMime } : {});
    handsFreeMediaRecorder.ondataavailable = (e) => {
        if (e.data && e.data.size > 0) chunks.push(e.data);
    };
    handsFreeMediaRecorder.onstop = () => {
        const blob = new Blob(chunks, { type: selectedMime || "audio/webm" });
        if (blob.size > 100) {
            runConversation(blob);
        } else {
            setStatus("Recording too short", "error");
        }
        isSpeechDetected = false;
    };
    try {
        handsFreeMediaRecorder.start(100);
        setStatus("Listening…", "listening");
    } catch (recErr) {
        console.error("Recording error:", recErr);
        setStatus("Recording failed: " + recErr.message, "error");
        isSpeechDetected = false;
    }
}

function stopHandsFreeRecording() {
    if (handsFreeMediaRecorder && handsFreeMediaRecorder.state !== "inactive") {
        handsFreeMediaRecorder.stop();
    }
}

const toggleBtn = document.getElementById("handsFreeToggle");
if (toggleBtn) {
    toggleBtn.addEventListener("click", () => {
        if (handsFreeActive) {
            stopHandsFree();
        } else {
            startHandsFree();
        }
    });
}
