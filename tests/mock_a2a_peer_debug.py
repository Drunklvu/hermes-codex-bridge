"""Mock A2A v1.0 server (debug) — prints raw request bodies."""
import json
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9999

CARD = {
    "name": "Mock Researcher",
    "description": "A mock research agent for A2A protocol testing.",
    "url": f"http://127.0.0.1:{PORT}",
    "version": "1.0.0",
    "provider": {"organization": "Mock Labs", "url": f"http://127.0.0.1:{PORT}"},
    "supportedInterfaces": [
        {"url": f"http://127.0.0.1:{PORT}", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
    ],
    "capabilities": {
        "streaming": False, "pushNotifications": False,
        "stateTransitionHistory": False, "extendedAgentCard": False,
    },
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [
        {"name": "web_search", "description": "Search the web"},
        {"name": "research", "description": "Research a topic"},
    ],
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write(f"[mock-a2a] {fmt % args}\n")

    def _send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/.well-known/agent-card"):
            self._send_json(CARD)
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        # DEBUG: print raw request
        sys.stderr.write(f"[mock-a2a] RAW BODY: {raw.decode('utf-8', 'replace')[:500]}\n")
        try:
            req = json.loads(raw)
        except json.JSONDecodeError as e:
            self._send_json({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": f"parse error: {e}"}}, 400)
            return

        method = req.get("method", "")
        req_id = req.get("id")
        params = req.get("params", {}) or {}
        task_id = uuid.uuid4().hex
        context_id = params.get("contextId") or params.get("context_id") or uuid.uuid4().hex

        # Accept both v1.0 (message/send) and v0.3 (SendMessage) method names.
        if method in ("message/send", "SendMessage"):
            message = params.get("message", {}) or {}
            text = ""
            for part in message.get("parts", []) or []:
                # v1.0: part["text"]; v0.3: part["text"] without kind
                if "text" in part:
                    text += part.get("text", "")
            reply = f"[mock research result] Received {len(text)} chars. Canned reply. (context {context_id[:8]})"
            task = {
                "id": task_id,
                "contextId": context_id,
                "status": {
                    "state": "completed",
                    "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                    "message": {"kind": "text", "text": reply, "role": "agent"},
                },
                "artifacts": [{"artifactId": uuid.uuid4().hex, "parts": [{"kind": "text", "text": reply, "role": "agent"}]}],
            }
            self._send_json({"jsonrpc": "2.0", "id": req_id, "result": task})
        else:
            self._send_json({
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"method not found: {method}"},
            }, 400)


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[mock-a2a-debug] listening on 127.0.0.1:{PORT}")
    server.serve_forever()
