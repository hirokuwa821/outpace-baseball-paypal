import datetime
import sqlite3
import tempfile
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_DIR = Path(__file__).resolve().parent.parent
import sys

sys.path.insert(0, str(PROJECT_DIR / "src"))

import paypal_return
import subscription_store
from paypal_return import clear_return_query_params


def _with_temp_db():
    temp = tempfile.TemporaryDirectory()
    db_path = Path(temp.name) / "subscriptions.db"
    return temp, db_path


def test_query_params_remove_only_paypal_callback_values():
    query_params = {
        "subscription_id": "I-SUB",
        "token": "one-time-token",
        "lang": "ja",
    }

    clear_return_query_params(query_params)

    assert query_params == {"lang": "ja"}


def test_valid_token_and_active_owned_subscription_grant_pro():
    temp, db_path = _with_temp_db()
    with temp:
        with patch.object(subscription_store, "DB_PATH", str(db_path)):
            subscription_store.init_db()
            subscription_store.store_pending("valid-token", "user-1")
            with patch.object(
                paypal_return,
                "get_subscription_details",
                return_value={"id": "I-SUB-1", "status": "ACTIVE", "custom_id": "user-1"},
            ) as get_details:
                result = paypal_return.handle_paypal_return("I-SUB-1", "valid-token")

            assert result.verified is True
            assert result.active is True
            assert result.user_id == "user-1"
            get_details.assert_called_once_with("I-SUB-1")
            assert subscription_store.get_by_subscription("I-SUB-1")["status"] == "ACTIVE"
            assert subscription_store.get_pending_user("valid-token") is None


def test_return_diagnostic_logs_only_safe_verification_fields(caplog):
    caplog.set_level("INFO", logger="paypal.return.diagnostics")
    temp, db_path = _with_temp_db()
    with temp:
        with patch.object(subscription_store, "DB_PATH", str(db_path)):
            subscription_store.init_db()
            subscription_store.store_pending("private-pending-token", "private-user-id")
            with patch.object(
                paypal_return,
                "get_subscription_details",
                return_value={
                    "id": "private-subscription-id",
                    "status": "ACTIVE",
                    "custom_id": "private-user-id",
                },
            ):
                result = paypal_return.handle_paypal_return(
                    "private-subscription-id",
                    "private-pending-token",
                    database_path_match=True,
                )

    assert result.active
    records = [
        record
        for record in caplog.records
        if record.name == "paypal.return.diagnostics"
    ]
    assert records
    diagnostic = json.loads(records[-1].getMessage())
    assert diagnostic == {
        "custom_id_match": True,
        "database_path_match": True,
        "paypal_api_result": "success",
        "pending_token_present": True,
        "pending_token_state": "consumed",
        "subscription_id_match": True,
        "subscription_id_present": True,
        "subscription_status": "ACTIVE",
        "verification_failure_reason": "none",
    }
    logged_text = records[-1].getMessage()
    assert "private-pending-token" not in logged_text
    assert "private-user-id" not in logged_text
    assert "private-subscription-id" not in logged_text


def test_return_diagnostic_classifies_paypal_api_failure(caplog):
    caplog.set_level("INFO", logger="paypal.return.diagnostics")
    temp, db_path = _with_temp_db()
    with temp:
        with patch.object(subscription_store, "DB_PATH", str(db_path)):
            subscription_store.init_db()
            subscription_store.store_pending("valid-token", "user-1")
            with patch.object(
                paypal_return,
                "get_subscription_details",
                side_effect=RuntimeError("must not be logged"),
            ):
                try:
                    paypal_return.handle_paypal_return(
                        "I-SUB-API-ERROR", "valid-token", database_path_match=False
                    )
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("PayPal API failure must propagate")

    records = [
        record
        for record in caplog.records
        if record.name == "paypal.return.diagnostics"
    ]
    diagnostic = json.loads(records[-1].getMessage())
    assert diagnostic["paypal_api_result"] == "failure"
    assert diagnostic["verification_failure_reason"] == "paypal_api_error"
    assert "must not be logged" not in records[-1].getMessage()


