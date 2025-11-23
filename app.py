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
        margin:0;
        padding:0;
        background:#000;
        color:white;
        font-family:Arial, sans-serif;
        overflow:hidden;
    }

    /* Full screen 2-video layout */
    #videoContainer {
        display:flex;
        flex-direction:row;
        width:100vw;
        height:100vh;
        background:#111;
    }

    .videoBox {
        flex:1;
        display:flex;
        justify-content:center;
        align-items:center;
        background:#000;
        border-right:2px solid #222;
    }

    .videoBox:last-child {
        border-right:none;
    }

    video, img {
        width:100%;
        height:100%;
        object-fit:cover;
        background:#000;
    }

    /* Bottom control bar */
    #controlBar {
        position:fixed;
        bottom:0;
        left:0;
        width:100%;
        height:80px;
        background:rgba(20,20,20,0.9);
        display:flex;
        justify-content:center;
        align-items:center;
        gap:25px;
        border-top:2px solid #333;
    }

    .ctrlBtn {
        width:55px;
        height:55px;
        border:none;
        border-radius:12px;
        background:#2b2b2b;
        display:flex;
        justify-content:center;
        align-items:center;
        font-size:28px;
        color:white;
        cursor:pointer;
        transition:0.2s;
    }

    .ctrlBtn.red {
        background:#b32626;
    }

    .ctrlBtn:hover {
        background:#444;
    }

    .ctrlBtn.red:hover {
        background:#d22;
    }
</style>
</head>

<body>

<!-- 2-camera fullscreen layout -->
<div id="videoContainer">
    <div class="videoBox">
        <video id="myVideo" autoplay muted playsinline></video>
    </div>

    <div class="videoBox">
        <img id="otherVideo">
    </div>
</div>

<!-- Control panel -->
<div id="controlBar">
    <button id="micBtn" class="ctrlBtn">🎤</button>
    <button id="deafenBtn" class="ctrlBtn">🎧</button>
    <button id="videoBtn" class="ctrlBtn">🎥</button>
</div>


<script>
/* --------------------------
   STATE
--------------------------- */
let micOn = false;
let deafened = false;
let videoOn = false;

/* --------------------------
   BUTTON LOGIC
--------------------------- */

document.getElementById("micBtn").onclick = () => {
    micOn = !micOn;
    document.getElementById("micBtn").classList.toggle("red", !micOn);
    toggleAudio();     // call your existing function
};

document.getElementById("deafenBtn").onclick = () => {
    deafened = !deafened;
    document.getElementById("deafenBtn").classList.toggle("red", deafened);

    // Deafened = mute mic AND mute incoming audio
    if (deafened) {
        if (window.mediaStream) {
            mediaStream.getTracks().forEach(t => t.enabled = false);
        }
        window._deafened = true;
    } else {
        if (window.mediaStream) {
            mediaStream.getTracks().forEach(t => t.enabled = true);
        }
        window._deafened = false;
    }
};

document.getElementById("videoBtn").onclick = () => {
    videoOn = !videoOn;
    document.getElementById("videoBtn").classList.toggle("red", !videoOn);
    toggleVideo();     // call your existing camera function
};

/* --------------------------
   VIDEO & AUDIO HOOKS
   (your functions remain)
--------------------------- */

const device_id = "{{ device_id }}";
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
