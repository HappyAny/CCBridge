from __future__ import annotations

import http.client
import json
import threading
import unittest

import cc_bridge as bridge


class HttpRouteTests(unittest.TestCase):
    def test_route_dispatch(self) -> None:
        class FakeBridge:
            http_token = ""
            state = {"selected_cwd": "D:/x"}

            def http_git_diff(self, cwd=None):
                return {"text": "diff", "diff": "d", "cwd": cwd, "sha": "s"}

            def http_config(self, cwd=None, include_layers=False):
                self.config_args = {"cwd": cwd, "include_layers": include_layers}
                return {"text": "config", "cwd": cwd, "config": {}, "includeLayers": include_layers}

            def http_apps(self, limit=50, cursor=None, force_refetch=False, thread_id=None):
                self.apps_args = {
                    "limit": limit,
                    "cursor": cursor,
                    "force_refetch": force_refetch,
                    "thread_id": thread_id,
                }
                return {"text": "apps", "apps": [], "nextCursor": cursor}

            def http_fork_thread(self, body):
                self.body = body
                return {"text": "fork", "threadId": "t2"}

            def http_auth_accounts(self):
                return {"accounts": [{"index": 1, "account": "a@example.com"}], "text": "accounts"}

            def http_auth_switch(self, body):
                self.auth_body = body
                return {"switched": True, "account": body["account"], "text": "switched"}

            def http_fast_status(self, thread_id=None):
                self.fast_status_thread_id = thread_id
                return {"threadId": thread_id, "fastEnabled": False, "text": "fast"}

            def http_set_fast(self, body):
                self.fast_body = body
                return {"threadId": "t1", "fastEnabled": body["mode"] == "on", "text": "fast"}

        fake = FakeBridge()
        server = bridge.ControlHttpServer(("127.0.0.1", 0), bridge.ControlHttpHandler, fake)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            conn.request("GET", "/diff?cwd=D:/y")
            resp = conn.getresponse()
            payload = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(resp.status, 200)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["cwd"], "D:/y")

            conn.request("GET", "/config?cwd=D:/z&includeLayers=1")
            resp = conn.getresponse()
            payload = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(resp.status, 200)
            self.assertTrue(payload["ok"])
            self.assertEqual(fake.config_args, {"cwd": "D:/z", "include_layers": True})

            conn.request("GET", "/apps?threadId=old-thread&limit=7&forceRefetch=1")
            resp = conn.getresponse()
            payload = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(resp.status, 200)
            self.assertTrue(payload["ok"])
            self.assertEqual(
                fake.apps_args,
                {"limit": 7, "cursor": None, "force_refetch": True, "thread_id": "old-thread"},
            )

            conn.request("POST", "/fork", body=json.dumps({"name": "fork"}), headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            payload = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(resp.status, 200)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["threadId"], "t2")
            self.assertEqual(fake.body, {"name": "fork"})

            conn.request("GET", "/auth/accounts")
            resp = conn.getresponse()
            payload = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(resp.status, 200)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["accounts"][0]["account"], "a@example.com")

            conn.request(
                "POST",
                "/auth/switch",
                body=json.dumps({"account": "a@example.com"}),
                headers={"Content-Type": "application/json"},
            )
            resp = conn.getresponse()
            payload = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(resp.status, 200)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["account"], "a@example.com")
            self.assertEqual(fake.auth_body, {"account": "a@example.com"})

            conn.request("GET", "/fast?threadId=t1")
            resp = conn.getresponse()
            payload = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(resp.status, 200)
            self.assertTrue(payload["ok"])
            self.assertFalse(payload["fastEnabled"])
            self.assertEqual(fake.fast_status_thread_id, "t1")

            conn.request("POST", "/fast", body=json.dumps({"mode": "on"}), headers={"Content-Type": "application/json"})
            resp = conn.getresponse()
            payload = json.loads(resp.read().decode("utf-8"))
            self.assertEqual(resp.status, 200)
            self.assertTrue(payload["ok"])
            self.assertTrue(payload["fastEnabled"])
            self.assertEqual(fake.fast_body, {"mode": "on"})
        finally:
            server.shutdown()
            server.server_close()


if __name__ == "__main__":
    unittest.main()
