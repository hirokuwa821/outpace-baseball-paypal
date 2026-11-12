"""PayPal state directory: path resolution, shared paths across processes, empty-state
guards, and migration helpers. Temp directories and mocks only. Nothing reads or writes
the real DB, cache, logs, or the real %LOCALAPPDATA% state folder.
"""
import contextlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

PROJECT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_DIR / "src"
sys.path.insert(0, str(SRC_DIR))

import paypal_sdk
import paypal_state
import paypal_state_migration as migration
import subscription_store
from paypal_sdk import PlanCreationError

STATE_ENV = paypal_state.STATE_DIR_ENV_VAR

_PATHS_SCRIPT = r'''
import json, sys
sys.path.insert(0, sys.argv[1])
{imports}
print(json.dumps({{
    "db": subscription_store.DB_PATH,
    "cache": str(paypal_sdk.RESOURCE_IDS_CACHE_FILE),
    "lock": str(paypal_sdk._plan_lock_path()),
    "diagnostics": str(paypal_return_diagnostics.DIAGNOSTIC_LOG_PATH),
}}))
'''

STREAMLIT_IMPORTS = "import subscription_store, paypal_sdk, paypal_return_diagnostics"
FASTAPI_IMPORTS = "import paypal_server, subscription_store, paypal_sdk, paypal_return_diagnostics"


def _child_paths(imports, env_overrides, remove=()):
    env = {**os.environ, "PYTHON_DOTENV_DISABLED": "1"}
    for name in remove:
        env.pop(name, None)
    env.update(env_overrides)
    script = _PATHS_SCRIPT.format(imports=imports)
    proc = subprocess.run(
        [sys.executable, "-c", script, str(SRC_DIR)],
        env=env, capture_output=True, text=True, timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip().splitlines()[-1])


# ---- path resolution ---------------------------------------------------------

def test_env_override_moves_every_runtime_path_into_the_state_directory():
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp).resolve()
        paths = _child_paths(STREAMLIT_IMPORTS, {STATE_ENV: str(root)})
        assert Path(paths["db"]).parent == root
        assert Path(paths["cache"]).parent == root
        assert Path(paths["lock"]).parent == root
        assert Path(paths["diagnostics"]).parent.parent == root


def test_default_location_is_local_app_data_not_onedrive():
    paths = _child_paths(STREAMLIT_IMPORTS, {}, remove=(STATE_ENV,))
    expected = paypal_state.default_state_dir().resolve()
    assert Path(paths["db"]).parent == expected
    assert "OneDrive" not in paths["db"]
    assert "OneDrive" not in paths["cache"]


def test_streamlit_side_and_fastapi_side_resolve_identical_paths():
    with tempfile.TemporaryDirectory() as temp:
        env = {STATE_ENV: temp}
        streamlit_side = _child_paths(STREAMLIT_IMPORTS, env)
        fastapi_side = _child_paths(FASTAPI_IMPORTS, env)
    assert streamlit_side == fastapi_side


def test_state_dir_is_resolved_and_not_created_by_import_alone():
    with tempfile.TemporaryDirectory() as temp:
        target = Path(temp) / "not-yet-created"
        with patch.dict(os.environ, {STATE_ENV: str(target)}):
            assert paypal_state.state_dir() == target.resolve()
        assert not target.exists()


def test_ensure_dir_reports_an_unusable_path_as_state_dir_error():
    with tempfile.TemporaryDirectory() as temp:
        blocker = Path(temp) / "plain-file"
        blocker.write_text("not a directory", encoding="utf-8")
        try:
            paypal_state.ensure_dir(blocker / "child")
        except paypal_state.StateDirError:
            return
        raise AssertionError("an unusable state path must raise StateDirError")


# ---- empty-state guards ------------------------------------------------------

def test_init_creates_the_database_only_inside_the_state_directory():
    with tempfile.TemporaryDirectory() as temp:
        state = Path(temp) / "state"
        with patch.dict(os.environ, {STATE_ENV: str(state)}), \
             patch.object(paypal_state, "LEGACY_DB_PATH", Path(temp) / "absent-legacy.db"), \
             patch.object(paypal_state, "LEGACY_CACHE_PATH", Path(temp) / "absent-legacy.json"), \
             patch.object(subscription_store, "DB_PATH", str(paypal_state.db_path())):
            subscription_store.init_db()
        assert (state.resolve() / "subscriptions.db").exists()


