"""Stage-1 guards for paypal_sdk: subscriber removal, approve URL host check, plan validation.

Every test is fully offline: the resource cache is redirected to a temp directory,
the environment is pinned, and any real HTTP call fails the test.
"""
import contextlib
import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))

import paypal_sdk
import subscription_store

SANDBOX_APPROVE = "https://www.sandbox.paypal.com/webapps/billing/subscriptions?ba_token=BA-TEST"
LIVE_APPROVE = "https://www.paypal.com/webapps/billing/subscriptions?ba_token=BA-TEST"
SEED_CACHE = {"sandbox": {"product_id": "PROD-S", "plan_id": "PLAN-OLD"}}


# ---- helpers ----------------------------------------------------------------

def _no_network(*args, **kwargs):
    raise AssertionError("test attempted a real network call")


def _json_response(payload):
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def _bad_json_response():
    response = MagicMock()
    response.status_code = 200
    response.json.side_effect = ValueError("Expecting value")
    return response


@contextlib.contextmanager
def _isolated(environment="sandbox"):
    """Yield a temp cache file path with env pinned and all network entry points blocked."""
    with tempfile.TemporaryDirectory() as temp_dir:
        cache_file = Path(temp_dir) / "paypal_resource_ids.json"
        db_path = Path(temp_dir) / "test_subscriptions.db"
        assert db_path.resolve() != (PROJECT_DIR / "src" / "subscriptions.db").resolve()
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(subscription_store, "DB_PATH", str(db_path)))
            subscription_store.init_db()
            stack.enter_context(patch.object(paypal_sdk, "RESOURCE_IDS_CACHE_FILE", cache_file))
            stack.enter_context(patch.object(paypal_sdk, "get_paypal_env", return_value=environment))
            stack.enter_context(patch.object(paypal_sdk.requests, "request", side_effect=_no_network))
            stack.enter_context(patch.object(paypal_sdk.requests, "post", side_effect=_no_network))
            yield cache_file


def _seed(cache_file):
    text = json.dumps(SEED_CACHE)
    cache_file.write_text(text, encoding="utf-8")
    return text


def _regular_cycle(currency="JPY", value="1980", interval_unit="MONTH", interval_count=1, tenure="REGULAR"):
    return {
        "tenure_type": tenure,
        "frequency": {"interval_unit": interval_unit, "interval_count": interval_count},
        "pricing_scheme": {"fixed_price": {"currency_code": currency, "value": value}},
    }


def _plan(cycles=None, product_id="PROD-S", status="ACTIVE", **cycle_overrides):
    if cycles is None:
        cycles = [_regular_cycle(**cycle_overrides)]
    return {"id": "PLAN-OLD", "product_id": product_id, "status": status, "billing_cycles": cycles}


def _call(cache_file, get_item, post_items=()):
    """Run get_or_create_plan with product creation stubbed and _request scripted."""
    with patch.object(paypal_sdk, "get_or_create_product", return_value="PROD-S"):
        with patch.object(paypal_sdk, "_request", side_effect=[get_item, *post_items]) as request:
            return paypal_sdk.get_or_create_plan(), request


def _expect_stop(get_item, expected=paypal_sdk.PlanValidationError):
    """Assert the check stops with `expected`, creates no plan, and leaves the cache untouched."""
    with _isolated() as cache_file:
        before = _seed(cache_file)
        with patch.object(
            paypal_sdk, "_create_plan", side_effect=AssertionError("must not create a plan")
        ) as create:
            with patch.object(paypal_sdk, "get_or_create_product", return_value="PROD-S"):
                with patch.object(paypal_sdk, "_request", side_effect=[get_item]):
                    try:
                        paypal_sdk.get_or_create_plan()
                    except expected:
                        pass
                    else:
                        raise AssertionError("expected the plan check to stop")
        assert not create.called
        assert cache_file.read_text(encoding="utf-8") == before


def _expect_reuse(get_item):
    with _isolated() as cache_file:
        before = _seed(cache_file)
        plan_id, request = _call(cache_file, get_item)
        assert plan_id == "PLAN-OLD"
        assert [entry.args[0] for entry in request.call_args_list] == ["GET"]
        assert cache_file.read_text(encoding="utf-8") == before


