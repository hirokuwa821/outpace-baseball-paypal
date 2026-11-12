"""Plan quarantine, error separation, and timeout behaviour. Fully offline and mocked.

Every test uses a temp cache file, a temp SQLite DB, pinned environment, and blocked
network entry points, so no test can reach PayPal or the real subscriptions.db.
"""
import contextlib
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
from fastapi.testclient import TestClient

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))

import paypal_sdk
import paypal_server
import subscription_store

REAL_DB_PATH = (PROJECT_DIR / "src" / "subscriptions.db").resolve()
ACTIVE_RESPONSE = {"id": "PLAN-NEW", "status": "ACTIVE"}


# ---- helpers ----------------------------------------------------------------

def _no_network(*args, **kwargs):
    raise AssertionError("test attempted a real network call")


def _json_response(payload):
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def _http_error(status, body=None):
    response = MagicMock()
    response.status_code = status
    response.json.return_value = body or {}
    return requests.HTTPError(f"HTTP {status}", response=response)


@contextlib.contextmanager
def _isolated(environment="sandbox"):
    """Yield (cache_file, db_path) with everything redirected and network blocked."""
    with tempfile.TemporaryDirectory() as temp_dir:
        cache_file = Path(temp_dir) / "paypal_resource_ids.json"
        db_path = Path(temp_dir) / "test_subscriptions.db"
        assert db_path.resolve() != REAL_DB_PATH
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(subscription_store, "DB_PATH", str(db_path)))
            subscription_store.init_db()
            stack.enter_context(patch.object(paypal_sdk, "RESOURCE_IDS_CACHE_FILE", cache_file))
            stack.enter_context(patch.object(paypal_sdk, "get_paypal_env", return_value=environment))
            stack.enter_context(patch.object(paypal_sdk.requests, "request", side_effect=_no_network))
            stack.enter_context(patch.object(paypal_sdk.requests, "post", side_effect=_no_network))
            stack.enter_context(patch.object(paypal_server, "log_pending_token_diagnostic"))
            yield cache_file, db_path


def _expect_plan_error(reason, fn):
    try:
        fn()
    except paypal_sdk.PlanCreationError as error:
        assert error.reason == reason, (error.reason, reason)
        return error
    raise AssertionError(f"expected PlanCreationError with reason {reason!r}")


def _methods(request):
    return [entry.args[0] for entry in request.call_args_list]


def _unresolved():
    return subscription_store.has_unresolved_plan_quarantine("sandbox", "PROD-S")


def _create():
    return paypal_sdk._create_plan("PROD-S")


# ---- quarantine of non-ACTIVE and cache-failure plans -----------------------

def test_non_active_plan_is_quarantined_and_blocks_next_creation():
    with _isolated() as (cache_file, _):
        with patch.object(paypal_sdk, "_request", side_effect=[_json_response({"id": "PLAN-NEW", "status": "CREATED"})]) as request:
            error = _expect_plan_error("status_not_active", _create)
        assert error.plan_id == "PLAN-NEW"
        assert error.entry_id is not None
        assert _unresolved()
        assert not cache_file.exists()
        with patch.object(paypal_sdk, "_request", side_effect=_no_network) as second:
            _expect_plan_error("quarantine_blocked", _create)
        assert not second.called
        assert _methods(request) == ["POST"]


def test_cache_save_failure_is_quarantined_with_plan_id_and_blocks_next_creation():
    with _isolated():
        with patch.object(paypal_sdk, "_write_resource_cache", side_effect=OSError("disk full")):
            with patch.object(paypal_sdk, "_request", side_effect=[_json_response(ACTIVE_RESPONSE)]) as request:
                error = _expect_plan_error("cache_save_failed", _create)
        assert error.plan_id == "PLAN-NEW"
        assert isinstance(error.__cause__, OSError)
        assert _unresolved()
        assert _methods(request) == ["POST"]
        with patch.object(paypal_sdk, "_request", side_effect=_no_network) as second:
            _expect_plan_error("quarantine_blocked", _create)
        assert not second.called


