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
    'pc1': {'frame_data': None, 'timestamp': 0},
    'pc2': {'frame_data': None, 'timestamp': 0},
    'phone': {'frame_data': None, 'timestamp': 0}
}
frame_lock = threading.Lock()


# ------------------------------
# Utility helpers
# ------------------------------
def get_black_frame_bytes():
    """Return a black jpeg frame bytes"""
    black_frame = np.zeros((480, 640, 3), dtype=np.uint8)
    ret, buf = cv2.imencode('.jpg', black_frame)
    return buf.tobytes()


# ------------------------------
# Video HTML template (client)
# Keep Jinja-style placeholders {{ var }} and we'll replace them manually
# ------------------------------
HTML_TEMPLATE = r'''
<!DOCTYPE html>
<html>
<head>
    <title>Video Chat - {{ device_name }}</title>
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <style>
        * {
            box-sizing: border-box;
            -webkit-tap-highlight-color: transparent;
        }
        body {
            margin: 0;
            padding: 10px;
            background: #1a1a1a;
            color: white;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            touch-action: manipulation;
        }
        .container {
            max-width: 100%;
            margin: 0 auto;
        }
        h1 {
            text-align: center;
            color: #4CAF50;
            margin-bottom: 15px;
            font-size: clamp(18px, 5vw, 24px);
        }
        .video-grid {
            display: flex;
            flex-direction: column;
            gap: 15px;
            margin: 15px 0;
        }
        @media (min-width: 768px) {
            .video-grid {
                flex-direction: row;
            }
        }
        .video-box {
            background: #2a2a2a;
            padding: 12px;
            border-radius: 10px;
            text-align: center;
            flex: 1;
            display: flex;
            flex-direction: column;
            align-items: center;
            justify-content: center;
        }
        .video-box h3 {
            margin: 0 0 10px 0;
            color: #fff;
            font-size: clamp(14px, 4vw, 18px);
            width: 100%;
        }
        img, video {
            max-width: 100%;
            height: auto;
            max-height: 50vh;
            border: 2px solid #444;
            border-radius: 8px;
            background: #000;
            object-fit: contain;
            display: block;
            margin: 0 auto;
        }
        video {
            transform: scaleX(-1);
        }
        .controls {
            text-align: center;
            margin: 20px 0;
        }
        button {
            padding: clamp(12px, 4vw, 16px) clamp(20px, 6vw, 30px);
            margin: 8px;
            border: none;
            border-radius: 8px;
            font-size: clamp(14px, 4vw, 16px);
            font-weight: bold;
            cursor: pointer;
            transition: all 0.3s;
            min-height: 44px;
        }
        #startBtn {
            background: #4CAF50;
            color: white;
        }
        #stopBtn {
            background: #f44336;
            color: white;
        }
        button:disabled {
            background: #666 !important;
            cursor: not-allowed;
            opacity: 0.6;
        }
        .status {
            text-align: center;
            padding: 12px;
            margin: 15px 0;
            border-radius: 8px;
            font-size: clamp(14px, 4vw, 16px);
        }
        .connected { background: #2e7d32; }
        .disconnected { background: #c62828; }
        .waiting { background: #ef6c00; }
        .instructions {
            background: #2a2a2a;
            padding: 15px;
            border-radius: 10px;
            margin: 15px 0;
            font-size: clamp(12px, 3.5vw, 14px);
        }
        code {
            background: #1a1a1a;
            padding: 2px 6px;
            border-radius: 4px;
            color: #4CAF50;
            font-size: clamp(11px, 3vw, 13px);
        }
        .debug-info {
            background: #333;
            padding: 8px;
            border-radius: 5px;
            margin: 8px 0;
            font-size: clamp(12px, 3vw, 14px);
            word-break: break-word;
        }
        .stats {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px;
            margin: 8px 0;
            font-size: clamp(12px, 3vw, 14px);
        }
        .stat-box {
            background: #2a2a2a;
            padding: 8px;
            border-radius: 5px;
            text-align: center;
        }
        .device-info {
            text-align: center;
            margin: 10px 0;
            padding: 10px;
            background: #2196F3;
            border-radius: 8px;
            font-size: clamp(12px, 3.5vw, 14px);
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>🎥 Video Chat - {{ device_name }}</h1>

        <div class="device-info">
            📱 <strong>{{ device_type }}</strong> - {{ device_name }}
        </div>

        <div id="status" class="status disconnected">
            🔴 Ready to start video chat
        </div>

        <div class="debug-info">
            <strong>Debug:</strong> <span id="debugText">Waiting to start...</span>
        </div>

        <div class="stats">
            <div class="stat-box">
                <strong>My Camera:</strong> <span id="myStatus">Off</span>
            </div>
            <div class="stat-box">
                <strong>Other Device:</strong> <span id="otherStatus">Off</span>
            </div>
        </div>

        <div class="controls">
            <button id="startBtn" onclick="startVideo()">Start Video Chat</button>
            <button id="stopBtn" onclick="stopVideo()" disabled>Stop Video</button>
            <button onclick="testUpload()" style="background: #2196F3;">Test Upload</button>
        </div>

        <div class="video-grid">
            <div class="video-box">
                <h3>📹 My Camera</h3>
                <video id="myVideo" autoplay muted playsinline></video>
                <div>Frames sent: <span id="frameCount">0</span></div>
            </div>
            <div class="video-box">
                <h3>👥 Other Device</h3>
                <img id="otherVideo" src="" alt="Other device video" crossorigin="anonymous">
                <div>Last update: <span id="lastUpdate">Never</span></div>
            </div>
        </div>

        <div class="instructions">
            <h3>📋 How to Connect:</h3>
            <ol>
                <li><strong>PC 1:</strong> <code>https://{{ render_url }}/pc1</code></li>
                <li><strong>PC 2:</strong> <code>https://{{ render_url }}/pc2</code></li>
                <li><strong>Phone:</strong> <code>https://{{ render_url }}/phone</code></li>
                <li>Click "Start Video Chat" on all devices</li>
                <li>You'll see each other's video streams!</li>
            </ol>
            <p><strong>📍 Server URL:</strong> <code>{{ render_url }}</code></p>
            <p><strong>🎯 You are:</strong> {{ device_name }} ({{ device_type }})</p>
        </div>
    </div>

    <script>
        let isStreaming = false;
        let localStream = null;
        let uploadCount = 0;
        let frameInterval = null;
        let lastFrameTime = 0;
        let otherVideoPollInterval = null;

        function updateDebug(text) {
            document.getElementById('debugText').textContent = text;
            console.log(text);
        }

        function updateStatus(message, statusClass) {
            const status = document.getElementById('status');
            status.textContent = message;
            status.className = 'status ' + statusClass;
        }

        function updateStats() {
            document.getElementById('frameCount').textContent = uploadCount;
            document.getElementById('myStatus').textContent = isStreaming ? 'Streaming' : 'Off';
            document.getElementById('otherStatus').textContent = isStreaming ? 'Connected' : 'Off';
        }

        async function startVideo() {
            try {
                updateStatus('🟡 Starting camera...', 'waiting');
                updateDebug('Requesting camera access...');
                const isMobile = /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent);
                // Mobile-friendly camera settings
                const constraints = {
                    video: {
                        width: { ideal: isMobile ? 640 : 1280 },
                        height: { ideal: isMobile ? 480 : 720 },
                        frameRate: { ideal: 15 },
                        facingMode: 'user',
                        aspectRatio: isMobile ? 4/3 : 16/9
                    },
                    audio: false
                };

                localStream = await navigator.mediaDevices.getUserMedia(constraints);

                updateDebug('Camera access granted, setting up video...');

                const videoElement = document.getElementById('myVideo');
                videoElement.srcObject = localStream;

                videoElement.onloadedmetadata = () => {
                    updateDebug('Video ready, starting frame upload...');
                    startContinuousUpload(videoElement);
                };

                startOtherVideoStream();

                document.getElementById('startBtn').disabled = true;
                document.getElementById('stopBtn').disabled = false;
                updateStatus('🟢 Video streaming!', 'connected');
                isStreaming = true;

                setInterval(updateStats, 1000);

            } catch (error) {
                console.error('Error starting video:', error);
                updateStatus('🔴 Camera error', 'disconnected');
                updateDebug('Error: ' + error.message);

                if (error.name === 'NotAllowedError') {
                    alert('Camera access was denied. Please allow camera permissions and try again.');
                } else if (error.name === 'NotFoundError') {
                    alert('No camera found. Please check your device has a camera.');
                } else {
                    alert('Could not access camera: ' + error.message);
                }
            }
        }

        function startContinuousUpload(video) {
            const canvas = document.createElement('canvas');
            const ctx = canvas.getContext('2d');

            canvas.width = 320;
            canvas.height = 240;

            updateDebug('Starting continuous upload...');

            const isMobile = /Android|webOS|iPhone|iPad|iPod|BlackBerry|IEMobile|Opera Mini/i.test(navigator.userAgent);
            const frameDelay = isMobile ? 67 : 67;

            frameInterval = setInterval(() => {
                if (!isStreaming) return;

                try {
                    if (video.readyState >= video.HAVE_CURRENT_DATA) {
                        ctx.drawImage(video, 0, 0, canvas.width, canvas.height);

                        canvas.toBlob((blob) => {
                            if (!blob) return;

                            const formData = new FormData();
                            formData.append('frame', blob);
                            formData.append('device_id', '{{ device_id }}');

                            fetch('/upload_frame', {
                                method: 'POST',
                                body: formData
                            })
                            .then(response => {
                                if (response.ok) {
                                    uploadCount++;
                                    lastFrameTime = Date.now();
                                    if (uploadCount === 1) {
                                        updateDebug('🎉 FIRST FRAME UPLOADED!');
                                    }
                                    if (uploadCount % 10 === 0) {
                                        updateDebug(`✅ ${uploadCount} frames sent`);
                                    }
                                }
                            })
                            .catch(error => {
                                console.log('Upload error:', error);
                            });
                        }, 'image/jpeg', 0.5);
                    }
                } catch (error) {
                    console.error('Frame capture error:', error);
                }
            }, frameDelay);
        }

        function startOtherVideoStream() {
            const otherVideo = document.getElementById('otherVideo');
            let lastUpdateTime = 0;

            function pollOtherVideo() {
                if (!isStreaming) return;
                
                fetch('/get_latest_frame?device_id={{ device_id }}&t=' + Date.now())
                    .then(response => {
                        if (response.ok) {
                            return response.blob();
                        }
                        throw new Error('Failed to get frame');
                    })
                    .then(blob => {
                        const url = URL.createObjectURL(blob);
                        otherVideo.src = url;
                        lastUpdateTime = Date.now();
                        document.getElementById('lastUpdate').textContent = 'Just now';
                        
                        if (otherVideo.currentSrc && otherVideo.currentSrc.startsWith('blob:')) {
                            URL.revokeObjectURL(otherVideo.currentSrc);
                        }
                    })
                    .catch(error => {
                        console.log('Polling error:', error);
                        const timeDiff = lastUpdateTime > 0 ? Math.round((Date.now() - lastUpdateTime) / 1000) : 'Never';
                        document.getElementById('lastUpdate').textContent = timeDiff + 's ago';
                    });
            }

            otherVideoPollInterval = setInterval(pollOtherVideo, 67);
        }

        async function testUpload() {
            const video = document.getElementById('myVideo');
            if (video.srcObject) {
                const canvas = document.createElement('canvas');
                const ctx = canvas.getContext('2d');
                canvas.width = 640;
                canvas.height = 480;

                ctx.drawImage(video, 0, 0, canvas.width, canvas.height);

                canvas.toBlob(async (blob) => {
                    updateDebug('Testing upload...');

                    try {
                        const formData = new FormData();
                        formData.append('frame', blob);
                        formData.append('device_id', '{{ device_id }}');

                        const response = await fetch('/upload_frame', {
                            method: 'POST',
                            body: formData
                        });

                        if (response.ok) {
                            uploadCount++;
                            lastFrameTime = Date.now();
                            updateDebug('✅ Test upload successful!');
                            updateStats();
                        } else {
                            updateDebug('❌ Test upload failed');
                        }
                    } catch (error) {
                        updateDebug('❌ Test upload error');
                    }
                }, 'image/jpeg', 0.8);
            } else {
                updateDebug('❌ Please start video first!');
            }
        }

        function stopVideo() {
            isStreaming = false;

            if (frameInterval) {
                clearInterval(frameInterval);
                frameInterval = null;
            }

            if (otherVideoPollInterval) {
                clearInterval(otherVideoPollInterval);
                otherVideoPollInterval = null;
            }

            if (localStream) {
                localStream.getTracks().forEach(track => track.stop());
                localStream = null;
            }

            document.getElementById('myVideo').srcObject = null;
            document.getElementById('otherVideo').src = '';

            fetch('/clear_frames/{{ device_id }}');

            document.getElementById('startBtn').disabled = false;
            document.getElementById('stopBtn').disabled = true;
            updateStatus('🔴 Video stopped', 'disconnected');
            updateDebug('Stream stopped');

            uploadCount = 0;
            lastFrameTime = 0;
            updateStats();
        }

        document.addEventListener('dblclick', (e) => {
            e.preventDefault();
        });

        updateStats();
    </script>
</body>
</html>
'''