# ---- subscriber removal and custom_id preservation --------------------------

def test_subscription_payload_omits_subscriber_and_keeps_custom_id():
    with _isolated():
        with patch.object(paypal_sdk, "get_or_create_plan", return_value="P-TEST"):
            with patch.object(
                paypal_sdk,
                "_request",
                return_value=_json_response({"links": [{"rel": "approve", "href": SANDBOX_APPROVE}]}),
            ) as request:
                paypal_sdk.create_subscription("user-42", "https://example.test/r", "https://example.test/c")

    payload = request.call_args.kwargs["json_body"]
    assert "subscriber" not in payload
    assert payload["custom_id"] == "user-42"
    assert payload["plan_id"] == "P-TEST"


# ---- approve URL validation -------------------------------------------------

def test_approve_url_accepts_sandbox_https_host():
    assert paypal_sdk._validated_approval_url(SANDBOX_APPROVE, "sandbox") == SANDBOX_APPROVE


def test_approve_url_accepts_live_host_only_in_live_environment():
    assert paypal_sdk._validated_approval_url(LIVE_APPROVE, "live") == LIVE_APPROVE
    try:
        paypal_sdk._validated_approval_url(LIVE_APPROVE, "sandbox")
        raise AssertionError("live host must be rejected in sandbox")
    except RuntimeError:
        pass


def test_approve_url_rejects_unsafe_urls():
    unsafe = [
        "http://www.sandbox.paypal.com/webapps/billing/subscriptions?ba_token=X",
        "https://evil.example/webapps/billing/subscriptions?ba_token=X",
        "https://www.sandbox.paypal.com.evil.example/x",
        "https://buyer@example.com/x",
        "https://www.sandbox.paypal.com:8443/x",
        "",
        None,
        42,
    ]
    for href in unsafe:
        try:
            paypal_sdk._validated_approval_url(href, "sandbox")
        except RuntimeError:
            continue
        raise AssertionError(f"unsafe approve URL was accepted: {href!r}")


def test_missing_approve_link_raises():
    with _isolated():
        with patch.object(paypal_sdk, "get_or_create_plan", return_value="P-TEST"):
            with patch.object(
                paypal_sdk,
                "_request",
                return_value=_json_response({"links": [{"rel": "edit", "href": SANDBOX_APPROVE}]}),
            ):
                try:
                    paypal_sdk.create_subscription("u", "https://example.test/r", "https://example.test/c")
                    raise AssertionError("missing approve link must raise")
                except RuntimeError as error:
                    assert "approval" in str(error).lower()


def test_approve_link_on_wrong_host_is_rejected_by_create_subscription():
    with _isolated("sandbox"):
        with patch.object(paypal_sdk, "get_or_create_plan", return_value="P-TEST"):
            with patch.object(
                paypal_sdk,
                "_request",
                return_value=_json_response({"links": [{"rel": "approve", "href": "https://evil.example/a"}]}),
            ):
                try:
                    paypal_sdk.create_subscription("u", "https://example.test/r", "https://example.test/c")
                    raise AssertionError("wrong-host approve URL must be rejected")
                except RuntimeError:
                    pass


# ---- plan validation: accepted plans ----------------------------------------

def test_normal_monthly_jpy_1980_plan_is_reused_without_writes():
    with _isolated() as cache_file:
        before = _seed(cache_file)
        plan_id, request = _call(cache_file, _json_response(_plan()))
        assert plan_id == "PLAN-OLD"
        assert [entry.args[0] for entry in request.call_args_list] == ["GET"]
        assert cache_file.read_text(encoding="utf-8") == before


def test_equivalent_amount_representations_are_reused():
    for value in ("1980", "1980.00", 1980, 1980.0):
        _expect_reuse(_json_response(_plan(value=value)))


# ---- plan validation: stop without creating a plan --------------------------

def test_stop_when_interval_unit_is_not_month():
    _expect_stop(_json_response(_plan(interval_unit="WEEK")))