def test_plan_id_record_failure_stops_without_caching_and_stays_blocked():
    real_update = subscription_store.update_plan_quarantine

    def failing_update(entry_id, reason, plan_id=None):
        if plan_id is not None:
            raise OSError("write failed")
        return real_update(entry_id, reason, plan_id=plan_id)

    with _isolated() as (cache_file, _):
        with patch.object(subscription_store, "update_plan_quarantine", side_effect=failing_update):
            with patch.object(paypal_sdk, "_request", side_effect=[_json_response(ACTIVE_RESPONSE)]) as request:
                error = _expect_plan_error("plan_id_not_recorded", _create)
        assert error.plan_id == "PLAN-NEW"
        assert not cache_file.exists()
        assert _unresolved()
        assert _methods(request) == ["POST"]


def test_success_leaves_no_unresolved_record_and_caches_plan():
    with _isolated() as (cache_file, _):
        with patch.object(paypal_sdk, "_request", side_effect=[_json_response(ACTIVE_RESPONSE)]) as request:
            assert _create() == "PLAN-NEW"
        assert _methods(request) == ["POST"]
        assert not _unresolved()
        assert '"plan_id": "PLAN-NEW"' in cache_file.read_text(encoding="utf-8")


# ---- stop before POST when the record cannot be written or checked ----------

def test_quarantine_write_failure_stops_before_post():
    with _isolated():
        with patch.object(subscription_store, "open_plan_quarantine", side_effect=OSError("db locked")):
            with patch.object(paypal_sdk, "_request", side_effect=_no_network) as request:
                _expect_plan_error("quarantine_write_failed", _create)
        assert not request.called


def test_quarantine_check_failure_stops_before_post():
    with _isolated():
        with patch.object(subscription_store, "has_unresolved_plan_quarantine", side_effect=OSError("db locked")):
            with patch.object(paypal_sdk, "_request", side_effect=_no_network) as request:
                _expect_plan_error("quarantine_check_failed", _create)
        assert not request.called


def test_existing_unresolved_record_blocks_new_post():
    with _isolated():
        subscription_store.open_plan_quarantine("sandbox", "PROD-S", "manual_review")
        with patch.object(paypal_sdk, "_request", side_effect=_no_network) as request:
            _expect_plan_error("quarantine_blocked", _create)
        assert not request.called


def test_unresolved_record_for_other_product_does_not_block():
    with _isolated():
        subscription_store.open_plan_quarantine("sandbox", "PROD-OTHER", "manual_review")
        with patch.object(paypal_sdk, "_request", side_effect=[_json_response(ACTIVE_RESPONSE)]) as request:
            assert _create() == "PLAN-NEW"
        assert _methods(request) == ["POST"]


def test_manual_resolution_allows_creation_again():
    with _isolated():
        entry = subscription_store.open_plan_quarantine("sandbox", "PROD-S", "manual_review")
        with patch.object(paypal_sdk, "_request", side_effect=_no_network):
            _expect_plan_error("quarantine_blocked", _create)
        subscription_store.resolve_plan_quarantine(
            entry,
            environment="sandbox",
            product_id="PROD-S",
            plan_id=None,
            resolved_by="test-operator",
            resolution_note="verified in PayPal sandbox dashboard",
        )
        assert not _unresolved()
        with patch.object(paypal_sdk, "_request", side_effect=[_json_response(ACTIVE_RESPONSE)]) as request:
            assert _create() == "PLAN-NEW"
        assert _methods(request) == ["POST"]


def test_operator_listing_shows_unresolved_entries_without_secrets():
    with _isolated():
        entry = subscription_store.open_plan_quarantine("sandbox", "PROD-S", "manual_review")
        rows = subscription_store.list_unresolved_plan_quarantine()
        assert [row["entry_id"] for row in rows] == [entry]
        assert set(rows[0]) == {"entry_id", "environment", "product_id", "plan_id", "reason", "created_at"}


# ---- timeouts and unknown outcomes: no automatic re-creation ----------------

