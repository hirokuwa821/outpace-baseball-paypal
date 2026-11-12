import streamlit as st
st.set_page_config(page_title="Outpace Baseball", page_icon="⚾", layout="wide")

import streamlit_authenticator as stauth
import yaml
from yaml.loader import SafeLoader
import pandas as pd
import numpy as np
import plotly.graph_objects as go
import plotly.express as px
import cv2
import os
import sys
from pathlib import Path
import datetime
import secrets
import hashlib
import json
import uuid

# --- Path and imports ---
sys.path.append(str(Path(__file__).parent))
import streamlit.components.v1 as components
import pitch_speed_v23_auto_distance as pitch_mod
import pitch_logic
import aws_utils
import alexa_experience
import baseball_3d_component
import i18n
# PayPal specific imports – replace RevenueCat
from subscription import get_current_plan, set_plan, Plan, is_judge_mode
from paypal_sdk import create_subscription
from subscription_store import init_db, get_database_path_fingerprint
import paypal_state  # noqa: F401  (state location is resolved through purchase_guard)
from purchase_guard import (
    StateMismatch,
    check_state_match,
    existing_fastapi_decision,
    guarded_purchase,
    launch_fastapi_process,
)
from paypal_return import (
    clear_return_query_params,
    handle_paypal_return,
    refresh_user_subscription,
)
import requests
import subprocess
import atexit
import time

# Directory constants
ROOT_DIR = Path(__file__).resolve().parent.parent
VIDEOS_DIR = ROOT_DIR / "videos"
OUTPUT_DIR = ROOT_DIR / "output"
HISTORY_FILE = OUTPUT_DIR / "analysis_history.csv"
CONFIG_FILE = ROOT_DIR / "config.yaml"

# Language init
if "lang" not in st.session_state:
    st.session_state["lang"] = "ja"
lang = st.session_state["lang"]

# --- App User ID handling (same as before, but without RevenueCat) ---
url_uid = st.query_params.get("rc_uid")  # keep for compatibility, but not used
if url_uid and url_uid.startswith("rc_user_"):
    app_user_id = url_uid
    st.session_state["app_user_id"] = app_user_id
elif "app_user_id" in st.session_state:
    app_user_id = st.session_state["app_user_id"]
else:
    # Generate a stable anonymous ID (no RevenueCat needed)
    app_user_id = f"user_{uuid.uuid4().hex[:12]}"
    st.session_state["app_user_id"] = app_user_id

# No RevenueCat SDK initialization – we will use PayPal

# Initialize SQLite DB for subscription persistence
init_db()

import socket

FASTAPI_HOST = "127.0.0.1"
FASTAPI_PORT = 8000
FASTAPI_BASE_URL = f"http://{FASTAPI_HOST}:{FASTAPI_PORT}"

# Link target for the "アプリを開く" button shown after a verified Pro purchase.
# No identifiers are passed. Override with OUTPACE_MAIN_APP_URL (http:// or https:// only).
_DEFAULT_MAIN_APP_URL = "https://h4f6fpczxucshywwe7zw4f.streamlit.app/"
MAIN_APP_URL = os.getenv("OUTPACE_MAIN_APP_URL", _DEFAULT_MAIN_APP_URL).strip()
if not MAIN_APP_URL.startswith(("http://", "https://")):
    MAIN_APP_URL = _DEFAULT_MAIN_APP_URL


def redact_paypal_secrets(value):
    text = str(value)
    for secret_name in (
        "PAYPAL_CLIENT_ID",
        "PAYPAL_CLIENT_SECRET",
        "PAYPAL_JUDGE_TOKEN",
    ):
        secret_value = os.getenv(secret_name)
        if secret_value:
            text = text.replace(secret_value, "[REDACTED]")
    return text


def _fastapi_state_matches() -> tuple[bool, str]:
    return check_state_match(FASTAPI_BASE_URL, get_database_path_fingerprint(), requests.post)


def _fetch_openapi() -> object:
    response = requests.get(f"{FASTAPI_BASE_URL}/openapi.json", timeout=1)
    response.raise_for_status()
    return response.json()


def fastapi_healthcheck():
    # Reuse an existing FastAPI only if it serves the purchase endpoint AND uses the same
    # state location. Any doubt returns False, so a new child is started instead.
    return existing_fastapi_decision(_fetch_openapi, _fastapi_state_matches)


