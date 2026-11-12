"""Concurrency, quarantine resolution guards, and log redaction. Offline, mocked, temp DB only.

Threads and real child processes share a temp cache directory, so the cross-process lock
and the partial unique index are exercised for real. No test reaches PayPal or the real
subscriptions.db.
"""
import contextlib
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests
from fastapi.testclient import TestClient

PROJECT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_DIR / "src"
sys.path.insert(0, str(SRC_DIR))

import paypal_sdk
import paypal_server
import subscription_store

REAL_DB_PATH = (SRC_DIR / "subscriptions.db").resolve()

VALID_PLAN = {
    "id": "PLAN-NEW",
    "product_id": "PROD-S",
    "status": "ACTIVE",
    "billing_cycles": [
        {
            "tenure_type": "REGULAR",
            "frequency": {"interval_unit": "MONTH", "interval_count": 1},
            "pricing_scheme": {"fixed_price": {"currency_code": "JPY", "value": "1980"}},
        }
    ],
}


def _response(payload):
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def _fake_paypal(post_delay=0.0):
    """Scripted PayPal: counts POST and GET calls. The POST sleeps to widen race windows."""
    lock = threading.Lock()
    state = {"posts": 0, "gets": 0}

    def fake(method, endpoint, json_body=None, params=None):
        if method == "POST":
            with lock:
                state["posts"] += 1
            if post_delay:
                import time

                time.sleep(post_delay)
            return _response({"id": "PLAN-NEW", "status": "ACTIVE"})
        if method == "GET":
            with lock:
                state["gets"] += 1
            return _response(VALID_PLAN)
        raise AssertionError(f"unexpected PayPal call: {method} {endpoint}")

    return fake, state


@contextlib.contextmanager
def _isolated(environment="sandbox"):
    """Yield (cache_file, db_path) with a temp cache and DB and blocked network."""
    with tempfile.TemporaryDirectory() as temp_dir:
        cache_file = Path(temp_dir) / "paypal_resource_ids.json"
        db_path = Path(temp_dir) / "test_subscriptions.db"
        assert db_path.resolve() != REAL_DB_PATH
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(subscription_store, "DB_PATH", str(db_path)))
            subscription_store.init_db()
            stack.enter_context(patch.object(paypal_sdk, "RESOURCE_IDS_CACHE_FILE", cache_file))
            stack.enter_context(patch.object(paypal_sdk, "get_paypal_env", return_value=environment))
            stack.enter_context(patch.object(paypal_sdk, "get_or_create_product", return_value="PROD-S"))
            stack.enter_context(patch.object(paypal_sdk.requests, "request", side_effect=_no_network))
            stack.enter_context(patch.object(paypal_sdk.requests, "post", side_effect=_no_network))
            stack.enter_context(patch.object(paypal_server, "log_pending_token_diagnostic"))
            yield cache_file, db_path


def _no_network(*args, **kwargs):
    raise AssertionError("test attempted a real network call")


def _raw_rows(db_path):
    with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
        conn.row_factory = sqlite3.Row
        return [dict(row) for row in conn.execute("SELECT * FROM paypal_plan_quarantine ORDER BY entry_id")]


# ---- 1. same-process concurrency (threads) -----------------------------------

def test_concurrent_threads_create_exactly_one_plan():
    with _isolated():
        fake, state = _fake_paypal(post_delay=0.3)
        results, errors = [], []
        with patch.object(paypal_sdk, "_request", side_effect=fake):
            def worker():
                try:
                    results.append(paypal_sdk.get_or_create_plan())
                except Exception as error:  # collected and asserted below
                    errors.append(error)

            threads = [threading.Thread(target=worker) for _ in range(6)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)
        assert not errors, errors
        assert state["posts"] == 1
        assert results == ["PLAN-NEW"] * 6
        # Every thread after the first re-read the cache under the lock and reused the plan.
        assert state["gets"] == 5


