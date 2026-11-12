# paypal_sdk.py – thin wrapper around PayPal Subscriptions API (sandbox & live)
"""Utility functions used by the FastAPI server and the Streamlit UI.

All secrets are loaded from the environment – never hard‑coded.
The module supports:
* OAuth2 client‑credentials flow (access token)
* Creating a subscription for a pre‑created plan
* Verifying a webhook via the official PayPal endpoint
* Querying subscription status
"""

import os
import base64
import json
import secrets
import datetime
import contextlib
import logging
import threading
import time
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Dict
from urllib.parse import quote, urlparse
import requests
from dotenv import load_dotenv
from paypal_pricing import (
    MONTHLY_PLAN_AMOUNT,
    MONTHLY_PLAN_CURRENCY,
    format_paypal_amount,
)
import paypal_state
import subscription_store

_LOGGER = logging.getLogger("paypal.sdk")

# ---------------------------------------------------------------------------
# Load environment variables from the PayPal integration directory only
# ---------------------------------------------------------------------------
PAYPAL_ENV_FILE = Path(__file__).resolve().parents[1] / ".env"


def load_paypal_env() -> Path | None:
    """Load only the PayPal integration's .env, independent of the CWD."""
    if not PAYPAL_ENV_FILE.is_file():
        return None
    load_dotenv(dotenv_path=PAYPAL_ENV_FILE, override=True)
    return PAYPAL_ENV_FILE

# Load .env upon module import
ENV_FILE_LOADED = load_paypal_env()

# ---------------------------------------------------------------------------
# Configuration – dynamic resolution with fallback
# ---------------------------------------------------------------------------
PAYPAL_BASE_URLS = {
    "sandbox": "https://api-m.sandbox.paypal.com",
    "live": "https://api-m.paypal.com",
}

def get_paypal_env() -> str:
    env = os.getenv("PAYPAL_ENV", "sandbox").lower()
    if env not in PAYPAL_BASE_URLS:
        raise ValueError(f"PAYPAL_ENV must be either 'sandbox' or 'live', got '{env}'")
    return env

def get_base_url() -> str:
    return PAYPAL_BASE_URLS[get_paypal_env()]

def get_paypal_client_id() -> str | None:
    val = os.getenv("PAYPAL_CLIENT_ID")
    return val.strip() if val else None

def get_paypal_client_secret() -> str | None:
    val = os.getenv("PAYPAL_CLIENT_SECRET")
    return val.strip() if val else None

def get_paypal_webhook_id() -> str | None:
    val = os.getenv("PAYPAL_WEBHOOK_ID")
    return val.strip() if val else None

# Backward-compatibility module-level variables
PAYPAL_CLIENT_ID = get_paypal_client_id()
PAYPAL_CLIENT_SECRET = get_paypal_client_secret()
PAYPAL_WEBHOOK_ID = get_paypal_webhook_id()
PAYPAL_ENV = get_paypal_env()
BASE_URL = get_base_url()


def _raise_for_status(
    response: requests.Response, sensitive_values: tuple[str, ...] = ()
) -> None:
    """Raise for HTTP errors and redact any sensitive data.

    Accepts explicit sensitive values to redact from error messages along with
    credentials. When called from API helpers like ``_request()``, the active
    Bearer token is passed via ``sensitive_values``.  Legacy tokens passed
    in ``sensitive_values`` as strings or dictionaries are also safely handled.
    To avoid deadlocks and concurrency exceptions, this function never accesses
    or acquires ``_token_cache_lock``.
    """
    try:
        response.raise_for_status()
    except requests.HTTPError:
        body = response.text
        client_id = get_paypal_client_id()
        client_secret = get_paypal_client_secret()

        # Extract strings from sensitive_values – supports str, dict, or None
        sanitized_values: list[str] = []
        for val in (*sensitive_values, client_id, client_secret):
            if isinstance(val, dict):
                tok = val.get("token")
                if isinstance(tok, str) and tok:
                    sanitized_values.append(tok)
            elif isinstance(val, str) and val:
                sanitized_values.append(val)

        for value in sanitized_values:
            body = body.replace(value, "[REDACTED]")

        body = body[:2000]
        message = f"PayPal API request failed with HTTP {response.status_code}"
        if body:
            message = f"{message}: {body}"
        raise requests.HTTPError(message, response=response) from None

