"""Lock deadline, lock-file path and flags, DB initialisation, and schema verification.

Temp DBs, temp cache directories, and mocks only. No PayPal calls. The real DB is never
opened: each test asserts its temp path differs from the real one.
"""
import asyncio
import contextlib
import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_DIR / "src"
sys.path.insert(0, str(SRC_DIR))

import paypal_sdk
import paypal_server
import subscription_store

REAL_DB_PATH = (SRC_DIR / "subscriptions.db").resolve()


def _no_network(*args, **kwargs):
    raise AssertionError("test attempted a real network call")


@contextlib.contextmanager
def _temp_env(initialise=True):
    """Yield (cache_file, db_path). DB is initialised only if asked; network is blocked."""
    with tempfile.TemporaryDirectory() as temp_dir:
        cache_file = Path(temp_dir) / "paypal_resource_ids.json"
        db_path = Path(temp_dir) / "test_subscriptions.db"
        assert db_path.resolve() != REAL_DB_PATH
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(subscription_store, "DB_PATH", str(db_path)))
            if initialise:
                subscription_store.init_db()
            stack.enter_context(patch.object(paypal_sdk, "RESOURCE_IDS_CACHE_FILE", cache_file))
            stack.enter_context(patch.object(paypal_sdk, "get_paypal_env", return_value="sandbox"))
            stack.enter_context(patch.object(paypal_sdk, "get_or_create_product", return_value="PROD-S"))
            stack.enter_context(patch.object(paypal_sdk.requests, "request", side_effect=_no_network))
            stack.enter_context(patch.object(paypal_sdk.requests, "post", side_effect=_no_network))
            yield cache_file, db_path


def _table_names(db_path):
    with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _index_names(db_path):
    with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
        return {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}


def _expect_reason(reason, fn):
    try:
        fn()
    except paypal_sdk.PlanCreationError as error:
        assert error.reason == reason, (error.reason, reason)
        return
    raise AssertionError(f"expected PlanCreationError with reason {reason!r}")


# ---- 1. one absolute deadline for both waits --------------------------------

def test_lock_wait_holds_to_one_deadline_when_thread_lock_is_busy():
    with _temp_env():
        holder_ready, release = threading.Event(), threading.Event()

        def holder():
            with paypal_sdk._plan_creation_lock():
                holder_ready.set()
                release.wait(10)

        thread = threading.Thread(target=holder)
        thread.start()
        assert holder_ready.wait(5)
        try:
            with patch.object(paypal_sdk, "PLAN_CREATION_LOCK_WAIT_SECONDS", 0.5):
                started = time.monotonic()
                with patch.object(paypal_sdk, "_request", side_effect=_no_network) as request:
                    _expect_reason("lock_timeout", paypal_sdk.get_or_create_plan)
                elapsed = time.monotonic() - started
        finally:
            release.set()
            thread.join(5)
        assert elapsed < 1.5, elapsed
        assert not request.called


def test_lock_wait_holds_to_one_deadline_when_os_lock_is_busy():
    with _temp_env():
        # Hold only the OS-level lock from a separate descriptor, leaving the thread lock free.
        fd = paypal_sdk._open_plan_lock_file()
        try:
            assert paypal_sdk._try_lock_fd(fd)
            with patch.object(paypal_sdk, "PLAN_CREATION_LOCK_WAIT_SECONDS", 0.5):
                started = time.monotonic()
                with patch.object(paypal_sdk, "_request", side_effect=_no_network) as request:
                    _expect_reason("lock_timeout", paypal_sdk.get_or_create_plan)
                elapsed = time.monotonic() - started
            assert elapsed < 1.5, elapsed
            assert not request.called
        finally:
            paypal_sdk._unlock_fd(fd)
            os.close(fd)
        assert not paypal_sdk._PLAN_THREAD_LOCK.locked()


def test_lock_is_released_after_an_exception_inside_the_critical_section():
    with _temp_env():
        try:
            with paypal_sdk._plan_creation_lock():
                raise RuntimeError("boom")
        except RuntimeError:
            pass
        assert not paypal_sdk._PLAN_THREAD_LOCK.locked()
        fd = paypal_sdk._open_plan_lock_file()
        try:
            assert paypal_sdk._try_lock_fd(fd)
            paypal_sdk._unlock_fd(fd)
        finally:
            os.close(fd)


def test_default_wait_budget_is_within_the_streamlit_timeout():
    assert paypal_sdk.PLAN_CREATION_LOCK_WAIT_SECONDS <= 60.0 < 180.0


# ---- 2. lock file path and flags ---------------------------------------------

def test_lock_path_is_resolved_and_binary_flag_is_used():
    with _temp_env():
        path = paypal_sdk._plan_lock_path()
        assert path.is_absolute()
        assert path == path.resolve()
        assert path.name == "paypal_plan_creation.lock"
        real_open = os.open
        with patch("os.open", side_effect=real_open) as opener:
            fd = paypal_sdk._open_plan_lock_file()
            os.close(fd)
        flags = opener.call_args.args[1]
        assert flags & (os.O_RDWR | os.O_CREAT)
        assert flags & getattr(os, "O_BINARY", 0) == getattr(os, "O_BINARY", 0)


