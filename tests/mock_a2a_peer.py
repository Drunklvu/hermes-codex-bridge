"""Mock A2A v1.0 server — simulates a peer agent for testing Hermes A2A client tools.

Implements just enough of the protocol:
  GET  /.well-known/agent-card.json  → Agent Card
  POST /                              → JSON-RPC message/send (returns completed Task)

Usage: python mock_a2a_peer.py [port]   (default 9999)
"""
import json
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9999

CARD = {
    "name": "Mock Researcher",
    "description": "A mock research agent for A2A protocol testing. Answers with canned replies.",
    "url": f"http://127.0.0.1:{PORT}",
    "version": "1.0.0",
    "provider": {"organization": "Mock Labs", "url": f"http://127.0.0.1:{PORT}"},
    "supportedInterfaces": [
        {"url": f"http://127.0.0.1:{PORT}", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
    ],
    "capabilities": {
        "streaming": False,
        "pushNotifications": False,
        "stateTransitionHistory": False,
        "extendedAgentCard": False,
    },
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [
        {"name": "web_search", "description": "Search the web for information"},
        {"name": "research", "description": "Research a topic and summarize findings"},
    ],
}


def text_part(text: str, role: str = "agent") -> dict:
    return {
        "kind": "text",
        "text": text,
        "metadata": {},
        "role": role,
    }


def build_task(task_id: str, context_id: str, state: str, text: str) -> dict:
    task = {
        "id": task_id,
        "contextId": context_id,
        "status": {
            "state": state,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        },
    }
    if text:
        task["status"]["message"] = text_part(text)
        if state == "completed":
            task["artifacts"] = [{
                "artifactId": uuid.uuid4().hex,
                "parts": [text_part(text)],
            }]
    return task


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
        if self.path in ("/.well-known/agent-card.json", "/.well-known/agent.json"):
            self._send_json(CARD)
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            req = json.loads(raw)
        except json.JSONDecodeError:
            self._send_json({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}, 400)
            return

        method = req.get("method", "")
        req_id = req.get("id")
        params = req.get("params", {}) or {}
        task_id = uuid.uuid4().hex
        context_id = params.get("contextId") or params.get("context_id") or uuid.uuid4().hex

        if method == "message/send":
            # Extract the inbound message text
            message = params.get("message", {}) or {}
            text = ""
            for part in message.get("parts", []) or []:
                if part.get("kind") == "text" or "text" in part:
                    text += part.get("text", "")
            reply = (
                f"[mock research result] Received {len(text)} chars of task input. "
                f"Topic summary: this is a canned reply from the mock A2A peer. "
                f"(context {context_id[:8]})"
            )
            result = build_task(task_id, context_id, "completed", reply)
            self._send_json({"jsonrpc": "2.0", "id": req_id, "result": result})
        else:
            self._send_json({
                "jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"method not found: {method}"},
            }, 400)


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"[mock-a2a] listening on 127.0.0.1:{PORT} — Agent Card at /.well-known/agent-card.json")
    server.serve_forever()