# ---------------------------------------------------------------------------
# Helper: obtain an OAuth2 access token (client‑credentials grant)
# ---------------------------------------------------------------------------
def _get_access_token() -> dict:
    """Retrieve a PayPal OAuth2 access token (client‑credentials grant).

    Returns a dict containing the token string and its UTC expiry datetime.
    The function validates that the token is a non‑empty, non‑whitespace string.
    If the token is missing or invalid, a RuntimeError is raised and nothing is
    cached.
    """
    client_id = get_paypal_client_id()
    client_secret = get_paypal_client_secret()

    missing = []
    if not client_id:
        missing.append("PAYPAL_CLIENT_ID")
    if not client_secret:
        missing.append("PAYPAL_CLIENT_SECRET")
    if missing:
        raise RuntimeError(
            f"PayPal client credentials are not configured. Missing environment variable(s): {', '.join(missing)}"
        )

    auth = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    headers = {
        "Authorization": f"Basic {auth}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {"grant_type": "client_credentials"}
    base_url = get_base_url()
    resp = requests.post(f"{base_url}/v1/oauth2/token", headers=headers, data=data, timeout=8)
    _raise_for_status(resp, (auth,))
    payload = resp.json()
    token = payload.get("access_token")
    # Validate token – must be a non‑empty, non‑whitespace string
    if not isinstance(token, str) or not token.strip():
        raise RuntimeError("Invalid access token received from PayPal OAuth endpoint")
    # Determine expiry – PayPal returns expires_in (seconds). Positive integer required.
    expires_in = payload.get("expires_in")
    if type(expires_in) is not int or expires_in <= 0:
        raise RuntimeError(
            "Invalid or missing expires_in in PayPal OAuth response"
        )
    expires_at = (
        datetime.datetime.now(datetime.timezone.utc)
        + datetime.timedelta(seconds=expires_in)
    )
    return {"token": token, "expires_at": expires_at}

# Simple token cache – stores token and its expiry (UTC datetime). refreshed on failure
_token_cache: Dict[str, Any] = {}
_token_cache_lock = threading.Lock()

def invalidate_access_token(token: str | None = None) -> None:
    """Invalidate cached access token for current environment.

    If `token` is provided, invalidate only if the currently cached token
    matches `token`. This prevents clearing a freshly refreshed token when
    a concurrent or delayed request fails with 401.
    """
    environment = get_paypal_env()
    with _token_cache_lock:
        cached = _token_cache.get(environment)
        if isinstance(cached, dict):
            cached_token = cached.get("token")
            if token is None or cached_token == token:
                _token_cache.pop(environment, None)
        elif isinstance(cached, str):
            if token is None or cached == token:
                _token_cache.pop(environment, None)

def get_access_token() -> str:
    """Return a valid PayPal access token.

    The token is cached together with its expiry datetime. If the cached token
    is missing, expired, or about to expire (within 30 seconds), a fresh token
    is obtained via ``_get_access_token()``.
    """
    environment = get_paypal_env()
    with _token_cache_lock:
        cached = _token_cache.get(environment)
        now = datetime.datetime.now(datetime.timezone.utc)
        if isinstance(cached, dict):
            token = cached.get("token")
            expires_at = cached.get("expires_at")
            # Consider token valid if we have a non‑empty token and expiry is later than now
            if isinstance(token, str) and token.strip() and isinstance(expires_at, datetime.datetime):
                # Refresh a little before actual expiry to avoid race conditions
                if now < expires_at - datetime.timedelta(seconds=30):
                    return token
        # Either no cache entry, malformed, or expired – fetch a new token
        fresh = _get_access_token()
        _token_cache[environment] = fresh
        return fresh["token"]

# ---------------------------------------------------------------------------
# HTTP helper – automatically inject auth header
# ---------------------------------------------------------------------------
def _request(method: str, endpoint: str, json_body: Any = None, params: Dict[str, Any] = None) -> requests.Response:
    base_url = get_base_url()
    url = f"{base_url}{endpoint}"
    is_get = method.upper() == "GET"
    max_attempts = 2 if is_get else 1

    for attempt in range(max_attempts):
        token = get_access_token()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        }
        resp = requests.request(method, url, headers=headers, json=json_body, params=params, timeout=10)
        if resp.status_code == 401:
            invalidate_access_token(token)
            if is_get and attempt == 0:
                continue
        _raise_for_status(resp, (token,))
        return resp
    _raise_for_status(resp, (token,))
    return resp

