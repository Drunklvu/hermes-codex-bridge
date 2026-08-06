"""MCP profile 路由测试：default + web-dev 两个 profile 都验证"""
import json
import subprocess
import sys

SERVER = sys.executable
SCRIPT = os.environ.get("HERMES_MCP_SCRIPT", r"C:\\Path\\To\\hermes_mcp_server.py")


def frame(payload: dict) -> bytes:
    body = json.dumps(payload).encode("utf-8")
    return f"Content-Length: {len(body)}\r\n\r\n".encode() + body


def read_frame(proc) -> dict:
    headers = b""
    while b"\r\n\r\n" not in headers:
        ch = proc.stdout.read(1)
        if not ch:
            raise RuntimeError("EOF")
        headers += ch
    length = int(headers.split(b":", 1)[1].strip())
    return json.loads(proc.stdout.read(length))


proc = subprocess.Popen(
    [SERVER, SCRIPT], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE
)

# initialize
proc.stdin.write(frame({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                        "params": {"protocolVersion": "2024-11-05", "capabilities": {},
                                   "clientInfo": {"name": "smoke", "version": "0"}}}))
proc.stdin.flush()
print(f"[1] initialize -> {read_frame(proc)['result']['serverInfo']}")

# tools/list 检查 schema 里有 profile
proc.stdin.write(frame({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}))
proc.stdin.flush()
tool = read_frame(proc)["result"]["tools"][0]
props = tool["inputSchema"]["properties"]
print(f"[2] tools/list -> schema props: {list(props.keys())}")

# 3a. 默认 profile（default/9900）
proc.stdin.write(frame({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                        "params": {"name": "call_hermes",
                                   "arguments": {"message": "回复三个字：默认通"}}}))
proc.stdin.flush()
resp = read_frame(proc)
text = "".join(c.get("text", "") for c in resp["result"].get("content", []) if c.get("type") == "text")
print(f"[3] profile=default -> {text[:100]}")

# 3b. web-dev profile（9901）
proc.stdin.write(frame({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                        "params": {"name": "call_hermes",
                                   "arguments": {"message": "回复五个字：网页通",
                                                 "profile": "web-dev"}}}))
proc.stdin.flush()
resp = read_frame(proc)
text = "".join(c.get("text", "") for c in resp["result"].get("content", []) if c.get("type") == "text")
print(f"[4] profile=web-dev -> {text[:100]}")

# 3c. 非法 profile
proc.stdin.write(frame({"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                        "params": {"name": "call_hermes",
                                   "arguments": {"message": "x", "profile": "bogus"}}}))
proc.stdin.flush()
resp = read_frame(proc)
err = resp["result"].get("isError", False)
print(f"[5] profile=bogus -> isError={err}")

proc.terminate()
