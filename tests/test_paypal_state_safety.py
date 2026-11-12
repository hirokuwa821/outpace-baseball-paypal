"""State-location agreement, completion-marker validation, atomic marker write, and the
migration-failure state. Temp directories, temp DBs, and mocks only. No migration runs
against real data, no PayPal call, and no server is started.
"""
import contextlib
import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

PROJECT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_DIR / "src"
sys.path.insert(0, str(SRC_DIR))

import paypal_server
import paypal_state
import paypal_state_migration as migration
import subscription_store
from paypal_server import DatabasePathCheckRequest

STATE_ENV = paypal_state.STATE_DIR_ENV_VAR


def _report():
    return {"database": {"verified_tables": {"pending": 0}}, "cache": {"entries": 1}}


@contextlib.contextmanager
def _state_with_files(temp, *, legacy_present=False):
    """A state dir holding a DB and a cache, with legacy presence controlled by the test."""
    state = Path(temp) / "state"
    state.mkdir()
    legacy = Path(temp) / "legacy.db"
    if legacy_present:
        legacy.write_bytes(b"legacy marker")
    with patch.dict(os.environ, {STATE_ENV: str(state)}), \
         patch.object(paypal_state, "LEGACY_DB_PATH", legacy), \
         patch.object(paypal_state, "LEGACY_CACHE_PATH", Path(temp) / "legacy.json"):
        with contextlib.closing(sqlite3.connect(str(state / "subscriptions.db"))) as conn:
            conn.execute("CREATE TABLE IF NOT EXISTS placeholder (x)")
            conn.commit()
        (state / "paypal_resource_ids.json").write_text("{}", encoding="utf-8")
        yield state


def _marker_body(**overrides):
    body = {
        "format": paypal_state.MARKER_FORMAT,
        "version": paypal_state.MARKER_VERSION,
        "completed_at_utc": "2026-11-12T10:00:00+00:00",
        "database": {"verified_tables": {}},
        "cache": {"entries": 1},
    }
    body.update(overrides)
    return body


# ---- A. state-location agreement --------------------------------------------

def test_child_env_sets_the_state_dir_explicitly_when_unset():
    with patch.dict(os.environ, {}, clear=False):
        os.environ.pop(STATE_ENV, None)
        env = paypal_state.child_env()
        assert env[STATE_ENV] == str(paypal_state.default_state_dir().resolve())


def test_child_env_carries_an_explicit_override_and_leaves_os_environ_alone():
    with tempfile.TemporaryDirectory() as temp:
        with patch.dict(os.environ, {STATE_ENV: temp}):
            env = paypal_state.child_env()
            assert env[STATE_ENV] == str(Path(temp).resolve())
            assert os.environ[STATE_ENV] == temp


def test_same_path_resolves_dot_dot_and_compares_windows_style():
    with tempfile.TemporaryDirectory() as temp:
        base = Path(temp)
        assert paypal_state.same_path(base / "a" / ".." / "b", base / "b")
        if os.name == "nt":
            text = str(base)
            assert paypal_state.same_path(text.upper(), text.lower())
        assert not paypal_state.same_path(base / "a", base / "b")


def test_fingerprint_endpoint_matches_only_the_same_database_location():
    with tempfile.TemporaryDirectory() as temp:
        here, there = Path(temp) / "here.db", Path(temp) / "there.db"
        with patch.object(subscription_store, "DB_PATH", str(here)):
            mine = subscription_store.get_database_path_fingerprint()
            same = paypal_server.database_path_match_endpoint(DatabasePathCheckRequest(fingerprint=mine))
        with patch.object(subscription_store, "DB_PATH", str(there)):
            other = paypal_server.database_path_match_endpoint(DatabasePathCheckRequest(fingerprint=mine))
        assert same == {"database_path_match": True}
        assert other == {"database_path_match": False}