# ---------------------------------------------------------------------------
# Subscription plan handling (create once, reuse)
# ---------------------------------------------------------------------------
RESOURCE_IDS_CACHE_FILE = paypal_state.resource_cache_path()
PRODUCT_NAME = "Outpace Baseball Pro"


# ---------------------------------------------------------------------------
# Plan-creation mutual exclusion (threads and processes)
#
# A byte-range lock on a sidecar file next to the resource cache. It is shared by all
# threads, workers, and processes. It is deliberately not an SQLite write transaction:
# holding a DB write lock across a PayPal call (up to about two minutes) would block
# unrelated DB writers. A threading.Lock sits in front as the in-process fast path.
# ---------------------------------------------------------------------------
# Total time a request may wait for the creation lock, covering both the in-process
# thread lock and the OS file lock together (one absolute deadline). Kept well under the
# Streamlit client timeout (180 s). A waiter normally reuses the cached plan after the
# lock, so its own work is short; a waiter that must create a plan can still run longer.
PLAN_CREATION_LOCK_WAIT_SECONDS = 60.0
_PLAN_THREAD_LOCK = threading.Lock()


def _plan_lock_path() -> Path:
    # Resolved so every process, whatever its working directory, uses the same file.
    return RESOURCE_IDS_CACHE_FILE.resolve().with_name("paypal_plan_creation.lock")


def _open_plan_lock_file() -> int:
    path = _plan_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    # O_BINARY stops the C runtime from applying text-mode translation on Windows.
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0))
    try:
        if os.fstat(fd).st_size == 0:
            os.write(fd, b"\0")  # byte-range locks need at least one byte to lock
    except Exception:
        os.close(fd)
        raise
    return fd


def _try_lock_fd(fd: int) -> bool:
    os.lseek(fd, 0, os.SEEK_SET)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return True
        except OSError:
            return False
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock_fd(fd: int) -> None:
    if os.name == "nt":
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


@contextlib.contextmanager
def _plan_creation_lock():
    """Serialize the whole get-or-create-plan sequence across threads and processes.

    Waits at most PLAN_CREATION_LOCK_WAIT_SECONDS. If the lock cannot be acquired,
    raises PlanCreationError before anything is read or sent to PayPal.
    """
    # One absolute deadline for both waits, so the total never exceeds the configured budget.
    deadline = time.monotonic() + PLAN_CREATION_LOCK_WAIT_SECONDS
    remaining = deadline - time.monotonic()
    if remaining <= 0 or not _PLAN_THREAD_LOCK.acquire(timeout=remaining):
        raise PlanCreationError("Plan creation is busy; no plan was created.", reason="lock_timeout")
    try:
        try:
            fd = _open_plan_lock_file()
        except OSError as error:
            raise PlanCreationError(
                "Could not open the plan creation lock; no plan was created.",
                reason="lock_unavailable",
            ) from error
        try:
            while not _try_lock_fd(fd):
                if time.monotonic() >= deadline:
                    raise PlanCreationError(
                        "Plan creation is busy; no plan was created.", reason="lock_timeout"
                    )
                time.sleep(0.1)
            try:
                yield
            finally:
                _unlock_fd(fd)
        finally:
            os.close(fd)
    finally:
        _PLAN_THREAD_LOCK.release()


def _read_resource_cache() -> dict[str, dict[str, str]]:
    if not RESOURCE_IDS_CACHE_FILE.is_file():
        return {}
    with RESOURCE_IDS_CACHE_FILE.open("r", encoding="utf-8") as cache_file:
        cache = json.load(cache_file)
    if not isinstance(cache, dict):
        raise RuntimeError("PayPal resource ID cache must contain a JSON object")
    return cache


def _write_resource_cache(cache: dict[str, dict[str, str]]) -> None:
    RESOURCE_IDS_CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = RESOURCE_IDS_CACHE_FILE.with_suffix(".json.tmp")
    with temporary_file.open("w", encoding="utf-8") as cache_file:
        json.dump(cache, cache_file, indent=2)
    os.replace(temporary_file, RESOURCE_IDS_CACHE_FILE)


