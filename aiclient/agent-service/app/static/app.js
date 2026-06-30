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
let analyser = null;
let animFrame = null;

/* ── Canvas waveform ───────────────────────────────────────── */
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

function drawWaveform() {
    if (!analyser) return;
    analyser.fftSize = 256;
    const bufferLength = analyser.frequencyBinCount;
    const dataArray = new Uint8Array(bufferLength);

    function tick() {
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

/* ── Status helper ─────────────────────────────────────────── */
function setStatus(text, className) {
    statusBar.textContent = text;
    statusBar.className = "status-bar " + (className || "");
}

/* ── Messages ──────────────────────────────────────────────── */
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

/* ── TTS: text-to-speech via /tts ─────────────────────────── */
async function speak(text) {
    if (!text || !text.trim()) return;
    console.log("[SPEAK] text:", JSON.stringify(text));
    if (window.speechSynthesis) {
        console.log("[SPEAK] speechSynthesis active?", !!speechSynthesis.speaking);
    }
    setStatus("Speaking", "speaking");
    try {
        const resp = await fetch("/tts", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ text }),
        });
        if (!resp.ok) return;
        const buffer = await resp.arrayBuffer();
        if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        const audioBuffer = await audioCtx.decodeAudioData(buffer);
        const source = audioCtx.createBufferSource();
        source.buffer = audioBuffer;
        source.connect(audioCtx.destination);
        source.start();
        source.onended = () => setStatus("Ready");
    } catch (err) {
        console.error("TTS error:", err);
        setStatus("Ready");
    }
}

/* ── STT: audio-to-text via /stt/file ─────────────────────── */
async function transcribe(blob) {
    setStatus("Transcribing…", "thinking");
    const formData = new FormData();
    formData.append("audio", blob, "audio.wav");
    const resp = await fetch("/stt/file", {
        method: "POST",
        body: formData,
    });
    if (!resp.ok) {
        throw new Error("STT failed: " + resp.statusText);
    }
    const data = await resp.json();
    return data.text;
}

/* ── Chat: stream response ─────────────────────────────────── */
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
                if (data.error) {
                    throw new Error(data.error);
                }
            } catch (e) {
                if (!e.message.includes("Unexpected token")) throw e;
            }
        }
    }

    return fullText;
}

/* ── Main conversation loop ────────────────────────────────── */
async function runConversation(userAudioBlob) {
    try {
        // 1. Transcribe
        const userText = await transcribe(userAudioBlob);
        if (!userText || !userText.trim()) {
            setStatus("Ready");
            return;
        }
        addMessage("user", userText);

        // 2. Chat stream
        const bubble = addStreamingMessage("assistant");
        const reply = await chatStream(userText, bubble);

        // 3. Speak
        if (reply && reply.trim()) {
            await speak(reply);
        } else {
            setStatus("Ready");
        }
    } catch (err) {
        console.error(err);
        setStatus("Error: " + err.message, "error");
    }
}

/* ── PTT Button (hold to speak) ────────────────────────────── */
let recognition = null;
let currentTranscript = "";

async function startRecording() {
    if (isRecording) return;

    try {
        // Use Web Speech API for voice recognition
        const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
        if (!SpeechRecognition) {
            setStatus("Voice recognition not supported", "error");
            return;
        }

        recognition = new SpeechRecognition();
        recognition.lang = "en-US";
        recognition.interimResults = false;
        recognition.maxAlternatives = 1;
        recognition.continuous = false;

        recognition.onstart = () => {
            isRecording = true;
            pttBtn.classList.add("active");
            setStatus("Listening…", "listening");
            drawWaveform();
        };

        recognition.onresult = (event) => {
            const transcript = event.results[0][0].transcript;
            console.log("Speech recognized:", transcript);
            currentTranscript = transcript;
            console.log("Speech recognized:", transcript);
            stopRecording();
            // Process the recognized speech
            processRecognizedSpeech(transcript);
        };

        recognition.onerror = (event) => {
            console.error("Speech recognition error:", event.error);
            if (event.error === "no-speech") {
                setStatus("No speech detected", "error");
            } else {
                setStatus("Speech error: " + event.error, "error");
            }
            stopRecording();
        };

        recognition.onend = () => {
            if (!currentTranscript && !isRecording) {
                setStatus("Ready");
            }
        };

        recognition.start();
    } catch (err) {
        console.error("Speech recognition error:", err);
        setStatus("Speech recognition not available", "error");
    }
}

function stopRecording() {
    if (recognition) {
        recognition.stop();
        recognition = null;
    }
    isRecording = false;
    pttBtn.classList.remove("active");
}

async function processRecognizedSpeech(text) {
    if (!text || !text.trim()) return;

    addMessage("user", text);
    const bubble = addStreamingMessage("assistant");
    const reply = await chatStream(text, bubble);

    if (reply && reply.trim()) {
        await speak(reply);
    } else {
        setStatus("Ready");
    }
}

/* ── Mouse events ──────────────────────────────────────────── */
console.log("Setting up PTT handlers");
pttBtn.addEventListener("click", (e) => { e.preventDefault(); startRecording(); setTimeout(stopRecording, 2000); });
pttBtn.addEventListener("mousedown", (e) => { pttBtn.style.background = "red"; startRecording(); });

/* ── Text chat ──────────────────────────────────────────────── */
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
textInput.addEventListener("keydown", (e) => {
    if (e.key === "Enter") sendTextMessage();
});
pttBtn.addEventListener("mouseup", stopRecording);
pttBtn.addEventListener("mouseleave", () => {
    if (isRecording) stopRecording();
});

/* ── Touch events (mobile) ─────────────────────────────────── */
pttBtn.addEventListener("touchstart", (e) => {
    e.preventDefault();
    startRecording();
}, { passive: false });
pttBtn.addEventListener("touchend", (e) => {
    e.preventDefault();
    stopRecording();
}, { passive: false });
pttBtn.addEventListener("touchcancel", (e) => {
    e.preventDefault();
    stopRecording();
}, { passive: false });