# ------------------------------
# AUDIO HTML template
# (uses {title} and {ws_endpoint} placeholders)
# ------------------------------
DEVICE_AUDIO_HTML = """
<!DOCTYPE html>
<html>
<head>
<title>{title}</title>
<style>
#micLevel {{ width: 200px; }}
#signalBars {{ font-size: 1.5em; color: green; }}
#status {{ font-weight: bold; }}
#talkBtn {{ font-size: 1.2em; padding: 0.5em 1em; margin-top: 10px; }}
</style>
</head>
<body>
<h2>{title}</h2>
<div>Status: <span id="status">Connecting...</span></div>
<div>Mic Level: <progress id="micLevel" value="0" max="1"></progress></div>
<div>Signal Bars: <span id="signalBars"></span></div>
<button id="talkBtn">Push to Talk</button>
<pre id="log"></pre>

<script>
const log = (msg) => document.getElementById("log").textContent += msg + "\\n";
const statusEl = document.getElementById("status");
const micEl = document.getElementById("micLevel");
const signalEl = document.getElementById("signalBars");
const talkBtn = document.getElementById("talkBtn");

let ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + window.location.host + "/{ws_endpoint}");
ws.binaryType = "arraybuffer";

ws.onopen = () => statusEl.textContent = "Connected";
ws.onclose = () => statusEl.textContent = "Disconnected";
ws.onerror = () => statusEl.textContent = "Error";

let audioCtx = new (window.AudioContext || window.webkitAudioContext)();
let audioQueue = [];
let playing = false;

ws.onmessage = (event) => {
    audioQueue.push(event.data);
    if(!playing) playNext();
};

function playNext() {
    if(audioQueue.length===0){ playing=false; return; }
    playing=true;
    let chunk = new Float32Array(audioQueue.shift());
    let buffer = audioCtx.createBuffer(1, chunk.length, 44100);
    buffer.copyToChannel(chunk,0);
    let src = audioCtx.createBufferSource();
    src.buffer = buffer;
    src.connect(audioCtx.destination);
    src.onended = playNext;
    src.start();

    let level = Math.max(...chunk.map(Math.abs));
    let bars = Math.min(5, Math.floor(level*10));
    signalEl.textContent = "▮".repeat(bars);
}

let talking=false;
function toggleTalk(){
    talking = !talking;
    talkBtn.textContent = talking ? "Talking..." : "Push to Talk";
}

talkBtn.addEventListener("click", toggleTalk);
talkBtn.addEventListener("touchstart", e => { e.preventDefault(); toggleTalk(); });

let mediaStream, processor;
navigator.mediaDevices.getUserMedia({audio:true}).then(stream=>{
    mediaStream = stream;
    let source = audioCtx.createMediaStreamSource(stream);
    processor = audioCtx.createScriptProcessor(4096,1,1);
    source.connect(processor);
    processor.connect(audioCtx.destination);

    processor.onaudioprocess = e => {
        let input = e.inputBuffer.getChannelData(0);
        let copy = new Float32Array(input.length);
        copy.set(input);

        let level = Math.max(...copy.map(Math.abs));
        micEl.value = level;

        if(talking && ws.readyState===WebSocket.OPEN){
            ws.send(copy.buffer);
        }
    };
}).catch(err=>log("Mic error: "+err));
</script>
</body>
</html>
"""