def _cached_resource_id(environment: str, resource_name: str) -> str | None:
    environment_cache = _read_resource_cache().get(environment, {})
    if not isinstance(environment_cache, dict):
        raise RuntimeError(
            f"PayPal {environment} resource ID cache must contain a JSON object"
        )
    resource_id = environment_cache.get(resource_name)
    return resource_id if isinstance(resource_id, str) and resource_id else None


def _store_resource_id(environment: str, resource_name: str, resource_id: str) -> None:
    cache = _read_resource_cache()
    environment_cache = cache.setdefault(environment, {})
    if not isinstance(environment_cache, dict):
        raise RuntimeError(
            f"PayPal {environment} resource ID cache must contain a JSON object"
        )
    environment_cache[resource_name] = resource_id
    _write_resource_cache(cache)


def _remove_resource_id(environment: str, resource_name: str) -> None:
    cache = _read_resource_cache()
    environment_cache = cache.get(environment)
    if isinstance(environment_cache, dict):
        environment_cache.pop(resource_name, None)
        if not environment_cache:
            cache.pop(environment, None)
        _write_resource_cache(cache)


def _is_invalid_resource_id_error(error: requests.HTTPError) -> bool:
    response = error.response
    if response is None or response.status_code != 404:
        return False
    try:
        details = response.json().get("details", [])
    except (ValueError, AttributeError):
        return False
    return isinstance(details, list) and any(
        isinstance(detail, dict) and detail.get("issue") == "INVALID_RESOURCE_ID"
        for detail in details
    )


class PlanCreationError(RuntimeError):
    """PayPal may have created a plan, but it cannot be used or cached safely.

    Operator-only details: ``plan_id`` (when known), ``reason`` (a short code), and
    ``entry_id`` (the quarantine record). The message and these fields must never be
    sent to the browser. The HTTP layer returns a generic message plus a reference code.
    No retry is attempted, and the PayPal-side plan is never modified.
    """

    def __init__(
        self,
        message: str,
        plan_id: str | None = None,
        *,
        reason: str = "creation_failed",
        entry_id: int | None = None,
    ):
        super().__init__(message)
        self.plan_id = plan_id
        self.reason = reason
        self.entry_id = entry_id


def _created_plan_id(response: requests.Response) -> str:
    """Return the ID of a newly created plan only if the response is a valid ACTIVE plan."""
    try:
        body = response.json()
    except ValueError as error:
        raise PlanCreationError(
            "PayPal plan creation response is not valid JSON",
            reason="creation_outcome_unknown",
        ) from error
    if not isinstance(body, dict):
        raise PlanCreationError(
            "PayPal plan creation response is not a JSON object",
            reason="creation_outcome_unknown",
        )
    plan_id = body.get("id")
    if not isinstance(plan_id, str) or not plan_id.strip():
        raise PlanCreationError(
            "PayPal plan creation response did not contain a valid ID",
            reason="creation_outcome_unknown",
        )
    if body.get("status") != "ACTIVE":
        raise PlanCreationError(
            f"PayPal created plan {plan_id} with a status other than ACTIVE; it was not cached",
            plan_id=plan_id,
            reason="status_not_active",
        )
    return plan_id


def _resource_id(response: requests.Response, resource_name: str) -> str:
    try:
        body = response.json()
    except ValueError as error:
        raise RuntimeError(f"PayPal {resource_name} response was not valid JSON") from error
    if not isinstance(body, dict):
        raise RuntimeError(f"PayPal {resource_name} response was not a JSON object")
    resource_id = body.get("id")
    if not isinstance(resource_id, str) or not resource_id:
        raise RuntimeError(f"PayPal {resource_name} response did not contain an ID")
    return resource_id


