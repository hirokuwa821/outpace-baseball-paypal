"""Purchase guard and FastAPI child launch: decisions tested with fake HTTP and fake Popen.

No server, no real DB, no PayPal call, no Streamlit import. The app.py checks at the end are
static text checks, so the purchase path cannot be bypassed by a later edit unnoticed.
"""
import os
import re
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

PROJECT_DIR = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_DIR / "src"
sys.path.insert(0, str(SRC_DIR))

import paypal_state
import purchase_guard as guard
from purchase_guard import StateMismatch

STATE_ENV = paypal_state.STATE_DIR_ENV_VAR
BASE = "http://127.0.0.1:8000"
FINGERPRINT = "a" * 64


def _resp(body=None, status=200, json_error=None):
    response = MagicMock()
    response.status_code = status
    if json_error:
        response.json.side_effect = json_error
    else:
        response.json.return_value = body
    if status >= 400:
        response.raise_for_status.side_effect = requests.HTTPError(f"HTTP {status}")
    else:
        response.raise_for_status.return_value = None
    return response


# ---- state-location check (fail closed) -------------------------------------

def test_matching_state_reports_true_with_one_check_call():
    post = MagicMock(return_value=_resp({"database_path_match": True}))
    assert guard.check_state_match(BASE, FINGERPRINT, post) == (True, "")
    assert post.call_count == 1


def test_mismatching_state_reports_false_with_a_safe_message():
    post = MagicMock(return_value=_resp({"database_path_match": False}))
    ok, message = guard.check_state_match(BASE, FINGERPRINT, post)
    assert ok is False
    assert message == guard.MISMATCH_MESSAGE


def test_transport_failure_fails_closed_without_leaking_its_text():
    post = MagicMock(side_effect=requests.ConnectionError("connect to http://internal.example:9 refused"))
    ok, message = guard.check_state_match(BASE, FINGERPRINT, post)
    assert ok is False
    assert message == guard.UNVERIFIED_MESSAGE
    assert "internal.example" not in message


def test_http_error_status_and_non_json_fail_closed():
    for response in (_resp(status=500), _resp(json_error=ValueError("bad json"))):
        ok, message = guard.check_state_match(BASE, FINGERPRINT, MagicMock(return_value=response))
        assert (ok, message) == (False, guard.UNVERIFIED_MESSAGE)


def test_unexpected_body_shapes_fail_closed():
    for body in ([True], "yes", {"database_path_match": "true"}, {}):
        ok, _ = guard.check_state_match(BASE, FINGERPRINT, MagicMock(return_value=_resp(body)))
        assert ok is False, body


# ---- reuse of an already-running FastAPI ------------------------------------

_OPENAPI_WITH_PURCHASE = {"paths": {"/create-subscription": {"post": {}}}}


def test_existing_fastapi_is_reused_only_when_state_matches():
    decision = guard.existing_fastapi_decision(lambda: _OPENAPI_WITH_PURCHASE, lambda: (True, ""))
    assert decision == (True, guard.REUSE_MESSAGE)


def test_existing_fastapi_with_a_different_state_location_is_not_reused():
    decision = guard.existing_fastapi_decision(lambda: _OPENAPI_WITH_PURCHASE, lambda: (False, guard.MISMATCH_MESSAGE))
    assert decision == (False, guard.MISMATCH_MESSAGE)


def test_unreachable_openapi_is_not_reused_and_state_is_not_even_checked():
    check = MagicMock(return_value=(True, ""))

    def unreachable():
        raise requests.ConnectionError("http://127.0.0.1:8000 refused")

    ok, message = guard.existing_fastapi_decision(unreachable, check)
    assert ok is False
    assert message == guard.UNAVAILABLE_MESSAGE
    assert "127.0.0.1" not in message
    assert not check.called


def test_server_without_the_purchase_endpoint_is_not_reused():
    ok, _ = guard.existing_fastapi_decision(lambda: {"paths": {}}, lambda: (True, ""))
    assert ok is False


# ---- purchase guard -----------------------------------------------------------

def test_purchase_proceeds_when_state_matches_and_hands_over_the_response():
    response = _resp({"approval_url": "https://www.sandbox.paypal.com/x"})
    post_check = MagicMock(return_value=_resp({"database_path_match": True}))
    post_create = MagicMock(return_value=response)
    payload = {"user_id": "u-1", "return_url": "r", "cancel_url": "c"}
    result = guard.guarded_purchase(BASE, payload, fingerprint=FINGERPRINT,
                                    post_check=post_check, post_create=post_create)
    assert result is response
    assert post_create.call_count == 1
    args, kwargs = post_create.call_args
    assert args == (f"{BASE}/create-subscription",)
    assert kwargs == {"json": payload, "timeout": 180}


