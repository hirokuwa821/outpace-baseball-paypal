"""Isolated tests for safe pending-token diagnostic events."""

import hashlib
import io
import json
import logging
import multiprocessing
import os
import sys
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "src"))

import paypal_return
import paypal_return_diagnostics
import paypal_server
import subscription_store


def _write_concurrent_diagnostic_records(log_path, worker_id, start_event):
    handler = paypal_return_diagnostics._ProcessSafeRotatingFileHandler(
        log_path,
        maxBytes=512,
        backupCount=20,
        encoding="utf-8",
        delay=True,
    )
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger = logging.getLogger(f"paypal.test.worker.{worker_id}")
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        start_event.wait(timeout=10)
        for record_id in range(50):
            logger.info(
                json.dumps(
                    {"worker": worker_id, "record": record_id, "pid": os.getpid()}
                )
            )
    finally:
        logger.removeHandler(handler)
        handler.close()


class PendingTokenDiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "diagnostics.db")
        self.log_path = Path(self.temp_dir.name) / "diagnostics.jsonl"
        self.db_patch = patch.object(subscription_store, "DB_PATH", self.db_path)
        self.log_path_patch = patch.object(
            paypal_return_diagnostics, "DIAGNOSTIC_LOG_PATH", self.log_path
        )
        self.handler_patch = patch.object(
            paypal_return_diagnostics, "_FILE_HANDLER", None
        )
        self.db_patch.start()
        self.log_path_patch.start()
        self.handler_patch.start()
        subscription_store.init_db()

    def tearDown(self):
        handler = paypal_return_diagnostics._FILE_HANDLER
        if handler is not None:
            handler.close()
            paypal_return_diagnostics._LOGGER.removeHandler(handler)
        paypal_return_diagnostics._FILE_HANDLER = None
        self.handler_patch.stop()
        self.log_path_patch.stop()
        self.db_patch.stop()
        self.temp_dir.cleanup()

    def _events(self):
        return [
            json.loads(line)
            for line in self.log_path.read_text(encoding="utf-8").splitlines()
        ]

    def test_purchase_store_event_is_written_after_mocked_store_and_paypal(self):
        token = "diagnostic-only-private-token"
        expected_db_fingerprint = "a" * 64
        with patch.object(
            paypal_server.secrets, "token_urlsafe", return_value=token
        ), patch.object(
            paypal_server, "get_database_path_fingerprint",
            return_value=expected_db_fingerprint,
        ), patch.object(
            paypal_server, "create_subscription",
            return_value="https://approval.invalid/mock",
        ) as create_subscription:
            response = paypal_server.create_subscription_endpoint(
                paypal_server.CreateSubscriptionRequest(
                    user_id="private-user-id",
                    return_url="http://localhost:8502/",
                    cancel_url="http://localhost:8502/",
                )
            )

        self.assertIn("approval_url", response)
        state, _ = subscription_store.inspect_pending_token(token)
        self.assertEqual(state, "found")
        create_subscription.assert_called_once()

        event = self._events()[-1]
        self.assertEqual(event["phase"], "purchase_store")
        self.assertIs(event["pending_save_success"], True)
        self.assertEqual(event["token_fingerprint"], hashlib.sha256(token.encode()).hexdigest())
        self.assertEqual(event["database_path_fingerprint"], expected_db_fingerprint)
        self.assertEqual(event["process_role"], "fastapi")
        self.assertIsInstance(event["process_id"], int)
        self.assertEqual(len(event["process_instance_id"]), 32)
        datetime.fromisoformat(event["timestamp_utc"])
        logged_text = json.dumps(event)
        self.assertNotIn(token, logged_text)
        self.assertNotIn("private-user-id", logged_text)
        self.assertNotIn("approval.invalid", logged_text)

    def test_store_failure_is_logged_and_prevents_paypal_sdk_call(self):
        token = "diagnostic-only-failed-token"
        with patch.object(
            paypal_server.secrets, "token_urlsafe", return_value=token
        ), patch.object(
            paypal_server, "get_database_path_fingerprint", return_value="b" * 64
        ), patch.object(
            paypal_server, "store_pending", side_effect=RuntimeError("private failure")
        ), patch.object(paypal_server, "create_subscription") as create_subscription:
            with self.assertRaises(HTTPException) as raised:
                paypal_server.create_subscription_endpoint(
                    paypal_server.CreateSubscriptionRequest(
                        user_id="private-user-id",
                        return_url="http://localhost:8502/",
                        cancel_url="http://localhost:8502/",
                    )
                )

        self.assertEqual(raised.exception.status_code, 400)
        create_subscription.assert_not_called()
        event = self._events()[-1]
        self.assertIs(event["pending_save_success"], False)
        self.assertEqual(event["token_fingerprint"], hashlib.sha256(token.encode()).hexdigest())
        logged_text = json.dumps(event)
        self.assertNotIn(token, logged_text)
        self.assertNotIn("private failure", logged_text)
        self.assertNotIn("private-user-id", logged_text)

    def test_return_missing_token_logs_lookup_state_without_paypal_call(self):
        token = "diagnostic-only-return-token"
        expected_db_fingerprint = "c" * 64
        with patch.object(
            paypal_return, "get_database_path_fingerprint",
            return_value=expected_db_fingerprint,
        ), patch.object(
            paypal_return, "get_subscription_details"
        ) as get_subscription_details:
            result = paypal_return.handle_paypal_return(
                "private-subscription-id", token
            )

        self.assertFalse(result.verified)
        self.assertEqual(result.reason, "invalid_or_expired_token")
        get_subscription_details.assert_not_called()

        event = [
            item
            for item in self._events()
            if item.get("event") == "pending_token_diagnostic"
            and item["phase"] == "return_lookup"
        ][-1]
        self.assertEqual(event["pending_lookup_result"], "not_found")
        self.assertEqual(event["pending_token_state"], "missing")
        self.assertEqual(event["token_fingerprint"], hashlib.sha256(token.encode()).hexdigest())
        self.assertEqual(event["database_path_fingerprint"], expected_db_fingerprint)
        self.assertEqual(event["process_role"], "streamlit")
        self.assertIsInstance(event["process_id"], int)
        datetime.fromisoformat(event["timestamp_utc"])
        logged_text = json.dumps(event)
        self.assertNotIn(token, logged_text)
        self.assertNotIn("private-subscription-id", logged_text)

    def test_return_found_token_is_logged_as_active(self):
        token = "diagnostic-only-active-token"
        subscription_store.store_pending(token, "private-user-id")
        with patch.object(
            paypal_return, "get_database_path_fingerprint", return_value="d" * 64
        ), patch.object(
            paypal_return,
            "get_subscription_details",
            return_value={
                "id": "private-subscription-id",
                "custom_id": "different-private-user",
                "status": "ACTIVE",
            },
        ):
            result = paypal_return.handle_paypal_return(
                "private-subscription-id", token
            )

        self.assertEqual(result.reason, "owner_mismatch")
        event = [
            item
            for item in self._events()
            if item.get("event") == "pending_token_diagnostic"
            and item["phase"] == "return_lookup"
        ][-1]
        self.assertEqual(event["pending_lookup_result"], "found")
        self.assertEqual(event["pending_token_state"], "active")

    def test_return_expired_and_consumed_states_are_logged(self):
        expected_db_fingerprint = "e" * 64
        with patch.object(
            paypal_return, "get_database_path_fingerprint",
            return_value=expected_db_fingerprint,
        ), patch.object(
            paypal_return, "inspect_pending_token",
            side_effect=[("expired", None), ("consumed", None)],
        ), patch.object(
            paypal_return, "get_subscription_details"
        ) as get_subscription_details:
            for token in ("expired-private-token", "consumed-private-token"):
                result = paypal_return.handle_paypal_return(
                    "private-subscription-id", token
                )
                self.assertFalse(result.verified)

        events = [
            item
            for item in self._events()
            if item.get("event") == "pending_token_diagnostic"
            and item["phase"] == "return_lookup"
        ]
        self.assertEqual(
            [item["pending_token_state"] for item in events],
            ["expired", "consumed"],
        )
        self.assertEqual(
            [item["pending_lookup_result"] for item in events],
            ["not_found", "not_found"],
        )
        get_subscription_details.assert_not_called()

    def test_concurrent_process_writes_preserve_complete_jsonl_records(self):
        context = multiprocessing.get_context("spawn")
        start_event = context.Event()
        processes = [
            context.Process(
                target=_write_concurrent_diagnostic_records,
                args=(str(self.log_path), worker_id, start_event),
            )
            for worker_id in range(2)
        ]
        for process in processes:
            process.start()
        start_event.set()
        for process in processes:
            process.join(timeout=20)
            if process.is_alive():
                process.terminate()
                process.join()
                self.fail("Concurrent diagnostic writer did not finish")
            self.assertEqual(process.exitcode, 0)

        log_files = [
            path
            for path in self.log_path.parent.glob(f"{self.log_path.name}*")
            if path == self.log_path
            or path.suffix[1:].isdigit()
        ]
        records = []
        for log_file in log_files:
            records.extend(
                json.loads(line)
                for line in log_file.read_text(encoding="utf-8").splitlines()
            )
        self.assertEqual(len(records), 100)
        self.assertGreater(len(log_files), 1)
        self.assertEqual(len({record["pid"] for record in records}), 2)
        self.assertEqual(
            {(record["worker"], record["record"]) for record in records},
            {(worker_id, record_id) for worker_id in range(2) for record_id in range(50)},
        )

    def test_logging_failure_does_not_escape_diagnostic_helper(self):
        with patch.object(
            paypal_return_diagnostics._LOGGER,
            "info",
            side_effect=RuntimeError("private logging failure"),
        ), patch("paypal_return_diagnostics.sys.stderr") as stderr:
            paypal_return_diagnostics._log_safely('{"safe":true}')

        stderr.write.assert_called_once_with("PayPal diagnostic logging failed.\n")
        self.assertNotIn(
            "private logging failure",
            stderr.write.call_args.args[0],
        )

    def test_handler_initialization_failure_does_not_stop_return(self):
        token = "diagnostic-only-init-failure-token"
        subscription_store.store_pending(token, "private-user-id")
        unsafe_error = "private initialization failure"
        with patch.object(paypal_return_diagnostics, "_FILE_HANDLER", None), patch.object(
            paypal_return_diagnostics,
            "DIAGNOSTIC_LOG_PATH",
            self.log_path.parent / "missing" / "diagnostics.jsonl",
        ), patch.object(
            paypal_return_diagnostics.Path,
            "mkdir",
            side_effect=RuntimeError(unsafe_error),
        ), patch.object(
            paypal_return,
            "get_subscription_details",
            return_value={
                "id": "private-subscription-id",
                "custom_id": "private-user-id",
                "status": "ACTIVE",
            },
        ), patch(
            "paypal_return_diagnostics.sys.stderr",
            new_callable=io.StringIO,
        ) as stderr:
            result = paypal_return.handle_paypal_return(
                "private-subscription-id", token
            )

        self.assertTrue(result.verified)
        self.assertTrue(result.active)
        self.assertEqual(
            subscription_store.get_by_subscription("private-subscription-id")[
                "status"
            ],
            "ACTIVE",
        )
        self.assertIn("PayPal diagnostic logging failed.", stderr.getvalue())
        self.assertNotIn(unsafe_error, stderr.getvalue())
        self.assertNotIn("private-user-id", stderr.getvalue())
        self.assertFalse(self.log_path.parent.joinpath("missing").exists())

    def test_concurrent_initialization_registers_one_handler_and_logs_once(self):
        thread_count = 16
        ready = threading.Barrier(thread_count)
        handler_created = threading.Event()
        release_handler = threading.Event()
        creation_count = 0
        creation_count_lock = threading.Lock()
        original_handler = (
            paypal_return_diagnostics._ProcessSafeRotatingFileHandler
        )

        def create_handler(*args, **kwargs):
            nonlocal creation_count
            with creation_count_lock:
                creation_count += 1
            handler_created.set()
            if not release_handler.wait(timeout=5):
                raise TimeoutError("test handler initialization timed out")
            return original_handler(*args, **kwargs)

        def log_from_thread(index):
            ready.wait(timeout=5)
            paypal_return_diagnostics.log_pending_token_diagnostic(
                phase="return_lookup",
                token=f"thread-only-token-{index}",
                database_path_fingerprint="f" * 64,
                process_role="streamlit",
                process_instance_id="a" * 32,
                lookup_result="not_found",
                token_state="missing",
            )

        with patch.object(
            paypal_return_diagnostics,
            "_ProcessSafeRotatingFileHandler",
            side_effect=create_handler,
        ):
            with ThreadPoolExecutor(max_workers=thread_count) as executor:
                futures = [
                    executor.submit(log_from_thread, index)
                    for index in range(thread_count)
                ]
                self.assertTrue(handler_created.wait(timeout=5))
                time.sleep(0.05)
                release_handler.set()
                for future in futures:
                    future.result(timeout=10)

        self.assertEqual(creation_count, 1)
        registered_handlers = [
            handler
            for handler in paypal_return_diagnostics._LOGGER.handlers
            if isinstance(
                handler,
                paypal_return_diagnostics._ProcessSafeRotatingFileHandler,
            )
        ]
        self.assertEqual(len(registered_handlers), 1)

        events = self._events()
        fingerprints = [event["token_fingerprint"] for event in events]
        self.assertEqual(len(events), thread_count)
        self.assertEqual(len(set(fingerprints)), thread_count)


if __name__ == "__main__":
    unittest.main(verbosity=2)