def get_or_create_product(force_refresh: bool = False) -> str:
    """Return an existing PayPal product ID or create the product in this environment."""
    environment = get_paypal_env()
    if force_refresh:
        _remove_resource_id(environment, "product_id")

    product_id = _cached_resource_id(environment, "product_id")
    if product_id:
        try:
            _request("GET", f"/v1/catalogs/products/{quote(product_id, safe='')}")
            return product_id
        except requests.HTTPError as error:
            if not _is_invalid_resource_id_error(error):
                raise
            _remove_resource_id(environment, "product_id")

    response = _request(
        "GET",
        "/v1/catalogs/products",
        params={"page_size": 20, "total_required": "true"},
    )
    products = response.json().get("products", [])
    if not isinstance(products, list):
        raise RuntimeError("PayPal product list response did not contain a product list")
    for product in products:
        if isinstance(product, dict) and product.get("name") == PRODUCT_NAME:
            product_id = product.get("id")
            if isinstance(product_id, str) and product_id:
                _store_resource_id(environment, "product_id", product_id)
                return product_id

    response = _request(
        "POST",
        "/v1/catalogs/products",
        json_body={
            "name": PRODUCT_NAME,
            "description": "Monthly subscription for premium AI pitching analysis",
            "type": "SERVICE",
            "category": "SOFTWARE",
        },
    )
    product_id = _resource_id(response, "product")
    _store_resource_id(environment, "product_id", product_id)
    return product_id


def _ensure_no_unresolved_plan(environment: str, product_id: str) -> None:
    """Stop before any POST if an unresolved quarantine record exists, or if the check fails."""
    try:
        # Schema must exist and be correct before any creation decision. A missing table
        # or index stops here, before any POST.
        subscription_store.verify_quarantine_schema()
        blocked = subscription_store.has_unresolved_plan_quarantine(environment, product_id)
    except Exception as error:
        raise PlanCreationError(
            "Could not verify pending plan records; plan creation was blocked.",
            reason="quarantine_check_failed",
        ) from error
    if blocked:
        raise PlanCreationError(
            "An unresolved plan creation exists for this environment and product; "
            "plan creation was blocked. An operator must verify it in PayPal and resolve the record.",
            reason="quarantine_blocked",
        )


def _open_quarantine_entry(environment: str, product_id: str) -> int:
    """Record the attempt as unresolved before the POST. If this fails, no POST is sent."""
    try:
        return subscription_store.open_plan_quarantine(
            environment, product_id, "creation_request_started"
        )
    except subscription_store.QuarantineConflict as error:
        raise PlanCreationError(
            "An unresolved plan creation exists; plan creation was blocked.",
            reason="quarantine_blocked",
        ) from error
    except Exception as error:
        raise PlanCreationError(
            "Could not record the plan creation attempt; no plan was created.",
            reason="quarantine_write_failed",
        ) from error


def _quarantine_update_safely(entry_id: int, reason: str, plan_id: str | None = None) -> None:
    """Best-effort update. The entry stays open on failure, so new creation remains blocked."""
    try:
        subscription_store.update_plan_quarantine(entry_id, reason, plan_id=plan_id)
    except Exception:
        _LOGGER.error("Could not update plan quarantine entry %s (reason %s)", entry_id, reason)


def _close_confirmed_safely(entry_id: int, outcome: str, plan_id: str | None = None) -> None:
    """Close only on a confirmed outcome. On failure the entry stays open, which is fail-safe."""
    try:
        subscription_store.close_plan_quarantine_confirmed(entry_id, outcome, plan_id=plan_id)
    except Exception:
        _LOGGER.error(
            "Could not close plan quarantine entry %s (outcome %s); it remains unresolved",
            entry_id,
            outcome,
        )


