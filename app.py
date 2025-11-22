# app.py
import base64
import time
import threading
from collections import defaultdict

import cv2
import numpy as np

from starlette.applications import Starlette
from starlette.routing import Route, WebSocketRoute
from starlette.responses import HTMLResponse, Response, JSONResponse, PlainTextResponse
from starlette.websockets import WebSocket
from starlette.requests import Request

# ------------------------------
# Shared state for video frames
# ------------------------------
uploaded_frames = {
    'device1': {'frame_data': None, 'timestamp': 0},
    'device2': {'frame_data': None, 'timestamp': 0}
}
frame_lock = threading.Lock()

# ------------------------------
# Utility helpers
# ------------------------------
def get_black_frame_bytes():
    black_frame = np.zeros((480, 640, 3), dtype=np.uint8)
    ret, buf = cv2.imencode('.jpg', black_frame)
    return buf.tobytes()

# ------------------------------
# Combined HTML for video + audio
# ------------------------------
DEVICE_PAGE_HTML = r'''
<!DOCTYPE html>
<html>
<head>
    <title>{{ device_name }} - Video+Audio</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <style>
        body { background: #111; color: white; font-family: Arial; padding: 10px; }
        button { padding: 10px; margin: 5px; font-size: 16px; }
        video, img { max-width: 100%; border: 2px solid #444; border-radius: 8px; }
        .stats { margin-top: 10px; }
    </style>
</head>
<body>
    <h1>{{ device_name }} - Video + Audio</h1>

    <div>
        <button id="videoBtn">Start Video</button>
        <button id="audioBtn">Start Audio</button>
    </div>

    <div style="margin-top:15px;">
        <video id="myVideo" autoplay muted playsinline></video>
        <img id="otherVideo" src="" alt="Other device video">
    </div>

    <div class="stats">
        <div>Frames sent: <span id="frameCount">0</span></div>
        <div>Last update from other: <span id="lastUpdate">Never</span></div>
    </div>

    <pre id="log"></pre>

    <script>
    let isVideo = false, isAudio = false;
    let localStream = null, videoInterval = null;
    let wsAudio = null;
    let uploadCount = 0;
    const logEl = document.getElementById("log");

    function log(msg){ logEl.textContent += msg + "\\n"; }

    // -------------------- VIDEO --------------------
    const videoBtn = document.getElementById("videoBtn");
    videoBtn.onclick = async () => {
        if(!isVideo){
            try{
                const stream = await navigator.mediaDevices.getUserMedia({video:true});
                localStream = stream;
                document.getElementById("myVideo").srcObject = stream;
                isVideo = true;
                videoBtn.textContent = "Stop Video";

                const canvas = document.createElement('canvas');
                const ctx = canvas.getContext('2d');
                canvas.width = 320; canvas.height = 240;

                videoInterval = setInterval(()=>{
                    if(!isVideo) return;
                    ctx.drawImage(document.getElementById("myVideo"),0,0,canvas.width,canvas.height);
                    canvas.toBlob(blob=>{
                        const formData = new FormData();
                        formData.append('frame', blob);
                        formData.append('device_id', '{{ device_id }}');
                        fetch('/upload_frame',{method:'POST',body:formData})
                          .then(r=>{ if(r.ok){ uploadCount++; document.getElementById("frameCount").textContent = uploadCount; } });
                    }, 'image/jpeg', 0.5);
                }, 67);

            } catch(e){ log("Video error: " + e); }
        } else {
            isVideo=false;
            videoBtn.textContent="Start Video";
            clearInterval(videoInterval);
            if(localStream){ localStream.getTracks().forEach(t=>t.stop()); localStream=null; }
            document.getElementById("myVideo").srcObject=null;
            uploadCount=0;
            document.getElementById("frameCount").textContent=0;
        }
    };

    // -------------------- AUDIO --------------------
    const audioBtn = document.getElementById("audioBtn");
    audioBtn.onclick = async ()=>{
        if(!isAudio){
            try{
                const stream = await navigator.mediaDevices.getUserMedia({audio:true});
                wsAudio = new WebSocket((location.protocol==="https:"?"wss://":"ws://")+window.location.host+"/{{ ws_endpoint }}");
                wsAudio.binaryType="arraybuffer";

                const audioCtx = new (window.AudioContext||window.webkitAudioContext)();
                const source = audioCtx.createMediaStreamSource(stream);
                const processor = audioCtx.createScriptProcessor(4096,1,1);
                source.connect(processor); processor.connect(audioCtx.destination);

                processor.onaudioprocess = e=>{
                    if(!isAudio) return;
                    let data = e.inputBuffer.getChannelData(0);
                    if(wsAudio && wsAudio.readyState===WebSocket.OPEN) wsAudio.send(data.buffer);
                };

                isAudio=true;
                audioBtn.textContent="Stop Audio";
                log("Audio started");

            } catch(e){ log("Audio error: "+e); }
        } else {
            isAudio=false;
            audioBtn.textContent="Start Audio";
            if(wsAudio){ wsAudio.close(); wsAudio=null; }
            log("Audio stopped");
        }
    };

    // -------------------- POLL OTHER VIDEO --------------------
    const otherVideo = document.getElementById("otherVideo");
    setInterval(()=>{
        fetch('/get_latest_frame?device_id={{ device_id }}&t='+Date.now())
            .then(r=>r.blob())
            .then(blob=>{ const url=URL.createObjectURL(blob); otherVideo.src=url; document.getElementById("lastUpdate").textContent="Just now"; })
            .catch(()=>{ document.getElementById("lastUpdate").textContent="Error"; });
    }, 67);

    </script>
</body>
</html>
'''

