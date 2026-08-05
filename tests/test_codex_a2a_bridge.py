import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from urllib.request import Request, urlopen


MODULE_PATH = Path(__file__).resolve().parent.parent / "codex_a2a_bridge.py"
SPEC = importlib.util.spec_from_file_location("codex_a2a_bridge", MODULE_PATH)
bridge_module = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(bridge_module)


class FakeBridge:
    sync_wait = 2

    def __init__(self, state_dir: Path):
        self.store = bridge_module.TaskStore(state_dir / "tasks.json")

    def card(self, base_url):
        return bridge_module.CodexBridge.card(self, base_url)

    def start_task(self, prompt, context_id):
        task_id = "task-test"
        task = {
            "id": task_id,
            "contextId": context_id,
            "status": {
                "state": "TASK_STATE_COMPLETED",
                "timestamp": bridge_module.utc_timestamp(),
                "message": bridge_module.agent_message(f"echo:{prompt}", context_id),
            },
            "artifacts": [{"artifactId": "artifact-test", "parts": [bridge_module.text_part(f"echo:{prompt}")]}],
        }
        self.store.add(task).set()
        return task_id


class BridgeProtocolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        fake = FakeBridge(Path(self.temp.name))
        self.server = bridge_module.BridgeServer(("127.0.0.1", 0), fake)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def rpc(self, method, params):
        body = json.dumps({"jsonrpc": "2.0", "id": "test", "method": method, "params": params}).encode()
        req = Request(self.url, data=body, headers={"Content-Type": "application/json"})
        with urlopen(req, timeout=5) as response:
            return json.load(response)

    def test_agent_card(self):
        with urlopen(self.url + "/.well-known/agent-card.json", timeout=5) as response:
            card = json.load(response)
        self.assertEqual(card["name"], "Codex CLI")
        self.assertEqual(card["supportedInterfaces"][0]["protocolVersion"], "1.0")
        self.assertEqual(card["skills"][0]["name"], "coding")

    def test_pascal_case_send_message(self):
        result = self.rpc("SendMessage", {"message": {"role": "ROLE_USER", "parts": [{"text": "hello"}]}})
        self.assertEqual(result["result"]["status"]["state"], "TASK_STATE_COMPLETED")
        self.assertEqual(result["result"]["artifacts"][0]["parts"][0]["text"], "echo:hello")

    def test_legacy_send_and_get(self):
        sent = self.rpc("message/send", {"message": {"role": "user", "parts": [{"text": "legacy"}]}})
        task_id = sent["result"]["id"]
        fetched = self.rpc("tasks/get", {"id": task_id})
        self.assertEqual(fetched["result"]["artifacts"][0]["parts"][0]["text"], "echo:legacy")


if __name__ == "__main__":
    unittest.main()
