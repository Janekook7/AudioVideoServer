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
    bottom: 70px; /* leave space for control bar */
    display: flex;
    background: #000;
}

/* LEFT (My camera) */
#myCamBox, #otherCamBox {
    flex: 1;
    display: flex;
    justify-content: center;
    align-items: center;
    background: #000;
}

video, img {
    width: 95%;
    height: auto;
    max-height: 90%;
    border-radius: 10px;
    background: #111;
    object-fit: cover;
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
    padding-bottom: 5px;
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
    display: flex;
    justify-content: center;
    align-items: center;
    transition: 0.2s;
}

/* States */
.btn-on { background: #4CAF50; }
.btn-off { background: #555; }
.btn-danger { background: #b40000; }

.controlBtn:active {
    transform: scale(0.93);
}

/* For status text (mic level etc.) */
#infoBox {
    position: fixed;
    top: 10px;
    left: 10px;
    color: #0f0;
    font-size: 14px;
}
</style>
</head>
<body>

<div id="mainContainer">
    <!-- LEFT: MY CAMERA -->
    <div id="myCamBox">
        <video id="myVideo" autoplay muted playsinline></video>
    </div>

    <!-- RIGHT: OTHER CAMERA -->
    <div id="otherCamBox">
        <img id="otherVideo" src="">
    </div>
</div>

<!-- INFO TEXT -->
<div id="infoBox">
    Frames sent: <span id="frameCount">0</span><br>
    Other last update: <span id="lastUpdate">Never</span>
</div>

<!-- CONTROL BAR -->
<div id="controls">

    <!-- MIC -->
    <button id="micBtn" class="controlBtn btn-off" onclick="toggleMicUI()">
        🎤
    </button>

    <!-- DEAFEN -->
    <button id="deafenBtn" class="controlBtn btn-off" onclick="toggleDeafenUI()">
        🔇
    </button>

    <!-- VIDEO -->
    <button id="videoBtn" class="controlBtn btn-danger" onclick="toggleVideoUI()">
        🎥
    </button>

</div>

<script>
/* -----------------------
   UI Toggle (visual only)
   Your existing backend
   functions still run!
------------------------*/

let micOn = false;
let deafenOn = false;
let videoOn = false;

/* MIC BUTTON */
function toggleMicUI() {
    micOn = !micOn;
    document.getElementById("micBtn").className = micOn ? "controlBtn btn-on" : "controlBtn btn-off";
    toggleAudio(); // your existing function
}

/* DEAFEN BUTTON (local mute + remote mute) */
function toggleDeafenUI() {
    deafenOn = !deafenOn;
    document.getElementById("deafenBtn").className = deafenOn ? "controlBtn btn-on" : "controlBtn btn-off";

    // LOCAL EFFECT:
    if (deafenOn) {
        if (window.mediaStream) {
            window.mediaStream.getAudioTracks().forEach(t => t.enabled = false);
        }
    } else {
        if (window.mediaStream) {
            window.mediaStream.getAudioTracks().forEach(t => t.enabled = true);
        }
    }

    // remote mute handled on your server if you want
}

/* VIDEO BUTTON */
function toggleVideoUI() {
    videoOn = !videoOn;

    const btn = document.getElementById("videoBtn");
    if (videoOn) {
        btn.className = "controlBtn btn-on";
    } else {
        btn.className = "controlBtn btn-danger";
    }

    toggleVideo(); // your existing function
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
