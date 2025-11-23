# app.py
import base64
import threading
import time
from collections import defaultdict

import cv2
import numpy as np
from starlette.applications import Starlette
from starlette.routing import Route, WebSocketRoute
from starlette.responses import HTMLResponse, Response, PlainTextResponse, JSONResponse
from starlette.requests import Request
from starlette.websockets import WebSocket

# ------------------------------
# Shared state for video frames
# ------------------------------
uploaded_frames = {
    'device1': {'frame_data': None, 'timestamp': 0},
    'device2': {'frame_data': None, 'timestamp': 0}
}
frame_lock = threading.Lock()

# ------------------------------
# Shared state for audio sockets
# ------------------------------
device1_ws = None
device2_ws = None

# ------------------------------
# Helper: black frame
# ------------------------------
def get_black_frame_bytes():
    black_frame = np.zeros((480, 640, 3), dtype=np.uint8)
    ret, buf = cv2.imencode('.jpg', black_frame)
    return buf.tobytes()

# ------------------------------
# HTML template for device page
# ------------------------------
DEVICE_PAGE_HTML = r"""
<!DOCTYPE html>
<html>
<head>
<title>{{ device_name }} - Video+Audio</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">

<style>
body {
    margin: 0;
    padding: 0;
    background: #000;
    font-family: Arial, Helvetica, sans-serif;
    color: white;
    overflow: hidden;
}

/* MAIN LAYOUT */
#mainContainer {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 70px; /* leave space for control bar */
    display: flex;
    background: #000;
}

/* Left / Right camera panels */
#myCamBox, #otherCamBox {
    flex: 1;
    display: flex;
    justify-content: center;
    align-items: center;
    padding: 12px;
}

/* Make a black "stage" background behind the video area */
#stage {
    position: absolute;
    inset: 0 0 70px 0; /* leave bottom control bar space */
    background: #000;
    z-index: 0;
}

/* video & img style */
video, img {
    width: 95%;
    height: auto;
    max-height: calc(100% - 24px);
    border-radius: 10px;
    object-fit: cover;
    background: #000;
    border: 2px solid #111;
    z-index: 1;
}

/* BOTTOM CONTROL BAR */
#controls {
    position: fixed;
    bottom: 0;
    left: 0;
    right: 0;
    height: 70px;
    background: rgba(20,20,20,0.98);
    display: flex;
    justify-content: center;
    align-items: center;
    gap: 28px;
    z-index: 2;
    box-shadow: 0 -6px 18px rgba(0,0,0,0.6);
}

/* BUTTONS */
.controlBtn {
    width: 64px;
    height: 64px;
    border-radius: 50%;
    border: none;
    font-size: 26px;
    color: white;
    cursor: pointer;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    transition: transform .12s ease, box-shadow .12s ease;
    box-shadow: 0 4px 10px rgba(0,0,0,0.5);
    user-select: none;
}

.controlBtn:active { transform: scale(0.95); }

.btn-on { background: #4CAF50; }
.btn-off { background: #4b4b4b; }
.btn-danger { background: #b40000; }

/* INFO BOX */
#infoBox {
    position: fixed;
    top: 10px;
    left: 10px;
    font-size: 13px;
    color: #bfbfbf;
    z-index: 3;
    background: rgba(0,0,0,0.35);
    padding: 8px 10px;
    border-radius: 8px;
    border: 1px solid rgba(255,255,255,0.03);
}
</style>
</head>

<body>
<div id="stage"></div>

<div id="mainContainer">
    <div id="myCamBox">
        <video id="myVideo" autoplay muted playsinline></video>
    </div>

    <div id="otherCamBox">
        <img id="otherVideo" alt="Other device video" />
    </div>
</div>

<div id="infoBox">
    Frames sent: <span id="frameCount">0</span> <br>
    Last update: <span id="lastUpdate">Never</span>
</div>

<div id="controls">
    <button id="micBtn" class="controlBtn btn-off" onclick="toggleMicUI()" title="Toggle microphone">🎤</button>
    <button id="deafenBtn" class="controlBtn btn-off" onclick="toggleDeafenUI()" title="Deafen (local mute)">🔇</button>
    <button id="videoBtn" class="controlBtn btn-danger" onclick="toggleVideoUI()" title="Toggle camera">🎥</button>
</div>

<script>
/* ====== device id from server template ====== */
const device_id = "{{ device_id }}";

/* ====== VIDEO logic (unchanged behavior) ====== */
let isVideoOn = false;
let localStream = null;
let frameInterval = null;
let uploadCount = 0;

async function toggleVideo() {
    if (!isVideoOn) {
        try {
            const stream = await navigator.mediaDevices.getUserMedia({
                video: { width: 640, height: 480, frameRate: 15 },
                audio: false
            });
            localStream = stream;
            document.getElementById("myVideo").srcObject = stream;
            startVideoUpload();
            isVideoOn = true;
        } catch (err) {
            alert("Camera error: " + err);
        }
    } else {
        stopVideo();
    }
}

function startVideoUpload() {
    const video = document.getElementById("myVideo");
    const canvas = document.createElement("canvas");
    const ctx = canvas.getContext("2d");
    canvas.width = 320; canvas.height = 240;

    frameInterval = setInterval(() => {
        if (video.readyState >= video.HAVE_CURRENT_DATA) {
            ctx.drawImage(video, 0, 0, canvas.width, canvas.height);
            canvas.toBlob(async (blob) => {
                try {
                    const fd = new FormData();
                    fd.append("frame", blob);
                    fd.append("device_id", device_id);
                    await fetch("/upload_frame", { method: "POST", body: fd });
                    uploadCount++;
                    document.getElementById("frameCount").textContent = uploadCount;
                } catch (e) {
                    console.log("upload error:", e);
                }
            }, "image/jpeg", 0.5);
        }
    }, 67);

    startOtherStream();
}

function stopVideo() {
    isVideoOn = false;
    if (frameInterval) { clearInterval(frameInterval); frameInterval = null; }
    if (localStream) { localStream.getTracks().forEach(t => t.stop()); localStream = null; }
    document.getElementById("myVideo").srcObject = null;
    uploadCount = 0;
    document.getElementById("frameCount").textContent = "0";
}

function startOtherStream() {
    const other = document.getElementById("otherVideo");

    // Reuse existing interval if exists by clearing first (just in case)
    if (window._otherStreamInterval) clearInterval(window._otherStreamInterval);

    window._otherStreamInterval = setInterval(async () => {
        try {
            const res = await fetch("/get_latest_frame?device_id=" + device_id + "&t=" + Date.now());
            if (!res.ok) return;
            const blob = await res.blob();
            // revoke old objectURL to avoid leaks
            if (window._lastOtherURL) URL.revokeObjectURL(window._lastOtherURL);
            const url = URL.createObjectURL(blob);
            window._lastOtherURL = url;
            other.src = url;
            document.getElementById("lastUpdate").textContent = "Just now";
        } catch (e) {
            // network error
            document.getElementById("lastUpdate").textContent = "Error";
            console.log("other frame fetch error", e);
        }
    }, 100);
}

/* ====== AUDIO logic (fixed send + receive) ====== */
/*
  Behavior:
  - Mic button toggles capturing + sending audio over the websocket.
  - Incoming audio frames are played immediately via AudioContext.
  - Deafen disables both sending and incoming playback (local mute).
*/

let ws = null;
let isAudioOn = false;
let audioCtx = null;
let mediaStream = null;
let processor = null;
let micOn = false;         // whether we *intend* to send mic
let deafenOn = false;      // deafen (local mute + stop sending)
let localGainZero = null;  // gain node to silence local capture playback
let playbackGain = null;   // gain node to control incoming playback volume

async function startAudioSendAndRecv() {
    if (isAudioOn) return;
    try {
        audioCtx = audioCtx || new (window.AudioContext || window.webkitAudioContext)();

        // create zero-gain node so we can run the processor without audible local echo
        localGainZero = audioCtx.createGain();
        localGainZero.gain.value = 0;
        localGainZero.connect(audioCtx.destination);

        // playback gain for incoming audio (we set to 1 normally, 0 when deafen)
        playbackGain = audioCtx.createGain();
        playbackGain.gain.value = deafenOn ? 0 : 1;
        playbackGain.connect(audioCtx.destination);

        // open websocket to server endpoint (inserted by template replaced when rendering)
        const wsUrl = (location.protocol === "https:" ? "wss://" : "ws://") + window.location.host + "/{{ ws_endpoint }}";
        ws = new WebSocket(wsUrl);
        ws.binaryType = "arraybuffer";

        ws.onopen = () => {
            console.log("audio ws open");
        };

        ws.onclose = () => {
            console.log("audio ws closed");
        };

        // receive handler - create buffer and play immediately
        ws.onmessage = (ev) => {
            try {
                if (!audioCtx) return;
                const arr = new Float32Array(ev.data);
                const buf = audioCtx.createBuffer(1, arr.length, audioCtx.sampleRate);
                buf.copyToChannel(arr, 0, 0);
                const src = audioCtx.createBufferSource();
                src.buffer = buf;
                src.connect(playbackGain);
                src.start();
            } catch (e) {
                console.error("ws.onmessage error", e);
            }
        };

        // capture local mic
        mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
        const source = audioCtx.createMediaStreamSource(mediaStream);

        // ScriptProcessor to read raw PCM and send as Float32
        processor = audioCtx.createScriptProcessor(4096, 1, 1);
        processor.onaudioprocess = (e) => {
            if (!ws || ws.readyState !== WebSocket.OPEN) return;
            if (!micOn) return; // only send when mic toggled on (and not deafened)
            const input = e.inputBuffer.getChannelData(0);
            // copy to avoid referencing the same buffer
            const copy = new Float32Array(input.length);
            copy.set(input);
            try {
                ws.send(copy.buffer);
            } catch (err) {
                console.warn("ws send error", err);
            }
        };

        // connect nodes so processor runs but we don't hear ourselves:
        // source -> processor -> localGainZero (zero) -> destination
        source.connect(processor);
        processor.connect(localGainZero);

        isAudioOn = true;
        console.log("audio started");
    } catch (err) {
        console.error("startAudioSendAndRecv error:", err);
    }
}

function stopAudioSendAndRecv() {
    try {
        if (processor) { processor.disconnect(); processor.onaudioprocess = null; processor = null; }
        if (mediaStream) { mediaStream.getTracks().forEach(t => t.stop()); mediaStream = null; }
        if (ws) { try { ws.close(); } catch(e){} ws = null; }
        if (localGainZero) { try { localGainZero.disconnect(); } catch(e){} localGainZero = null; }
        if (playbackGain) { try { playbackGain.disconnect(); } catch(e){} playbackGain = null; }
    } finally {
        isAudioOn = false;
        audioCtx = audioCtx; // keep the audioCtx instance so subsequent playbacks reuse it
        console.log("audio stopped");
    }
}

/* Toggle mic sending (UI + logic) */
function toggleMicUI() {
    micOn = !micOn;
    const micBtn = document.getElementById("micBtn");
    micBtn.className = micOn ? "controlBtn btn-on" : "controlBtn btn-off";

    if (deafenOn && micOn) {
        micOn = false;
        micBtn.className = "controlBtn btn-off";
        return;
    }

    if (micOn) {
        if (isAudioOn && mediaStream) {
            // Re-enable tracks if stack already running
            mediaStream.getAudioTracks().forEach(t => t.enabled = true);
        } else {
            // Start audio stack if not already started
            startAudioSendAndRecv();
        }
    } else {
        if (mediaStream) {
            mediaStream.getAudioTracks().forEach(t => t.enabled = false);
        }
        // keep ws open to continue receiving
    }
}

/* Deafen: local mute + stop sending (both directions local) */
function toggleDeafenUI() {
    deafenOn = !deafenOn;
    document.getElementById("deafenBtn").className = deafenOn ? "controlBtn btn-on" : "controlBtn btn-off";

    if (deafenOn) {
        // stop sending & disable mic
        micOn = false;
        document.getElementById("micBtn").className = "controlBtn btn-off";

        // close send/receive stack to ensure remote no longer hears us and we can't hear them
        stopAudioSendAndRecv();

    } else {
        micOn = !micOn;
        document.getElementById("micBtn").className = "controlBtn btn-on";
        startAudioSendAndRecv();

    }
}

/* toggleVideoUI uses same toggleVideo() function and updates button */
function toggleVideoUI() {
    const btn = document.getElementById("videoBtn");
    if (!isVideoOn) {
        btn.className = "controlBtn btn-on";
    } else {
        btn.className = "controlBtn btn-danger";
    }
    toggleVideo();
}

/* When leaving page - cleanup */
window.addEventListener("beforeunload", () => {
    try { stopAudioSendAndRecv(); } catch(e){}
    try { stopVideo(); } catch(e){}
});
</script>

</body>
</html>
"""