def test_empty_state_over_legacy_data_is_refused_and_no_database_is_created():
    with tempfile.TemporaryDirectory() as temp:
        legacy = Path(temp) / "legacy.db"
        legacy.write_bytes(b"legacy marker")
        state = Path(temp) / "state"
        with patch.dict(os.environ, {STATE_ENV: str(state)}), \
             patch.object(paypal_state, "LEGACY_DB_PATH", legacy), \
             patch.object(subscription_store, "DB_PATH", str(paypal_state.db_path())):
            try:
                subscription_store.init_db()
            except paypal_state.StateMigrationRequired:
                pass
            else:
                raise AssertionError("an empty state DB beside legacy data must be refused")
        assert not (state.resolve() / "subscriptions.db").exists()


def test_plan_creation_refuses_when_state_cache_is_missing_but_legacy_cache_exists():
    with tempfile.TemporaryDirectory() as temp:
        legacy_cache = Path(temp) / "legacy.json"
        legacy_cache.write_text("{}", encoding="utf-8")
        state = Path(temp) / "state"
        with patch.dict(os.environ, {STATE_ENV: str(state)}), \
             patch.object(paypal_state, "LEGACY_CACHE_PATH", legacy_cache), \
             patch.object(paypal_sdk, "RESOURCE_IDS_CACHE_FILE", paypal_state.resource_cache_path()), \
             patch.object(paypal_sdk, "get_paypal_env", return_value="sandbox"), \
             patch.object(paypal_sdk, "get_or_create_product", return_value="PROD-S"), \
             patch.object(paypal_sdk, "_request", side_effect=AssertionError("no PayPal call allowed")) as request:
            try:
                paypal_sdk.get_or_create_plan()
            except PlanCreationError as error:
                assert error.reason == "state_migration_required"
            else:
                raise AssertionError("a missing state cache beside legacy data must stop plan creation")
        assert not request.called


# ---- migration helpers (prepared, not invoked) -------------------------------

def _make_legacy_db(path: Path) -> None:
    with patch.object(subscription_store, "DB_PATH", str(path)), \
         patch.object(paypal_state, "LEGACY_DB_PATH", path.parent / "absent.db"):
        subscription_store.init_db()
    with contextlib.closing(sqlite3.connect(str(path))) as conn:
        conn.execute(
            "INSERT INTO pending (token, user_id, created_at) VALUES ('tok-1', 'u-1', '2026-01-01T00:00:00')"
        )
        conn.commit()


def test_plan_migration_reports_without_writing():
    with tempfile.TemporaryDirectory() as temp:
        target = Path(temp) / "target"
        report = migration.plan_migration(
            legacy_db=Path(temp) / "legacy.db",
            legacy_cache=Path(temp) / "legacy.json",
            target_dir=target,
        )
        assert report["writes_performed"] is False
        assert report["legacy_db_exists"] is False
        assert not target.exists()


def test_copy_database_verifies_and_never_modifies_the_source():
    with tempfile.TemporaryDirectory() as temp:
        source = Path(temp) / "legacy" / "subscriptions.db"
        source.parent.mkdir()
        _make_legacy_db(source)
        before = migration.file_sha256(source)
        destination = Path(temp) / "target" / "subscriptions.db"
        counts = migration.copy_database(source, destination)
        assert counts["verified_tables"]["pending"] == 1
        assert migration.file_sha256(source) == before
        with contextlib.closing(sqlite3.connect(str(destination))) as conn:
            assert conn.execute("SELECT token FROM pending").fetchall() == [("tok-1",)]


def test_copy_database_refuses_overwrite_and_missing_source():
    with tempfile.TemporaryDirectory() as temp:
        source = Path(temp) / "legacy" / "subscriptions.db"
        source.parent.mkdir()
        _make_legacy_db(source)
        destination = Path(temp) / "target" / "subscriptions.db"
        destination.parent.mkdir()
        destination.write_bytes(b"existing data")
        for call in (
            lambda: migration.copy_database(source, destination),
            lambda: migration.copy_database(Path(temp) / "absent.db", Path(temp) / "x.db"),
        ):
            try:
                call()
            except migration.MigrationError:
                continue
            raise AssertionError("refused copy must raise MigrationError")
        assert destination.read_bytes() == b"existing data"