def test_cache_is_read_only_while_the_creation_lock_is_held():
    observed = []
    real_read = paypal_sdk._cached_resource_id

    def spy(environment, resource_name):
        observed.append(paypal_sdk._PLAN_THREAD_LOCK.locked())
        return real_read(environment, resource_name)

    with _isolated():
        fake, _ = _fake_paypal()
        with patch.object(paypal_sdk, "_cached_resource_id", side_effect=spy):
            with patch.object(paypal_sdk, "_request", side_effect=fake):
                assert paypal_sdk.get_or_create_plan() == "PLAN-NEW"
    assert observed and all(observed)


def test_lock_wait_timeout_stops_without_any_paypal_call():
    with _isolated():
        fake, state = _fake_paypal()
        holder_ready = threading.Event()
        release = threading.Event()

        def holder():
            with paypal_sdk._plan_creation_lock():
                holder_ready.set()
                release.wait(10)

        thread = threading.Thread(target=holder)
        thread.start()
        assert holder_ready.wait(5)
        try:
            with patch.object(paypal_sdk, "PLAN_CREATION_LOCK_WAIT_SECONDS", 0.3):
                with patch.object(paypal_sdk, "_request", side_effect=fake):
                    try:
                        paypal_sdk.get_or_create_plan()
                    except paypal_sdk.PlanCreationError as error:
                        assert error.reason == "lock_timeout"
                    else:
                        raise AssertionError("busy lock must stop creation")
        finally:
            release.set()
            thread.join(5)
        assert state["posts"] == 0
        assert state["gets"] == 0


# ---- 2. cross-process concurrency (real child processes) --------------------

_CHILD_SCRIPT = r'''
import sys, time
from pathlib import Path
from unittest.mock import MagicMock, patch
src, cache, db, marker = sys.argv[1:5]
sys.path.insert(0, src)
import paypal_sdk, subscription_store
subscription_store.DB_PATH = db
paypal_sdk.RESOURCE_IDS_CACHE_FILE = Path(cache)
PLAN = {
    "id": "PLAN-NEW", "product_id": "PROD-S", "status": "ACTIVE",
    "billing_cycles": [{"tenure_type": "REGULAR",
        "frequency": {"interval_unit": "MONTH", "interval_count": 1},
        "pricing_scheme": {"fixed_price": {"currency_code": "JPY", "value": "1980"}}}],
}
def response(payload):
    r = MagicMock(); r.status_code = 200; r.json.return_value = payload
    r.raise_for_status.return_value = None
    return r
def fake(method, endpoint, json_body=None, params=None):
    if method == "POST":
        with open(marker, "a", encoding="utf-8") as handle:
            handle.write("POST\n")
        time.sleep(0.5)
        return response({"id": "PLAN-NEW", "status": "ACTIVE"})
    if method == "GET":
        return response(PLAN)
    raise SystemExit("unexpected method " + method)
with patch.object(paypal_sdk, "get_or_create_product", return_value="PROD-S"), \
     patch.object(paypal_sdk, "get_paypal_env", return_value="sandbox"), \
     patch.object(paypal_sdk, "_request", side_effect=fake):
    print(paypal_sdk.get_or_create_plan())
'''