# ------------------------------
# Starlette route handlers (video)
# ------------------------------
async def homepage(request: Request):
    html = """
    <html>
        <head><meta name="viewport" content="width=device-width, initial-scale=1.0"><title>Video+Audio Server</title></head>
        <body style="background:#111;color:#fff;font-family:Arial;padding:20px">
            <h1>Audio + Video Relay</h1>
            <p><a href="/pc1">PC 1 (video)</a></p>
            <p><a href="/pc2">PC 2 (video)</a></p>
            <p><a href="/phone">Phone (video)</a></p>
            <hr style="border-color:#333">
            <p><a href="/device1">Device 1 (audio)</a></p>
            <p><a href="/device2">Device 2 (audio)</a></p>
            <p>Use the video pages to stream frames; audio pages connect over WebSocket.</p>
        </body>
    </html>
    """
    return HTMLResponse(html)


async def render_video_page(device_id: str, device_name: str, device_type: str, request: Request):
    # Replace template placeholders
    host = request.url.hostname or "localhost"
    html = HTML_TEMPLATE.replace("{{ device_id }}", device_id)\
                        .replace("{{ device_name }}", device_name)\
                        .replace("{{ device_type }}", device_type)\
                        .replace("{{ render_url }}", host)
    return HTMLResponse(html)


