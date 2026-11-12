"""Support for rehearsing the Streamlit purchase screen without real data or PayPal.

* ``build_synthetic_state``: a temp state folder with a fresh DB, a synthetic cache, and a
  valid completion marker. It never reads the real DB, cache, or logs.
* ``StubFastAPI``: a localhost-only stand-in for the FastAPI endpoints the purchase path
  calls. It answers the state check using a fingerprint it was given, counts purchase
  attempts, and returns a fake approval URL that is deliberately not a PayPal host. It
  never forwards anything to PayPal.

The stub is the purchase API mock: when verification passes, the request reaches the stub
and is counted; when it does not, the stub is never called.
"""
from __future__ import annotations

import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import paypal_state
import subscription_store

FAKE_APPROVAL_URL = "http://127.0.0.1:9/fake-approval"


def build_synthetic_state(root: Path) -> Path:
    """Create a complete, synthetic state folder under ``root``. Returns the state directory."""
    state = Path(root) / "state"
    state.mkdir(parents=True)
    with patch.dict(os.environ, {paypal_state.STATE_DIR_ENV_VAR: str(state)}), \
         patch.object(paypal_state, "LEGACY_DB_PATH", Path(root) / "absent.db"), \
         patch.object(paypal_state, "LEGACY_CACHE_PATH", Path(root) / "absent.json"), \
         patch.object(subscription_store, "DB_PATH", str(paypal_state.db_path())):
        subscription_store.init_db()
    (state / "paypal_resource_ids.json").write_text(
        json.dumps({"sandbox": {"product_id": "PROD-SYNTH", "plan_id": "PLAN-SYNTH"}}), encoding="utf-8"
    )
    paypal_state.write_migration_marker(state, {"database": {"verified_tables": {}}, "cache": {"entries": 1}})
    return state


class StubFastAPI:
    """Localhost stand-in for the FastAPI purchase endpoints. Never contacts PayPal."""

    def __init__(self, expected_fingerprint: str, port: int = 0):
        self.expected_fingerprint = expected_fingerprint
        self.purchase_calls = 0
        self._lock = threading.Lock()
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # keep test output quiet
                pass

            def _send(self, status: int, body: dict) -> None:
                data = json.dumps(body).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path == "/openapi.json":
                    self._send(200, {"paths": {
                        "/create-subscription": {"post": {}},
                        "/diagnostics/database-path-match": {"post": {}},
                    }})
                else:
                    self._send(404, {"detail": "not found"})

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0") or 0)
                raw = self.rfile.read(length) if length else b"{}"
                try:
                    body = json.loads(raw.decode("utf-8") or "{}")
                except ValueError:
                    body = {}
                if self.path == "/diagnostics/database-path-match":
                    matched = isinstance(body, dict) and body.get("fingerprint") == stub.expected_fingerprint
                    self._send(200, {"database_path_match": matched})
                elif self.path == "/create-subscription":
                    with stub._lock:
                        stub.purchase_calls += 1
                    self._send(200, {"approval_url": FAKE_APPROVAL_URL, "pending_token": "stub-token"})
                else:
                    self._send(404, {"detail": "not found"})

        self._server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
        self._thread = None

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> "StubFastAPI":
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(5)
