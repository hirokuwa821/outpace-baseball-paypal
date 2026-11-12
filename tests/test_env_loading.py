import sys
import os
from pathlib import Path
from unittest.mock import patch
from fastapi import HTTPException
from dotenv import dotenv_values

# Add src to sys.path
PROJECT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_DIR / "src"
sys.path.insert(0, str(SRC_DIR))

import paypal_sdk
import paypal_server

def test_env_presence():
    """PayPal専用 .env のみが絶対パスで読み込まれることを確認"""
    env_path = (PROJECT_DIR / ".env").resolve()
    print("Test 1: Check PayPal-specific .env")
    assert env_path.is_file(), "paypal_integration/.env is missing"
    assert paypal_sdk.ENV_FILE_LOADED == env_path

    with patch.object(paypal_sdk, "load_dotenv") as mock_load:
        assert paypal_sdk.load_paypal_env() == env_path
    mock_load.assert_called_once()
    assert Path(mock_load.call_args.kwargs["dotenv_path"]) == env_path
    assert mock_load.call_args.kwargs["override"] is True
    assert env_path != (PROJECT_DIR.parent / ".env").resolve()

    config = dotenv_values(env_path)
    assert config["PAYPAL_ENV"] == "sandbox"
    expected_keys = {
        "PAYPAL_CLIENT_ID",
        "PAYPAL_CLIENT_SECRET",
        "PAYPAL_ENV",
        "PAYPAL_WEBHOOK_ID",
        "PAYPAL_JUDGE_TOKEN",
    }
    assert set(config) >= expected_keys

    example_config = dotenv_values(PROJECT_DIR / ".env.example")
    assert set(example_config) >= expected_keys
    assert example_config["PAYPAL_ENV"] == "sandbox"
    assert all(
        example_config[key] in (None, "")
        for key in expected_keys - {"PAYPAL_ENV"}
    )

    # Even if the parent application's config has populated process variables,
    # the PayPal-specific file remains authoritative.
    with patch.dict(os.environ, {"PAYPAL_ENV": "live", "PAYPAL_CLIENT_ID": "ambient"}):
        paypal_sdk.load_paypal_env()
        assert paypal_sdk.get_paypal_env() == "sandbox"
        assert paypal_sdk.get_paypal_client_id() == (config["PAYPAL_CLIENT_ID"] or None)
    print("  -> PASS")

def test_credentials_check_without_showing_values():
    """2. Client IDとClient Secretの設定有無を、値を表示せずに確認"""
    print("Test 2: Check credentials configured (safe check without revealing values)")
    cid = paypal_sdk.get_paypal_client_id()
    sec = paypal_sdk.get_paypal_client_secret()
    print(f"  PAYPAL_CLIENT_ID configured: {bool(cid)}")
    print(f"  PAYPAL_CLIENT_SECRET configured: {bool(sec)}")
    print(f"  PAYPAL_ENV: {paypal_sdk.get_paypal_env()}")
    print("  -> PASS")

def test_sandbox_url_selected():
    """3. SandboxのAPI URLが選択されること"""
    print("Test 3: Verify Sandbox base URL")
    with patch.dict(os.environ, {"PAYPAL_ENV": "sandbox"}):
        env = paypal_sdk.get_paypal_env()
        base_url = paypal_sdk.get_base_url()
    print(f"  Current ENV: {env}")
    print(f"  Base URL: {base_url}")
    assert env == "sandbox"
    assert base_url == "https://api-m.sandbox.paypal.com"
    print("  -> PASS")

def test_fastapi_uses_shared_paypal_sdk():
    """FastAPIがStreamlitと同じSDK設定・関数を使用すること"""
    assert paypal_server.create_subscription is paypal_sdk.create_subscription
    assert paypal_server.is_valid_judge_token is paypal_sdk.is_valid_judge_token
    assert paypal_sdk.PAYPAL_ENV_FILE == (PROJECT_DIR / ".env").resolve()

def test_oauth_error_message_clarity():
    """4. 認証情報未設定時のエラーメッセージが分かりやすく、秘密情報を漏洩しないこと"""
    print("Test 4: Error handling when credentials missing")
    orig_id = os.environ.get("PAYPAL_CLIENT_ID")
    orig_sec = os.environ.get("PAYPAL_CLIENT_SECRET")
    try:
        os.environ.pop("PAYPAL_CLIENT_ID", None)
        os.environ.pop("PAYPAL_CLIENT_SECRET", None)
        try:
            paypal_sdk._get_access_token()
            assert False, "Should have raised RuntimeError"
        except RuntimeError as e:
            msg = str(e)
            print(f"  Captured expected error: {msg}")
            assert "Missing environment variable(s): PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET" in msg
    finally:
        if orig_id is not None:
            os.environ["PAYPAL_CLIENT_ID"] = orig_id
        if orig_sec is not None:
            os.environ["PAYPAL_CLIENT_SECRET"] = orig_sec
    print("  -> PASS")

def test_fastapi_endpoint_direct():
    """5. FastAPIエンドポイントの直接呼び出しによるエラーハンドリングと動作検証"""
    print("Test 5: FastAPI create_subscription_endpoint direct verification")
    # Verify openapi schema generation
    openapi = paypal_server.app.openapi()
    paths = openapi.get("paths", {})
    assert "/create-subscription" in paths
    print("  OpenAPI schema generated and contains /create-subscription: PASS")

    # Call endpoint with missing credentials without writing to the real subscription store.
    orig_id = os.environ.get("PAYPAL_CLIENT_ID")
    orig_sec = os.environ.get("PAYPAL_CLIENT_SECRET")
    try:
        os.environ.pop("PAYPAL_CLIENT_ID", None)
        os.environ.pop("PAYPAL_CLIENT_SECRET", None)
        req = paypal_server.CreateSubscriptionRequest(
            user_id="test_user_safe",
            return_url="http://localhost:8502/?",
            cancel_url="http://localhost:8502/?",
        )
        with patch.object(paypal_server, "store_pending"):
            try:
                paypal_server.create_subscription_endpoint(req)
                assert False, "Should have raised HTTPException"
            except HTTPException as exc:
                assert exc.status_code == 400
                assert "Missing environment variable(s)" in exc.detail
    finally:
        if orig_id is not None:
            os.environ["PAYPAL_CLIENT_ID"] = orig_id
        if orig_sec is not None:
            os.environ["PAYPAL_CLIENT_SECRET"] = orig_sec
    print("  -> PASS")

if __name__ == "__main__":
    test_env_presence()
    test_credentials_check_without_showing_values()
    test_sandbox_url_selected()
    test_fastapi_uses_shared_paypal_sdk()
    test_oauth_error_message_clarity()
    test_fastapi_endpoint_direct()
    print("\nALL TESTS PASSED SUCCESSFULLY!")