def test_missing_invalid_and_expired_tokens_never_call_paypal():
    temp, db_path = _with_temp_db()
    with temp:
        with patch.object(subscription_store, "DB_PATH", str(db_path)):
            subscription_store.init_db()
            expired_time = (
                datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
                - datetime.timedelta(hours=25)
            ).isoformat()
            conn = sqlite3.connect(db_path)
            try:
                conn.execute(
                    "INSERT INTO pending (token, user_id, created_at) VALUES (?, ?, ?)",
                    ("expired-token", "user-old", expired_time),
                )
                conn.commit()
            finally:
                conn.close()

            with patch.object(paypal_return, "get_subscription_details") as get_details:
                missing = paypal_return.handle_paypal_return("I-SUB", None)
                invalid = paypal_return.handle_paypal_return("I-SUB", "unknown")
                expired = paypal_return.handle_paypal_return("I-SUB", "expired-token")

            assert all(not result.active for result in (missing, invalid, expired))
            assert all(not result.verified for result in (missing, invalid, expired))
            assert expired.reason == "invalid_or_expired_token"
            get_details.assert_not_called()


def test_owner_mismatch_does_not_store_or_consume_token(caplog):
    caplog.set_level("INFO", logger="paypal.return.diagnostics")
    temp, db_path = _with_temp_db()
    with temp:
        with patch.object(subscription_store, "DB_PATH", str(db_path)):
            subscription_store.init_db()
            subscription_store.store_pending("valid-token", "user-1")
            with patch.object(
                paypal_return,
                "get_subscription_details",
                return_value={"id": "I-SUB-2", "status": "ACTIVE", "custom_id": "someone-else"},
            ):
                result = paypal_return.handle_paypal_return("I-SUB-2", "valid-token")

            assert not result.active
            assert not result.verified
            assert result.reason == "owner_mismatch"
            assert subscription_store.get_by_subscription("I-SUB-2") is None
            assert subscription_store.get_pending_user("valid-token") == "user-1"
            diagnostic = json.loads(
                [
                    record.getMessage()
                    for record in caplog.records
                    if record.name == "paypal.return.diagnostics"
                ][-1]
            )
            assert diagnostic["subscription_id_match"] is True
            assert diagnostic["custom_id_match"] is False
            assert diagnostic["verification_failure_reason"] == "owner_mismatch"


def test_consumed_token_cannot_be_replayed_for_another_subscription():
    temp, db_path = _with_temp_db()
    with temp:
        with patch.object(subscription_store, "DB_PATH", str(db_path)):
            subscription_store.init_db()
            subscription_store.store_pending("valid-token", "user-1")
            with patch.object(
                paypal_return,
                "get_subscription_details",
                side_effect=[
                    {"id": "I-SUB-9", "status": "ACTIVE", "custom_id": "user-1"},
                    {"id": "I-SUB-10", "status": "ACTIVE", "custom_id": "user-1"},
                ],
            ):
                first = paypal_return.handle_paypal_return("I-SUB-9", "valid-token")
                replay = paypal_return.handle_paypal_return("I-SUB-10", "valid-token")

            assert first.active
            assert not replay.active
            assert not replay.verified
            assert replay.reason == "invalid_or_expired_token"
            assert subscription_store.get_by_subscription("I-SUB-9") is not None
            assert subscription_store.get_by_subscription("I-SUB-10") is None


def test_non_active_paypal_status_is_persisted_but_remains_free(caplog):
    caplog.set_level("INFO", logger="paypal.return.diagnostics")
    temp, db_path = _with_temp_db()
    with temp:
        with patch.object(subscription_store, "DB_PATH", str(db_path)):
            subscription_store.init_db()
            subscription_store.store_pending("valid-token", "user-1")
            with patch.object(
                paypal_return,
                "get_subscription_details",
                return_value={
                    "id": "I-SUB-3",
                    "status": "APPROVAL_PENDING",
                    "custom_id": "user-1",
                },
            ):
                result = paypal_return.handle_paypal_return("I-SUB-3", "valid-token")

            assert result.verified
            assert not result.active
            assert result.status == "APPROVAL_PENDING"
            assert subscription_store.get_by_subscription("I-SUB-3")["status"] == "APPROVAL_PENDING"
            diagnostic = json.loads(
                [
                    record.getMessage()
                    for record in caplog.records
                    if record.name == "paypal.return.diagnostics"
                ][-1]
            )
            assert diagnostic["subscription_status"] == "APPROVAL_PENDING"
            assert diagnostic["verification_failure_reason"] == "subscription_not_active"


