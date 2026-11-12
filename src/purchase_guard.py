"""UI-independent guards for the PayPal purchase flow.

Imported by app.py and by tests. It never imports Streamlit, and every network or process
call is passed in, so tests can run without a server, a DB, or PayPal.

Fail-closed rules:
* Any failure to verify the FastAPI state location means no purchase request is sent.
* Messages never include paths, fingerprints, exception text, or PayPal response bodies.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Callable

import paypal_state

MISMATCH_MESSAGE = "PayPal サーバーと画面の保存先が一致しないため、購入を行いません。"
UNVERIFIED_MESSAGE = "PayPal サーバーの保存先を確認できないため、購入を行いません。"
UNAVAILABLE_MESSAGE = "FastAPI の応答を確認できません。"
REUSE_MESSAGE = "既存のFastAPIを利用します。"


class StateMismatch(RuntimeError):
    """Purchase refused: the state location is not verified as the same. Message is safe to show."""


def check_state_match(base_url: str, fingerprint: str, post: Callable) -> tuple[bool, str]:
    """Ask FastAPI whether it uses the same DB as this process (fingerprints only)."""
    try:
        response = post(
            f"{base_url}/diagnostics/database-path-match",
            json={"fingerprint": fingerprint},
            timeout=2,
        )
        response.raise_for_status()
        body = response.json()
    except Exception:  # any transport, status, or parse failure is treated as unverified
        return False, UNVERIFIED_MESSAGE
    if isinstance(body, dict) and body.get("database_path_match") is True:
        return True, ""
    return False, MISMATCH_MESSAGE


def existing_fastapi_decision(
    fetch_openapi: Callable[[], object],
    check_state: Callable[[], tuple[bool, str]],
) -> tuple[bool, str]:
    """Decide whether an already-running FastAPI may be reused. Fail closed on any doubt."""
    try:
        openapi = fetch_openapi()
    except Exception:
        return False, UNAVAILABLE_MESSAGE
    paths = openapi.get("paths") if isinstance(openapi, dict) else None
    subscription_path = paths.get("/create-subscription") if isinstance(paths, dict) else None
    if not isinstance(subscription_path, dict) or "post" not in subscription_path:
        return False, "既存サーバーに購入の受付機能がありません。"
    state_ok, state_message = check_state()
    if not state_ok:
        return False, state_message
    return True, REUSE_MESSAGE


def guarded_purchase(
    base_url: str,
    payload: dict,
    *,
    fingerprint: str,
    post_check: Callable,
    post_create: Callable,
    timeout: float = 180,
):
    """Send the purchase request only after the state location is verified as the same.

    If verification fails, ``post_create`` is never called: no request reaches FastAPI or
    PayPal.
    """
    ok, message = check_state_match(base_url, fingerprint, post_check)
    if not ok:
        raise StateMismatch(message)
    return post_create(f"{base_url}/create-subscription", json=payload, timeout=timeout)


def fastapi_child_args(host: str, port: int, root_dir) -> list:
    return [
        sys.executable,
        "-m",
        "uvicorn",
        "paypal_server:app",
        "--app-dir",
        str(Path(root_dir) / "src"),
        "--host",
        host,
        "--port",
        str(port),
    ]


def launch_fastapi_process(popen: Callable, host: str, port: int, root_dir, *, creationflags: int = 0,
                           base_env=None):
    """Start the FastAPI child with the state directory set explicitly in its environment."""
    return popen(
        fastapi_child_args(host, port, root_dir),
        cwd=str(root_dir),
        env=paypal_state.child_env(base_env),
        creationflags=creationflags,
    )
