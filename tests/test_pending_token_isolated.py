"""Isolated pending-token lifecycle tests using temporary SQLite databases."""

import datetime
import os
import secrets
import sqlite3
import sys
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "src"))

import paypal_server
import subscription_store


class PendingTokenIsolatedTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "isolated.db")
        self.db_path_patch = patch.object(subscription_store, "DB_PATH", self.db_path)
        self.db_path_patch.start()
        subscription_store.init_db()

    def tearDown(self):
        self.db_path_patch.stop()
        self.temp_dir.cleanup()

    def test_store_is_visible_from_an_independent_connection(self):
        token = secrets.token_urlsafe(24)
        owner = secrets.token_urlsafe(12)

        subscription_store.store_pending(token, owner)

        with closing(sqlite3.connect(self.db_path)) as connection:
            row = connection.execute(
                "SELECT 1 FROM pending WHERE token = ?", (token,)
            ).fetchone()
        self.assertIsNotNone(row)

        state, found_owner = subscription_store.inspect_pending_token(token)
        self.assertEqual(state, "found")
        self.assertTrue(found_owner == owner)

        changed_token = token + "-changed"
        changed_state, changed_owner = subscription_store.inspect_pending_token(
            changed_token
        )
        self.assertEqual(changed_state, "missing")
        self.assertIsNone(changed_owner)

    def test_expired_token_is_classified_and_removed(self):
        token = secrets.token_urlsafe(24)
        created_at = (
            datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
            - datetime.timedelta(hours=25)
        ).isoformat()
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute(
                "INSERT INTO pending (token, user_id, created_at) VALUES (?, ?, ?)",
                (token, secrets.token_urlsafe(12), created_at),
            )
            connection.commit()

        state, owner = subscription_store.inspect_pending_token(token)
        self.assertEqual(state, "expired")
        self.assertIsNone(owner)
        with closing(sqlite3.connect(self.db_path)) as connection:
            row = connection.execute(
                "SELECT 1 FROM pending WHERE token = ?", (token,)
            ).fetchone()
        self.assertIsNone(row)

    def test_consumed_token_is_classified_and_cannot_be_consumed_twice(self):
        token = secrets.token_urlsafe(24)
        owner = secrets.token_urlsafe(12)
        subscription_store.store_pending(token, owner)

        consumed_owner = subscription_store.consume_pending(token)
        self.assertTrue(consumed_owner == owner)
        self.assertIsNone(subscription_store.consume_pending(token))

        state, found_owner = subscription_store.inspect_pending_token(token)
        self.assertEqual(state, "consumed")
        self.assertIsNone(found_owner)

    def test_repeated_operations_preserve_other_pending_rows(self):
        first_token = secrets.token_urlsafe(24)
        second_token = secrets.token_urlsafe(24)
        first_owner = secrets.token_urlsafe(12)
        second_owner = secrets.token_urlsafe(12)
        subscription_store.store_pending(first_token, first_owner)
        subscription_store.store_pending(second_token, second_owner)

        for _ in range(3):
            first_state, current_first_owner = (
                subscription_store.inspect_pending_token(first_token)
            )
            second_state, current_second_owner = (
                subscription_store.inspect_pending_token(second_token)
            )
            self.assertEqual(first_state, "found")
            self.assertEqual(second_state, "found")
            self.assertTrue(current_first_owner == first_owner)
            self.assertTrue(current_second_owner == second_owner)

        self.assertTrue(subscription_store.consume_pending(first_token) == first_owner)
        self.assertEqual(
            subscription_store.inspect_pending_token(first_token)[0], "consumed"
        )
        self.assertEqual(
            subscription_store.inspect_pending_token(second_token)[0], "found"
        )
        self.assertTrue(subscription_store.consume_pending(second_token) == second_owner)

        with closing(sqlite3.connect(self.db_path)) as connection:
            pending_count = connection.execute(
                "SELECT COUNT(*) FROM pending"
            ).fetchone()[0]
            consumed_count = connection.execute(
                "SELECT COUNT(*) FROM pending_token_history WHERE state = 'consumed'"
            ).fetchone()[0]
        self.assertEqual(pending_count, 0)
        self.assertEqual(consumed_count, 2)

    def test_pending_commit_precedes_mocked_paypal_creation(self):
        events = []

        def save_pending(token, owner):
            result = original_store_pending(token, owner)
            events.append("pending_saved")
            return result

        def mock_paypal_creation(*, user_id, return_url, cancel_url):
            self.assertEqual(events, ["pending_saved"])
            token = parse_qs(urlparse(return_url).query)["token"][0]
            with closing(sqlite3.connect(self.db_path)) as connection:
                row = connection.execute(
                    "SELECT 1 FROM pending WHERE token = ?", (token,)
                ).fetchone()
            self.assertIsNotNone(row)
            events.append("paypal_creation_mocked")
            return "https://approval.invalid/mock"

        original_store_pending = subscription_store.store_pending
        request = paypal_server.CreateSubscriptionRequest(
            user_id=secrets.token_urlsafe(12),
            return_url="http://localhost:8502/?lang=ja",
            cancel_url="http://localhost:8502/?cancel=1",
        )
        with patch.object(paypal_server, "store_pending", side_effect=save_pending):
            with patch.object(
                paypal_server,
                "create_subscription",
                side_effect=mock_paypal_creation,
            ):
                response = paypal_server.create_subscription_endpoint(request)

        self.assertEqual(
            events, ["pending_saved", "paypal_creation_mocked"]
        )
        self.assertIn("approval_url", response)


if __name__ == "__main__":
    unittest.main(verbosity=2)
