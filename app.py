import os
import sys
import json
import time
import hashlib
import asyncio
import threading
import mimetypes
import requests
import io
from datetime import datetime
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from google import genai
import websockets
from mcp.server.fastmcp import FastMCP

# Try to import Pillow elements, fallback safe check
try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    raise ImportError("The Pillow library is required. Install it using 'pip install Pillow'.")

# --- CONFIGURATION ---
KNOWN_HASH = "081390df21e1d49e0af02bf37ff289e7385450db0fadbf7dd937720027759d68"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")
DEFAULT_MODEL = "gemini-2.5-flash"

# --- INITIALIZATION ---
app = Flask(__name__)
CORS(app)
mcp = FastMCP("WebHub-Integrated")
genai_client = genai.Client(api_key=GEMINI_API_KEY)

state = {"last_app": None}
owot_clients = {}

# --- SAFE PRINT UTILITY ---
def safe_log(message: str):
    """Safely logs to stderr if stdout is closed (common on headless hosts like Render)."""
    try:
        if not sys.stdout.closed:
            print(message, flush=True)
        else:
            sys.stderr.write(message + "\n")
            sys.stderr.flush()
    except Exception:
        pass

# --- OWOT WEBSOCKET MANAGER ---
class OWOTManager:
    def __init__(self, world):
        self.world = world
        self.url = f"wss://ourworldoftext.com/{world}/ws/"
        self.chat_buffer = []
        self.tiles = {}
        self.edit_id = 1
        self.ws = None
        self.loop = asyncio.new_event_loop()
        
    def start(self):
        threading.Thread(target=self._run_loop, daemon=True).start()

    def _run_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._listen())

    async def _listen(self):
        async with websockets.connect(self.url) as ws:
            self.ws = ws
            while True:
                try:
                    msg = await ws.recv()
                    data = json.loads(msg)
                    kind = data.get("kind")
                    if kind == "chat":
                        self.chat_buffer.append(f"[{data.get('nickname')}]: {data.get('message')}")
                        if len(self.chat_buffer) > 50: self.chat_buffer.pop(0)
                    elif kind == "fetch":
                        self.tiles.update(data.get("tiles", {}))
                except Exception:
                    break

    def run_coro(self, coro):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()

def get_owot_client(world):
    if world not in owot_clients:
        mgr = OWOTManager(world)
        mgr.start()
        time.sleep(1.5) # Handshake time
        owot_clients[world] = mgr
    return owot_clients[world]

# --- MCP TOOLS (For Claude) ---

@mcp.tool()
def owot_read_chat(world: str = "") -> str:
    """Gets the 20 most recent chat messages from an OWOT world."""
    client = get_owot_client(world)
    return "\n".join(client.chat_buffer[-20:]) if client.chat_buffer else "No chat history found."

@mcp.tool()
def owot_write(world: str, text: str, tileX: int, tileY: int, charX: int = 0, charY: int = 0) -> str:
    """Writes text to the OWOT infinite grid at specific coordinates."""
    client = get_owot_client(world)
    edit = [tileY, tileX, charY, charX, int(time.time() * 1000), text, client.edit_id]
    client.edit_id += 1
    client.run_coro(client.ws.send(json.dumps({"kind": "write", "edits": [edit]})))
    return f"Wrote '{text}' to {world} at Tile({tileX},{tileY})"

@mcp.tool()
def ask_gemini(prompt: str) -> str:
    """Ask the Gemini AI model a question using the configured API key."""
    response = genai_client.models.generate_content(model=DEFAULT_MODEL, contents=prompt)
    return response.text

# --- DYNAMIC IMAGE ROUTE HANDLERS ---

def get_client_ip():
    """Helper to retrieve the remote client's IP, handling reverse proxies."""
    ip = request.headers.get("X-Forwarded-For", request.remote_addr)
    if ip and "," in ip:
        ip = ip.split(",")[0].strip()
    return ip

@app.route("/images/ip.png")
def get_ip_image():
    """Generates a dynamic image showing the client's current IP address."""
    ip = get_client_ip() or "Unknown IP"
    
    # Create canvas (dark background)
    img = Image.new("RGB", (320, 80), color=(20, 24, 33))
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()
    
    # Draw label and IP text
    draw.text((15, 15), "Client IP Address:", fill=(110, 120, 140), font=font)
    draw.text((15, 40), ip, fill=(74, 222, 128), font=font)
    
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    
    return Response(buf.getvalue(), mimetype="image/png")

@app.route("/images/time.png")
def get_time_image():
    """Generates a dynamic image displaying the server's current UTC time."""
    current_time = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
    
    img = Image.new("RGB", (320, 80), color=(31, 41, 55))
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()
    
    draw.text((15, 15), "Current Server Time:", fill=(156, 163, 175), font=font)
    draw.text((15, 40), current_time, fill=(251, 191, 36), font=font)
    
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    
    return Response(buf.getvalue(), mimetype="image/png")