# ------------------------------
# Starlette route handlers
# ------------------------------
async def homepage(request: Request):
    html = """
    <html>
    <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Video+Audio Server</title></head>
    <body style="background:#111;color:#fff;font-family:Arial;padding:20px">
        <h1>Audio + Video Relay</h1>
        <p><a href="/device1">Device 1 (video+audio)</a></p>
        <p><a href="/device2">Device 2 (video+audio)</a></p>
        <p>Use the pages to stream frames and audio.</p>
    </body>
    </html>
    """
    return HTMLResponse(html)

async def render_device_page(device_id: str, device_name: str, ws_endpoint: str, request: Request):
    html = DEVICE_PAGE_HTML.replace("{{ device_id }}", device_id)\
                           .replace("{{ device_name }}", device_name)\
                           .replace("{{ ws_endpoint }}", ws_endpoint)
    return HTMLResponse(html)

async def device1(request: Request):
    return await render_device_page("device1", "Device 1", "device1ws", request)

async def device2(request: Request):
    return await render_device_page("device2", "Device 2", "device2ws", request)

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
    except Exception as e:
        return Response(get_black_frame_bytes(), media_type='image/jpeg')

async def upload_frame(request: Request):
    try:
        form = await request.form()
        device_id = form.get('device_id')
        frame_file = form.get('frame')
        if not device_id or not frame_file:
            return PlainTextResponse("Missing device_id or frame", status_code=400)
        frame_bytes = await frame_file.read()
        b64 = base64.b64encode(frame_bytes).decode('utf-8')
        with frame_lock:
            uploaded_frames[device_id] = {'frame_data': b64, 'timestamp': time.time()}
        return PlainTextResponse("OK")
    except Exception as e:
        return PlainTextResponse("ERROR", status_code=500)

async def clear_frames(request: Request):
    device_id = request.path_params.get('device_id')
    with frame_lock:
        uploaded_frames[device_id] = {'frame_data': None, 'timestamp':0}
    return PlainTextResponse(f"Frames cleared for {device_id}")

async def debug_status(request: Request):
    now = time.time()
    status = {d:{'has_frame':uploaded_frames[d]['frame_data'] is not None,'age_seconds': (now-uploaded_frames[d]['timestamp']) if uploaded_frames[d]['timestamp']>0 else None} for d in uploaded_frames}
    return JSONResponse(status)

async def health(request: Request):
    return JSONResponse({'status':'healthy','timestamp':time.time()})

# ------------------------------
# Audio WebSocket relay
# ------------------------------
device1_ws = None
device2_ws = None

async def ws_device1(websocket: WebSocket):
    global device1_ws, device2_ws
    await websocket.accept()
    device1_ws = websocket
    try:
        while True:
            data = await websocket.receive_bytes()
            if device2_ws:
                await device2_ws.send_bytes(data)
    except:
        pass
    finally:
        device1_ws = None

async def ws_device2(websocket: WebSocket):
    global device1_ws, device2_ws
    await websocket.accept()
    device2_ws = websocket
    try:
        while True:
            data = await websocket.receive_bytes()
            if device1_ws:
                await device1_ws.send_bytes(data)
    except:
        pass
    finally:
        device2_ws = None

# ------------------------------
# Routes
# ------------------------------
routes = [
    Route("/", homepage),
    Route("/device1", device1),
    Route("/device2", device2),
    Route("/get_latest_frame", get_latest_frame),
    Route("/upload_frame", upload_frame, methods=["POST"]),
    Route("/clear_frames/{device_id}", clear_frames),
    Route("/debug", debug_status),
    Route("/health", health),
    WebSocketRoute("/device1ws", ws_device1),
    WebSocketRoute("/device2ws", ws_device2),
]

app = Starlette(debug=True, routes=routes)
