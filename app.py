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
<title>{{ device_name }}</title>
<meta name="viewport" content="width=device-width, initial-scale=1.0">

<style>
body {
    margin: 0;
    padding: 0;
    background: black;
    overflow: hidden;
    font-family: Arial, sans-serif;
    color: white;
}

/* --- LAYOUT --- */
#mainContainer {
    display: flex;
    justify-content: center;
    align-items: center;
    gap: 15px;
    height: calc(100vh - 100px);
    padding: 10px;
}

.videoBox {
    background: #111;
    border-radius: 10px;
    padding: 5px;
    width: 48%;
    height: 100%;
    display: flex;
    justify-content: center;
    align-items: center;
}

video, img {
    width: 100%;
    height: 100%;
    object-fit: cover;
    border-radius: 10px;
}

/* --- BOTTOM CONTROL BAR (Discord style) --- */
#controlBar {
    position: fixed;
    bottom: 25px;
    left: 50%;
    transform: translateX(-50%);
    background: #1e1e1e;
    padding: 10px 25px;
    display: flex;
    gap: 20px;
    border-radius: 15px;
}

.controlButton {
    background: #2c2c2c;
    border: none;
    padding: 12px 20px;
    color: white;
    border-radius: 10px;
    font-size: 16px;
    cursor: pointer;
}

.controlButton.red {
    background: #b52828;
}

.controlButton.green {
    background: #3ba55d;
}
</style>
</head>

<body>

<div id="mainContainer">
    <!-- My Camera -->
    <div class="videoBox">
        <video id="myVideo" autoplay muted playsinline></video>
    </div>

    <!-- Other Camera -->
    <div class="videoBox">
        <img id="otherVideo" src="">
    </div>
</div>

<!-- ==================== CONTROL BAR ==================== -->
<div id="controlBar">

    <button id="micBtn" class="controlButton green" onclick="toggleMic()">
        🎤 Mic On
    </button>

    <button id="deafenBtn" class="controlButton" onclick="toggleDeafen()">
        🔇 Deafen
    </button>

    <button id="videoBtn" class="controlButton green" onclick="toggleVideo()">
        🎥 Video On
    </button>

</div>

<script>
/* ==========================================================
   JAVASCRIPT – Wire into your existing functions
   ========================================================== */

let micOn = true;
let videoOn = false;
let deafened = false;

function toggleMic() {
    micOn = !micOn;

    if (micOn) {
        document.getElementById("micBtn").textContent = "🎤 Mic On";
        document.getElementById("micBtn").classList.remove("red");
        document.getElementById("micBtn").classList.add("green");
        toggleAudio(true);
    } else {
        document.getElementById("micBtn").textContent = "🔇 Mic Off";
        document.getElementById("micBtn").classList.remove("green");
        document.getElementById("micBtn").classList.add("red");
        toggleAudio(false);
    }
}

function toggleDeafen() {
    deafened = !deafened;

    if (deafened) {
        document.getElementById("deafenBtn").textContent = "🔇 Deafened";
        document.getElementById("deafenBtn").classList.add("red");

        // Stop sending + receiving audio
        toggleAudio(false);
    } else {
        document.getElementById("deafenBtn").textContent = "🔈 Undeafened";
        document.getElementById("deafenBtn").classList.remove("red");

        toggleAudio(true);
    }
}

function toggleVideo() {
    videoOn = !videoOn;

    if (videoOn) {
        document.getElementById("videoBtn").textContent = "🎥 Video On";
        document.getElementById("videoBtn").classList.add("green");
        startVideoUpload();
    } else {
        document.getElementById("videoBtn").textContent = "🚫 Video Off";
        document.getElementById("videoBtn").classList.remove("green");
        document.getElementById("videoBtn").classList.add("red");
        stopVideo();
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