def test_unset_environment_gives_identical_paths_in_both_processes():
    import subprocess

    script = (
        "import json, sys; sys.path.insert(0, sys.argv[1]);"
        "import subscription_store, paypal_sdk, paypal_return_diagnostics;"
        "print(json.dumps([subscription_store.DB_PATH, str(paypal_sdk.RESOURCE_IDS_CACHE_FILE),"
        " str(paypal_return_diagnostics.DIAGNOSTIC_LOG_PATH)]))"
    )
    env = {k: v for k, v in os.environ.items() if k != STATE_ENV}
    env["PYTHON_DOTENV_DISABLED"] = "1"
    results = []
    for _ in range(2):
        proc = subprocess.run([sys.executable, "-c", script, str(SRC_DIR)], env=env,
                              capture_output=True, text=True, timeout=120)
        assert proc.returncode == 0, proc.stderr
        results.append(json.loads(proc.stdout.strip().splitlines()[-1]))
    assert results[0] == results[1]


# ---- B. completion-marker validation ----------------------------------------

def test_a_complete_current_marker_is_accepted():
    with tempfile.TemporaryDirectory() as temp:
        path = Path(temp) / "m.json"
        path.write_text(json.dumps(_marker_body()), encoding="utf-8")
        assert paypal_state.validate_marker_file(path)["version"] == paypal_state.MARKER_VERSION


def test_damaged_or_incomplete_markers_are_rejected():
    cases = {
        "empty": "",
        "not json": "{broken",
        "not an object": "[1, 2]",
        "unknown format": json.dumps(_marker_body(format="something-else")),
        "unsupported version": json.dumps(_marker_body(version=99)),
        "missing field": json.dumps({k: v for k, v in _marker_body().items() if k != "cache"}),
        "bad timestamp": json.dumps(_marker_body(completed_at_utc="yesterday")),
        "malformed section": json.dumps(_marker_body(database="not-a-dict")),
    }
    with tempfile.TemporaryDirectory() as temp:
        for label, text in cases.items():
            path = Path(temp) / "m.json"
            path.write_text(text, encoding="utf-8")
            try:
                paypal_state.validate_marker_file(path)
            except paypal_state.MigrationMarkerError:
                continue
            raise AssertionError(f"marker accepted: {label}")


def test_marker_with_files_present_passes_and_content_changes_do_not_break_it():
    with tempfile.TemporaryDirectory() as temp:
        with _state_with_files(temp) as state:
            paypal_state.write_migration_marker(state, _report())
            paypal_state.assert_migration_state()
            # Normal operation updates both files; the check looks only at structure and presence.
            (state / "paypal_resource_ids.json").write_text('{"sandbox": {"plan_id": "PLAN-NEW"}}', encoding="utf-8")
            with contextlib.closing(sqlite3.connect(str(state / "subscriptions.db"))) as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS touched (x)")
                conn.commit()
            paypal_state.assert_migration_state()


def test_marker_without_database_or_cache_is_refused():
    for missing in ("subscriptions.db", "paypal_resource_ids.json"):
        with tempfile.TemporaryDirectory() as temp:
            with _state_with_files(temp) as state:
                paypal_state.write_migration_marker(state, _report())
                (state / missing).unlink()
                try:
                    paypal_state.assert_migration_state()
                except paypal_state.MigrationMarkerError:
                    continue
                raise AssertionError(f"marker without {missing} must be refused")


def test_damaged_marker_is_refused_even_when_files_exist():
    with tempfile.TemporaryDirectory() as temp:
        with _state_with_files(temp) as state:
            (state / paypal_state.MIGRATION_MARKER_NAME).write_text("", encoding="utf-8")
            try:
                paypal_state.assert_migration_state()
            except paypal_state.MigrationMarkerError:
                return
            raise AssertionError("an empty marker must not count as complete")