def test_failed_verification_removes_only_the_destination_it_created():
    with tempfile.TemporaryDirectory() as temp:
        source = Path(temp) / "legacy" / "subscriptions.db"
        source.parent.mkdir()
        _make_legacy_db(source)
        before = migration.file_sha256(source)
        destination = Path(temp) / "target" / "subscriptions.db"
        with patch.object(migration, "verify_database_copy", side_effect=migration.MigrationError("mismatch")):
            try:
                migration.copy_database(source, destination)
            except migration.MigrationError:
                pass
            else:
                raise AssertionError("a failed verification must raise")
        assert not destination.exists()
        assert migration.file_sha256(source) == before


def test_copy_cache_validates_json_and_never_modifies_the_source():
    with tempfile.TemporaryDirectory() as temp:
        (Path(temp) / "legacy").mkdir()
        source = Path(temp) / "legacy" / "legacy.json"
        source.write_text(json.dumps({"sandbox": {"product_id": "PROD-S", "plan_id": "PLAN-S"}}), encoding="utf-8")
        before = migration.file_sha256(source)
        destination = Path(temp) / "target" / "paypal_resource_ids.json"
        assert migration.copy_cache(source, destination) == {"entries": 1}
        assert json.loads(destination.read_text(encoding="utf-8"))["sandbox"]["plan_id"] == "PLAN-S"
        assert migration.file_sha256(source) == before

        not_an_object = Path(temp) / "legacy" / "list.json"
        not_an_object.write_text("[1, 2]", encoding="utf-8")
        try:
            migration.copy_cache(not_an_object, Path(temp) / "target" / "other.json")
        except migration.MigrationError:
            pass
        else:
            raise AssertionError("a non-object cache must be refused")

        try:
            migration.copy_cache(source, destination)
        except migration.MigrationError:
            return
        raise AssertionError("an existing destination must not be overwritten")


# ---- completeness marker and audit gaps -------------------------------------

def test_db_guard_refuses_a_pre_created_target_until_the_marker_exists():
    with tempfile.TemporaryDirectory() as temp:
        temp = Path(temp)
        legacy = temp / "legacy.db"
        legacy.write_bytes(b"legacy marker")
        state = temp / "state"
        with patch.dict(os.environ, {STATE_ENV: str(state)}), \
             patch.object(paypal_state, "LEGACY_DB_PATH", temp / "absent.db"), \
             patch.object(paypal_state, "LEGACY_CACHE_PATH", temp / "absent.json"), \
             patch.object(subscription_store, "DB_PATH", str(paypal_state.db_path())):
            subscription_store.init_db()  # a DB now exists in the target, as if created early
        with patch.dict(os.environ, {STATE_ENV: str(state)}), \
             patch.object(paypal_state, "LEGACY_DB_PATH", legacy), \
             patch.object(paypal_state, "LEGACY_CACHE_PATH", temp / "absent.json"), \
             patch.object(subscription_store, "DB_PATH", str(paypal_state.db_path())):
            try:
                subscription_store.init_db()
            except paypal_state.StateMigrationRequired:
                pass
            else:
                raise AssertionError("a pre-created target beside legacy data must be refused")
            (state / "paypal_resource_ids.json").write_text("{}", encoding="utf-8")
            paypal_state.write_migration_marker(state, {"database": {}, "cache": {"entries": 0}})
            subscription_store.init_db()  # valid marker and both files present: allowed


def test_plan_creation_refuses_without_marker_even_when_state_cache_exists():
    with tempfile.TemporaryDirectory() as temp:
        temp = Path(temp)
        legacy_cache = temp / "legacy.json"
        legacy_cache.write_text("{}", encoding="utf-8")
        state = temp / "state"
        state.mkdir()
        (state / "paypal_resource_ids.json").write_text("{}", encoding="utf-8")
        with patch.dict(os.environ, {STATE_ENV: str(state)}), \
             patch.object(paypal_state, "LEGACY_CACHE_PATH", legacy_cache), \
             patch.object(paypal_sdk, "RESOURCE_IDS_CACHE_FILE", paypal_state.resource_cache_path()), \
             patch.object(paypal_sdk, "get_paypal_env", return_value="sandbox"), \
             patch.object(paypal_sdk, "get_or_create_product", return_value="PROD-S"), \
             patch.object(paypal_sdk, "_request", side_effect=AssertionError("no PayPal call allowed")) as request:
            try:
                paypal_sdk.get_or_create_plan()
            except PlanCreationError as error:
                assert error.reason == "state_migration_required"
            else:
                raise AssertionError("no marker beside legacy data must stop plan creation")
        assert not request.called


