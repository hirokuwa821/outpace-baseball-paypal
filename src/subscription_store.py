# subscription_store.py – SQLite based persistence for PayPal subscriptions
"""Simple SQLite wrapper to store subscription ↔ user mappings and status.

The DB file is created next to this module ("subscriptions.db").
Only the fields needed for the basic flow are persisted:
* subscription_id (PayPal subscription identifier)
* user_id        (our internal anonymous identifier)
* status         (PayPal status string, e.g. "ACTIVE")
* created_at     (ISO‑8601 timestamp when the row was first inserted)
* last_checked   (ISO‑8601 timestamp of the latest status verification)
"""

import os
import sqlite3
import datetime
import hashlib
from contextlib import closing
from pathlib import Path
from typing import Optional, Dict, Any

# Path to the SQLite database file (can be overridden in tests)
import paypal_state

# Lives in the PayPal state directory (outside OneDrive), resolved at import.
DB_PATH = str(paypal_state.db_path())
PENDING_TOKEN_TTL = datetime.timedelta(hours=24)
PENDING_DIAGNOSTIC_RETENTION = datetime.timedelta(days=30)


def _utcnow() -> datetime.datetime:
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)


# Seconds to wait for a competing writer before SQLite raises "database is locked".
# Explicit so that startup contention is handled deliberately rather than by the default.
SQLITE_BUSY_TIMEOUT_SECONDS = 10.0


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        DB_PATH,
        detect_types=sqlite3.PARSE_DECLTYPES,
        timeout=SQLITE_BUSY_TIMEOUT_SECONDS,
    )
    conn.row_factory = sqlite3.Row
    return conn


def get_database_path_fingerprint() -> str:
    """Return a stable fingerprint for comparing DB paths without disclosing them."""
    normalized_path = os.path.normcase(os.path.realpath(DB_PATH))
    return hashlib.sha256(normalized_path.encode("utf-8")).hexdigest()


def _token_fingerprint(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _record_pending_state(
    conn: sqlite3.Connection, token: str, state: str
) -> None:
    conn.execute(
        """
        INSERT INTO pending_token_history (token_hash, state, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(token_hash) DO UPDATE SET
            state = excluded.state,
            updated_at = excluded.updated_at
        """,
        (_token_fingerprint(token), state, _utcnow().isoformat()),
    )


def _refuse_unmigrated_state_over_legacy() -> None:
    """Refuse to use the state location while legacy data exists but no migration marker does.

    A missing DB, or a pre-created or partially copied one, would otherwise hide existing
    subscriptions and pending tokens. The marker is written only after a verified migration.
    """
    if not paypal_state.same_path(DB_PATH, paypal_state.db_path()):
        return  # an explicit other location (tests, or the legacy folder itself)
    if paypal_state.same_path(DB_PATH, paypal_state.LEGACY_DB_PATH):
        return
    paypal_state.assert_migration_state()


def init_db() -> None:
    """Create the subscription, pending, and token diagnostic tables if needed.

    The ``subscriptions`` table schema is immutable for this project.
    The ``pending`` table holds a one‑time token → user_id mapping used when
    creating a subscription.
    """
    paypal_state.ensure_dir(Path(os.path.dirname(os.path.abspath(DB_PATH))))
    _refuse_unmigrated_state_over_legacy()
    with closing(_connect()) as conn:
        # One transaction for every DDL statement: a failure anywhere rolls back all of
        # it, including tables created earlier in the same call. Closing the connection
        # without commit also rolls back, so a raise below never leaves partial schema.
        conn.execute("BEGIN IMMEDIATE")
        # Subscriptions table – existing schema must stay untouched
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                subscription_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                status TEXT NOT NULL,
                created_at TEXT NOT NULL,
                last_checked TEXT NOT NULL
            )
            """
        )
        # Pending token table – created only once here
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending (
                token TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS pending_token_history (
                token_hash TEXT PRIMARY KEY,
                state TEXT NOT NULL CHECK (state IN ('expired', 'consumed')),
                updated_at TEXT NOT NULL
            )
            """
        )
        _ensure_quarantine_schema(conn)
        _verify_schema(conn)
        conn.commit()


def upsert(subscription_id: str, user_id: str, status: str) -> None:
    """Insert a new subscription row or update an existing one.

    ``created_at`` is set when the row is first inserted; ``last_checked`` is
    always updated to the current UTC time.
    """
    with closing(_connect()) as conn:
        _upsert_subscription(conn, subscription_id, user_id, status)
        conn.commit()