def test_post_timeout_is_quarantined_as_unknown_and_not_retried():
    with _isolated():
        with patch.object(paypal_sdk, "_request", side_effect=[requests.Timeout("timed out")]) as request:
            error = _expect_plan_error("creation_outcome_unknown", _create)
        assert error.plan_id is None
        assert isinstance(error.__cause__, requests.Timeout)
        assert _unresolved()
        assert _methods(request) == ["POST"]
        with patch.object(paypal_sdk, "_request", side_effect=_no_network) as second:
            _expect_plan_error("quarantine_blocked", _create)
        assert not second.called


def test_get_or_create_plan_timeout_does_not_recreate_on_retry():
    # No cached plan, so get_or_create_plan POSTs directly. A connection error from that
    # POST is quarantined, and the user's retry must be blocked without another POST.
    with _isolated():
        with patch.object(paypal_sdk, "get_or_create_product", return_value="PROD-S"):
            with patch.object(paypal_sdk, "_request", side_effect=[requests.ConnectionError("reset")]) as request:
                _expect_plan_error("creation_outcome_unknown", lambda: paypal_sdk.get_or_create_plan())
            assert _methods(request) == ["POST"]
            with patch.object(paypal_sdk, "_request", side_effect=_no_network) as second:
                _expect_plan_error("quarantine_blocked", lambda: paypal_sdk.get_or_create_plan())
            assert not second.called


def test_post_http_4xx_rejection_closes_record_and_is_not_blocking():
    with _isolated():
        with patch.object(paypal_sdk, "_request", side_effect=[_http_error(400, {"name": "INVALID_REQUEST"})]) as request:
            try:
                _create()
            except requests.HTTPError:
                pass
            else:
                raise AssertionError("4xx rejection must propagate unchanged")
        assert _methods(request) == ["POST"]
        assert not _unresolved()
        with patch.object(paypal_sdk, "_request", side_effect=[_json_response(ACTIVE_RESPONSE)]):
            assert _create() == "PLAN-NEW"


def test_post_http_5xx_is_quarantined_as_unknown():
    with _isolated():
        with patch.object(paypal_sdk, "_request", side_effect=[_http_error(500)]) as request:
            _expect_plan_error("creation_outcome_unknown", _create)
        assert _unresolved()
        assert _methods(request) == ["POST"]


# ---- HTTP responses: no plan IDs, no PayPal bodies, operator log kept -------

def _post_endpoint(side_effect):
    client = TestClient(paypal_server.app)
    with patch.object(paypal_server, "create_subscription", side_effect=side_effect):
        return client.post(
            "/create-subscription",
            json={
                "user_id": "u-test",
                "return_url": "http://localhost:8502/",
                "cancel_url": "http://localhost:8502/",
            },
        )


def test_plan_id_is_not_in_http_response_and_original_exception_is_logged():
    exc = paypal_sdk.PlanCreationError(
        "PayPal plan PLAN-SECRET-123 was created but caching failed",
        plan_id="PLAN-SECRET-123",
        reason="cache_save_failed",
        entry_id=7,
    )
    with _isolated():
        with patch.object(paypal_server, "logger") as logger:
            response = _post_endpoint(exc)
    assert response.status_code == 400
    assert "PLAN-SECRET-123" not in response.text
    detail = response.json()["detail"]
    assert detail.startswith("Could not start the subscription.")
    reference = detail.split("Reference code:")[1].strip()
    assert len(reference) == 8
    logger.error.assert_called_once()
    args, kwargs = logger.error.call_args
    assert reference in args
    assert kwargs.get("exc_info") is exc


def test_paypal_error_body_is_not_in_http_response():
    exc = requests.HTTPError('PayPal API request failed with HTTP 400: {"details":"BODY-SECRET-XYZ"}')
    with _isolated():
        with patch.object(paypal_server, "logger"):
            response = _post_endpoint(exc)
    assert response.status_code == 400
    assert "BODY-SECRET-XYZ" not in response.text
    assert "Reference code:" in response.json()["detail"]


def test_internal_exception_text_is_not_in_http_response():
    with _isolated():
        with patch.object(paypal_server, "logger") as logger:
            response = _post_endpoint(RuntimeError("INTERNAL-DETAIL-XYZ"))
    assert "INTERNAL-DETAIL-XYZ" not in response.text
    logger.error.assert_called_once()
    assert isinstance(logger.error.call_args.kwargs["exc_info"], RuntimeError)