@app.route("/images/identicon.png")
def get_identicon_image():
    """Generates a unique, symmetric grid avatar based on the client's IP."""
    ip = get_client_ip() or "127.0.0.1"
    ip_hash = hashlib.md5(ip.encode("utf-8")).hexdigest()
    
    # Extract colors from the hash
    r = int(ip_hash[0:2], 16)
    g = int(ip_hash[2:4], 16)
    b = int(ip_hash[4:6], 16)
    fg_color = (r, g, b)
    bg_color = (243, 244, 246)
    
    grid_size = 5
    block_size = 24
    padding = 12
    img_width = grid_size * block_size + padding * 2
    
    img = Image.new("RGB", (img_width, img_width), color=bg_color)
    draw = ImageDraw.Draw(img)
    
    for col in range(grid_size):
        source_col = col if col < 3 else 4 - col
        for row in range(grid_size):
            bit_index = source_col * grid_size + row
            byte_index = bit_index // 4
            bit_shift = (bit_index % 4) * 2
            is_filled = (int(ip_hash[byte_index], 16) >> bit_shift) & 1
            
            if is_filled:
                x1 = padding + col * block_size
                y1 = padding + row * block_size
                x2 = x1 + block_size
                y2 = y1 + block_size
                draw.rectangle([x1, y1, x2, y2], fill=fg_color)
                
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    
    return Response(buf.getvalue(), mimetype="image/png")

# --- FLASK ROUTES (Your Original App) ---

def authorized():
    supplied = request.headers.get("Authorization", "")
    return hashlib.sha256(supplied.encode()).hexdigest() == KNOWN_HASH

@app.route("/gemini", methods=["POST"])
def gemini_proxy():
    if not authorized(): return "unauthorized", 401
    prompt = request.json.get("prompt")
    return jsonify({"text": ask_gemini(prompt)})

@app.route("/github/<user>/<repo>/<branch>/", defaults={'filepath': 'index.html'})
@app.route("/github/<user>/<repo>/<branch>/<path:filepath>")
def github_proxy(user, repo, branch, filepath):
    raw_url = f"https://raw.githubusercontent.com/{user}/{repo}/{branch}/{filepath}"
    try:
        resp = requests.get(raw_url)
        ctype, _ = mimetypes.guess_type(filepath)
        if not ctype:
            ctype = "application/javascript" if filepath.endswith(".js") else "text/html"
        return Response(resp.content, mimetype=ctype, status=resp.status_code)
    except Exception as e: return str(e), 500

@app.route('/proxy')
def get_cors_proxy():
    return requests.get(request.args.get("url")).content

@app.route("/update", methods=["POST"])
def update():
    if not authorized(): return "unauthorized", 401
    state["last_app"] = request.args.get("app")
    return "updated", 200

@app.route("/get", methods=["GET"])
def get_last():
    return jsonify(state)

@app.route("/keepalive", methods=["GET"])
def keepalive():
    return "ok" if authorized() else "unauthorized but alive", 200

@app.route("/p/<host>/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@app.route("/p/<host>/<path:path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
def subpage_proxy(host, path):
    base = f"https://{host}"
    url = f"{base}/{path}" if path else f"{base}/"
    forwarded_headers = {
        key: value
        for key, value in request.headers
        if key.lower() not in {"host", "content-length", "connection"}
    }
    resp = requests.request(
        method=request.method,
        url=url,
        params=request.args,
        data=request.get_data(),
        headers=forwarded_headers,
        allow_redirects=False,
        timeout=20,
    )

    excluded_headers = {"content-encoding", "transfer-encoding", "connection"}
    response_headers = [
        (key, value)
        for key, value in resp.headers.items()
        if key.lower() not in excluded_headers
    ]
    return Response(resp.content, status=resp.status_code, headers=response_headers)

# --- SERVER RUNNER ---

def run_flask():
    port = int(os.environ.get("PORT", 5000))
    # We must use use_reloader=False when running in a thread
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)

if __name__ == "__main__":
    # 1. Start Flask in the background
    safe_log("Starting Flask background thread on port 5000...")
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()

    # Give Flask a brief moment to establish its socket
    time.sleep(0.5)

    # 2. Start MCP Server in stdio mode
    safe_log("Starting MCP Server (stdio mode)...")
    try:
        mcp.run(transport="stdio")
    except Exception as e:
        safe_log(f"MCP Server closed or is unsupported in this environment: {e}")

    # 3. Prevent the main thread from exiting if the stdio stream terminates
    safe_log("MCP Server run-loop finished. Keeping main thread alive for Flask proxy routes...")
    try:
        while True:
            time.sleep(1000)
    except KeyboardInterrupt:
        safe_log("Stopping WebHub Server...")