def _upsert_subscription(
    conn: sqlite3.Connection, subscription_id: str, user_id: str, status: str
) -> None:
    now = _utcnow().isoformat()
    conn.execute(
        """
        INSERT INTO subscriptions (subscription_id, user_id, status, created_at, last_checked)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(subscription_id) DO UPDATE SET
            user_id = excluded.user_id,
            status = excluded.status,
            last_checked = excluded.last_checked
        """,
        (subscription_id, user_id, status, now, now),
    )


def persist_verified_subscription(
    token: str, user_id: str, subscription_id: str, status: str
) -> bool:
    """Atomically consume a fresh pending token and store its verified subscription."""
    with closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT user_id, created_at FROM pending WHERE token = ?",
            (token,),
        ).fetchone()
        if row is None:
            conn.commit()
            return False
        if row["user_id"] != user_id:
            conn.commit()
            return False
        if not _is_pending_fresh(row["created_at"]):
            conn.execute("DELETE FROM pending WHERE token = ?", (token,))
            _record_pending_state(conn, token, "expired")
            conn.commit()
            return False

        _upsert_subscription(conn, subscription_id, user_id, status)
        conn.execute("DELETE FROM pending WHERE token = ?", (token,))
        _record_pending_state(conn, token, "consumed")
        conn.commit()
        return True


def store_pending(token: str, user_id: str) -> None:
    """Store a one‑time token → user_id mapping.

    The token must be a cryptographically‑secure random string generated by the
    caller (e.g. ``secrets.token_urlsafe(32)``). The function records the creation
    time in UTC to allow expiry checks.
    """
    now = _utcnow().isoformat()
    with closing(_connect()) as conn:
        cutoff = (_utcnow() - PENDING_DIAGNOSTIC_RETENTION).isoformat()
        conn.execute(
            "DELETE FROM pending_token_history WHERE updated_at < ?", (cutoff,)
        )
        conn.execute(
            "DELETE FROM pending_token_history WHERE token_hash = ?",
            (_token_fingerprint(token),),
        )
        conn.execute(
            """
            INSERT INTO pending (token, user_id, created_at) VALUES (?, ?, ?)
            """,
            (token, user_id, now),
        )
        conn.commit()


def _is_pending_fresh(created_at: str) -> bool:
    created = datetime.datetime.fromisoformat(created_at)
    age = _utcnow() - created
    return datetime.timedelta(0) <= age <= PENDING_TOKEN_TTL


def get_pending_user(token: str) -> Optional[str]:
    """Validate a pending token without consuming it; expired rows are removed."""
    state, user_id = inspect_pending_token(token)
    return user_id if state == "found" else None


def inspect_pending_token(token: str) -> tuple[str, Optional[str]]:
    """Return a safe pending-token state and its user only when it is still valid."""
    with closing(_connect()) as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT user_id, created_at FROM pending WHERE token = ?",
            (token,),
        ).fetchone()
        if row is None:
            history = conn.execute(
                "SELECT state FROM pending_token_history WHERE token_hash = ?",
                (_token_fingerprint(token),),
            ).fetchone()
            conn.commit()
            return (history["state"], None) if history else ("missing", None)

        if not _is_pending_fresh(row["created_at"]):
            conn.execute("DELETE FROM pending WHERE token = ?", (token,))
            _record_pending_state(conn, token, "expired")
            conn.commit()
            return "expired", None

        conn.commit()
        return "found", row["user_id"]


def consume_pending(token: str) -> Optional[str]:
    """Retrieve and delete a pending token.

    Returns the associated ``user_id`` if the token exists **and** was created
    within the 24-hour window. Tokens older than 24 hours are considered
    expired and ``None`` is returned. In all cases the token row is removed to
    guarantee one‑time use.
    """
    with closing(_connect()) as conn:
        # Use an IMMEDIATE transaction to lock the table for the duration of
        # the read‑delete sequence, preventing race conditions.
        conn.execute("BEGIN IMMEDIATE")
        cur = conn.execute(
            "SELECT user_id, created_at FROM pending WHERE token = ?",
            (token,),
        )
        row = cur.fetchone()
        if row:
            user_id = row["user_id"]
            # Delete the token regardless of expiry status
            conn.execute("DELETE FROM pending WHERE token = ?", (token,))
            state = "expired"
            if _is_pending_fresh(row["created_at"]):
                state = "consumed"
            _record_pending_state(conn, token, state)
            conn.commit()
            if state == "consumed":
                return user_id
        else:
            # Token not found – ensure any lingering transaction is closed
            conn.commit()
    return None


