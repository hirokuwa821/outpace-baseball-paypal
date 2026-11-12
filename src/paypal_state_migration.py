"""Migration helpers for PayPal runtime state. Prepared only: nothing calls these automatically.

Rules enforced here:
* The source is opened read-only and is never modified, moved, or deleted.
* Destinations are claimed with an exclusive create, so an existing file, a link, or a file
  that appears during the run is never overwritten, and cleanup removes only a file this
  call created.
* The source must use plain ``delete`` journaling, so a read-only open creates no sidecars.
* Success requires an integrity check, identical schema, and identical row contents.
* ``migrate_all`` copies the DB, then the cache, and writes the completion marker last.
  If the cache step fails, the DB copied by that run is removed.

Usage (manual, only after stopping Streamlit and uvicorn):
    plan_migration()                       # dry run, writes nothing
    migrate_all(LEGACY_DB, LEGACY_CACHE, state_dir)
Legacy files are kept for rollback. Point OUTPACE_PAYPAL_STATE_DIR at the state dir to switch.
"""
from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path

import paypal_state

DB_FILE_NAME = "subscriptions.db"
CACHE_FILE_NAME = "paypal_resource_ids.json"


class MigrationError(RuntimeError):
    """A migration step was refused or failed verification."""


def plan_migration(legacy_db=None, legacy_cache=None, target_dir=None) -> dict:
    """Report what a migration would see. Reads metadata only and writes nothing."""
    legacy_db = Path(legacy_db) if legacy_db else paypal_state.LEGACY_DB_PATH
    legacy_cache = Path(legacy_cache) if legacy_cache else paypal_state.LEGACY_CACHE_PATH
    target = Path(target_dir) if target_dir else paypal_state.state_dir()
    return {
        "legacy_db": str(legacy_db),
        "legacy_db_exists": legacy_db.is_file(),
        "legacy_cache": str(legacy_cache),
        "legacy_cache_exists": legacy_cache.is_file(),
        "target_dir": str(target),
        "target_dir_exists": target.is_dir(),
        "target_db_exists": os.path.lexists(target / DB_FILE_NAME),
        "target_cache_exists": os.path.lexists(target / CACHE_FILE_NAME),
        "target_marker_exists": os.path.lexists(target / paypal_state.MIGRATION_MARKER_NAME),
        "writes_performed": False,
    }


# ---- shared checks ----------------------------------------------------------

def _readonly_uri(path: Path) -> str:
    return path.resolve().as_uri() + "?mode=ro"


def _remove_if_exists(path: Path) -> None:
    with contextlib.suppress(FileNotFoundError):
        os.remove(path)


def _refuse_unsafe_destination(source: Path, destination: Path) -> None:
    """Refuse same-file, same-folder, nested-folder, and already-present destinations."""
    if paypal_state.same_path(source, destination):
        raise MigrationError("source and destination are the same file")
    if os.path.lexists(destination):
        raise MigrationError("destination already exists or is a link; refusing to overwrite")
    src_dir = os.path.normcase(os.path.realpath(source.parent))
    dst_dir = os.path.normcase(os.path.realpath(destination.parent))
    if src_dir == dst_dir:
        raise MigrationError("destination must be a different folder from the source")
    if os.path.commonpath([src_dir, dst_dir]) in (src_dir, dst_dir):
        raise MigrationError("source and destination folders must not be nested in each other")


def _assert_plain_journal(source: Path) -> None:
    with contextlib.closing(sqlite3.connect(_readonly_uri(source), uri=True)) as conn:
        mode = str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    if mode != "delete":
        raise MigrationError(f"source journal_mode is {mode!r}; only 'delete' is supported")


