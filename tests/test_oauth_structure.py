import sys
import os
from pathlib import Path
from unittest.mock import patch, MagicMock
from fastapi import HTTPException

# Add src to sys.path
PROJECT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_DIR / "src"
sys.path.insert(0, str(SRC_DIR))

import paypal_sdk
import paypal_server

def test_partial_missing_secret():
    """Client IDのみ設定され、Secretが欠けている場合のエラーメッセージ検証"""
    print("Test 6: Partial credentials (only ID set)")
    orig_id = os.environ.get("PAYPAL_CLIENT_ID")
    orig_sec = os.environ.get("PAYPAL_CLIENT_SECRET")
    try:
        os.environ["PAYPAL_CLIENT_ID"] = "dummy_client_id"
        os.environ.pop("PAYPAL_CLIENT_SECRET", None)
        try:
            paypal_sdk._get_access_token()
            assert False, "Should have raised RuntimeError"
        except RuntimeError as e:
            msg = str(e)
            print(f"  Captured: {msg}")
            assert "Missing environment variable(s): PAYPAL_CLIENT_SECRET" in msg
            assert "PAYPAL_CLIENT_ID" not in msg.split("Missing environment variable(s): ")[1]
    finally:
        if orig_id is not None:
            os.environ["PAYPAL_CLIENT_ID"] = orig_id
        else:
            os.environ.pop("PAYPAL_CLIENT_ID", None)
        if orig_sec is not None:
            os.environ["PAYPAL_CLIENT_SECRET"] = orig_sec
        else:
            os.environ.pop("PAYPAL_CLIENT_SECRET", None)
    print("  -> PASS")

def test_oauth_request_structure():
    """Client IDとSecretが設定された際、正しいエンドポイント・ヘッダー・データでPOSTされるか（モック検証）"""
    print("Test 7: OAuth request payload structure with mock")
    orig_id = os.environ.get("PAYPAL_CLIENT_ID")
    orig_sec = os.environ.get("PAYPAL_CLIENT_SECRET")
    try:
        os.environ["PAYPAL_CLIENT_ID"] = "mock_client_id_val"
        os.environ["PAYPAL_CLIENT_SECRET"] = "mock_secret_val"
        # Reset token cache
        paypal_sdk._token_cache.clear()

        with patch("requests.post") as mock_post:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {"access_token": "mock_access_token_123"}
            mock_post.return_value = mock_resp

            token = paypal_sdk.get_access_token()
            assert token == "mock_access_token_123"
            mock_post.assert_called_once()
            call_url = mock_post.call_args[0][0]
            call_headers = mock_post.call_args[1]["headers"]
            call_data = mock_post.call_args[1]["data"]

            assert call_url == "https://api-m.sandbox.paypal.com/v1/oauth2/token"
            assert "Basic " in call_headers["Authorization"]
            assert call_data == {"grant_type": "client_credentials"}
            print("  Correct Sandbox OAuth URL called with Basic Auth header: PASS")
    finally:
        paypal_sdk._token_cache.clear()
        if orig_id is not None:
            os.environ["PAYPAL_CLIENT_ID"] = orig_id
        else:
            os.environ.pop("PAYPAL_CLIENT_ID", None)
        if orig_sec is not None:
            os.environ["PAYPAL_CLIENT_SECRET"] = orig_sec
        else:
            os.environ.pop("PAYPAL_CLIENT_SECRET", None)
    print("  -> PASS")

if __name__ == "__main__":
    test_partial_missing_secret()
    test_oauth_request_structure()
    print("\nALL ADDITIONAL TESTS PASSED!")