def test_stop_when_interval_count_is_not_one():
    _expect_stop(_json_response(_plan(interval_count=2)))
    _expect_stop(_json_response(_plan(interval_count="3")))


def test_stop_when_no_regular_cycle():
    _expect_stop(_json_response(_plan(cycles=[_regular_cycle(tenure="TRIAL")])))


def test_stop_when_multiple_regular_cycles():
    _expect_stop(_json_response(_plan(cycles=[_regular_cycle(), _regular_cycle(value="500")])))


def test_stop_when_amount_differs():
    _expect_stop(_json_response(_plan(value="1000")))


def test_stop_when_currency_differs():
    _expect_stop(_json_response(_plan(currency="USD", value="1980")))


def test_stop_on_invalid_amount_values():
    for bad in ("abc", "", None, True, False, "NaN", "Infinity", [], {}):
        _expect_stop(_json_response(_plan(value=bad)))


def test_stop_when_billing_cycles_missing():
    plan = _plan()
    del plan["billing_cycles"]
    _expect_stop(_json_response(plan))


def test_stop_when_frequency_missing():
    cycle = _regular_cycle()
    del cycle["frequency"]
    _expect_stop(_json_response(_plan(cycles=[cycle])))


def test_stop_when_fixed_price_missing():
    cycle = _regular_cycle()
    del cycle["pricing_scheme"]["fixed_price"]
    _expect_stop(_json_response(_plan(cycles=[cycle])))


def test_stop_when_plan_response_is_not_a_dict():
    _expect_stop(_json_response([_plan()]))
    _expect_stop(_json_response("PLAN-OLD"))


def test_stop_when_product_or_status_field_is_missing():
    plan = _plan()
    del plan["status"]
    _expect_stop(_json_response(plan))
    plan = _plan()
    del plan["product_id"]
    _expect_stop(_json_response(plan))


def test_stop_on_json_parse_error():
    _expect_stop(_bad_json_response())


def test_stop_on_connection_error_without_creating_plan():
    _expect_stop(requests.ConnectionError("network down"), expected=requests.ConnectionError)


def test_stop_on_timeout_without_creating_plan():
    _expect_stop(requests.Timeout("timed out"), expected=requests.Timeout)


def test_stop_on_non_invalid_http_error_without_creating_plan():
    response = MagicMock()
    response.status_code = 500
    response.json.return_value = {"name": "INTERNAL_SERVICE_ERROR"}
    _expect_stop(requests.HTTPError("server error", response=response), expected=requests.HTTPError)


# ---- stale references: product or status mismatch --------------------------

def test_product_mismatch_recreates_plan_without_modifying_old_plan():
    with _isolated() as cache_file:
        _seed(cache_file)
        plan_id, request = _call(
            cache_file,
            _json_response(_plan(product_id="PROD-OTHER")),
            [_json_response({"id": "PLAN-NEW", "status": "ACTIVE"})],
        )
    assert plan_id == "PLAN-NEW"
    assert [entry.args[0] for entry in request.call_args_list] == ["GET", "POST"]


def test_inactive_status_recreates_plan_without_modifying_old_plan():
    with _isolated() as cache_file:
        _seed(cache_file)
        plan_id, request = _call(
            cache_file,
            _json_response(_plan(status="INACTIVE")),
            [_json_response({"id": "PLAN-NEW", "status": "ACTIVE"})],
        )
        assert plan_id == "PLAN-NEW"
        assert [entry.args[0] for entry in request.call_args_list] == ["GET", "POST"]
        assert json.loads(cache_file.read_text(encoding="utf-8"))["sandbox"]["plan_id"] == "PLAN-NEW"


# ---- plan creation response validation (_create_plan) ----------------------

def _expect_creation_error(post_item, expected_plan_id=None):
    """Assert _create_plan raises PlanCreationError, makes one POST, and leaves the cache untouched."""
    with _isolated() as cache_file:
        before = _seed(cache_file)
        with patch.object(paypal_sdk, "_request", side_effect=[post_item]) as request:
            try:
                paypal_sdk._create_plan("PROD-S")
            except paypal_sdk.PlanCreationError as error:
                assert error.plan_id == expected_plan_id
            else:
                raise AssertionError("expected PlanCreationError")
        assert [entry.args[0] for entry in request.call_args_list] == ["POST"]
        assert cache_file.read_text(encoding="utf-8") == before


