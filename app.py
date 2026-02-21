import os
import json
import time
import hashlib
import asyncio
import threading
import mimetypes
import requests
from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from google import genai
import websockets
from mcp.server.fastmcp import FastMCP

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
    print("Starting Flask background thread on port 5000...")
    threading.Thread(target=run_flask, daemon=True).start()

    # 2. Start MCP in the main thread (stdio)
    # This allows Claude Desktop to communicate via stdin/stdout
    print("Starting MCP Server (stdio mode)...")
    mcp.run(transport="stdio")
