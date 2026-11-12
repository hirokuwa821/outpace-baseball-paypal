"""Verify PayPal return callbacks and restore access from stored subscriptions."""

import uuid
from dataclasses import dataclass
from typing import MutableMapping

from paypal_sdk import get_subscription_details
from subscription_store import (
    get_by_subscription,
    get_by_user,
    get_database_path_fingerprint,
    inspect_pending_token,
    persist_verified_subscription,
    upsert,
)
from paypal_return_diagnostics import (
    log_pending_token_diagnostic,
    log_paypal_return_diagnostic,
)

_PROCESS_INSTANCE_ID = uuid.uuid4().hex


@dataclass(frozen=True)
class SubscriptionVerification:
    user_id: str | None
    status: str
    verified: bool
    active: bool
    reason: str | None = None


def clear_return_query_params(query_params: MutableMapping[str, object]) -> None:
    """Remove only PayPal callback parameters, retaining unrelated query state."""
    for key in ("subscription_id", "token", "pending_token"):
        if key in query_params:
            del query_params[key]


def _verify_and_store(
    subscription_id: str, user_id: str, diagnostic: dict[str, object] | None = None
) -> SubscriptionVerification:
    try:
        details = get_subscription_details(subscription_id)
    except Exception:
        if diagnostic is not None:
            diagnostic["verification_failure_reason"] = "paypal_api_error"
        raise
    if not isinstance(details, dict):
        if diagnostic is not None:
            diagnostic["verification_failure_reason"] = "paypal_invalid_response"
        return SubscriptionVerification(
            user_id=user_id,
            status="UNKNOWN",
            verified=False,
            active=False,
            reason="paypal_invalid_response",
        )

    if diagnostic is not None:
        diagnostic["paypal_api_result"] = "success"
    status = details.get("status")
    if not isinstance(status, str):
        status = "UNKNOWN"
    if diagnostic is not None:
        diagnostic["subscription_status"] = status

    subscription_id_match = details.get("id") == subscription_id
    custom_id_match = details.get("custom_id") == user_id
    if diagnostic is not None:
        diagnostic["subscription_id_match"] = subscription_id_match
        diagnostic["custom_id_match"] = custom_id_match

    if not subscription_id_match:
        if diagnostic is not None:
            diagnostic["verification_failure_reason"] = "subscription_id_mismatch"
        return SubscriptionVerification(
            user_id=user_id,
            status=status,
            verified=False,
            active=False,
            reason="subscription_id_mismatch",
        )
    if not custom_id_match:
        if diagnostic is not None:
            diagnostic["verification_failure_reason"] = "owner_mismatch"
        return SubscriptionVerification(
            user_id=user_id,
            status=status,
            verified=False,
            active=False,
            reason="owner_mismatch",
        )

    if status != "ACTIVE" and diagnostic is not None:
        diagnostic["verification_failure_reason"] = "subscription_not_active"
    return SubscriptionVerification(
        user_id=user_id,
        status=status,
        verified=True,
        active=status == "ACTIVE",
    )


def handle_paypal_return(
    subscription_id: str | None,
    token: str | None,
    database_path_match: bool = False,
) -> SubscriptionVerification:
    """Validate the return token and PayPal ownership before persisting access."""
    diagnostic: dict[str, object] = {
        "subscription_id_present": bool(subscription_id),
        "pending_token_present": bool(token),
        "pending_token_state": "missing",
        "database_path_match": database_path_match,
        "paypal_api_result": "failure",
        "subscription_id_match": False,
        "custom_id_match": False,
        "subscription_status": "UNKNOWN",
        "verification_failure_reason": "none",
    }
    pending_lookup_result = "not_attempted"
    try:
        user_id: str | None = None
        if token:
            try:
                pending_state, user_id = inspect_pending_token(token)
            except Exception:
                pending_lookup_result = "error"
                diagnostic["pending_token_state"] = "error"
                diagnostic["verification_failure_reason"] = (
                    "pending_token_lookup_error"
                )
                raise
            pending_lookup_result = (
                "found" if pending_state == "found" else "not_found"
            )
            diagnostic["pending_token_state"] = pending_state

        if not subscription_id or not token:
            diagnostic["verification_failure_reason"] = "missing_return_data"
            return SubscriptionVerification(
                user_id=None,
                status="UNKNOWN",
                verified=False,
                active=False,
                reason="missing_return_data",
            )

        if user_id is None:
            if pending_state == "found":
                pending_state = "missing"
            diagnostic["pending_token_state"] = pending_state
            diagnostic["verification_failure_reason"] = (
                f"pending_token_{pending_state}"
                if pending_state in {"missing", "expired", "consumed"}
                else "invalid_or_expired_token"
            )
            return SubscriptionVerification(
                user_id=None,
                status="UNKNOWN",
                verified=False,
                active=False,
                reason="invalid_or_expired_token",
            )

        result = _verify_and_store(subscription_id, user_id, diagnostic)
        if not result.verified:
            return result

        try:
            persisted = persist_verified_subscription(
                token, user_id, subscription_id, result.status
            )
        except Exception:
            diagnostic["verification_failure_reason"] = (
                "pending_persistence_error"
            )
            raise
        if persisted:
            diagnostic["pending_token_state"] = "consumed"
        else:
            try:
                existing = get_by_subscription(subscription_id)
            except Exception:
                diagnostic["verification_failure_reason"] = (
                    "pending_persistence_error"
                )
                raise
            if not existing or existing.get("user_id") != user_id:
                try:
                    pending_state, _ = inspect_pending_token(token)
                except Exception:
                    diagnostic["verification_failure_reason"] = (
                        "pending_token_lookup_error"
                    )
                    raise
                diagnostic["pending_token_state"] = pending_state
                diagnostic["verification_failure_reason"] = (
                    "pending_persistence_failed"
                )
                return SubscriptionVerification(
                    user_id=None,
                    status=result.status,
                    verified=False,
                    active=False,
                    reason="invalid_or_expired_token",
                )
            diagnostic["pending_token_state"] = "consumed"
        return result
    finally:
        try:
            database_path_fingerprint = get_database_path_fingerprint()
        except Exception:
            database_path_fingerprint = None
        logged_state = diagnostic["pending_token_state"]
        if logged_state == "found":
            logged_state = "active"
        if logged_state not in {
            "active",
            "missing",
            "expired",
            "consumed",
            "error",
        }:
            logged_state = "error"
        log_pending_token_diagnostic(
            phase="return_lookup",
            token=token,
            database_path_fingerprint=database_path_fingerprint,
            process_role="streamlit",
            process_instance_id=_PROCESS_INSTANCE_ID,
            lookup_result=pending_lookup_result,
            token_state=logged_state,
        )
        log_paypal_return_diagnostic(**diagnostic)


def refresh_user_subscription(user_id: str) -> SubscriptionVerification:
    """Recheck the latest stored subscription with PayPal before restoring access."""
    subscription = get_by_user(user_id)
    if subscription is None:
        return SubscriptionVerification(
            user_id=user_id,
            status="NONE",
            verified=False,
            active=False,
            reason="no_subscription",
        )

    subscription_id = subscription.get("subscription_id")
    if not isinstance(subscription_id, str) or not subscription_id:
        return SubscriptionVerification(
            user_id=user_id,
            status="UNKNOWN",
            verified=False,
            active=False,
            reason="invalid_stored_subscription",
        )
    result = _verify_and_store(subscription_id, user_id)
    if result.verified:
        upsert(subscription_id=subscription_id, user_id=user_id, status=result.status)
    return result
