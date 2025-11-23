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
    font-family: Arial;
    color: white;
    overflow: hidden;
}

/* MAIN LAYOUT */
#mainContainer {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 70px;
    display: flex;
    background: #000;
}

#myCamBox, #otherCamBox {
    flex: 1;
    display: flex;
    justify-content: center;
    align-items: center;
}

video, img {
    width: 95%;
    height: auto;
    max-height: 90%;
    border-radius: 10px;
    object-fit: cover;
    background: #111;
}

/* BOTTOM CONTROL BAR */
#controls {
    position: fixed;
    bottom: 0;
    left: 0;
    right: 0;
    height: 70px;
    background: rgba(20,20,20,0.95);
    display: flex;
    justify-content: center;
    align-items: center;
    gap: 25px;
}

/* BUTTONS */
.controlBtn {
    width: 60px;
    height: 60px;
    border-radius: 50%;
    border: none;
    font-size: 28px;
    color: white;
    cursor: pointer;
}

.btn-on { background: #4CAF50; }
.btn-off { background: #555; }
.btn-danger { background: #b40000; }

/* INFO BOX */
#infoBox {
    position: fixed;
    top: 10px;
    left: 10px;
    font-size: 14px;
}
</style>
</head>

<body>

<div id="mainContainer">
    <div id="myCamBox"><video id="myVideo" autoplay muted playsinline></video></div>
    <div id="otherCamBox"><img id="otherVideo"></div>
</div>

<div id="infoBox">
    Frames sent: <span id="frameCount">0</span><br>
    Last update: <span id="lastUpdate">Never</span>
</div>

<div id="controls">
    <button id="micBtn" class="controlBtn btn-off" onclick="toggleMicUI()">🎤</button>
    <button id="deafenBtn" class="controlBtn btn-off" onclick="toggleDeafenUI()">🔇</button>
    <button id="videoBtn" class="controlBtn btn-danger" onclick="toggleVideoUI()">🎥</button>
</div>

<script>
let device_id = "{{ device_id }}";

/* -------------------------------
       VIDEO LOGIC (ORIGINAL)
--------------------------------*/
let isVideoOn = false;
let localStream = null;
let frameInterval = null;
let uploadCount = 0;

async function toggleVideo() {
    if (!isVideoOn) {
        const stream = await navigator.mediaDevices.getUserMedia({
            video: { width: 640, height: 480, frameRate: 15 },
            audio: false
        });
        localStream = stream;
        document.getElementById("myVideo").srcObject = stream;
        startVideoUpload();
        isVideoOn = true;
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
            ctx.drawImage(video,0,0,canvas.width,canvas.height);
            canvas.toBlob(async (blob) => {
                const fd = new FormData();
                fd.append("frame", blob);
                fd.append("device_id", device_id);
                await fetch("/upload_frame", { method: "POST", body: fd });
                uploadCount++;
                document.getElementById("frameCount").textContent = uploadCount;
            }, "image/jpeg",0.5);
        }
    }, 67);

    startOtherStream();
}

function stopVideo() {
    isVideoOn = false;
    if (frameInterval) clearInterval(frameInterval);
    if (localStream) localStream.getTracks().forEach(t => t.stop());
    document.getElementById("myVideo").srcObject = null;
    uploadCount = 0;
    document.getElementById("frameCount").textContent = "0";
}

function startOtherStream() {
    const other = document.getElementById("otherVideo");
    setInterval(async () => {
        const res = await fetch("/get_latest_frame?device_id=" + device_id);
        const blob = await res.blob();
        other.src = URL.createObjectURL(blob);
        document.getElementById("lastUpdate").textContent = "Just now";
    }, 100);
}

/* -------------------------------
       AUDIO LOGIC (ORIGINAL)
--------------------------------*/
let ws = null;
let isAudioOn = false;
let audioCtx = null;
let mediaStream = null;
let processor = null;

async function toggleAudio() {
    if (!isAudioOn) {
        ws = new WebSocket((location.protocol==="https:"?"wss://":"ws://") + window.location.host + "/{{ ws_endpoint }}");
        ws.binaryType = "arraybuffer";

        audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });

        const source = audioCtx.createMediaStreamSource(mediaStream);
        processor = audioCtx.createScriptProcessor(4096, 1, 1);
        source.connect(processor);
        processor.connect(audioCtx.destination);

        processor.onaudioprocess = (e) => {
            const data = e.inputBuffer.getChannelData(0);
            const copy = new Float32Array(data);
            if (ws.readyState === WebSocket.OPEN) ws.send(copy.buffer);
        };

        isAudioOn = true;
    } else {
        isAudioOn = false;
        if (processor) processor.disconnect();
        if (mediaStream) mediaStream.getTracks().forEach(t => t.stop());
        if (ws) ws.close();
    }
}

/* --------------------------------
     UI Buttons (visual + logic)
--------------------------------*/
function toggleVideoUI() {
    const btn = document.getElementById("videoBtn");
    if (!isVideoOn) btn.className = "controlBtn btn-on";
    else btn.className = "controlBtn btn-danger";
    toggleVideo();
}

let micOn = false;
function toggleMicUI() {
    micOn = !micOn;
    document.getElementById("micBtn").className = micOn ? "controlBtn btn-on" : "controlBtn btn-off";
    toggleAudio();
}

let deafenOn = false;
function toggleDeafenUI() {
    deafenOn = !deafenOn;
    document.getElementById("deafenBtn").className = deafenOn ? "controlBtn btn-on" : "controlBtn btn-off";

    if (mediaStream) {
        mediaStream.getAudioTracks().forEach(t => t.enabled = !deafenOn);
    }
}
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