def _create_plan(product_id: str) -> str:
    environment = get_paypal_env()
    _ensure_no_unresolved_plan(environment, product_id)
    entry_id = _open_quarantine_entry(environment, product_id)
    plan_payload = {
        "product_id": product_id,
        "name": "Outpace Baseball Pro – Monthly",
        "description": "Monthly subscription for premium AI pitching analysis",
        "billing_cycles": [
            {
                "frequency": {"interval_unit": "MONTH", "interval_count": 1},
                "tenure_type": "REGULAR",
                "sequence": 1,
                "total_cycles": 0,
                "pricing_scheme": {
                    "fixed_price": {
                        "value": format_paypal_amount(
                            MONTHLY_PLAN_AMOUNT, MONTHLY_PLAN_CURRENCY
                        ),
                        "currency_code": MONTHLY_PLAN_CURRENCY,
                    }
                },
            }
        ],
        "payment_preferences": {"auto_bill_outstanding": True, "setup_fee_failure_action": "CANCEL", "payment_failure_threshold": 3},
        "status": "ACTIVE",
    }
    try:
        response = _request("POST", "/v1/billing/plans", json_body=plan_payload)
    except requests.HTTPError as error:
        status = error.response.status_code if error.response is not None else None
        if status is not None and 400 <= status < 500:
            # PayPal rejected the request, so no plan was created. Close the record and
            # re-raise unchanged: get_or_create_plan may retry for INVALID_RESOURCE_ID.
            _close_confirmed_safely(entry_id, "rejected_by_paypal")
            raise
        # 5xx or no response: the outcome is unknown and a plan may exist. Keep the
        # record open with an explicit reason. Do not retry automatically.
        _quarantine_update_safely(entry_id, "creation_outcome_unknown")
        raise PlanCreationError(
            f"PayPal plan creation outcome is unknown (entry {entry_id}); a plan may exist. "
            "Check PayPal manually before resolving the record.",
            reason="creation_outcome_unknown",
            entry_id=entry_id,
        ) from error
    except Exception as error:
        # Timeout or connection failure after the request may have reached PayPal.
        _quarantine_update_safely(entry_id, "creation_outcome_unknown")
        raise PlanCreationError(
            f"PayPal plan creation outcome is unknown (entry {entry_id}); a plan may exist. "
            "Check PayPal manually before resolving the record.",
            reason="creation_outcome_unknown",
            entry_id=entry_id,
        ) from error

    try:
        plan_id = _created_plan_id(response)
    except PlanCreationError as error:
        error.entry_id = entry_id
        _quarantine_update_safely(entry_id, error.reason, plan_id=error.plan_id)
        raise

    # The plan exists in PayPal. Record its ID in the quarantine entry before touching the
    # cache, so the ID survives a cache failure. Creation is never retried.
    try:
        subscription_store.update_plan_quarantine(entry_id, "plan_created_cache_pending", plan_id=plan_id)
    except Exception as error:
        raise PlanCreationError(
            f"PayPal plan {plan_id} was created, but its ID could not be recorded (entry {entry_id}).",
            plan_id=plan_id,
            reason="plan_id_not_recorded",
            entry_id=entry_id,
        ) from error

    try:
        _store_resource_id(environment, "plan_id", plan_id)
    except Exception as error:
        _quarantine_update_safely(entry_id, "cache_save_failed", plan_id=plan_id)
        raise PlanCreationError(
            f"PayPal plan {plan_id} was created, but saving it to the local cache failed "
            f"(entry {entry_id}). No new plan was created.",
            plan_id=plan_id,
            reason="cache_save_failed",
            entry_id=entry_id,
        ) from error

    _close_confirmed_safely(entry_id, "completed", plan_id=plan_id)
    return plan_id


class PlanValidationError(RuntimeError):
    """A cached PayPal plan could not be trusted. Raised without creating a new plan."""