# ------------------------------
# Video + audio pages
# ------------------------------
async def render_device_page(device_id: str, device_name: str, ws_endpoint: str, request: Request):
    host = request.url.hostname or "localhost"
    html = DEVICE_PAGE_HTML.replace("{{ device_id }}", device_id)\
                           .replace("{{ device_name }}", device_name)\
                           .replace("{{ ws_endpoint }}", ws_endpoint)
    return HTMLResponse(html)

async def device1_page(request: Request):
    return await render_device_page("device1", "Device 1", "device1ws", request)

async def device2_page(request: Request):
    return await render_device_page("device2", "Device 2", "device2ws", request)

# ------------------------------
# Video frame endpoints
# ------------------------------
async def get_latest_frame(request: Request):
    try:
        device_id = request.query_params.get('device_id','device1')
        other_device = 'device2' if device_id=='device1' else 'device1'
        with frame_lock:
            stored = uploaded_frames.get(other_device, {})
            frame_b64 = stored.get('frame_data')
        if frame_b64:
            image_bytes = base64.b64decode(frame_b64)
            return Response(image_bytes, media_type='image/jpeg')
        else:
            return Response(get_black_frame_bytes(), media_type='image/jpeg')
    except:
        return Response(get_black_frame_bytes(), media_type='image/jpeg')

