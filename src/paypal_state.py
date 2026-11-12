"""Single source of truth for PayPal runtime state locations.

Runtime state (SQLite DB, Plan ID cache, Plan-creation lock, diagnostics log) lives in one
directory outside OneDrive:

* ``OUTPACE_PAYPAL_STATE_DIR`` if set, otherwise
* ``%LOCALAPPDATA%\\OutpaceBaseball\\paypal`` (or ``~/.local/share/OutpaceBaseball/paypal``
  where LOCALAPPDATA is not set).

Paths are computed when called, never at import, so no directory is created merely by
importing a module. ``ensure_dir`` creates and write-probes a directory explicitly.

The legacy locations (inside ``src/``) are recorded only so that an empty new state
directory is never silently treated as a fresh install. Nothing here moves or deletes files.
"""
from __future__ import annotations

import contextlib
import datetime
import json
import os
import tempfile
from pathlib import Path

STATE_DIR_ENV_VAR = "OUTPACE_PAYPAL_STATE_DIR"

_SRC_DIR = Path(__file__).resolve().parent
LEGACY_DB_PATH = _SRC_DIR / "subscriptions.db"
LEGACY_CACHE_PATH = _SRC_DIR / "paypal_resource_ids.json"


class StateDirError(RuntimeError):
    """The state directory cannot be created or is not writable."""


class StateMigrationRequired(RuntimeError):
    """The new state location is empty while legacy data still exists."""


def default_state_dir() -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    if local_app_data:
        return Path(local_app_data) / "OutpaceBaseball" / "paypal"
    return Path.home() / ".local" / "share" / "OutpaceBaseball" / "paypal"


def state_dir_from(env) -> Path:
    """Resolve the state directory from a given environment mapping (not from os.environ)."""
    override = str(env.get(STATE_DIR_ENV_VAR, "") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    local_app_data = str(env.get("LOCALAPPDATA", "") or "").strip()
    if local_app_data:
        return (Path(local_app_data) / "OutpaceBaseball" / "paypal").resolve()
    return (Path.home() / ".local" / "share" / "OutpaceBaseball" / "paypal").resolve()


def state_dir() -> Path:
    return state_dir_from(os.environ)


def db_path() -> Path:
    return state_dir() / "subscriptions.db"


def resource_cache_path() -> Path:
    return state_dir() / "paypal_resource_ids.json"


def plan_lock_path() -> Path:
    """Reference path. The lock itself is derived from the cache path in paypal_sdk."""
    return state_dir() / "paypal_plan_creation.lock"


# Written last, by a successful migration, after both the DB and the cache verify. Guards
# refuse to use a state directory that sits beside legacy data but has no marker, so a
# partial or pre-created target cannot be used silently.
MIGRATION_MARKER_NAME = "migration-complete.json"


def migration_marker_path() -> Path:
    return state_dir() / MIGRATION_MARKER_NAME


def diagnostics_log_path() -> Path:
    return state_dir() / "logs" / "paypal_return_diagnostics.jsonl"


def ensure_dir(directory: Path) -> Path:
    """Create ``directory`` if needed and confirm it is writable. Raises StateDirError."""
    directory = Path(directory)
    try:
        directory.mkdir(parents=True, exist_ok=True)
        fd, probe = tempfile.mkstemp(dir=str(directory), prefix=".write-probe-")
        os.close(fd)
        os.remove(probe)
    except OSError as error:
        raise StateDirError(
            f"PayPal state directory is not usable: {directory} ({type(error).__name__})"
        ) from error
    return directory


def same_path(first, second) -> bool:
    """Windows-aware comparison: case-insensitive, with links and '..' resolved."""
    return os.path.normcase(os.path.realpath(first)) == os.path.normcase(os.path.realpath(second))


# ---- explicit environment for child processes --------------------------------

def child_env(base=None) -> dict:
    """Environment for FastAPI child processes. The state directory is always set explicitly.

    The resolved path is written even when the variable was unset, so the child cannot
    fall back to a different default than the parent computed.
    """
    env = dict(os.environ if base is None else base)
    env[STATE_DIR_ENV_VAR] = str(state_dir_from(env))
    return env


# ---- completion marker -------------------------------------------------------

MARKER_FORMAT = "outpace-paypal-state-migration"
MARKER_VERSION = 1
_REQUIRED_MARKER_KEYS = ("format", "version", "completed_at_utc", "database", "cache")


class MigrationMarkerError(StateMigrationRequired):
    """The completion marker is missing its files, unreadable, or malformed."""


def validate_marker_file(path) -> dict:
    """Return the marker's contents if it is a complete, current-format marker. Else raise."""
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise MigrationMarkerError("completion marker is unreadable or not valid JSON") from error
    if not isinstance(data, dict):
        raise MigrationMarkerError("completion marker is not a JSON object")
    if data.get("format") != MARKER_FORMAT:
        raise MigrationMarkerError("completion marker has an unknown format")
    if data.get("version") != MARKER_VERSION:
        raise MigrationMarkerError("completion marker has an unsupported version")
    missing = [key for key in _REQUIRED_MARKER_KEYS if key not in data]
    if missing:
        raise MigrationMarkerError(f"completion marker is missing fields: {', '.join(missing)}")
    completed = data["completed_at_utc"]
    try:
        datetime.datetime.fromisoformat(completed)
    except (TypeError, ValueError) as error:
        raise MigrationMarkerError("completion marker timestamp is invalid") from error
    if not isinstance(data["database"], dict) or not isinstance(data["cache"], dict):
        raise MigrationMarkerError("completion marker sections are malformed")
    return data


def assert_migration_state() -> None:
    """Apply the completion-marker rules at the configured state location.

    * Marker present: it must be valid, and both the DB and the cache must exist. Contents
      are not compared, so normal updates to them never trip this check.
    * Marker absent and legacy data present: refuse, because the state location was never
      completed.
    * Marker absent and no legacy data: a fresh install, which is allowed.
    """
    marker = migration_marker_path()
    if os.path.lexists(marker):
        validate_marker_file(marker)
        absent = [
            name
            for name, path in (("subscriptions.db", db_path()), ("paypal_resource_ids.json", resource_cache_path()))
            if not path.exists()
        ]
        if absent:
            raise MigrationMarkerError(
                f"completion marker exists but required files are missing: {', '.join(absent)}"
            )
        return
    if LEGACY_DB_PATH.exists() or LEGACY_CACHE_PATH.exists():
        raise StateMigrationRequired(
            "Legacy PayPal data exists but the state directory has no completion marker. "
            "Run the verified migration before starting."
        )


def write_migration_marker(target_dir, report: dict) -> Path:
    """Write the completion marker atomically: temp file, fsync, then replace.

    The final name appears only after the complete content is on disk. An interrupted
    write leaves at most a temp file, which the guards ignore.
    """
    target_dir = Path(target_dir)
    final = target_dir / MIGRATION_MARKER_NAME
    if os.path.lexists(final):
        raise MigrationMarkerError("completion marker already exists; refusing to overwrite")
    body = {
        "format": MARKER_FORMAT,
        "version": MARKER_VERSION,
        "completed_at_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        **report,
    }
    target_dir.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(dir=str(target_dir), prefix=".migration-complete-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(body, handle, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        if os.path.lexists(final):
            raise MigrationMarkerError("completion marker appeared during write; refusing to overwrite")
        os.replace(temp_name, final)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(temp_name)
        raise
    validate_marker_file(final)
    return final
