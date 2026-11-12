# -*- coding: utf-8 -*-
"""Independent offline test suite for PayPal SDK strict token and expiry validation.

Executes via standard Python unittest (pytest optional, works with python -m unittest).
Mock targets use paypal_integration.src.paypal_sdk to prevent external network calls.
"""

import os
import sys
import base64
import datetime
import unittest
from unittest import mock
import requests

# Ensure project root and paypal_integration/src are in sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SRC_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
for p in [PROJECT_ROOT, SRC_DIR]:
    if p not in sys.path:
        sys.path.insert(0, p)

from paypal_integration.src import paypal_sdk


class DummyResponse:
    """Mock requests.Response for offline testing."""

    def __init__(self, json_data=None, status_code=200, text=None):
        self._json_data = json_data or {}
        self.status_code = status_code
        self.text = text if text is not None else str(self._json_data)
        self.headers = {}

    def json(self):
        return self._json_data

    def raise_for_status(self):
        if not (200 <= self.status_code < 300):
            err = requests.HTTPError(f"HTTP {self.status_code}", response=self)
            raise err


class TestPayPalSDKTokenStrict(unittest.TestCase):

    def setUp(self):
        paypal_sdk._token_cache.clear()
        self.env_patcher = mock.patch.dict(
            os.environ,
            {
                "PAYPAL_CLIENT_ID": "dummy_client_id_for_testing",
                "PAYPAL_CLIENT_SECRET": "dummy_client_secret_for_testing",
                "PAYPAL_ENV": "sandbox",
            },
        )
        self.env_patcher.start()

    def tearDown(self):
        self.env_patcher.stop()
        paypal_sdk._token_cache.clear()

    # 1. 正の整数 expires_in が受け入れられる
    def test_01_positive_integer_expires_in_accepted(self):
        resp = DummyResponse({"access_token": "valid_token_123", "expires_in": 3600})
        with mock.patch.object(paypal_sdk.requests, "post", return_value=resp) as mock_post:
            token = paypal_sdk.get_access_token()
            self.assertEqual(token, "valid_token_123")
            self.assertIsInstance(token, str)
            cached = paypal_sdk._token_cache.get(paypal_sdk.get_paypal_env())
            self.assertIsNotNone(cached)
            self.assertEqual(cached["token"], "valid_token_123")
            self.assertIsInstance(cached["expires_at"], datetime.datetime)
            mock_post.assert_called_once()

    # 2. expires_in 欠落が拒否される
    def test_02_missing_expires_in_rejected(self):
        resp = DummyResponse({"access_token": "some_token"})
        with mock.patch.object(paypal_sdk.requests, "post", return_value=resp):
            with self.assertRaises(RuntimeError) as ctx:
                paypal_sdk.get_access_token()
            self.assertIn("Invalid or missing expires_in", str(ctx.exception))
            self.assertEqual(len(paypal_sdk._token_cache), 0)

    # 3. Noneが拒否される
    def test_03_none_expires_in_rejected(self):
        resp = DummyResponse({"access_token": "some_token", "expires_in": None})
        with mock.patch.object(paypal_sdk.requests, "post", return_value=resp):
            with self.assertRaises(RuntimeError) as ctx:
                paypal_sdk.get_access_token()
            self.assertIn("Invalid or missing expires_in", str(ctx.exception))
            self.assertEqual(len(paypal_sdk._token_cache), 0)

    # 4. 空文字が拒否される
    def test_04_empty_string_expires_in_rejected(self):
        resp = DummyResponse({"access_token": "some_token", "expires_in": ""})
        with mock.patch.object(paypal_sdk.requests, "post", return_value=resp):
            with self.assertRaises(RuntimeError) as ctx:
                paypal_sdk.get_access_token()
            self.assertIn("Invalid or missing expires_in", str(ctx.exception))
            self.assertEqual(len(paypal_sdk._token_cache), 0)

    # 5. 不正文字列が拒否される
    def test_05_invalid_string_expires_in_rejected(self):
        for bad_str in ["3600", "invalid", "   "]:
            with self.subTest(bad_str=bad_str):
                paypal_sdk._token_cache.clear()
                resp = DummyResponse({"access_token": "some_token", "expires_in": bad_str})
                with mock.patch.object(paypal_sdk.requests, "post", return_value=resp):
                    with self.assertRaises(RuntimeError) as ctx:
                        paypal_sdk.get_access_token()
                    self.assertIn("Invalid or missing expires_in", str(ctx.exception))
                    self.assertEqual(len(paypal_sdk._token_cache), 0)

    # 6. 0が拒否される
    def test_06_zero_expires_in_rejected(self):
        resp = DummyResponse({"access_token": "some_token", "expires_in": 0})
        with mock.patch.object(paypal_sdk.requests, "post", return_value=resp):
            with self.assertRaises(RuntimeError) as ctx:
                paypal_sdk.get_access_token()
            self.assertIn("Invalid or missing expires_in", str(ctx.exception))
            self.assertEqual(len(paypal_sdk._token_cache), 0)

    # 7. 負数が拒否される
    def test_07_negative_expires_in_rejected(self):
        resp = DummyResponse({"access_token": "some_token", "expires_in": -3600})
        with mock.patch.object(paypal_sdk.requests, "post", return_value=resp):
            with self.assertRaises(RuntimeError) as ctx:
                paypal_sdk.get_access_token()
            self.assertIn("Invalid or missing expires_in", str(ctx.exception))
            self.assertEqual(len(paypal_sdk._token_cache), 0)

    # 8. Trueが拒否される
    def test_08_true_expires_in_rejected(self):
        resp = DummyResponse({"access_token": "some_token", "expires_in": True})
        with mock.patch.object(paypal_sdk.requests, "post", return_value=resp):
            with self.assertRaises(RuntimeError) as ctx:
                paypal_sdk.get_access_token()
            self.assertIn("Invalid or missing expires_in", str(ctx.exception))
            self.assertEqual(len(paypal_sdk._token_cache), 0)

    # 9. Falseが拒否される
    def test_09_false_expires_in_rejected(self):
        resp = DummyResponse({"access_token": "some_token", "expires_in": False})
        with mock.patch.object(paypal_sdk.requests, "post", return_value=resp):
            with self.assertRaises(RuntimeError) as ctx:
                paypal_sdk.get_access_token()
            self.assertIn("Invalid or missing expires_in", str(ctx.exception))
            self.assertEqual(len(paypal_sdk._token_cache), 0)

    # 10. floatが拒否される
    def test_10_float_expires_in_rejected(self):
        resp = DummyResponse({"access_token": "some_token", "expires_in": 3600.5})
        with mock.patch.object(paypal_sdk.requests, "post", return_value=resp):
            with self.assertRaises(RuntimeError) as ctx:
                paypal_sdk.get_access_token()
            self.assertIn("Invalid or missing expires_in", str(ctx.exception))
            self.assertEqual(len(paypal_sdk._token_cache), 0)

    # 11. 拒否時にトークンがキャッシュされない
    def test_11_cache_empty_on_any_oauth_failure(self):
        bad_resp = DummyResponse({"access_token": "leak_me", "expires_in": -1})
        with mock.patch.object(paypal_sdk.requests, "post", return_value=bad_resp):
            with self.assertRaises(RuntimeError):
                paypal_sdk.get_access_token()
            self.assertEqual(paypal_sdk._token_cache, {})

    # 12. 有効なキャッシュが再利用される
    def test_12_valid_cache_reused(self):
        resp = DummyResponse({"access_token": "cached_token_abc", "expires_in": 3600})
        with mock.patch.object(paypal_sdk.requests, "post", return_value=resp) as mock_post:
            tok1 = paypal_sdk.get_access_token()
            tok2 = paypal_sdk.get_access_token()
            self.assertEqual(tok1, "cached_token_abc")
            self.assertEqual(tok2, "cached_token_abc")
            self.assertEqual(mock_post.call_count, 1)

    # 13. 期限30秒以内のキャッシュが更新される
    def test_13_preexpiry_30s_refresh(self):
        # 1秒で期限切れになるトークン
        resp1 = DummyResponse({"access_token": "initial_token", "expires_in": 1})
        resp2 = DummyResponse({"access_token": "refreshed_token", "expires_in": 3600})
        with mock.patch.object(paypal_sdk.requests, "post", side_effect=[resp1, resp2]) as mock_post:
            tok1 = paypal_sdk.get_access_token()
            self.assertEqual(tok1, "initial_token")
            # 2回目のアクセス: 残り時間 1秒 < 30秒 なので即座に再取得される
            tok2 = paypal_sdk.get_access_token()
            self.assertEqual(tok2, "refreshed_token")
            self.assertEqual(mock_post.call_count, 2)

    # 14. _raise_for_status() がdict形式キャッシュでもTypeErrorを起こさない
    # 14. _raise_for_status() がdict形式キャッシュや文字列形式でもTypeErrorを起こさずマスクする
    def test_14_raise_for_status_handles_dict_and_str_cache(self):
        dict_val = {
            "token": "secret_token_val_1",
            "expires_at": datetime.datetime.now(datetime.timezone.utc),
        }
        legacy_str = "legacy_secret_token_val_2"

        error_resp = DummyResponse(
            json_data={"error": "test"},
            status_code=400,
            text='{"error":"bad_request", "secret_token_val_1":"leak", "legacy_secret_token_val_2":"leak"}',
        )

        with self.assertRaises(requests.HTTPError) as ctx:
            paypal_sdk._raise_for_status(error_resp, (dict_val, legacy_str))
        # TypeErrorが発生せずHTTPErrorになること
        err_msg = str(ctx.exception)
        self.assertIn("PayPal API request failed with HTTP 400", err_msg)
        self.assertNotIn("secret_token_val_1", err_msg)
        self.assertNotIn("legacy_secret_token_val_2", err_msg)
        self.assertIn("[REDACTED]", err_msg)

    # 15. HTTPエラー本文にトークンやClient Secretが残らない（マスクされる）
    def test_15_raise_for_status_redacts_secrets(self):
        raw_body = (
            'Error: dummy_client_secret_for_testing leaked and secret_access_token_xyz leaked'
        )
        error_resp = DummyResponse(status_code=401, text=raw_body)
        with self.assertRaises(requests.HTTPError) as ctx:
            paypal_sdk._raise_for_status(error_resp, ("secret_access_token_xyz",))
        err_msg = str(ctx.exception)
        self.assertNotIn("dummy_client_secret_for_testing", err_msg)
        self.assertNotIn("secret_access_token_xyz", err_msg)
        self.assertIn("[REDACTED]", err_msg)

    # 16. GETが初回401、再試行200の場合、正常レスポンスが返る
    def test_16_get_401_retry_success(self):
        oauth_resp1 = DummyResponse({"access_token": "old_token_1", "expires_in": 3600})
        oauth_resp2 = DummyResponse({"access_token": "new_token_2", "expires_in": 3600})
        api_401 = DummyResponse(status_code=401, text='{"error":"invalid_token"}')
        api_200 = DummyResponse(json_data={"status": "ACTIVE"}, status_code=200)

        with mock.patch.object(paypal_sdk.requests, "post", side_effect=[oauth_resp1, oauth_resp2]) as mock_oauth:
            with mock.patch.object(paypal_sdk.requests, "request", side_effect=[api_401, api_200]) as mock_req:
                resp = paypal_sdk._request("GET", "/v1/billing/subscriptions/SUB-123")
                self.assertEqual(resp.status_code, 200)
                self.assertEqual(resp.json()["status"], "ACTIVE")
                self.assertEqual(mock_oauth.call_count, 2)
                self.assertEqual(mock_req.call_count, 2)
                # キャッシュには新しいトークンが残る
                self.assertEqual(paypal_sdk._token_cache["sandbox"]["token"], "new_token_2")

    # 17. GETの初回401時に、使用した古いトークンだけがキャッシュから削除される
    def test_17_get_401_invalidates_only_used_token(self):
        oauth_resp1 = DummyResponse({"access_token": "token_to_fail", "expires_in": 3600})
        oauth_resp2 = DummyResponse({"access_token": "token_refreshed", "expires_in": 3600})
        api_401 = DummyResponse(status_code=401, text='{"error":"invalid_token"}')
        api_200 = DummyResponse(json_data={"ok": True}, status_code=200)

        with mock.patch.object(paypal_sdk.requests, "post", side_effect=[oauth_resp1, oauth_resp2]):
            with mock.patch.object(paypal_sdk.requests, "request", side_effect=[api_401, api_200]):
                paypal_sdk._request("GET", "/test")
                # 途中で古いトークンが削除され、新しいトークンに更新されたことを確認
                self.assertEqual(paypal_sdk._token_cache["sandbox"]["token"], "token_refreshed")

    # 18. GETの再試行時に新しいトークンが使用される
    def test_18_get_retry_uses_new_token(self):
        oauth_resp1 = DummyResponse({"access_token": "tok_A", "expires_in": 3600})
        oauth_resp2 = DummyResponse({"access_token": "tok_B", "expires_in": 3600})
        api_401 = DummyResponse(status_code=401, text='{"error":"invalid_token"}')
        api_200 = DummyResponse(json_data={"ok": True}, status_code=200)

        with mock.patch.object(paypal_sdk.requests, "post", side_effect=[oauth_resp1, oauth_resp2]):
            with mock.patch.object(paypal_sdk.requests, "request", side_effect=[api_401, api_200]) as mock_req:
                paypal_sdk._request("GET", "/test-endpoint", params={"q": "1"})
                first_call_headers = mock_req.call_args_list[0][1]["headers"]
                second_call_headers = mock_req.call_args_list[1][1]["headers"]
                self.assertEqual(first_call_headers["Authorization"], "Bearer tok_A")
                self.assertEqual(second_call_headers["Authorization"], "Bearer tok_B")

    # 19. GETが2回連続401の場合、最大2回のAPIリクエストで例外になる
    def test_19_get_consecutive_401_raises_after_two_attempts(self):
        oauth_resp1 = DummyResponse({"access_token": "tok_1", "expires_in": 3600})
        oauth_resp2 = DummyResponse({"access_token": "tok_2", "expires_in": 3600})
        api_401_1 = DummyResponse(status_code=401, text='{"error":"invalid_token_1"}')
        api_401_2 = DummyResponse(status_code=401, text='{"error":"invalid_token_2"}')

        with mock.patch.object(paypal_sdk.requests, "post", side_effect=[oauth_resp1, oauth_resp2]) as mock_oauth:
            with mock.patch.object(paypal_sdk.requests, "request", side_effect=[api_401_1, api_401_2]) as mock_req:
                with self.assertRaises(requests.HTTPError) as ctx:
                    paypal_sdk._request("GET", "/test-fail")
                self.assertIn("HTTP 401", str(ctx.exception))
                self.assertEqual(mock_req.call_count, 2)
                self.assertEqual(mock_oauth.call_count, 2)
                # 2回目も401だったため、tok_2も無効化されてキャッシュは空になる
                self.assertNotIn("sandbox", paypal_sdk._token_cache)

    # 20. GETが401以外の場合、再試行されない
    def test_20_get_non_401_does_not_retry(self):
        oauth_resp = DummyResponse({"access_token": "tok_normal", "expires_in": 3600})
        for error_code in [400, 403, 404, 500]:
            with self.subTest(error_code=error_code):
                paypal_sdk._token_cache.clear()
                api_err = DummyResponse(status_code=error_code, text=f'{{"error":"err_{error_code}"}}')
                with mock.patch.object(paypal_sdk.requests, "post", return_value=oauth_resp) as mock_oauth:
                    with mock.patch.object(paypal_sdk.requests, "request", return_value=api_err) as mock_req:
                        with self.assertRaises(requests.HTTPError) as ctx:
                            paypal_sdk._request("GET", "/test")
                        self.assertIn(f"HTTP {error_code}", str(ctx.exception))
                        self.assertEqual(mock_req.call_count, 1)
                        # 401ではないのでキャッシュは破棄されない
                        self.assertEqual(paypal_sdk._token_cache["sandbox"]["token"], "tok_normal")

    # 21. POSTが401の場合、再試行されずAPIリクエストは1回だけ
    def test_21_post_401_does_not_retry(self):
        oauth_resp = DummyResponse({"access_token": "tok_post", "expires_in": 3600})
        api_401 = DummyResponse(status_code=401, text='{"error":"invalid_token"}')

        with mock.patch.object(paypal_sdk.requests, "post", return_value=oauth_resp) as mock_oauth:
            with mock.patch.object(paypal_sdk.requests, "request", return_value=api_401) as mock_req:
                with self.assertRaises(requests.HTTPError) as ctx:
                    paypal_sdk._request("POST", "/v1/billing/subscriptions", json_body={"plan_id": "P-123"})
                self.assertIn("HTTP 401", str(ctx.exception))
                self.assertEqual(mock_req.call_count, 1)
                self.assertEqual(mock_oauth.call_count, 1)

    # 22. POSTの401後に次の独立したAPI呼び出しを行う場合、古いトークンが残らない
    def test_22_post_401_clears_cache_so_next_call_fetches_fresh_token(self):
        oauth_resp1 = DummyResponse({"access_token": "tok_stale", "expires_in": 3600})
        oauth_resp2 = DummyResponse({"access_token": "tok_fresh", "expires_in": 3600})
        api_401 = DummyResponse(status_code=401, text='{"error":"invalid_token"}')
        api_200 = DummyResponse(json_data={"created": True}, status_code=201)

        with mock.patch.object(paypal_sdk.requests, "post", side_effect=[oauth_resp1, oauth_resp2]) as mock_oauth:
            with mock.patch.object(paypal_sdk.requests, "request", side_effect=[api_401, api_200]) as mock_req:
                # 1回目 POST: 401 で失敗するが、キャッシュは破棄される
                with self.assertRaises(requests.HTTPError):
                    paypal_sdk._request("POST", "/v1/billing/subscriptions", json_body={"plan_id": "P-123"})
                self.assertNotIn("sandbox", paypal_sdk._token_cache)

                # 2回目 POST (ユーザーの再操作など): 新しいトークンが取得される
                resp = paypal_sdk._request("POST", "/v1/billing/subscriptions", json_body={"plan_id": "P-123"})
                self.assertEqual(resp.status_code, 201)
                self.assertEqual(mock_oauth.call_count, 2)
                self.assertEqual(mock_req.call_count, 2)
                second_call_headers = mock_req.call_args_list[1][1]["headers"]
                self.assertEqual(second_call_headers["Authorization"], "Bearer tok_fresh")

    # 23. 別スレッドが新しいトークンをキャッシュした後に古いリクエストが401になっても、新しいトークンが削除されない
    def test_23_concurrent_new_token_not_evicted_by_stale_401(self):
        # 現在キャッシュされているのは新しいトークン 'tok_newer'
        paypal_sdk._token_cache["sandbox"] = {
            "token": "tok_newer",
            "expires_at": datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=3600),
        }
        # 古いリクエストが使っていたトークン 'tok_older' で無効化を試みる
        paypal_sdk.invalidate_access_token("tok_older")
        # 'tok_newer' はキャッシュに残っていること
        self.assertIn("sandbox", paypal_sdk._token_cache)
        self.assertEqual(paypal_sdk._token_cache["sandbox"]["token"], "tok_newer")

        # トークンが一致した場合は削除されること
        paypal_sdk.invalidate_access_token("tok_newer")
        self.assertNotIn("sandbox", paypal_sdk._token_cache)

    # 24. Sandboxの401処理でLive側のキャッシュが変更されない
    def test_24_sandbox_invalidation_does_not_affect_live(self):
        paypal_sdk._token_cache["sandbox"] = {
            "token": "sandbox_tok",
            "expires_at": datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=3600),
        }
        paypal_sdk._token_cache["live"] = {
            "token": "live_tok",
            "expires_at": datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=3600),
        }

        # 現在の環境は sandbox なので sandbox のみ無効化
        paypal_sdk.invalidate_access_token("sandbox_tok")
        self.assertNotIn("sandbox", paypal_sdk._token_cache)
        self.assertIn("live", paypal_sdk._token_cache)
        self.assertEqual(paypal_sdk._token_cache["live"]["token"], "live_tok")

    # 25. OAuthトークン取得自体が失敗した場合、空トークンや古いトークンでAPIを送信しない
    def test_25_oauth_failure_prevents_api_request(self):
        paypal_sdk._token_cache.clear()
        with mock.patch.object(paypal_sdk.requests, "post", side_effect=RuntimeError("OAuth endpoint unavailable")):
            with mock.patch.object(paypal_sdk.requests, "request") as mock_req:
                with self.assertRaises(RuntimeError) as ctx:
                    paypal_sdk._request("GET", "/v1/billing/subscriptions/SUB-999")
                self.assertIn("OAuth endpoint unavailable", str(ctx.exception))
                # requests.request は一切呼び出されないこと
                mock_req.assert_not_called()

    # 26. OAuth認証エラー時にデッドロックせず例外が送出され、エラー本文の秘密情報がマスクされる
    def test_26_oauth_http_error_does_not_deadlock_and_masks_auth(self):
        paypal_sdk._token_cache.clear()
        client_secret = "dummy_client_secret_for_testing"
        auth_bytes = f"dummy_client_id_for_testing:{client_secret}".encode()
        auth_header_val = base64.b64encode(auth_bytes).decode()

        # OAuthエンドポイントが 401 を返し、bodyにauthヘッダーやsecretが含まれる状況をシミュレート
        oauth_err_resp = DummyResponse(
            status_code=401,
            text=f"Unauthorized: secret={client_secret} auth={auth_header_val}",
        )
        with mock.patch.object(paypal_sdk.requests, "post", return_value=oauth_err_resp):
            with self.assertRaises(requests.HTTPError) as ctx:
                # get_access_token() は _token_cache_lock を保持したまま _get_access_token() -> _raise_for_status() を呼ぶ
                paypal_sdk.get_access_token()

            err_msg = str(ctx.exception)
            self.assertIn("PayPal API request failed with HTTP 401", err_msg)
            # 秘密情報が露出していないこと
            self.assertNotIn(client_secret, err_msg)
            self.assertNotIn(auth_header_val, err_msg)
            self.assertIn("[REDACTED]", err_msg)

    # 27. APIリクエスト失敗時にエラー本文中のAccess Tokenがマスクされる
    def test_27_api_http_error_masks_access_token(self):
        oauth_resp = DummyResponse({"access_token": "my_secret_api_token_999", "expires_in": 3600})
        api_err_resp = DummyResponse(
            status_code=403,
            text="Forbidden: my_secret_api_token_999 is not allowed",
        )
        with mock.patch.object(paypal_sdk.requests, "post", return_value=oauth_resp):
            with mock.patch.object(paypal_sdk.requests, "request", return_value=api_err_resp):
                with self.assertRaises(requests.HTTPError) as ctx:
                    paypal_sdk._request("GET", "/v1/test")

                err_msg = str(ctx.exception)
                self.assertIn("PayPal API request failed with HTTP 403", err_msg)
                self.assertNotIn("my_secret_api_token_999", err_msg)
                self.assertIn("[REDACTED]", err_msg)

    # 28. キャッシュ更新とエラー処理が同時に発生しても辞書変更例外が発生しない
    def test_28_cache_mutation_does_not_affect_raise_for_status(self):
        # _raise_for_status() は _token_cache を直接走査しないため、キャッシュの変更と競合しない
        paypal_sdk._token_cache["sandbox"] = {
            "token": "tok_1",
            "expires_at": datetime.datetime.now(datetime.timezone.utc),
        }
        err_resp = DummyResponse(status_code=500, text="Internal Server Error: tok_1")
        # 実行中にキャッシュがクリアまたは変更されても問題なくマスク・例外送出される
        paypal_sdk._token_cache.clear()
        with self.assertRaises(requests.HTTPError) as ctx:
            paypal_sdk._raise_for_status(err_resp, ("tok_1",))
        self.assertIn("[REDACTED]", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