def get_by_user(user_id: str) -> Optional[Dict[str, Any]]:
    """Return the subscription row for a given ``user_id`` (first match)."""
    with closing(_connect()) as conn:
        cur = conn.execute(
            "SELECT * FROM subscriptions WHERE user_id = ? ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def get_by_subscription(subscription_id: str) -> Optional[Dict[str, Any]]:
    """Return the subscription row for a given ``subscription_id``."""
    with closing(_connect()) as conn:
        cur = conn.execute(
            "SELECT * FROM subscriptions WHERE subscription_id = ?",
            (subscription_id,),
        )
        row = cur.fetchone()
        return dict(row) if row else None


def close() -> None:
    """Placeholder – SQLite connections are opened/closed per‑call, nothing to do."""
    pass

# ---------------------------------------------------------------------------
# PayPal plan quarantine: plan-creation attempts that need operator review.
# Rows are never resolved automatically. An unresolved row blocks new plan creation
# for its environment and product until an operator calls resolve_plan_quarantine().
# ---------------------------------------------------------------------------
_QUARANTINE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS paypal_plan_quarantine (
    entry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    environment TEXT NOT NULL,
    product_id TEXT NOT NULL,
    plan_id TEXT,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolved_by TEXT,
    resolution_note TEXT
)
"""

# At most one unresolved entry per environment and product. The database enforces this,
# so two racing inserts cannot both succeed.
_QUARANTINE_OPEN_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS ux_plan_quarantine_open
ON paypal_plan_quarantine(environment, product_id)
WHERE resolved_at IS NULL
"""

_SYSTEM_CLOSE_OUTCOMES = frozenset({"completed", "rejected_by_paypal"})


class QuarantineConflict(RuntimeError):
    """An unresolved entry already exists for this environment and product."""


class QuarantineNotOpen(RuntimeError):
    """No unresolved entry matched the given identifiers. Nothing was changed."""


def _ensure_quarantine_schema(conn: sqlite3.Connection) -> None:
    """Create the quarantine table and index. Called only from init_db, never from CRUD paths."""
    conn.execute(_QUARANTINE_TABLE_SQL)
    conn.execute(_QUARANTINE_OPEN_INDEX_SQL)


class SchemaError(RuntimeError):
    """The database lacks a required table, column, or the unique open-entry index."""


_REQUIRED_TABLES = ("subscriptions", "pending", "pending_token_history", "paypal_plan_quarantine")
_REQUIRED_QUARANTINE_COLUMNS = {
    "entry_id", "environment", "product_id", "plan_id", "reason",
    "created_at", "resolved_at", "resolved_by", "resolution_note",
}


def _verify_schema(conn: sqlite3.Connection) -> None:
    """Fail unless every required table, column, and the partial unique index is present.

    Run inside init_db before commit, and by plan creation before any POST. The index
    check requires UNIQUE and the open-entry predicate, so a plain index does not pass.
    """
    names = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    missing_tables = [name for name in _REQUIRED_TABLES if name not in names]
    if missing_tables:
        raise SchemaError(f"missing tables: {', '.join(missing_tables)}")

    columns = {row[1] for row in conn.execute("PRAGMA table_info(paypal_plan_quarantine)")}
    missing_columns = sorted(_REQUIRED_QUARANTINE_COLUMNS - columns)
    if missing_columns:
        raise SchemaError(f"quarantine table is missing columns: {', '.join(missing_columns)}")

    index_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = 'ux_plan_quarantine_open' "
        "AND tbl_name = 'paypal_plan_quarantine'"
    ).fetchone()
    if index_row is None or not index_row[0]:
        raise SchemaError("required index ux_plan_quarantine_open is missing")
    index_sql = " ".join(index_row[0].split()).upper()
    if "UNIQUE" not in index_sql or "WHERE RESOLVED_AT IS NULL" not in index_sql:
        raise SchemaError("ux_plan_quarantine_open is not the required partial unique index")


def verify_quarantine_schema() -> None:
    """Read-only schema check used by plan creation before any POST."""
    with closing(_connect()) as conn:
        _verify_schema(conn)


def open_plan_quarantine(environment: str, product_id: str, reason: str) -> int:
    """Record a plan-creation attempt as unresolved before the POST. Returns its entry_id.

    Raises QuarantineConflict if an unresolved entry already exists for this
    environment and product (enforced by the partial unique index).
    """
    with closing(_connect()) as conn:
        try:
            cursor = conn.execute(
                """
                INSERT INTO paypal_plan_quarantine
                    (environment, product_id, plan_id, reason, created_at, resolved_at)
                VALUES (?, ?, NULL, ?, ?, NULL)
                """,
                (environment, product_id, reason, _utcnow().isoformat()),
            )
            conn.commit()
        except sqlite3.IntegrityError as error:
            conn.rollback()
            raise QuarantineConflict(
                "an unresolved plan quarantine entry already exists"
            ) from error
        return int(cursor.lastrowid)


