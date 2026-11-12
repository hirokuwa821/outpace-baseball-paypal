"""Write PayPal return diagnostics without logging sensitive identifiers."""

import hashlib
import json
import logging
import os
import re
import sys
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Iterator, Literal

PendingTokenState = Literal["found", "missing", "expired", "consumed"]

import paypal_state

# Diagnostics live in the PayPal state directory, next to the DB and cache.
DIAGNOSTIC_LOG_PATH = paypal_state.diagnostics_log_path()
_LOGGER = logging.getLogger("paypal.return.diagnostics")
_LOGGER.setLevel(logging.INFO)
_FILE_HANDLER: RotatingFileHandler | None = None
_HANDLER_INIT_LOCK = threading.Lock()


@contextmanager
def _interprocess_file_lock(lock_path: Path) -> Iterator[None]:
    """Serialize writes and rotation across processes."""
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as lock_file:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)

        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


class _ProcessSafeRotatingFileHandler(RotatingFileHandler):
    """Keep appends and rollover mutually exclusive across worker processes."""

    def handleError(self, record: logging.LogRecord) -> None:
        _warn_safely()

    def emit(self, record: logging.LogRecord) -> None:
        lock_path = Path(self.baseFilename).with_suffix(
            Path(self.baseFilename).suffix + ".lock"
        )
        try:
            with _interprocess_file_lock(lock_path):
                self._reopen_if_replaced()
                try:
                    super().emit(record)
                finally:
                    self._close_stream()
        except Exception:
            self.handleError(record)

    def _close_stream(self) -> None:
        stream = self.stream
        self.stream = None
        if stream is not None:
            try:
                stream.flush()
            finally:
                stream.close()

    def _reopen_if_replaced(self) -> None:
        """Reopen the active log under the lock after another process rotates it."""
        current_identity = None
        try:
            current_stat = os.stat(self.baseFilename)
            current_identity = (current_stat.st_dev, current_stat.st_ino)
        except FileNotFoundError:
            pass

        stream_identity = None
        if self.stream is not None:
            try:
                stream_stat = os.fstat(self.stream.fileno())
                stream_identity = (stream_stat.st_dev, stream_stat.st_ino)
            except OSError:
                pass

        if self.stream is not None and current_identity == stream_identity:
            return

        if self.stream is not None:
            self.stream.close()
        self.stream = self._open()


def _warn_safely() -> None:
    try:
        sys.stderr.write("PayPal diagnostic logging failed.\n")
    except Exception:
        pass


def _log_safely(message: str) -> None:
    try:
        _LOGGER.info("%s", message)
    except Exception:
        _warn_safely()


def _ensure_file_handler() -> None:
    global _FILE_HANDLER

    if _FILE_HANDLER is not None:
        return

    with _HANDLER_INIT_LOCK:
        if _FILE_HANDLER is not None:
            return

        handler = None
        try:
            DIAGNOSTIC_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
            handler = _ProcessSafeRotatingFileHandler(
                DIAGNOSTIC_LOG_PATH,
                maxBytes=1_000_000,
                backupCount=2,
                encoding="utf-8",
                delay=True,
            )
            handler.setFormatter(logging.Formatter("%(message)s"))
            _LOGGER.addHandler(handler)
            _FILE_HANDLER = handler
        except Exception:
            if handler is not None:
                try:
                    handler.close()
                except Exception:
                    pass
            _warn_safely()