async def upload_frame(request: Request):
    try:
        form = await request.form()
        device_id = form.get("device_id")
        frame_file = form.get("frame")

        if not device_id or not frame_file:
            return PlainTextResponse("Missing device_id or frame", status_code=400)

        # Some browsers may send frame differently
        if hasattr(frame_file, "file"):
            frame_bytes = await frame_file.read()
        else:
            frame_bytes = bytes(frame_file)

        if not frame_bytes:
            return PlainTextResponse("Empty frame", status_code=400)

        b64 = base64.b64encode(frame_bytes).decode("utf-8")
        with frame_lock:
            uploaded_frames[device_id] = {"frame_data": b64, "timestamp": time.time()}

        print(f"✅ Frame stored for {device_id} ({len(frame_bytes)} bytes)")
        return PlainTextResponse("OK")
    except Exception as e:
        print("❌ Upload error:", e)
        return PlainTextResponse("ERROR", status_code=500)

# ------------------------------
# WebSocket audio relay
# ------------------------------
async def ws_device1(websocket: WebSocket):
    global device1_ws, device2_ws
    await websocket.accept()
    device1_ws = websocket
    try:
        while True:
            data = await websocket.receive_bytes()
            if device2_ws: await device2_ws.send_bytes(data)
    except: pass
    finally: device1_ws=None

async def ws_device2(websocket: WebSocket):
    global device1_ws, device2_ws
    await websocket.accept()
    device2_ws = websocket
    try:
        while True:
            data = await websocket.receive_bytes()
            if device1_ws: await device1_ws.send_bytes(data)
    except: pass
    finally: device2_ws=None

# ------------------------------
# Homepage
# ------------------------------
async def homepage(request: Request):
    html = """
    <html><body style="background:#111;color:white;padding:20px;">
    <h1>Video + Audio Server</h1>
    <p><a href="/device1">Device 1</a></p>
    <p><a href="/device2">Device 2</a></p>
    </body></html>
    """
    return HTMLResponse(html)

# ------------------------------
# Routes & app
# ------------------------------
routes = [
    Route("/", homepage),
    Route("/device1", device1_page),
    Route("/device2", device2_page),
    Route("/get_latest_frame", get_latest_frame),
    Route("/upload_frame", upload_frame, methods=["POST"]),
    WebSocketRoute("/device1ws", ws_device1),
    WebSocketRoute("/device2ws", ws_device2)
]

app = Starlette(debug=True, routes=routes)