async def pc1(request: Request):
    return await render_video_page("pc1", "PC 1", "Desktop", request)


async def pc2(request: Request):
    return await render_video_page("pc2", "PC 2", "Desktop", request)


async def phone(request: Request):
    return await render_video_page("phone", "Phone", "Mobile", request)


async def get_latest_frame(request: Request):
    """
    Query param: device_id (the caller) -> returns the 'other' device's latest frame.
    """
    try:
        device_id = request.query_params.get('device_id', 'pc1')
        # choose another device to show
        other_devices = ['pc1', 'pc2', 'phone']
        if device_id in other_devices:
            other_devices.remove(device_id)
        other_device = other_devices[0] if other_devices else 'pc1'

        with frame_lock:
            stored = uploaded_frames.get(other_device, {})
            frame_b64 = stored.get('frame_data')

        if frame_b64:
            # if it's raw base64 (no data: prefix)
            if frame_b64.startswith('data:image'):
                image_bytes = base64.b64decode(frame_b64.split(',')[1])
            else:
                image_bytes = base64.b64decode(frame_b64)
            return Response(image_bytes, media_type='image/jpeg')
        else:
            black = get_black_frame_bytes()
            return Response(black, media_type='image/jpeg')
    except Exception as e:
        print("Error in get_latest_frame:", e)
        black = get_black_frame_bytes()
        return Response(black, media_type='image/jpeg')


