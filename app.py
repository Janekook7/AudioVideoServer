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
body { margin:0; padding:10px; font-family:Arial; background:#111; color:white;}
h1 { text-align:center; color:#4CAF50; }
.video-box, .audio-box { margin:10px 0; padding:10px; background:#222; border-radius:8px; }
video, img { max-width:100%; border:2px solid #444; border-radius:8px; background:#000; display:block; margin:0 auto; }
button { padding:10px 20px; margin:5px; font-size:16px; border:none; border-radius:6px; cursor:pointer; }
#startVideoBtn { background:#4CAF50; color:white; }
#startAudioBtn { background:#2196F3; color:white; }
#stopVideoBtn, #stopAudioBtn { background:#f44336; color:white; }
</style>
</head>
<body>
<h1>{{ device_name }} - Video+Audio</h1>

<div class="video-box">
    <h3>🎥 My Camera</h3>
    <video id="myVideo" autoplay muted playsinline></video>
    <div>Frames sent: <span id="frameCount">0</span></div>
    <button id="startVideoBtn" onclick="toggleVideo()">Start Video</button>
</div>

<div class="video-box">
    <h3>👥 Other Device</h3>
    <img id="otherVideo" src="" alt="Other device video">
    <div>Last update: <span id="lastUpdate">Never</span></div>
</div>

<div class="audio-box">
    <h3>🎤 Audio</h3>
    <div>Status: <span id="audioStatus">Off</span></div>
    <progress id="micLevel" value="0" max="1" style="width:100%;"></progress>
    <button id="startAudioBtn" onclick="toggleAudio()">Start Audio</button>
</div>

<script>
let isVideoOn = false;
let isAudioOn = false;
let localStream = null;
let frameInterval = null;
let uploadCount = 0;
let ws = null;
let audioCtx = null;
let mediaStream = null;
let processor = null;
let audioQueue = [];
let playing=false;
let lastOtherBlobUrl = null;

const device_id = "{{ device_id }}";

function updateStats() {
    document.getElementById('frameCount').textContent = uploadCount;
}

async function toggleVideo() {
    if(!isVideoOn){
        try{
            const constraints = { video: { width:640, height:480, frameRate:15 }, audio:false };
            localStream = await navigator.mediaDevices.getUserMedia(constraints);
            document.getElementById("myVideo").srcObject = localStream;
            startVideoUpload();
            isVideoOn = true;
            document.getElementById("startVideoBtn").textContent = "Stop Video";
        }catch(e){ alert("Camera error: "+e); }
    }else{
        stopVideo();
    }
}

function startVideoUpload(){
    const video = document.getElementById("myVideo");
    const canvas = document.createElement("canvas");
    const ctx = canvas.getContext("2d");
    canvas.width=320; canvas.height=240;
    frameInterval = setInterval(()=>{
        if(video.readyState>=video.HAVE_CURRENT_DATA){
            ctx.drawImage(video,0,0,canvas.width,canvas.height);
            canvas.toBlob(async (blob)=>{
                const formData = new FormData();
                formData.append("frame", blob);
                formData.append("device_id", device_id);
                try{
                    const res = await fetch("/upload_frame",{method:"POST",body:formData});
                    if(res.ok){ uploadCount++; updateStats(); }
                }catch(e){ console.log(e); }
            }, "image/jpeg",0.5);
        }
    }, 67);
    startOtherVideoStream();
}

function stopVideo(){
    isVideoOn=false;
    if(frameInterval){ clearInterval(frameInterval); frameInterval=null; }
    if(localStream){ localStream.getTracks().forEach(t=>t.stop()); localStream=null; }
    document.getElementById("myVideo").srcObject=null;
    uploadCount=0; updateStats();
    document.getElementById("startVideoBtn").textContent = "Start Video";
}

function startOtherVideoStream(){
    const otherVideo = document.getElementById("otherVideo");
    setInterval(async ()=>{
        try{
            const resp = await fetch('/get_latest_frame?device_id='+device_id+'&t='+Date.now());
            if(!resp.ok) return;
            const blob = await resp.blob();
            const url = URL.createObjectURL(blob);
            otherVideo.src = url;
            document.getElementById("lastUpdate").textContent = "Just now";
            if(lastOtherBlobUrl) URL.revokeObjectURL(lastOtherBlobUrl);
            lastOtherBlobUrl = url;
        }catch(e){ document.getElementById("lastUpdate").textContent = "Error"; }
    }, 67);
}

async function toggleAudio(){
    if(!isAudioOn){
        ws = new WebSocket((location.protocol==="https:"?"wss://":"ws://")+window.location.host+"/{{ ws_endpoint }}");
        ws.binaryType="arraybuffer";
        ws.onmessage=(ev)=>{ audioQueue.push(ev.data); if(!playing) playNext(); };
        audioCtx = new (window.AudioContext||window.webkitAudioContext)();
        mediaStream = await navigator.mediaDevices.getUserMedia({audio:true});
        const source = audioCtx.createMediaStreamSource(mediaStream);
        processor = audioCtx.createScriptProcessor(4096,1,1);
        source.connect(processor); processor.connect(audioCtx.destination);
        processor.onaudioprocess=(e)=>{
            const input = e.inputBuffer.getChannelData(0);
            const copy = new Float32Array(input.length); copy.set(input);
            document.getElementById("micLevel").value = Math.max(...copy.map(Math.abs));
            if(ws && ws.readyState===WebSocket.OPEN){ ws.send(copy.buffer); }
        };
        isAudioOn=true;
        document.getElementById("startAudioBtn").textContent="Stop Audio";
        document.getElementById("audioStatus").textContent="On";
    }else{
        isAudioOn=false;
        if(processor) processor.disconnect(); processor=null;
        if(mediaStream){ mediaStream.getTracks().forEach(t=>t.stop()); mediaStream=null; }
        if(ws) ws.close(); ws=null;
        document.getElementById("startAudioBtn").textContent="Start Audio";
        document.getElementById("audioStatus").textContent="Off";
    }
}

function playNext(){
    if(audioQueue.length===0){ playing=false; return; }
    playing=true;
    const chunk = new Float32Array(audioQueue.shift());
    const buffer = audioCtx.createBuffer(1, chunk.length, 44100);
    buffer.copyToChannel(chunk,0);
    const src = audioCtx.createBufferSource();
    src.buffer=buffer; src.connect(audioCtx.destination);
    src.onended=playNext; src.start();
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
        device_id = form.get('device_id')
        frame_file = form.get('frame')
        frame_bytes = await frame_file.read()
        b64 = base64.b64encode(frame_bytes).decode('utf-8')
        with frame_lock:
            uploaded_frames[device_id] = {'frame_data': b64, 'timestamp': time.time()}
        return PlainTextResponse("OK")
    except Exception as e:
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