def test_purchase_is_refused_on_mismatch_and_the_purchase_api_is_called_zero_times():
    post_check = MagicMock(return_value=_resp({"database_path_match": False}))
    post_create = MagicMock()
    try:
        guard.guarded_purchase(BASE, {}, fingerprint=FINGERPRINT, post_check=post_check, post_create=post_create)
    except StateMismatch as error:
        assert str(error) == guard.MISMATCH_MESSAGE
    else:
        raise AssertionError("mismatch must refuse the purchase")
    assert post_create.call_count == 0


def test_purchase_is_refused_when_verification_fails_and_the_purchase_api_is_called_zero_times():
    failures = (
        MagicMock(side_effect=requests.ConnectionError("down")),
        MagicMock(side_effect=RuntimeError("unexpected")),
        MagicMock(return_value=_resp(status=503)),
    )
    for post_check in failures:
        post_create = MagicMock()
        try:
            guard.guarded_purchase(BASE, {}, fingerprint=FINGERPRINT, post_check=post_check, post_create=post_create)
        except StateMismatch as error:
            assert str(error) == guard.UNVERIFIED_MESSAGE
        else:
            raise AssertionError("failed verification must refuse the purchase")
        assert post_create.call_count == 0


# ---- child environment and launch --------------------------------------------

def test_child_env_sets_the_state_dir_when_the_variable_is_unset():
    with tempfile.TemporaryDirectory() as local:
        env = paypal_state.child_env({"PATH": "p", "LOCALAPPDATA": local})
        assert env[STATE_ENV] == str((Path(local) / "OutpaceBaseball" / "paypal").resolve())


def test_child_env_keeps_an_explicit_override():
    with tempfile.TemporaryDirectory() as temp:
        env = paypal_state.child_env({STATE_ENV: temp})
        assert env[STATE_ENV] == str(Path(temp).resolve())


def test_launch_passes_args_cwd_env_and_flags_to_popen():
    popen = MagicMock(return_value="PROC")
    with tempfile.TemporaryDirectory() as temp:
        base = {"PATH": "p", STATE_ENV: temp}
        result = guard.launch_fastapi_process(popen, "127.0.0.1", 8000, PROJECT_DIR,
                                              creationflags=7, base_env=base)
    assert result == "PROC"
    args, kwargs = popen.call_args
    assert args[0][1:] == ["-m", "uvicorn", "paypal_server:app", "--app-dir",
                           str(PROJECT_DIR / "src"), "--host", "127.0.0.1", "--port", "8000"]
    assert kwargs["cwd"] == str(PROJECT_DIR)
    assert kwargs["creationflags"] == 7
    assert kwargs["env"][STATE_ENV] == str(Path(base[STATE_ENV]).resolve())


def test_launch_keeps_other_variables_and_does_not_touch_os_environ():
    popen = MagicMock(return_value=None)
    before = dict(os.environ)
    with tempfile.TemporaryDirectory() as local:
        guard.launch_fastapi_process(popen, "127.0.0.1", 8000, PROJECT_DIR,
                                     base_env={"PATH": "p", "KEEP": "1", "LOCALAPPDATA": local})
        env = popen.call_args.kwargs["env"]
        assert env["PATH"] == "p" and env["KEEP"] == "1"
        assert env[STATE_ENV] == str((Path(local) / "OutpaceBaseball" / "paypal").resolve())
    assert dict(os.environ) == before


# ---- messages and structure ---------------------------------------------------

_HEX64 = re.compile(r"[0-9a-f]{64}")


def test_user_messages_contain_no_paths_fingerprints_or_secrets():
    messages = [
        guard.MISMATCH_MESSAGE,
        guard.UNVERIFIED_MESSAGE,
        guard.UNAVAILABLE_MESSAGE,
        guard.REUSE_MESSAGE,
    ]
    for text in messages:
        assert "\\" not in text and "/" not in text
        assert "subscriptions.db" not in text
        assert not _HEX64.search(text)
        assert "CLIENT" not in text.upper() and "SECRET" not in text.upper()


def test_guard_module_does_not_import_streamlit():
    source =(SRC_DIR / "purchase_guard.py").read_text(encoding="utf-8")
    assert "import streamlit" not in source and "from streamlit" not in source


def test_app_has_no_direct_purchase_request_or_direct_popen():
    text = (SRC_DIR / "app.py").read_text(encoding="utf-8")
    assert "/create-subscription" not in text
    assert "subprocess.Popen(" not in text
    assert "_StateMismatch" not in text
    assert "guarded_purchase(" in text
    assert "launch_fastapi_process(" in text
    assert "except StateMismatch as exc:" in text