def test_creation_response_not_dict_is_rejected_without_caching():
    _expect_creation_error(_json_response(["PLAN-NEW", "ACTIVE"]))
    _expect_creation_error(_json_response("PLAN-NEW"))


def test_creation_response_invalid_json_is_rejected_without_caching():
    _expect_creation_error(_bad_json_response())


def test_creation_response_missing_id_is_rejected_without_caching():
    _expect_creation_error(_json_response({"status": "ACTIVE"}))


def test_creation_response_empty_id_is_rejected_without_caching():
    _expect_creation_error(_json_response({"id": "", "status": "ACTIVE"}))
    _expect_creation_error(_json_response({"id": "   ", "status": "ACTIVE"}))


def test_creation_response_non_active_status_is_rejected_without_caching():
    _expect_creation_error(
        _json_response({"id": "PLAN-NEW", "status": "CREATED"}), expected_plan_id="PLAN-NEW"
    )
    _expect_creation_error(_json_response({"id": "PLAN-NEW"}), expected_plan_id="PLAN-NEW")


def test_valid_active_creation_response_is_cached():
    with _isolated() as cache_file:
        _seed(cache_file)
        with patch.object(
            paypal_sdk,
            "_request",
            side_effect=[_json_response({"id": "PLAN-NEW", "status": "ACTIVE"})],
        ) as request:
            assert paypal_sdk._create_plan("PROD-S") == "PLAN-NEW"
        assert [entry.args[0] for entry in request.call_args_list] == ["POST"]
        stored = json.loads(cache_file.read_text(encoding="utf-8"))
        assert stored["sandbox"]["plan_id"] == "PLAN-NEW"


def test_product_resource_id_rejects_non_dict_with_runtime_error():
    try:
        paypal_sdk._resource_id(_json_response(["PROD-X"]), "product")
    except RuntimeError as error:
        assert not isinstance(error, AttributeError)
    else:
        raise AssertionError("non-dict product response must raise")


# ---- cache-save failure after successful plan creation ----------------------

def test_cache_save_failure_raises_with_plan_id_and_never_retries_creation():
    with _isolated() as cache_file:
        # No seeded cache, so get_or_create_plan goes straight to creation.
        post = _json_response({"id": "PLAN-NEW", "status": "ACTIVE"})
        with patch.object(paypal_sdk, "_write_resource_cache", side_effect=OSError("disk full")):
            with patch.object(paypal_sdk, "get_or_create_product", return_value="PROD-S"):
                with patch.object(paypal_sdk, "_request", side_effect=[post]) as request:
                    try:
                        paypal_sdk.get_or_create_plan()
                    except paypal_sdk.PlanCreationError as error:
                        assert error.plan_id == "PLAN-NEW"
                        assert isinstance(error.__cause__, OSError)
                        assert "disk full" in str(error.__cause__)
                    else:
                        raise AssertionError("cache save failure must raise, not succeed")
        # Exactly one POST: no retry, no GET, no PATCH or DELETE of any plan.
        assert [entry.args[0] for entry in request.call_args_list] == ["POST"]
        assert not cache_file.exists()


def test_cache_save_failure_does_not_remove_existing_cache_entries():
    with _isolated() as cache_file:
        before = _seed(cache_file)
        post = _json_response({"id": "PLAN-NEW", "status": "ACTIVE"})
        with patch.object(paypal_sdk, "_write_resource_cache", side_effect=OSError("disk full")):
            with patch.object(paypal_sdk, "_request", side_effect=[post]) as request:
                try:
                    paypal_sdk._create_plan("PROD-S")
                except paypal_sdk.PlanCreationError:
                    pass
                else:
                    raise AssertionError("cache save failure must raise")
        assert [entry.args[0] for entry in request.call_args_list] == ["POST"]
        assert cache_file.read_text(encoding="utf-8") == before