def _parse_decimal(value: object) -> Decimal | None:
    """Parse a numeric JSON value. Return None for bool, None, non-numeric, NaN, or Infinity."""
    if value is None or isinstance(value, bool):
        return None
    if not isinstance(value, (str, int, float, Decimal)):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _check_cached_plan(plan: object, product_id: str) -> bool:
    """Validate a plan returned by GET.

    Returns True if the plan is the expected monthly plan. Returns False if it is a
    stale reference (product or status mismatch); the caller then drops only the
    cache entry. Raises PlanValidationError for malformed responses and for
    cycle, frequency, currency, or amount mismatches, so that no plan is created.
    """
    if not isinstance(plan, dict):
        raise PlanValidationError("PayPal plan response is not a JSON object")
    plan_product_id = plan.get("product_id")
    status = plan.get("status")
    if not isinstance(plan_product_id, str) or not isinstance(status, str):
        raise PlanValidationError("PayPal plan response is missing product_id or status")
    if plan_product_id != product_id or status != "ACTIVE":
        return False

    cycles = plan.get("billing_cycles")
    if not isinstance(cycles, list):
        raise PlanValidationError("PayPal plan billing_cycles is missing or not a list")
    regular_cycles = []
    for cycle in cycles:
        if not isinstance(cycle, dict) or not isinstance(cycle.get("tenure_type"), str):
            raise PlanValidationError("PayPal plan contains a malformed billing cycle")
        if cycle["tenure_type"] == "REGULAR":
            regular_cycles.append(cycle)
    if len(regular_cycles) != 1:
        raise PlanValidationError(
            f"PayPal plan must have exactly one REGULAR cycle, found {len(regular_cycles)}"
        )
    regular = regular_cycles[0]

    frequency = regular.get("frequency")
    if not isinstance(frequency, dict):
        raise PlanValidationError("PayPal plan REGULAR cycle is missing frequency")
    interval_unit = frequency.get("interval_unit")
    if not isinstance(interval_unit, str):
        raise PlanValidationError("PayPal plan frequency.interval_unit is missing")
    if interval_unit != "MONTH":
        raise PlanValidationError("PayPal plan interval_unit mismatch: expected MONTH")
    interval_count = _parse_decimal(frequency.get("interval_count"))
    if interval_count is None:
        raise PlanValidationError("PayPal plan frequency.interval_count is invalid")
    if interval_count != 1:
        raise PlanValidationError("PayPal plan interval_count mismatch: expected 1")

    scheme = regular.get("pricing_scheme")
    fixed = scheme.get("fixed_price") if isinstance(scheme, dict) else None
    if not isinstance(fixed, dict):
        raise PlanValidationError("PayPal plan REGULAR cycle is missing fixed_price")
    currency = fixed.get("currency_code")
    if not isinstance(currency, str):
        raise PlanValidationError("PayPal plan currency_code is missing")
    if currency != MONTHLY_PLAN_CURRENCY:
        raise PlanValidationError("PayPal plan currency mismatch")
    amount = _parse_decimal(fixed.get("value"))
    if amount is None:
        raise PlanValidationError("PayPal plan fixed_price.value is invalid")
    if amount != MONTHLY_PLAN_AMOUNT:
        raise PlanValidationError("PayPal plan amount mismatch")
    return True


def get_or_create_plan() -> str:
    """Return a valid plan ID. The cache is read only after the creation lock is held."""
    with _plan_creation_lock():
        return _get_or_create_plan_locked()


def _check_cache_migration() -> None:
    """Refuse to create a plan when the state cache is missing but a legacy cache exists.

    Without this, an empty state cache would cause a new plan to be created even though
    the legacy cache already holds a valid one.
    """
    if not paypal_state.same_path(RESOURCE_IDS_CACHE_FILE, paypal_state.resource_cache_path()):
        return  # an explicit other location (tests, or the legacy folder itself)
    if paypal_state.same_path(RESOURCE_IDS_CACHE_FILE, paypal_state.LEGACY_CACHE_PATH):
        return
    try:
        paypal_state.assert_migration_state()
    except paypal_state.StateMigrationRequired as error:
        raise PlanCreationError(
            "PayPal state is not ready (migration marker). No plan was created.",
            reason="state_migration_required",
        ) from error


def _get_or_create_plan_locked() -> str:
    _check_cache_migration()
    """Return a valid environment-specific plan for the monthly ¥1,980 subscription."""
    environment = get_paypal_env()
    product_id = get_or_create_product()

    plan_id = _cached_resource_id(environment, "plan_id")
    if plan_id:
        try:
            response = _request("GET", f"/v1/billing/plans/{quote(plan_id, safe='')}")
        except requests.HTTPError as error:
            # Only "this resource ID does not exist" marks the cache reference as stale.
            # Any other HTTP error is raised without creating a new plan.
            if not _is_invalid_resource_id_error(error):
                raise
            _remove_resource_id(environment, "plan_id")
        else:
            try:
                plan = response.json()
            except ValueError as error:
                raise PlanValidationError("PayPal plan response is not valid JSON") from error
            if _check_cached_plan(plan, product_id):
                return plan_id
            # Product or status mismatch: drop the cache reference only. The old plan
            # is neither modified nor deleted.
            _remove_resource_id(environment, "plan_id")

    try:
        return _create_plan(product_id)
    except requests.HTTPError as error:
        if not _is_invalid_resource_id_error(error):
            raise

    product_id = get_or_create_product(force_refresh=True)
    return _create_plan(product_id)