# FastAPI server start (independent process) with port‑conflict handling
@st.cache_resource(show_spinner=False)
def start_fastapi():
    host = FASTAPI_HOST
    port = FASTAPI_PORT
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            healthy, detail = fastapi_healthcheck()
            if healthy:
                return None, True, detail
            return None, False, (
                f"ポート {port} は使用中ですが、PayPal用FastAPIを確認できません。{detail}"
            )

    try:
        # The child gets the state directory this process resolved, set explicitly even when
        # the variable was unset, so both sides always use the same DB.
        proc = launch_fastapi_process(
            subprocess.Popen,
            host,
            port,
            ROOT_DIR,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
    except OSError as exc:
        return None, False, f"FastAPIプロセスを起動できません: {exc}"

    deadline = time.monotonic() + 15
    last_detail = "FastAPIの起動確認がタイムアウトしました。"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            return None, False, (
                f"FastAPIプロセスが起動確認前に終了しました (exit code {proc.returncode})。"
            )
        healthy, detail = fastapi_healthcheck()
        if healthy:
            return proc, True, "FastAPIを起動しました。"
        last_detail = detail
        time.sleep(0.25)

    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    return None, False, last_detail


fastapi_proc, fastapi_available, fastapi_status = start_fastapi()
if fastapi_proc is not None:
    atexit.register(lambda: fastapi_proc.terminate())
if not fastapi_available:
    st.warning(f"PayPal APIの利用可否を確認できません。購入時に接続エラーが表示されます。{fastapi_status}")
elif fastapi_proc is None:
    st.info(fastapi_status)

# Session state defaults
if "user_plan" not in st.session_state:
    st.session_state["user_plan"] = "Free"
if "isSubscribed" not in st.session_state:
    st.session_state["isSubscribed"] = False
if "is_premium" not in st.session_state:
    st.session_state["is_premium"] = False
if "show_paywall" not in st.session_state:
    st.session_state["show_paywall"] = False

# ------------------------------------------------------------------
# Process the PayPal return and restore persisted access on subsequent runs.
# ------------------------------------------------------------------
return_subscription_id = st.query_params.get("subscription_id")
return_token = st.query_params.get("pending_token")
return_verified = False

if return_subscription_id is not None or return_token is not None:
    try:
        database_path_match = False
        try:
            database_check = requests.post(
                f"{FASTAPI_BASE_URL}/diagnostics/database-path-match",
                json={"fingerprint": get_database_path_fingerprint()},
                timeout=2,
            )
            database_check.raise_for_status()
            check_result = database_check.json()
            database_path_match = (
                isinstance(check_result, dict)
                and check_result.get("database_path_match") is True
            )
        except (requests.RequestException, ValueError):
            database_path_match = False

        verification = handle_paypal_return(
            return_subscription_id,
            return_token,
            database_path_match=database_path_match,
        )
        if verification.user_id:
            app_user_id = verification.user_id
            st.session_state["app_user_id"] = app_user_id
        if verification.verified:
            return_verified = True
            set_plan("Pro" if verification.active else "Free", from_server=True)
            if verification.active:
                st.toast(i18n.t("paywall_purchase_success", lang, name="Pro"), icon="💎")
                st.session_state["show_open_app"] = True
            else:
                st.error(
                    f"PayPal subscription status is {verification.status}. Access remains Free."
                )
        elif verification.reason == "owner_mismatch":
            st.error("PayPal subscription owner mismatch. Access remains Free.")
        elif verification.reason == "subscription_id_mismatch":
            st.error("PayPal returned a different subscription ID. Access remains Free.")
        else:
            st.error("PayPal return could not be verified; no access was granted.")
    except Exception as exc:
        set_plan("Free", from_server=True)
        st.error(
            f"PayPal subscription verification failed ({type(exc).__name__}). "
            "Access remains Free."
        )
    finally:
        try:
            clear_return_query_params(st.query_params)
        except Exception as exc:
            st.error(f"Could not clear PayPal return parameters ({type(exc).__name__}).")

if not return_verified:
    try:
        verification = refresh_user_subscription(app_user_id)
        set_plan("Pro" if verification.active else "Free", from_server=True)
        if verification.reason == "owner_mismatch":
            st.error("Stored PayPal subscription owner mismatch. Access remains Free.")
    except Exception as exc:
        set_plan("Free", from_server=True)
        st.error(
            f"Could not verify the stored PayPal subscription ({type(exc).__name__}). "
            "Access remains Free."
        )

# ------------------------------------------------------------------
# UI – display plan selection and PayPal purchase button
# ------------------------------------------------------------------
col_head_t, col_head_sub = st.columns([3.5, 1.3])
with col_head_t:
    st.markdown(f"<h1 style='margin-bottom:0;color:#0f172a'>{i18n.t('app_title', lang)}</h1>", unsafe_allow_html=True)
    st.markdown(f"<p style='color:#334155'>{i18n.t('app_subtitle', lang)}</p>", unsafe_allow_html=True)
if st.session_state.get("show_open_app") and st.session_state.get("user_plan") == "Pro":
    st.link_button("アプリを開く", MAIN_APP_URL, type="primary")

with col_head_sub:
    cur_p = st.session_state.get("user_plan", "Free")

# Show Paywall button (only for demonstration – we will use PayPal directly)
if st.session_state.get("show_paywall", False):
    st.info("PayPal payment flow will be shown here in future revisions.")

# Subscription plan UI (reuse existing i18n plans)
current_plan = st.session_state.get("user_plan", "Free")
plans = i18n.get_subscription_plans(lang=lang, current_plan=current_plan)
cols = st.columns(3)
for col, p in zip(cols, plans):
    with col:
        items_html = "".join([f"<li>{f}</li>" for f in p["features"]])
        st.markdown(
            f"""
            <div style="border:2px solid {p['border']};border-radius:12px;padding:14px;background:{p['bg']};height:320px;box-shadow:0 2px 8px rgba(0,0,0,0.04);margin-bottom:10px;">
                <div style="font-size:11px;font-weight:bold;color:#0284c7;">{p['badge']}</div>
                <h4 style="margin:4px 0 0 0;color:#0f172a;">{p['name']}</h4>
                <div style="font-size:22px;font-weight:900;color:#0f172a;margin:8px 0;">{p['price']} <span style="font-size:11px;font-weight:normal;color:#64748b;">{p['period']}</span></div>
                <ul style="font-size:11.5px;color:#334155;padding-left:16px;line-height:1.7;margin-bottom:0;">{items_html}</ul>
            </div>
            """,
            unsafe_allow_html=True,
        )
        if current_plan == p["id"]:
            btn_curr_lbl = i18n.t("paywall_current_plan", lang)
            st.button(btn_curr_lbl, key=f"pw_btn_{p['id']}", disabled=True, use_container_width=True)
        else:
            if p["id"] == "Free":
                btn_select_lbl = i18n.t("paywall_test_purchase", lang, name=p['name'])
                if st.button(btn_select_lbl, key=f"pw_btn_{p['id']}", type="secondary", use_container_width=True):
                    set_plan("Free", from_server=False)
                    st.toast(i18n.t("paywall_free_switched", lang), icon="🌱")
                    st.rerun()
            else:
                btn_select_lbl = f"💳 {p['name']} を購入 (Sandbox)"
                if st.button(btn_select_lbl, key=f"pw_btn_{p['id']}", type="primary", use_container_width=True):
                    st.session_state.pop("paypal_approval_url", None)
                    st.session_state.pop("paypal_purchase_error", None)
                    try:
                        payload = {
                            "user_id": app_user_id,
                            "return_url": "http://localhost:8502/",
                            "cancel_url": "http://localhost:8502/",
                        }
                        # Verifies the state location first; no request is sent on mismatch.
                        resp = guarded_purchase(
                            FASTAPI_BASE_URL,
                            payload,
                            fingerprint=get_database_path_fingerprint(),
                            post_check=requests.post,
                            post_create=requests.post,
                            timeout=180,
                        )
                        if not resp.ok:
                            try:
                                response_body = resp.json()
                                detail = (
                                    response_body.get("detail", response_body)
                                    if isinstance(response_body, dict)
                                    else response_body
                                )
                            except ValueError:
                                detail = resp.text or resp.reason
                            detail = json.dumps(detail, ensure_ascii=False) if not isinstance(detail, str) else detail
                            detail = redact_paypal_secrets(detail)
                            st.session_state["paypal_purchase_error"] = (
                                f"FastAPIがHTTP {resp.status_code}を返しました: {detail[:1500]}"
                            )
                        else:
                            try:
                                approval_url = resp.json().get("approval_url")
                            except ValueError:
                                approval_url = None
                            if isinstance(approval_url, str) and approval_url:
                                st.session_state["paypal_approval_url"] = approval_url
                            else:
                                st.session_state["paypal_purchase_error"] = (
                                    f"FastAPIはHTTP {resp.status_code}を返しましたが、"
                                    "approval_urlが応答にありません。"
                                )
                    except StateMismatch as exc:
                        st.session_state["paypal_purchase_error"] = str(exc)
                    except requests.RequestException as exc:
                        st.session_state["paypal_purchase_error"] = (
                            f"FastAPIへのPOSTに失敗しました: {redact_paypal_secrets(exc)}"
                        )
                        start_fastapi.clear()
                    except Exception as exc:
                        st.session_state["paypal_purchase_error"] = (
                            f"購入処理に失敗しました ({type(exc).__name__}): "
                            f"{redact_paypal_secrets(exc)}"
                        )
        # Footer note per plan
        footer_note = i18n.t("paywall_footer_note", lang)
        st.markdown(f"<div style='text-align:center;margin-top:14px;padding-top:10px;border-top:1px solid #e2e8f0;font-size:11px;color:#64748b;'>{footer_note}</div>", unsafe_allow_html=True)

if st.session_state.get("paypal_purchase_error"):
    st.error(st.session_state["paypal_purchase_error"])
if st.session_state.get("paypal_approval_url"):
    st.link_button(
        label="Open PayPal Checkout",
        url=st.session_state["paypal_approval_url"],
        type="primary",
    )

# Custom CSS (unchanged) ... (rest of file unchanged)

col_head_t, col_head_sub = st.columns([3.5, 1.3])
# ... rest of existing UI code (omitted for brevity)
