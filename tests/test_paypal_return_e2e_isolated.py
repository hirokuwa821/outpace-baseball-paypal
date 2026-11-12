"""Mocked purchase-to-return tests isolated from PayPal and the application DB."""

import os
import secrets
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from fastapi.testclient import TestClient


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "src"))

import paypal_return
import paypal_server
import subscription_store


class PayPalReturnEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "paypal-return-e2e.db")
        self.db_path_patch = patch.object(subscription_store, "DB_PATH", self.db_path)
        self.db_path_patch.start()
        subscription_store.init_db()
        self.client = TestClient(paypal_server.app)

    def tearDown(self):
        self.client.close()
        self.db_path_patch.stop()
        self.temp_dir.cleanup()

    def _begin_mocked_purchase(self):
        user_id = secrets.token_urlsafe(12)
        with patch.object(
            paypal_server,
            "create_subscription",
            return_value="https://approval.invalid/mock",
        ) as create_subscription:
            response = self.client.post(
                "/create-subscription",
                json={
                    "user_id": user_id,
                    "return_url": "http://localhost:8502/?lang=ja",
                    "cancel_url": "http://localhost:8502/?cancel=1",
                },
            )
        self.assertTrue(response.status_code == 200)
        body = response.json()
        return_url = create_subscription.call_args.kwargs["return_url"]
        return body, user_id, return_url

    def test_purchase_token_round_trips_into_return_handler(self):
        body, user_id, return_url = self._begin_mocked_purchase()
        return_query = parse_qs(urlparse(return_url).query)
        returned_tokens = return_query.get("token", [])
        self.assertTrue(len(returned_tokens) == 1)
        returned_token = returned_tokens[0]

        # Compare the endpoint result without including either value in failures.
        self.assertTrue(body.get("pending_token") == returned_token)
        self.assertTrue(return_query.get("lang") == ["ja"])

        pre_api_state, pre_api_owner = subscription_store.inspect_pending_token(
            returned_token
        )
        self.assertTrue(pre_api_state == "found")
        self.assertTrue(pre_api_owner == user_id)

        api_observed_found_token = []

        def mock_subscription_lookup(subscription_id):
            state, owner = subscription_store.inspect_pending_token(returned_token)
            api_observed_found_token.append(state == "found" and owner == user_id)
            return {
                "id": subscription_id,
                "custom_id": user_id,
                "status": "APPROVAL_PENDING",
            }

        with patch.object(
            paypal_return, "get_subscription_details", side_effect=mock_subscription_lookup
        ) as get_subscription_details:
            with patch.object(paypal_return, "log_paypal_return_diagnostic"):
                result = paypal_return.handle_paypal_return(
                    "I-TEST-MOCK-SUBSCRIPTION", returned_token
                )

        self.assertTrue(api_observed_found_token == [True])
        get_subscription_details.assert_called_once()
        self.assertTrue(result.verified)
        self.assertFalse(result.active)
        self.assertEqual(result.status, "APPROVAL_PENDING")
        post_return_state, post_return_owner = subscription_store.inspect_pending_token(
            returned_token
        )
        self.assertTrue(post_return_state == "consumed")
        self.assertIsNone(post_return_owner)

    def test_missing_and_mismatched_tokens_are_rejected_before_api_lookup(self):
        body, _, return_url = self._begin_mocked_purchase()
        return_query = parse_qs(urlparse(return_url).query)
        returned_token = return_query["token"][0]
        self.assertTrue(body.get("pending_token") == returned_token)

        with patch.object(
            paypal_return, "get_subscription_details"
        ) as get_subscription_details:
            with patch.object(paypal_return, "log_paypal_return_diagnostic"):
                missing = paypal_return.handle_paypal_return(
                    "I-TEST-MOCK-MISSING", None
                )
                mismatched = paypal_return.handle_paypal_return(
                    "I-TEST-MOCK-MISMATCHED",
                    returned_token + "-changed",
                )

        self.assertFalse(missing.verified)
        self.assertFalse(missing.active)
        self.assertEqual(missing.reason, "missing_return_data")
        self.assertFalse(mismatched.verified)
        self.assertFalse(mismatched.active)
        self.assertEqual(mismatched.reason, "invalid_or_expired_token")
        get_subscription_details.assert_not_called()
        self.assertTrue(
            subscription_store.inspect_pending_token(returned_token)[0] == "found"
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