# ---------------------------------------------------------------------------
# Create a subscription (user‑facing flow)
# ---------------------------------------------------------------------------
PAYPAL_APPROVAL_HOSTS = {
    "sandbox": "www.sandbox.paypal.com",
    "live": "www.paypal.com",
}


def _validated_approval_url(href: object, environment: str) -> str:
    """Return the approval URL only if it is HTTPS on the expected PayPal host for this environment."""
    if not isinstance(href, str) or not href:
        raise RuntimeError("PayPal approval URL is missing or invalid")
    parsed = urlparse(href)
    # netloc includes any userinfo or port, so those are rejected by the equality check.
    if parsed.scheme != "https" or parsed.netloc.lower() != PAYPAL_APPROVAL_HOSTS[environment]:
        raise RuntimeError("PayPal approval URL failed host validation")
    return href


def create_subscription(user_id: str, return_url: str, cancel_url: str) -> str:
    """Create a PayPal subscription and return the approval URL.

    The `user_id` is stored as the `custom_id` field so we can later verify the
    mapping server‑side.
    """
    plan_id = get_or_create_plan()
    payload = {
        "plan_id": plan_id,
        "application_context": {
            "brand_name": "Outpace Baseball",
            "locale": "ja-JP",
            "shipping_preference": "NO_SHIPPING",
            "user_action": "SUBSCRIBE_NOW",
            "return_url": return_url,
            "cancel_url": cancel_url,
        },
        "custom_id": user_id,
    }
    resp = _request("POST", "/v1/billing/subscriptions", json_body=payload)
    # The response contains a list of links – pick the approve link.
    for link in resp.json().get("links", []):
        if link.get("rel") == "approve":
            return _validated_approval_url(link.get("href"), get_paypal_env())
    raise RuntimeError("Approval URL not found in PayPal response")

# ---------------------------------------------------------------------------
# Query subscription status and details
# ---------------------------------------------------------------------------
def get_subscription_status(subscription_id: str) -> str:
    """Return PayPal's `status` field for a subscription (e.g. ACTIVE)."""
    resp = _request(
        "GET", f"/v1/billing/subscriptions/{quote(subscription_id, safe='')}"
    )
    return resp.json().get("status", "UNKNOWN")

def get_subscription_details(subscription_id: str) -> dict:
    """Return the full subscription JSON object.

    Used to verify the ``custom_id`` that was stored when the subscription was
    created.  The object includes fields like ``status`` and ``custom_id`` under
    ``subscriber`` (PayPal puts ``custom_id`` at the top‑level)."""
    resp = _request(
        "GET", f"/v1/billing/subscriptions/{quote(subscription_id, safe='')}"
    )
    return resp.json()

# ---------------------------------------------------------------------------
# Verify webhook – official PayPal endpoint
# ---------------------------------------------------------------------------
def verify_webhook_with_api(headers: Dict[str, str], body: Dict[str, Any]) -> bool:
    """Call PayPal's verify‑webhook‑signature endpoint.

    Returns ``True`` only when PayPal answers with ``"SUCCESS"``.
    """
    webhook_id = get_paypal_webhook_id()
    verification_payload = {
        "auth_algo": headers.get("PAYPAL-AUTH-ALG"),
        "cert_url": headers.get("PAYPAL-CERT-URL"),
        "transmission_id": headers.get("PAYPAL-TRANSMISSION-ID"),
        "transmission_sig": headers.get("PAYPAL-TRANSMISSION-SIG"),
        "transmission_time": headers.get("PAYPAL-TRANSMISSION-TIME"),
        "webhook_id": webhook_id,
        "webhook_event": body,
    }
    # The verification endpoint lives under the same base URL.
    resp = _request("POST", "/v1/notifications/verify-webhook-signature", json_body=verification_payload)
    result = resp.json().get("verification_status")
    return result == "SUCCESS"

# ---------------------------------------------------------------------------
# Helper used by the FastAPI server to validate the synthetic judge request
# ---------------------------------------------------------------------------
def is_valid_judge_token(token: str) -> bool:
    expected = os.getenv("PAYPAL_JUDGE_TOKEN")
    return expected is not None and secrets.compare_digest(token, expected)

# End of file