def test_legacy_data_without_marker_is_refused_and_fresh_install_is_allowed():
    with tempfile.TemporaryDirectory() as temp:
        with _state_with_files(temp, legacy_present=True):
            try:
                paypal_state.assert_migration_state()
            except paypal_state.StateMigrationRequired:
                pass
            else:
                raise AssertionError("legacy data without a marker must be refused")
    with tempfile.TemporaryDirectory() as temp:
        state = Path(temp) / "fresh"
        with patch.dict(os.environ, {STATE_ENV: str(state)}), \
             patch.object(paypal_state, "LEGACY_DB_PATH", Path(temp) / "absent.db"), \
             patch.object(paypal_state, "LEGACY_CACHE_PATH", Path(temp) / "absent.json"):
            paypal_state.assert_migration_state()  # no legacy data: allowed


# ---- C. atomic marker write and interruption --------------------------------

def test_marker_write_failure_leaves_no_final_marker_and_no_temp_file():
    with tempfile.TemporaryDirectory() as temp:
        state = Path(temp) / "state"
        with patch.object(paypal_state.json, "dump", side_effect=OSError("disk full")):
            try:
                paypal_state.write_migration_marker(state, _report())
            except OSError:
                pass
            else:
                raise AssertionError("a failed write must raise")
        assert not (state / paypal_state.MIGRATION_MARKER_NAME).exists()
        assert not list(state.glob(".migration-complete-*.tmp"))


def test_interrupted_write_leftover_temp_file_is_never_treated_as_complete():
    with tempfile.TemporaryDirectory() as temp:
        with _state_with_files(temp, legacy_present=True) as state:
            (state / ".migration-complete-crash.tmp").write_text(json.dumps(_marker_body()), encoding="utf-8")
            try:
                paypal_state.assert_migration_state()
            except paypal_state.StateMigrationRequired:
                return
            raise AssertionError("a leftover temp file must not complete the migration")


def test_existing_marker_is_never_overwritten():
    with tempfile.TemporaryDirectory() as temp:
        with _state_with_files(temp) as state:
            paypal_state.write_migration_marker(state, _report())
            before = (state / paypal_state.MIGRATION_MARKER_NAME).read_text(encoding="utf-8")
            try:
                paypal_state.write_migration_marker(state, _report())
            except paypal_state.MigrationMarkerError:
                pass
            else:
                raise AssertionError("an existing marker must not be overwritten")
            assert (state / paypal_state.MIGRATION_MARKER_NAME).read_text(encoding="utf-8") == before


# ---- D. migration failure after both files were copied ----------------------

def test_marker_failure_after_copies_leaves_files_but_no_marker_and_guards_refuse():
    with tempfile.TemporaryDirectory() as temp:
        temp = Path(temp)
        legacy_db = temp / "legacy" / "subscriptions.db"
        legacy_db.parent.mkdir()
        with contextlib.closing(sqlite3.connect(str(legacy_db))) as conn:
            conn.execute("CREATE TABLE pending (token TEXT, user_id TEXT, created_at TEXT)")
            conn.commit()
        legacy_cache = temp / "legacy" / "paypal_resource_ids.json"
        legacy_cache.write_text(json.dumps({"sandbox": {"plan_id": "PLAN-S"}}), encoding="utf-8")
        state = temp / "state"
        with patch.object(paypal_state, "write_migration_marker", side_effect=OSError("marker failed")):
            try:
                migration.migrate_all(legacy_db, legacy_cache, state)
            except OSError:
                pass
            else:
                raise AssertionError("a failed marker must fail the migration")
        assert (state / "subscriptions.db").exists()
        assert (state / "paypal_resource_ids.json").exists()
        assert not (state / paypal_state.MIGRATION_MARKER_NAME).exists()
        with patch.dict(os.environ, {STATE_ENV: str(state)}), \
             patch.object(paypal_state, "LEGACY_DB_PATH", legacy_db), \
             patch.object(paypal_state, "LEGACY_CACHE_PATH", legacy_cache):
            try:
                paypal_state.assert_migration_state()
            except paypal_state.StateMigrationRequired:
                return
            raise AssertionError("files without a marker must stay refused")