def _claim_destination(destination: Path) -> None:
    """Create the destination exclusively. Fails if anything exists, including a link."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(
            str(destination),
            os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0),
        )
    except FileExistsError as error:
        raise MigrationError("destination appeared; refusing to overwrite") from error
    os.close(fd)


# ---- database ---------------------------------------------------------------

def _schema(path: Path) -> list:
    with contextlib.closing(sqlite3.connect(_readonly_uri(path), uri=True)) as conn:
        return conn.execute(
            "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
        ).fetchall()


def _user_tables(path: Path) -> list:
    with contextlib.closing(sqlite3.connect(_readonly_uri(path), uri=True)) as conn:
        return [
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]


def _table_contents(path: Path, table: str) -> list:
    with contextlib.closing(sqlite3.connect(_readonly_uri(path), uri=True)) as conn:
        rows = conn.execute(f'SELECT * FROM "{table}"').fetchall()
    return sorted(repr(tuple(row)) for row in rows)


def verify_database_copy(source: Path, destination: Path) -> dict:
    """Integrity, schema, and full row-content comparison. Raises MigrationError on mismatch."""
    with contextlib.closing(sqlite3.connect(str(destination))) as conn:
        integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if integrity != "ok":
        raise MigrationError(f"destination failed integrity_check: {integrity}")
    if _schema(source) != _schema(destination):
        raise MigrationError("destination schema differs from source")
    tables = _user_tables(source)
    if tables != _user_tables(destination):
        raise MigrationError("destination tables differ from source")
    counts = {}
    for table in tables:
        expected = _table_contents(source, table)
        if expected != _table_contents(destination, table):
            raise MigrationError(f"contents of table {table} differ from source")
        counts[table] = len(expected)
    return {"verified_tables": counts}


def copy_database(source, destination) -> dict:
    """Copy the SQLite DB with the backup API, then verify. Source is opened read-only."""
    source, destination = Path(source), Path(destination)
    if not source.is_file():
        raise MigrationError("source database was not found")
    _refuse_unsafe_destination(source, destination)
    _assert_plain_journal(source)
    _claim_destination(destination)  # from here on, this call owns the destination file
    try:
        with contextlib.closing(sqlite3.connect(_readonly_uri(source), uri=True)) as src:
            with contextlib.closing(sqlite3.connect(str(destination))) as dst:
                src.backup(dst)
        return verify_database_copy(source, destination)
    except BaseException:
        _remove_if_exists(destination)
        raise


# ---- cache ------------------------------------------------------------------

def _validate_cache(data: object) -> None:
    """Shape written by paypal_sdk: {environment: {resource_name: non-empty string}}."""
    if not isinstance(data, dict):
        raise MigrationError("cache must be a JSON object")
    for environment, resources in data.items():
        if not isinstance(environment, str) or not isinstance(resources, dict):
            raise MigrationError("cache environments must map to JSON objects")
        for name, value in resources.items():
            if not isinstance(name, str) or not isinstance(value, str) or not value:
                raise MigrationError("cache resource IDs must be non-empty strings")


def copy_cache(source, destination) -> dict:
    """Copy the Plan ID cache as validated JSON. Source is only read."""
    source, destination = Path(source), Path(destination)
    if not source.is_file():
        raise MigrationError("source cache was not found")
    _refuse_unsafe_destination(source, destination)
    try:
        data = json.loads(source.read_text(encoding="utf-8"))
    except (ValueError, OSError) as error:
        raise MigrationError("source cache is not readable JSON") from error
    _validate_cache(data)

    _claim_destination(destination)  # placeholder owned by this call
    fd, temp_name = tempfile.mkstemp(dir=str(destination.parent), prefix=".cache-copy-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, destination)  # replaces only the placeholder this call owns
    except BaseException:
        _remove_if_exists(Path(temp_name))
        _remove_if_exists(destination)
        raise
    try:
        if json.loads(destination.read_text(encoding="utf-8")) != data:
            raise MigrationError("cache copy did not verify")
    except BaseException:
        _remove_if_exists(destination)
        raise
    return {"entries": len(data)}


# ---- combined migration -----------------------------------------------------

def _write_marker(target: Path, report: dict) -> None:
    # Atomic: temp file, fsync, then replace. See paypal_state.write_migration_marker.
    paypal_state.write_migration_marker(target, report)


def migrate_all(legacy_db, legacy_cache, target_dir) -> dict:
    """DB first, then cache, then the completion marker. Refuses before any write if needed."""
    legacy_db, legacy_cache, target = Path(legacy_db), Path(legacy_cache), Path(target_dir)
    db_destination = target / DB_FILE_NAME
    cache_destination = target / CACHE_FILE_NAME
    for path in (db_destination, cache_destination, target / paypal_state.MIGRATION_MARKER_NAME):
        if os.path.lexists(path):
            raise MigrationError(f"{path.name} already exists in the target; nothing was written")

    db_report = copy_database(legacy_db, db_destination)
    try:
        cache_report = copy_cache(legacy_cache, cache_destination)
    except BaseException:
        _remove_if_exists(db_destination)  # the DB this run copied; legacy files untouched
        raise
    report = {"database": db_report, "cache": cache_report}
    _write_marker(target, report)
    return report


def file_sha256(path) -> str:
    """Hash for checking that a source file was not changed by a migration step."""
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()
