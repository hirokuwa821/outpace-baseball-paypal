# src/paypal_server.py – FastAPI server for PayPal subscription flow

"""Minimal FastAPI service used by the PayPal‑integration version of Outpace Baseball.

The Streamlit UI launches this server in a background process (see ``src/app.py``).
It provides a single endpoint:

* ``POST /create-subscription`` – receives a JSON payload with ``user_id``,
  ``return_url`` and ``cancel_url``. It calls :func:`paypal_sdk.create_subscription`
  to obtain an approval URL and returns ``{"approval_url": <url>}``.

The endpoint does **not** require a judge token – normal users can start a
subscription without any extra authentication.  Errors are returned with an
appropriate HTTP status code and a JSON ``{"detail": ...}`` payload so that the
client can display a helpful message.
"""

import logging
import secrets
import uuid
from contextlib import asynccontextmanager

import requests
from urllib.parse import urlparse, parse_qsl, urlencode, urlunparse

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from paypal_sdk import PlanCreationError, create_subscription, is_valid_judge_token
import subscription_store
from subscription_store import store_pending, get_database_path_fingerprint
from paypal_return_diagnostics import log_pending_token_diagnostic

@asynccontextmanager
async def _lifespan(_app: FastAPI):
    # Schema creation happens at server start only, never on import or per request.
    # Tests that use TestClient without the context manager never run this.
    subscription_store.init_db()
    yield


app = FastAPI(lifespan=_lifespan)
logger = logging.getLogger("paypal.server")


def _summarize_failure(exc: Exception) -> dict:
    """Operator-only summary. Never includes PayPal response bodies or credentials."""
    summary: dict = {"error_type": type(exc).__name__}
    if isinstance(exc, PlanCreationError):
        summary.update(reason=exc.reason, entry_id=exc.entry_id, plan_id=exc.plan_id)
    elif isinstance(exc, requests.HTTPError) and exc.response is not None:
        summary["http_status"] = exc.response.status_code
        try:
            body = exc.response.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            summary["paypal_name"] = str(body.get("name", ""))[:100]
            summary["debug_id"] = str(body.get("debug_id", ""))[:100]
    return summary
_PROCESS_INSTANCE_ID = uuid.uuid4().hex
PURCHASE_FAILED_MESSAGE = "Could not start the subscription."

class CreateSubscriptionRequest(BaseModel):
    """Payload expected from the Streamlit UI.

    ``return_url`` and ``cancel_url`` are URLs that PayPal will redirect to after
    the buyer approves or cancels the checkout.  They normally point back to the
    Streamlit front‑end (e.g. ``http://localhost:8501/?``).
    """

    user_id: str = Field(..., description="An opaque identifier for the user – stored as ``custom_id`` in PayPal.")
    return_url: str = Field(..., description="URL PayPal redirects to after successful approval.")
    cancel_url: str = Field(..., description="URL PayPal redirects to after a cancel action.")
    # Optional judge token – only used in the hackathon judge mode.
    judge_token: str | None = Field(default=None, description="Token that enables the judge‑only shortcut.")


class DatabasePathCheckRequest(BaseModel):
    fingerprint: str = Field(..., min_length=64, max_length=64)


@app.post("/diagnostics/database-path-match")
def database_path_match_endpoint(req: DatabasePathCheckRequest):
    """Compare DB paths through fingerprints without returning either path."""
    return {
        "database_path_match": secrets.compare_digest(
            req.fingerprint, get_database_path_fingerprint()
        )
    }


@app.post("/create-subscription")
def create_subscription_endpoint(req: CreateSubscriptionRequest):
    """Create a PayPal subscription and return the approval URL.

    The function validates the optional ``judge_token`` when present.  Normal
    users do not provide a token, so the check is bypassed.
    """
    # Judge‑mode validation – only allowed when the server explicitly receives a
    # token and the environment is sandbox.  The SDK performs the environment
    # check, we just guard the token.
    if req.judge_token is not None:
        if not is_valid_judge_token(req.judge_token):
            raise HTTPException(status_code=403, detail="Invalid judge token")

    # Generate a secure pending token and store it before calling PayPal SDK
    pending_token = secrets.token_urlsafe(32)
    try:
        store_pending(pending_token, req.user_id)
    except Exception as exc:
        log_pending_token_diagnostic(
            phase="purchase_store",
            token=pending_token,
            database_path_fingerprint=get_database_path_fingerprint(),
            process_role="fastapi",
            process_instance_id=_PROCESS_INSTANCE_ID,
            save_success=False,
        )
        # DB insertion failed – do not attempt PayPal call
        raise HTTPException(status_code=400, detail="Failed to store pending token")
    log_pending_token_diagnostic(
        phase="purchase_store",
        token=pending_token,
        database_path_fingerprint=get_database_path_fingerprint(),
        process_role="fastapi",
        process_instance_id=_PROCESS_INSTANCE_ID,
        save_success=True,
    )

    # Append pending_token as a query parameter to the return_url (preserving existing params)
    parsed_url = urlparse(req.return_url)
    query_params = dict(parse_qsl(parsed_url.query))
    query_params["pending_token"] = pending_token
    return_url_with_token = urlunparse(parsed_url._replace(query=urlencode(query_params)))

    try:
        approval_url = create_subscription(
            user_id=req.user_id,
            return_url=return_url_with_token,
            cancel_url=req.cancel_url,
        )
    except Exception as exc:
        # Never send exception text, PayPal response bodies, or plan IDs to the browser.
        # Log the full exception for operators under a random reference code instead.
        reference = secrets.token_hex(4).upper()
        # HTTP errors are logged without traceback, because the exception message contains
        # the PayPal response body. Other errors keep the traceback for operators.
        logger.error(
            "create-subscription failed reference=%s summary=%s",
            reference,
            _summarize_failure(exc),
            exc_info=None if isinstance(exc, requests.HTTPError) else exc,
        )
        raise HTTPException(
            status_code=400,
            detail=f"{PURCHASE_FAILED_MESSAGE} Reference code: {reference}",
        )

    return {"approval_url": approval_url, "pending_token": pending_token}