async def upload_frame(request: Request):
    """
    Accept multipart/form-data with 'frame' (file) and 'device_id' form field.
    """
    try:
        form = await request.form()
        device_id = form.get('device_id')
        frame_file = form.get('frame')  # starlette UploadFile

        print(f"📤 Upload request from {device_id}")

        if not device_id:
            return PlainTextResponse("Missing device_id", status_code=400)

        if frame_file is None:
            return PlainTextResponse("No frame file", status_code=400)

        # Read bytes
        if hasattr(frame_file, "file"):
            frame_bytes = await frame_file.read()
        else:
            # fallback
            frame_bytes = bytes(frame_file)

        if not frame_bytes:
            return PlainTextResponse("Empty frame", status_code=400)

        # store base64
        b64 = base64.b64encode(frame_bytes).decode('utf-8')
        with frame_lock:
            uploaded_frames[device_id] = {'frame_data': b64, 'timestamp': time.time()}

        # Debug
        print(f"✅ Frame stored for {device_id} ({len(frame_bytes)} bytes)")
        return PlainTextResponse("OK")
    except Exception as e:
        print("❌ Upload error:", e)
        return PlainTextResponse("ERROR", status_code=500)


async def clear_frames(request: Request):
    device_id = request.path_params.get('device_id')
    if not device_id:
        return PlainTextResponse("Missing device id", status_code=400)
    with frame_lock:
        uploaded_frames[device_id] = {'frame_data': None, 'timestamp': 0}
    print(f"🧹 Cleared frames for {device_id}")
    return PlainTextResponse(f"Frames cleared for {device_id}")


