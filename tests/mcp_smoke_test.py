"""MCP 冒烟测试：启动 hermes_mcp_server.py，走 initialize → tools/list → call_hermes 全流程"""
import json
import subprocess
import sys
import time

SERVER = sys.executable  # 当前 Python
SCRIPT = os.environ.get("HERMES_MCP_SCRIPT", r"C:\\Path\\To\\hermes_mcp_server.py")


def frame(payload: dict) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode() + body


def read_frame(proc) -> dict:
    # 读 Content-Length 头
    headers = b""
    while b"\r\n\r\n" not in headers:
        ch = proc.stdout.read(1)
        if not ch:
            raise RuntimeError("EOF while reading headers")
        headers += ch
    length = int(headers.split(b":", 1)[1].strip())
    body = proc.stdout.read(length)
    return json.loads(body)


proc = subprocess.Popen(
    [SERVER, SCRIPT],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
)

# 1. initialize
proc.stdin.write(frame({
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2024-11-05", "capabilities": {},
               "clientInfo": {"name": "smoke", "version": "0"}},
}))
proc.stdin.flush()
resp = read_frame(proc)
print(f"[1] initialize -> {resp.get('result', {}).get('serverInfo', 'ERR')}")

# 2. tools/list
proc.stdin.write(frame({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}))
proc.stdin.flush()
resp = read_frame(proc)
tools = resp.get("result", {}).get("tools", [])
print(f"[2] tools/list -> {len(tools)} tool(s): {[t['name'] for t in tools]}")

# 3. call_hermes —— 真实调用 Hermes（9900 入站），发简单任务
proc.stdin.write(frame({
    "jsonrpc": "2.0", "id": 3, "method": "tools/call",
    "params": {"name": "call_hermes",
               "arguments": {"message": "回复四个字：MCP链路通"}},
}))
proc.stdin.flush()
resp = read_frame(proc)
result = resp.get("result", {})
content = result.get("content", [])
text = "".join(c.get("text", "") for c in content if c.get("type") == "text")
print(f"[3] call_hermes -> {text[:200]}")

proc.terminate()
