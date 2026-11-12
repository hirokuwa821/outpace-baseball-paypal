# subscription.py – abstraction layer for payment back‑ends
"""Utility module that hides whether the app uses RevenueCat or PayPal.

Only the PayPal‑integration branch sets ``USE_REVENUECAT=0`` in ``.env``.
All UI code imports from this module, so the original RevenueCat code
remains untouched.

The module also encapsulates the *judge‑only* mode required for the
Hackathon judges.
"""

import os
import streamlit as st
from typing import Literal

# ---------------------------------------------------------------------------
# Configuration flags (read from .env – never hard‑coded)
# ---------------------------------------------------------------------------
USE_REVENUECAT: bool = os.getenv("USE_REVENUECAT", "1") == "1"
PAYPAL_JUDGE_MODE: bool = os.getenv("PAYPAL_JUDGE_MODE", "0") == "1"
# The environment PayPal is running in (sandbox|live).  Default to sandbox.
PAYPAL_ENV: str = os.getenv("PAYPAL_ENV", "sandbox").lower()

# ---------------------------------------------------------------------------
# Helper predicates
# ---------------------------------------------------------------------------
def use_paypal() -> bool:
    """Return ``True`` when the PayPal implementation should be used.

    The PayPal branch forces ``USE_REVENUECAT`` to ``0``; the original
    branch leaves it at the default ``1``.
    """
    return not USE_REVENUECAT


def is_judge_mode() -> bool:
    """True when the hackathon judge‑only shortcut is enabled.

    The flag is evaluated **server‑side only**; the UI never exposes it.
    """
    return PAYPAL_JUDGE_MODE


def judge_allowed() -> bool:
    """Judge mode may be used **only** in the PayPal sandbox.

    If the environment is anything other than ``sandbox`` an exception is
    raised to avoid accidental production activation.
    """
    if is_judge_mode() and PAYPAL_ENV != "sandbox":
        raise RuntimeError(
            "PAYPAL_JUDGE_MODE is enabled outside the sandbox – this is not allowed."
        )
    return is_judge_mode()

# ---------------------------------------------------------------------------
# Session‑state helpers – all UI code works through these functions
# ---------------------------------------------------------------------------
Plan = Literal["Free", "Pro"]

def get_current_plan() -> Plan:
    """Return the user's current plan (defaults to ``Free``)."""
    return st.session_state.get("user_plan", "Free")  # type: ignore[return-value]


def set_plan(plan: Plan, *, from_server: bool = False) -> None:
    """Persist the chosen plan in ``st.session_state``.

    ``from_server`` signals that the change originated from a verified
    server‑side event (e.g. webhook or PayPal API verification).  When the
    flag is ``False`` the call comes from UI interaction and will trigger a
    ``st.rerun()`` after the update.
    """
    st.session_state["user_plan"] = plan
    # Convenience flags used throughout the existing code base
    st.session_state["isSubscribed"] = plan == "Pro"
    st.session_state["is_premium"] = plan == "Pro"
    if not from_server:
        # Force a UI refresh so the newly unlocked features appear instantly
        st.rerun()

# Exported names – the UI imports ``*`` from this file
__all__ = [
    "use_paypal",
    "is_judge_mode",
    "judge_allowed",
    "get_current_plan",
    "set_plan",
    "Plan",
]