async def debug_status(request: Request):
    now = time.time()
    status = {
        'server': 'Starlette Deployment',
        'uploaded_frames': {
            device_id: {
                'has_frame': uploaded_frames[device_id]['frame_data'] is not None,
                'timestamp': uploaded_frames[device_id]['timestamp'],
                'age_seconds': (now - uploaded_frames[device_id]['timestamp']) if uploaded_frames[device_id]['timestamp'] > 0 else None
            } for device_id in uploaded_frames
        }
    }
    return JSONResponse(status)


async def test_frame(request: Request):
    test_image = np.zeros((480, 640, 3), dtype=np.uint8)
    cv2.rectangle(test_image, (100, 100), (300, 200), (255, 0, 0), -1)
    cv2.rectangle(test_image, (350, 100), (550, 200), (0, 255, 0), -1)
    cv2.putText(test_image, 'TEST FRAME', (200, 300), cv2.FONT_HERSHEY_SIMPLEX, 1, (255, 255, 255), 2)
    ret, buf = cv2.imencode('.jpg', test_image)
    frame_bytes = buf.tobytes()
    body = b'--frame\r\nContent-Type: image/jpeg\r\n\r\n' + frame_bytes + b'\r\n'
    return Response(body, media_type='multipart/x-mixed-replace; boundary=frame')


async def health(request: Request):
    return JSONResponse({
        'status': 'healthy',
        'timestamp': time.time(),
        'uploaded_frames': {d: uploaded_frames[d]['frame_data'] is not None for d in uploaded_frames}
    })


# ------------------------------
# AUDIO: WebSocket audio relay (device1 <-> device2)
# ------------------------------
device1_ws = None
device2_ws = None


async def device1_page(request: Request):
    html = DEVICE_AUDIO_HTML.format(title="Device 1", ws_endpoint="device1ws")
    return HTMLResponse(html)


async def device2_page(request: Request):
    html = DEVICE_AUDIO_HTML.format(title="Device 2", ws_endpoint="device2ws")
    return HTMLResponse(html)


async def ws_device1(websocket: WebSocket):
    global device1_ws, device2_ws
    await websocket.accept()
    device1_ws = websocket
    print("device1 connected")
    try:
        while True:
            data = await websocket.receive_bytes()
            if device2_ws:
                await device2_ws.send_bytes(data)
    except Exception:
        pass
    finally:
        print("device1 disconnected")
        device1_ws = None


async def ws_device2(websocket: WebSocket):
    global device1_ws, device2_ws
    await websocket.accept()
    device2_ws = websocket
    print("device2 connected")
    try:
        while True:
            data = await websocket.receive_bytes()
            if device1_ws:
                await device1_ws.send_bytes(data)
    except Exception:
        pass
    finally:
        print("device2 disconnected")
        device2_ws = None


# ------------------------------
# Starlette app & routes
# ------------------------------
routes = [
    Route("/", homepage),
    Route("/pc1", pc1),
    Route("/pc2", pc2),
    Route("/phone", phone),
    Route("/get_latest_frame", get_latest_frame),
    Route("/upload_frame", upload_frame, methods=["POST"]),
    Route("/clear_frames/{device_id}", clear_frames),
    Route("/debug", debug_status),
    Route("/test_frame", test_frame),
    Route("/health", health),

    # audio pages
    Route("/device1", device1_page),
    Route("/device2", device2_page),

    # audio websockets
    WebSocketRoute("/device1ws", ws_device1),
    WebSocketRoute("/device2ws", ws_device2),
]

app = Starlette(debug=True, routes=routes)