def test_reload_refreshes_subscription_status_with_paypal():
    temp, db_path = _with_temp_db()
    with temp:
        with patch.object(subscription_store, "DB_PATH", str(db_path)):
            subscription_store.init_db()
            subscription_store.upsert("I-SUB-4", "user-1", "ACTIVE")

            with patch.object(
                paypal_return,
                "get_subscription_details",
                return_value={"id": "I-SUB-4", "status": "ACTIVE", "custom_id": "user-1"},
            ) as get_details:
                active = paypal_return.refresh_user_subscription("user-1")

            assert active.active
            get_details.assert_called_once_with("I-SUB-4")

            with patch.object(
                paypal_return,
                "get_subscription_details",
                return_value={
                    "id": "I-SUB-4",
                    "status": "CANCELLED",
                    "custom_id": "user-1",
                },
            ):
                cancelled = paypal_return.refresh_user_subscription("user-1")

            assert cancelled.verified
            assert not cancelled.active
            assert subscription_store.get_by_subscription("I-SUB-4")["status"] == "CANCELLED"


def test_reload_does_not_grant_another_users_subscription():
    temp, db_path = _with_temp_db()
    with temp:
        with patch.object(subscription_store, "DB_PATH", str(db_path)):
            subscription_store.init_db()
            subscription_store.upsert("I-SUB-5", "user-1", "ACTIVE")
            with patch.object(
                paypal_return,
                "get_subscription_details",
                return_value={"id": "I-SUB-5", "status": "ACTIVE", "custom_id": "user-2"},
            ):
                result = paypal_return.refresh_user_subscription("user-1")

            assert not result.active
            assert not result.verified
            assert result.reason == "owner_mismatch"


def test_paypal_verification_errors_propagate_for_app_to_handle():
    temp, db_path = _with_temp_db()
    with temp:
        with patch.object(subscription_store, "DB_PATH", str(db_path)):
            subscription_store.init_db()
            subscription_store.upsert("I-SUB-6", "user-1", "ACTIVE")
            with patch.object(
                paypal_return,
                "get_subscription_details",
                side_effect=RuntimeError("PayPal unavailable"),
            ):
                try:
                    paypal_return.refresh_user_subscription("user-1")
                    raise AssertionError("PayPal verification error must propagate")
                except RuntimeError as error:
                    assert str(error) == "PayPal unavailable"


def test_return_rejects_mismatched_subscription_id(caplog):
    caplog.set_level("INFO", logger="paypal.return.diagnostics")
    temp, db_path = _with_temp_db()
    with temp:
        with patch.object(subscription_store, "DB_PATH", str(db_path)):
            subscription_store.init_db()
            subscription_store.store_pending("valid-token", "user-1")
            with patch.object(
                paypal_return,
                "get_subscription_details",
                return_value={
                    "id": "I-DIFFERENT",
                    "status": "ACTIVE",
                    "custom_id": "user-1",
                },
            ):
                result = paypal_return.handle_paypal_return("I-SUB-7", "valid-token")

            assert not result.active
            assert not result.verified
            assert result.reason == "subscription_id_mismatch"
            assert subscription_store.get_by_subscription("I-SUB-7") is None
            diagnostic = json.loads(
                [
                    record.getMessage()
                    for record in caplog.records
                    if record.name == "paypal.return.diagnostics"
                ][-1]
            )
            assert diagnostic["subscription_id_match"] is False
            assert diagnostic["custom_id_match"] is True
            assert diagnostic["verification_failure_reason"] == "subscription_id_mismatch"


def test_paypal_subscription_is_requested_from_sandbox_api():
    import paypal_sdk

    response = MagicMock()
    response.json.return_value = {"id": "I-SUB-8", "status": "ACTIVE"}
    with patch.dict("os.environ", {"PAYPAL_ENV": "sandbox"}):
        assert paypal_sdk.get_base_url() == "https://api-m.sandbox.paypal.com"
        with patch.object(paypal_sdk, "_request", return_value=response) as request:
            assert paypal_sdk.get_subscription_details("I-SUB-8") == response.json.return_value

    request.assert_called_once_with("GET", "/v1/billing/subscriptions/I-SUB-8")