def test_verification_detects_an_extra_index_on_the_destination():
    with tempfile.TemporaryDirectory() as temp:
        source = Path(temp) / "legacy" / "subscriptions.db"
        source.parent.mkdir()
        _make_legacy_db(source)
        destination = Path(temp) / "target" / "subscriptions.db"
        migration.copy_database(source, destination)
        with contextlib.closing(sqlite3.connect(str(destination))) as conn:
            conn.execute("CREATE INDEX extra_idx ON pending(user_id)")
            conn.commit()
        try:
            migration.verify_database_copy(source, destination)
        except migration.MigrationError:
            return
        raise AssertionError("schema differences must fail verification")


def test_verification_detects_changed_row_contents():
    with tempfile.TemporaryDirectory() as temp:
        source = Path(temp) / "legacy" / "subscriptions.db"
        source.parent.mkdir()
        _make_legacy_db(source)
        destination = Path(temp) / "target" / "subscriptions.db"
        migration.copy_database(source, destination)
        with contextlib.closing(sqlite3.connect(str(destination))) as conn:
            conn.execute("UPDATE pending SET user_id = 'changed' WHERE token = 'tok-1'")
            conn.commit()
        try:
            migration.verify_database_copy(source, destination)
        except migration.MigrationError:
            return
        raise AssertionError("changed contents must fail verification")


def test_same_folder_destination_is_refused_and_source_is_untouched():
    with tempfile.TemporaryDirectory() as temp:
        source = Path(temp) / "subscriptions.db"
        _make_legacy_db(source)
        before = migration.file_sha256(source)
        other = Path(temp) / "copy-here.db"
        try:
            migration.copy_database(source, other)
        except migration.MigrationError:
            pass
        else:
            raise AssertionError("a destination in the source folder must be refused")
        assert not other.exists()
        assert migration.file_sha256(source) == before


def test_wal_source_is_refused_before_any_copy():
    with tempfile.TemporaryDirectory() as temp:
        source_dir = Path(temp) / "legacy"
        source_dir.mkdir()
        source = source_dir / "subscriptions.db"
        _make_legacy_db(source)
        with contextlib.closing(sqlite3.connect(str(source))) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
        destination = Path(temp) / "target" / "subscriptions.db"
        try:
            migration.copy_database(source, destination)
        except migration.MigrationError:
            pass
        else:
            raise AssertionError("a WAL-mode source must be refused")
        assert not destination.exists()


def test_cache_shape_is_validated_before_copying():
    with tempfile.TemporaryDirectory() as temp:
        bad_shapes = [
            {"sandbox": "PLAN-S"},
            {"sandbox": {"plan_id": ""}},
            {"sandbox": {"plan_id": 42}},
        ]
        (Path(temp) / "legacy").mkdir()
        for index, shape in enumerate(bad_shapes):
            source = Path(temp) / "legacy" / f"bad-{index}.json"
            source.write_text(json.dumps(shape), encoding="utf-8")
            destination = Path(temp) / "target" / f"out-{index}.json"
            try:
                migration.copy_cache(source, destination)
            except migration.MigrationError:
                assert not destination.exists()
                continue
            raise AssertionError(f"malformed cache accepted: {shape}")