def test_concurrent_processes_create_exactly_one_plan():
    with tempfile.TemporaryDirectory() as temp_dir:
        temp = Path(temp_dir)
        cache = temp / "paypal_resource_ids.json"
        db = temp / "test_subscriptions.db"
        marker = temp / "posts.txt"
        assert db.resolve() != REAL_DB_PATH
        with patch.object(subscription_store, "DB_PATH", str(db)):
            subscription_store.init_db()

        env = {**os.environ, "PYTHON_DOTENV_DISABLED": "1", "PAYPAL_ENV": "sandbox"}
        children = [
            subprocess.Popen(
                [sys.executable, "-c", _CHILD_SCRIPT, str(SRC_DIR), str(cache), str(db), str(marker)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=env,
            )
            for _ in range(3)
        ]
        outputs = []
        for child in children:
            stdout, stderr = child.communicate(timeout=120)
            outputs.append((child.returncode, stdout.strip(), stderr))

        for returncode, stdout, stderr in outputs:
            assert returncode == 0, stderr
            assert stdout == "PLAN-NEW", (stdout, stderr)
        posts = marker.read_text(encoding="utf-8").splitlines() if marker.exists() else []
        assert posts == ["POST"], posts


# ---- 3. partial unique index --------------------------------------------------

def test_partial_unique_index_allows_one_open_entry_per_environment_and_product():
    with _isolated() as (_, db_path):
        first = subscription_store.open_plan_quarantine("sandbox", "PROD-S", "creation_request_started")
        try:
            subscription_store.open_plan_quarantine("sandbox", "PROD-S", "creation_request_started")
        except subscription_store.QuarantineConflict:
            pass
        else:
            raise AssertionError("second open entry for the same environment and product must conflict")
        subscription_store.open_plan_quarantine("live", "PROD-S", "manual_review")
        subscription_store.open_plan_quarantine("sandbox", "PROD-OTHER", "manual_review")

        with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
            index_sql = "".join(
                row[0] for row in conn.execute(
                    "SELECT sql FROM sqlite_master WHERE name = 'ux_plan_quarantine_open'"
                ) if row[0]
            )
        assert "UNIQUE" in index_sql and "WHERE resolved_at IS NULL" in index_sql

        subscription_store.resolve_plan_quarantine(
            first,
            environment="sandbox",
            product_id="PROD-S",
            plan_id=None,
            resolved_by="test-operator",
            resolution_note="checked",
        )
        subscription_store.open_plan_quarantine("sandbox", "PROD-S", "creation_request_started")


# ---- 4. resolution guards -----------------------------------------------------

def _resolve_kwargs(**overrides):
    kwargs = dict(
        environment="sandbox",
        product_id="PROD-S",
        plan_id=None,
        resolved_by="test-operator",
        resolution_note="checked in PayPal",
    )
    kwargs.update(overrides)
    return kwargs


def test_resolution_refuses_mismatched_identifiers_and_keeps_entry_open():
    with _isolated():
        entry = subscription_store.open_plan_quarantine("sandbox", "PROD-S", "creation_outcome_unknown")
        for overrides in (
            {"environment": "live"},
            {"product_id": "PROD-OTHER"},
            {"plan_id": "PLAN-X"},
        ):
            try:
                subscription_store.resolve_plan_quarantine(entry, **_resolve_kwargs(**overrides))
            except subscription_store.QuarantineNotOpen:
                continue
            raise AssertionError(f"mismatched identifiers must not resolve: {overrides}")
        assert subscription_store.has_unresolved_plan_quarantine("sandbox", "PROD-S")


def test_resolution_refuses_unknown_entry_and_already_resolved_entry():
    with _isolated() as (_, db_path):
        entry = subscription_store.open_plan_quarantine("sandbox", "PROD-S", "creation_outcome_unknown")
        try:
            subscription_store.resolve_plan_quarantine(entry + 1000, **_resolve_kwargs())
        except subscription_store.QuarantineNotOpen:
            pass
        else:
            raise AssertionError("unknown entry must not resolve")

        subscription_store.resolve_plan_quarantine(entry, **_resolve_kwargs())
        try:
            subscription_store.resolve_plan_quarantine(entry, **_resolve_kwargs())
        except subscription_store.QuarantineNotOpen:
            pass
        else:
            raise AssertionError("re-resolving must be refused, not treated as success")

        row = _raw_rows(db_path)[0]
        assert row["resolved_by"] == "test-operator"
        assert row["resolution_note"] == "checked in PayPal"
        assert row["resolved_at"] is not None


def test_resolution_requires_who_and_why():
    with _isolated():
        entry = subscription_store.open_plan_quarantine("sandbox", "PROD-S", "creation_outcome_unknown")
        for overrides in ({"resolved_by": " "}, {"resolution_note": ""}):
            try:
                subscription_store.resolve_plan_quarantine(entry, **_resolve_kwargs(**overrides))
            except ValueError:
                continue
            raise AssertionError(f"missing audit field must be refused: {overrides}")
        assert subscription_store.has_unresolved_plan_quarantine("sandbox", "PROD-S")


def test_system_close_only_accepts_confirmed_outcomes_and_matching_plan():
    with _isolated():
        entry = subscription_store.open_plan_quarantine("sandbox", "PROD-S", "creation_request_started")
        subscription_store.update_plan_quarantine(entry, "plan_created_cache_pending", plan_id="PLAN-NEW")
        for outcome, plan_id in (("unknown", "PLAN-NEW"), ("completed", "PLAN-OTHER")):
            try:
                subscription_store.close_plan_quarantine_confirmed(entry, outcome, plan_id=plan_id)
            except (ValueError, subscription_store.QuarantineNotOpen):
                continue
            raise AssertionError(f"invalid close accepted: {outcome}, {plan_id}")
        assert subscription_store.has_unresolved_plan_quarantine("sandbox", "PROD-S")
        subscription_store.close_plan_quarantine_confirmed(entry, "completed", plan_id="PLAN-NEW")
        assert not subscription_store.has_unresolved_plan_quarantine("sandbox", "PROD-S")


def test_update_on_resolved_entry_is_refused_not_silent():
    with _isolated():
        entry = subscription_store.open_plan_quarantine("sandbox", "PROD-S", "creation_request_started")
        subscription_store.close_plan_quarantine_confirmed(entry, "rejected_by_paypal")
        try:
            subscription_store.update_plan_quarantine(entry, "late_update")
        except subscription_store.QuarantineNotOpen:
            return
        raise AssertionError("update on a resolved entry must be refused")


# ---- 5. unknown outcome records an explicit reason --------------------------

def test_post_timeout_records_explicit_reason_and_blocks_next_attempt():
    with _isolated() as (_, db_path):
        with patch.object(paypal_sdk, "_request", side_effect=[requests.Timeout("timed out")]) as request:
            try:
                paypal_sdk._create_plan("PROD-S")
            except paypal_sdk.PlanCreationError as error:
                assert error.reason == "creation_outcome_unknown"
            else:
                raise AssertionError("timeout must be recorded as an unknown outcome")
        row = _raw_rows(db_path)[0]
        assert row["reason"] == "creation_outcome_unknown"
        assert row["plan_id"] is None
        assert row["resolved_at"] is None
        assert [entry.args[0] for entry in request.call_args_list] == ["POST"]
        with patch.object(paypal_sdk, "_request", side_effect=_no_network) as second:
            try:
                paypal_sdk._create_plan("PROD-S")
            except paypal_sdk.PlanCreationError as error:
                assert error.reason == "quarantine_blocked"
            else:
                raise AssertionError("unknown outcome must block the next POST")
        assert not second.called


# ---- 6. log content and HTTP responses --------------------------------------

def test_http_error_body_is_not_logged_and_summary_is_minimal():
    body = {"name": "UNPROCESSABLE_ENTITY", "debug_id": "DBG-123", "details": "BODY-SECRET-XYZ"}
    failing = MagicMock()
    failing.status_code = 422
    failing.json.return_value = body
    exc = requests.HTTPError(
        'PayPal API request failed with HTTP 422: {"details":"BODY-SECRET-XYZ"}', response=failing
    )
    with _isolated():
        with patch.object(paypal_server, "logger") as logger:
            with patch.object(paypal_server, "create_subscription", side_effect=exc):
                client = TestClient(paypal_server.app)
                response = client.post(
                    "/create-subscription",
                    json={"user_id": "u-log", "return_url": "http://localhost:8502/", "cancel_url": "http://localhost:8502/"},
                )
    assert response.status_code == 400
    assert "BODY-SECRET-XYZ" not in response.text
    logger.error.assert_called_once()
    args, kwargs = logger.error.call_args
    logged = repr(args) + repr(kwargs)
    assert "BODY-SECRET-XYZ" not in logged
    assert kwargs.get("exc_info") is None
    summary = args[2]
    assert summary["http_status"] == 422
    assert summary["debug_id"] == "DBG-123"
    assert summary["paypal_name"] == "UNPROCESSABLE_ENTITY"


def test_reference_code_matches_the_logged_reference():
    with _isolated():
        with patch.object(paypal_server, "logger") as logger:
            with patch.object(paypal_server, "create_subscription", side_effect=RuntimeError("internal")):
                client = TestClient(paypal_server.app)
                response = client.post(
                    "/create-subscription",
                    json={"user_id": "u-ref", "return_url": "http://localhost:8502/", "cancel_url": "http://localhost:8502/"},
                )
    reference = response.json()["detail"].split("Reference code:")[1].strip()
    args, _ = logger.error.call_args
    assert args[1] == reference