def _log_pending_token_diagnostic(
    *,
    phase: Literal["purchase_store", "return_lookup"],
    token: str | None,
    database_path_fingerprint: str | None,
    process_role: Literal["fastapi", "streamlit"],
    process_instance_id: str,
    save_success: bool | None = None,
    lookup_result: Literal["found", "not_found", "error", "not_attempted"] | None = None,
    token_state: Literal[
        "active", "missing", "expired", "consumed", "error"
    ] | None = None,
) -> None:
    """Log pending-token lifecycle metadata without identifiers or token values."""
    global _FILE_HANDLER

    token_fingerprint = (
        hashlib.sha256(token.encode("utf-8")).hexdigest() if token else None
    )
    safe_database_fingerprint = (
        database_path_fingerprint
        if isinstance(database_path_fingerprint, str)
        and re.fullmatch(r"[a-f0-9]{64}", database_path_fingerprint)
        else "unavailable"
    )
    safe_instance_id = (
        process_instance_id
        if re.fullmatch(r"[a-f0-9]{32}", process_instance_id)
        else "unavailable"
    )
    fields = {
        "event": "pending_token_diagnostic",
        "phase": phase,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(
            timespec="milliseconds"
        ),
        "token_fingerprint": token_fingerprint,
        "database_path_fingerprint": safe_database_fingerprint,
        "pending_save_success": save_success,
        "pending_lookup_result": lookup_result,
        "pending_token_state": token_state,
        "process_role": process_role,
        "process_id": os.getpid(),
        "process_instance_id": safe_instance_id,
    }
    message = json.dumps(fields, separators=(",", ":"), sort_keys=True)

    _ensure_file_handler()
    _log_safely(message)


def _safe_status(status: str) -> str:
    return status if re.fullmatch(r"[A-Z_]{1,40}", status) else "UNKNOWN"


def _log_paypal_return_diagnostic(
    *,
    subscription_id_present: bool,
    pending_token_present: bool,
    pending_token_state: PendingTokenState,
    database_path_match: bool,
    paypal_api_result: Literal["success", "failure"],
    subscription_id_match: bool,
    custom_id_match: bool,
    subscription_status: str,
    verification_failure_reason: str,
) -> None:
    """Log only the explicitly approved, non-identifying diagnostic fields."""
    global _FILE_HANDLER

    fields = {
        "subscription_id_present": bool(subscription_id_present),
        "pending_token_present": bool(pending_token_present),
        "pending_token_state": pending_token_state,
        "database_path_match": bool(database_path_match),
        "paypal_api_result": paypal_api_result,
        "subscription_id_match": bool(subscription_id_match),
        "custom_id_match": bool(custom_id_match),
        "subscription_status": _safe_status(subscription_status),
        "verification_failure_reason": (
            verification_failure_reason
            if re.fullmatch(r"[a-z_]{1,48}", verification_failure_reason)
            else "unknown"
        ),
    }
    message = json.dumps(fields, separators=(",", ":"), sort_keys=True)

    _ensure_file_handler()
    _log_safely(message)


def log_pending_token_diagnostic(
    *,
    phase: Literal["purchase_store", "return_lookup"],
    token: str | None,
    database_path_fingerprint: str | None,
    process_role: Literal["fastapi", "streamlit"],
    process_instance_id: str,
    save_success: bool | None = None,
    lookup_result: Literal["found", "not_found", "error", "not_attempted"] | None = None,
    token_state: Literal[
        "active", "missing", "expired", "consumed", "error"
    ] | None = None,
) -> None:
    """Attempt diagnostics without allowing any diagnostic exception to escape."""
    try:
        _log_pending_token_diagnostic(
            phase=phase,
            token=token,
            database_path_fingerprint=database_path_fingerprint,
            process_role=process_role,
            process_instance_id=process_instance_id,
            save_success=save_success,
            lookup_result=lookup_result,
            token_state=token_state,
        )
    except Exception:
        _warn_safely()


def log_paypal_return_diagnostic(
    *,
    subscription_id_present: bool,
    pending_token_present: bool,
    pending_token_state: PendingTokenState,
    database_path_match: bool,
    paypal_api_result: Literal["success", "failure"],
    subscription_id_match: bool,
    custom_id_match: bool,
    subscription_status: str,
    verification_failure_reason: str,
) -> None:
    """Attempt verification diagnostics without affecting return processing."""
    try:
        _log_paypal_return_diagnostic(
            subscription_id_present=subscription_id_present,
            pending_token_present=pending_token_present,
            pending_token_state=pending_token_state,
            database_path_match=database_path_match,
            paypal_api_result=paypal_api_result,
            subscription_id_match=subscription_id_match,
            custom_id_match=custom_id_match,
            subscription_status=subscription_status,
            verification_failure_reason=verification_failure_reason,
        )
    except Exception:
        _warn_safely()