def test_migrate_all_copies_both_files_then_writes_the_marker_last():
    with tempfile.TemporaryDirectory() as temp:
        temp = Path(temp)
        legacy_db = temp / "legacy" / "subscriptions.db"
        legacy_db.parent.mkdir()
        _make_legacy_db(legacy_db)
        legacy_cache = temp / "legacy" / "paypal_resource_ids.json"
        legacy_cache.write_text(
            json.dumps({"sandbox": {"product_id": "PROD-S", "plan_id": "PLAN-S"}}), encoding="utf-8"
        )
        db_before, cache_before = migration.file_sha256(legacy_db), migration.file_sha256(legacy_cache)
        state = temp / "state"
        report = migration.migrate_all(legacy_db, legacy_cache, state)
        assert report["cache"] == {"entries": 1}
        assert (state / "subscriptions.db").exists()
        assert (state / "paypal_resource_ids.json").exists()
        assert (state / paypal_state.MIGRATION_MARKER_NAME).exists()
        assert migration.file_sha256(legacy_db) == db_before
        assert migration.file_sha256(legacy_cache) == cache_before


def test_migrate_all_removes_only_its_own_db_when_the_cache_step_fails():
    with tempfile.TemporaryDirectory() as temp:
        temp = Path(temp)
        legacy_db = temp / "legacy" / "subscriptions.db"
        legacy_db.parent.mkdir()
        _make_legacy_db(legacy_db)
        legacy_cache = temp / "legacy" / "paypal_resource_ids.json"
        legacy_cache.write_text("[1, 2]", encoding="utf-8")  # not a JSON object
        db_before = migration.file_sha256(legacy_db)
        state = temp / "state"
        try:
            migration.migrate_all(legacy_db, legacy_cache, state)
        except migration.MigrationError:
            pass
        else:
            raise AssertionError("a failing cache step must fail the migration")
        assert not (state / "subscriptions.db").exists()
        assert not (state / paypal_state.MIGRATION_MARKER_NAME).exists()
        assert migration.file_sha256(legacy_db) == db_before


def test_migrate_all_refuses_when_the_target_already_has_files_and_writes_nothing():
    with tempfile.TemporaryDirectory() as temp:
        temp = Path(temp)
        legacy_db = temp / "legacy" / "subscriptions.db"
        legacy_db.parent.mkdir()
        _make_legacy_db(legacy_db)
        legacy_cache = temp / "legacy" / "paypal_resource_ids.json"
        legacy_cache.write_text(json.dumps({"sandbox": {"plan_id": "PLAN-S"}}), encoding="utf-8")
        state = temp / "state"
        state.mkdir()
        (state / "paypal_resource_ids.json").write_text("{}", encoding="utf-8")
        try:
            migration.migrate_all(legacy_db, legacy_cache, state)
        except migration.MigrationError:
            pass
        else:
            raise AssertionError("an existing target cache must stop the migration")
        assert not (state / "subscriptions.db").exists()
        assert (state / "paypal_resource_ids.json").read_text(encoding="utf-8") == "{}"


def test_migrated_state_keeps_plan_ids_quarantine_and_schema():
    with tempfile.TemporaryDirectory() as temp:
        temp = Path(temp)
        legacy_db = temp / "legacy" / "subscriptions.db"
        legacy_db.parent.mkdir()
        _make_legacy_db(legacy_db)
        with patch.object(subscription_store, "DB_PATH", str(legacy_db)):
            entry = subscription_store.open_plan_quarantine("sandbox", "PROD-S", "creation_outcome_unknown")
        legacy_cache = temp / "legacy" / "paypal_resource_ids.json"
        legacy_cache.write_text(
            json.dumps({"sandbox": {"product_id": "PROD-S", "plan_id": "PLAN-S"}}), encoding="utf-8"
        )
        state = temp / "state"
        migration.migrate_all(legacy_db, legacy_cache, state)
        with patch.dict(os.environ, {STATE_ENV: str(state)}), \
             patch.object(subscription_store, "DB_PATH", str(paypal_state.db_path())), \
             patch.object(paypal_state, "LEGACY_DB_PATH", legacy_db):
            subscription_store.init_db()  # marker present, so allowed
            rows = subscription_store.list_unresolved_plan_quarantine()
            assert [row["entry_id"] for row in rows] == [entry]
            assert rows[0]["product_id"] == "PROD-S"
            assert rows[0]["reason"] == "creation_outcome_unknown"
            subscription_store.verify_quarantine_schema()
            cache = json.loads((state / "paypal_resource_ids.json").read_text(encoding="utf-8"))
            assert cache == {"sandbox": {"product_id": "PROD-S", "plan_id": "PLAN-S"}}