def update_plan_quarantine(entry_id: int, reason: str, plan_id: Optional[str] = None) -> None:
    """Update an open entry's reason and, if known, its plan ID.

    Raises QuarantineNotOpen if the entry is not open, so a failed update is never silent.
    """
    with closing(_connect()) as conn:
        cursor = conn.execute(
            """
            UPDATE paypal_plan_quarantine
            SET reason = ?, plan_id = COALESCE(?, plan_id)
            WHERE entry_id = ? AND resolved_at IS NULL
            """,
            (reason, plan_id, entry_id),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            raise QuarantineNotOpen(f"entry {entry_id} is not an open quarantine entry")
        conn.commit()


def close_plan_quarantine_confirmed(entry_id: int, outcome: str, plan_id: Optional[str] = None) -> None:
    """System-only close for a confirmed outcome.

    ``completed``: the plan is ACTIVE and cached. ``rejected_by_paypal``: PayPal refused
    the request with a 4xx, so no plan exists. Unknown outcomes are never closed here.
    """
    if outcome not in _SYSTEM_CLOSE_OUTCOMES:
        raise ValueError(f"unsupported confirmed outcome: {outcome!r}")
    with closing(_connect()) as conn:
        cursor = conn.execute(
            """
            UPDATE paypal_plan_quarantine
            SET resolved_at = ?, resolved_by = 'system', resolution_note = ?
            WHERE entry_id = ? AND plan_id IS ? AND resolved_at IS NULL
            """,
            (_utcnow().isoformat(), outcome, entry_id, plan_id),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            raise QuarantineNotOpen(f"entry {entry_id} did not match an open record")
        conn.commit()


def resolve_plan_quarantine(
    entry_id: int,
    *,
    environment: str,
    product_id: str,
    plan_id: Optional[str],
    resolved_by: str,
    resolution_note: str,
) -> None:
    """Operator resolution. Every identifier must match, and the entry must still be open.

    Nothing is resolved automatically. Raises QuarantineNotOpen if no open entry matches
    all of entry_id, environment, product_id, and plan_id (NULL matches an unknown plan_id).
    """
    if not resolved_by.strip() or not resolution_note.strip():
        raise ValueError("resolved_by and resolution_note are required")
    with closing(_connect()) as conn:
        cursor = conn.execute(
            """
            UPDATE paypal_plan_quarantine
            SET resolved_at = ?, resolved_by = ?, resolution_note = ?
            WHERE entry_id = ? AND environment = ? AND product_id = ?
              AND plan_id IS ? AND resolved_at IS NULL
            """,
            (
                _utcnow().isoformat(),
                resolved_by,
                resolution_note,
                entry_id,
                environment,
                product_id,
                plan_id,
            ),
        )
        if cursor.rowcount != 1:
            conn.rollback()
            raise QuarantineNotOpen(
                "no open entry matches all identifiers; nothing was resolved"
            )
        conn.commit()


def has_unresolved_plan_quarantine(environment: str, product_id: str) -> bool:
    """Return True if an unresolved entry exists for this environment and product."""
    with closing(_connect()) as conn:
        row = conn.execute(
            """
            SELECT 1 FROM paypal_plan_quarantine
            WHERE environment = ? AND product_id = ? AND resolved_at IS NULL
            LIMIT 1
            """,
            (environment, product_id),
        ).fetchone()
        return row is not None


def list_unresolved_plan_quarantine() -> list[Dict[str, Any]]:
    """Return unresolved entries for operator review (no secrets are stored in this table)."""
    with closing(_connect()) as conn:
        rows = conn.execute(
            """
            SELECT entry_id, environment, product_id, plan_id, reason, created_at
            FROM paypal_plan_quarantine
            WHERE resolved_at IS NULL
            ORDER BY entry_id
            """
        ).fetchall()
        return [dict(row) for row in rows]


# Exported names for ``from subscription_store import *``
__all__ = [
    "init_db",
    "upsert",
    "store_pending",
    "get_pending_user",
    "inspect_pending_token",
    "consume_pending",
    "get_by_user",
    "get_by_subscription",
    "get_database_path_fingerprint",
    "close",
    "open_plan_quarantine",
    "update_plan_quarantine",
    "resolve_plan_quarantine",
    "has_unresolved_plan_quarantine",
    "list_unresolved_plan_quarantine",
    "close_plan_quarantine_confirmed",
    "QuarantineConflict",
    "QuarantineNotOpen",
    "SchemaError",
    "verify_quarantine_schema",
]