def test_lock_file_open_failure_stops_before_cache_read_and_post():
    with _temp_env():
        with patch.object(paypal_sdk, "_open_plan_lock_file", side_effect=OSError("sharing violation")):
            with patch.object(paypal_sdk, "_cached_resource_id") as cache_read:
                with patch.object(paypal_sdk, "_request", side_effect=_no_network) as request:
                    _expect_reason("lock_unavailable", paypal_sdk.get_or_create_plan)
        assert not cache_read.called
        assert not request.called


# ---- 3. DB initialisation -----------------------------------------------------

def test_index_creation_failure_fails_init_and_rolls_back_the_whole_schema():
    with _temp_env(initialise=False) as (_, db_path):
        broken = "CREATE UNIQUE INDEX ux_plan_quarantine_open ON no_such_table(environment) WHERE resolved_at IS NULL"
        with patch.object(subscription_store, "_QUARANTINE_OPEN_INDEX_SQL", broken):
            try:
                subscription_store.init_db()
            except sqlite3.OperationalError:
                pass
            else:
                raise AssertionError("init must fail when the index cannot be created")
        tables = _table_names(db_path)
        assert "subscriptions" not in tables
        assert "paypal_plan_quarantine" not in tables


def test_init_is_idempotent_and_preserves_existing_rows():
    with _temp_env() as (_, db_path):
        with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
            conn.execute(
                "INSERT INTO subscriptions (subscription_id, user_id, status, created_at, last_checked) "
                "VALUES ('I-1', 'u1', 'ACTIVE', '2026-01-01', '2026-01-01')"
            )
            conn.commit()
        subscription_store.init_db()
        subscription_store.init_db()
        with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
            rows = conn.execute("SELECT subscription_id, user_id, status FROM subscriptions").fetchall()
        assert rows == [("I-1", "u1", "ACTIVE")]
        subscription_store.verify_quarantine_schema()


def test_init_creates_the_partial_unique_index():
    with _temp_env() as (_, db_path):
        assert "ux_plan_quarantine_open" in _index_names(db_path)


# ---- 4. schema verification --------------------------------------------------

def test_verify_rejects_a_missing_index():
    with _temp_env() as (_, db_path):
        with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
            conn.execute("DROP INDEX ux_plan_quarantine_open")
            conn.commit()
        try:
            subscription_store.verify_quarantine_schema()
        except subscription_store.SchemaError:
            return
        raise AssertionError("missing index must fail verification")


def test_verify_rejects_a_non_unique_index_with_the_same_name():
    with _temp_env() as (_, db_path):
        with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
            conn.execute("DROP INDEX ux_plan_quarantine_open")
            conn.execute(
                "CREATE INDEX ux_plan_quarantine_open ON paypal_plan_quarantine(environment, product_id) "
                "WHERE resolved_at IS NULL"
            )
            conn.commit()
        try:
            subscription_store.verify_quarantine_schema()
        except subscription_store.SchemaError:
            return
        raise AssertionError("a non-unique index must not pass as the partial unique index")


def test_verify_rejects_a_missing_table():
    with _temp_env(initialise=False):
        try:
            subscription_store.verify_quarantine_schema()
        except subscription_store.SchemaError:
            return
        raise AssertionError("an uninitialised database must fail verification")


# ---- 5. no plan creation without a valid schema ------------------------------

def test_plan_creation_on_an_uninitialised_database_never_posts():
    with _temp_env(initialise=False):
        with patch.object(paypal_sdk, "_request", side_effect=_no_network) as request:
            _expect_reason("quarantine_check_failed", lambda: paypal_sdk._create_plan("PROD-S"))
        assert not request.called


def test_plan_creation_with_a_missing_index_never_posts():
    with _temp_env() as (_, db_path):
        with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
            conn.execute("DROP INDEX ux_plan_quarantine_open")
            conn.commit()
        with patch.object(paypal_sdk, "_request", side_effect=_no_network) as request:
            _expect_reason("quarantine_check_failed", lambda: paypal_sdk._create_plan("PROD-S"))
        assert not request.called


# ---- 6. startup behaviour ----------------------------------------------------

def test_lifespan_runs_init_once_before_serving():
    calls = []

    async def run():
        with patch.object(subscription_store, "init_db", side_effect=lambda: calls.append("init")):
            async with paypal_server._lifespan(paypal_server.app):
                calls.append("serving")

    asyncio.run(run())
    assert calls == ["init", "serving"]


def test_lifespan_failure_stops_startup_before_serving():
    async def run():
        with patch.object(subscription_store, "init_db", side_effect=RuntimeError("init failed")):
            async with paypal_server._lifespan(paypal_server.app):
                raise AssertionError("server must not serve after a failed init")

    try:
        asyncio.run(run())
    except RuntimeError as error:
        assert "init failed" in str(error)
        return
    raise AssertionError("a failed init must abort startup")
