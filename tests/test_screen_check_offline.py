"""Offline rehearsal of the Streamlit purchase path: matching and mismatching state locations.

The purchase endpoint is the localhost stub from screen_check_support, so no PayPal call is
possible. Proxy variables are cleared so localhost traffic cannot leave the machine. All
data is in temp folders built by build_synthetic_state.
"""
import contextlib
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

import requests

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))
sys.path.insert(0, str(PROJECT_DIR / "tests"))

import paypal_state
import purchase_guard as guard
import subscription_store
from purchase_guard import StateMismatch
from screen_check_support import FAKE_APPROVAL_URL, StubFastAPI, build_synthetic_state

STATE_ENV = paypal_state.STATE_DIR_ENV_VAR
_NO_PROXY = {"NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
             "HTTP_PROXY": "", "HTTPS_PROXY": "", "http_proxy": "", "https_proxy": ""}
_PAYLOAD = {"user_id": "u-screen", "return_url": "http://localhost:8502/", "cancel_url": "http://localhost:8502/"}


@contextlib.contextmanager
def _streamlit_view(state: Path):
    """What the Streamlit process sees: the same env var and the same DB location."""
    with patch.dict(os.environ, {STATE_ENV: str(state), **_NO_PROXY}), \
         patch.object(subscription_store, "DB_PATH", str(paypal_state.db_path())):
        yield


def _fingerprint_of(path: Path) -> str:
    with patch.object(subscription_store, "DB_PATH", str(path)):
        return subscription_store.get_database_path_fingerprint()


# ---- A. matching locations: the purchase proceeds to the stub -----------------

def test_matching_locations_reuse_fastapi_and_reach_the_stub_once():
    with tempfile.TemporaryDirectory() as temp:
        state = build_synthetic_state(Path(temp))
        fingerprint = _fingerprint_of(state / "subscriptions.db")
        stub = StubFastAPI(expected_fingerprint=fingerprint).start()
        try:
            with _streamlit_view(state):
                assert guard.check_state_match(stub.base_url, fingerprint, requests.post) == (True, "")
                reuse = guard.existing_fastapi_decision(
                    lambda: requests.get(f"{stub.base_url}/openapi.json", timeout=2).json(),
                    lambda: guard.check_state_match(stub.base_url, fingerprint, requests.post),
                )
                assert reuse == (True, guard.REUSE_MESSAGE)
                response = guard.guarded_purchase(
                    stub.base_url, _PAYLOAD, fingerprint=fingerprint,
                    post_check=requests.post, post_create=requests.post,
                )
                assert response.json()["approval_url"] == FAKE_APPROVAL_URL
            assert stub.purchase_calls == 1
        finally:
            stub.stop()


# ---- B. mismatching locations: the purchase never reaches the stub ------------

def test_mismatching_locations_refuse_reuse_and_never_reach_the_purchase_endpoint():
    with tempfile.TemporaryDirectory() as temp:
        state = build_synthetic_state(Path(temp))
        own = _fingerprint_of(state / "subscriptions.db")
        other = _fingerprint_of(Path(temp) / "elsewhere" / "subscriptions.db")
        stub = StubFastAPI(expected_fingerprint=other).start()  # the server uses another location
        try:
            with _streamlit_view(state):
                reuse = guard.existing_fastapi_decision(
                    lambda: requests.get(f"{stub.base_url}/openapi.json", timeout=2).json(),
                    lambda: guard.check_state_match(stub.base_url, own, requests.post),
                )
                assert reuse == (False, guard.MISMATCH_MESSAGE)
                try:
                    guard.guarded_purchase(stub.base_url, _PAYLOAD, fingerprint=own,
                                           post_check=requests.post, post_create=requests.post)
                except StateMismatch as error:
                    assert str(error) == guard.MISMATCH_MESSAGE
                else:
                    raise AssertionError("mismatch must refuse the purchase")
            assert stub.purchase_calls == 0
        finally:
            stub.stop()


def test_unreachable_server_fails_closed_and_sends_no_purchase():
    with tempfile.TemporaryDirectory() as temp:
        state = build_synthetic_state(Path(temp))
        own = _fingerprint_of(state / "subscriptions.db")
        stub = StubFastAPI(expected_fingerprint=own).start()
        base_url = stub.base_url
        stub.stop()  # nothing is listening any more
        with _streamlit_view(state):
            post_create = requests.post
            calls = []

            def counting_create(*args, **kwargs):
                calls.append(args)
                return post_create(*args, **kwargs)

            try:
                guard.guarded_purchase(base_url, _PAYLOAD, fingerprint=own,
                                       post_check=requests.post, post_create=counting_create)
            except StateMismatch as error:
                assert str(error) == guard.UNVERIFIED_MESSAGE
            else:
                raise AssertionError("an unreachable server must refuse the purchase")
        assert calls == []


# ---- C. the synthetic state passes the same startup checks as the app ---------

def test_synthetic_state_passes_startup_checks_and_touches_no_legacy_file():
    with tempfile.TemporaryDirectory() as temp:
        state = build_synthetic_state(Path(temp))
        with _streamlit_view(state):
            subscription_store.init_db()  # what app.py runs on import
            paypal_state.assert_migration_state()
            subscription_store.verify_quarantine_schema()
        assert (state / paypal_state.MIGRATION_MARKER_NAME).exists()
        assert paypal_state.same_path(state, Path(temp) / "state")


def test_stub_is_localhost_only_and_its_approval_url_is_not_a_paypal_host():
    assert FAKE_APPROVAL_URL.startswith("http://127.0.0.1")
    assert "paypal" not in FAKE_APPROVAL_URL.lower()
