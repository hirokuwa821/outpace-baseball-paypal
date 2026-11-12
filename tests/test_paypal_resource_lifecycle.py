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

# Route all plan-quarantine DB access in this module to a temp file, never the real DB.
_TEMP_DB_DIR = tempfile.TemporaryDirectory()
_DB_PATCH = patch.object(
    subscription_store, "DB_PATH", str(Path(_TEMP_DB_DIR.name) / "test_subscriptions.db")
)
_DB_PATCH.start()
subscription_store.init_db()


def _api_response(payload, status_code=200):
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = payload
    response.raise_for_status.return_value = None
    return response


def _invalid_resource_error():
    response = _api_response(
        {
            "name": "RESOURCE_NOT_FOUND",
            "details": [{"issue": "INVALID_RESOURCE_ID"}],
        },
        status_code=404,
    )
    return requests.HTTPError("resource not found", response=response)


def test_missing_product_is_created_before_plan_and_cached():
    with tempfile.TemporaryDirectory() as temp_dir:
        cache_file = Path(temp_dir) / "paypal_resource_ids.json"
        with patch.object(paypal_sdk, "RESOURCE_IDS_CACHE_FILE", cache_file):
            with patch.object(paypal_sdk, "_request") as request:
                request.side_effect = [
                    _api_response({"products": []}),
                    _api_response({"id": "PROD-SANDBOX"}),
                    _api_response({"id": "PLAN-SANDBOX", "status": "ACTIVE"}),
                ]

                assert paypal_sdk.get_or_create_plan() == "PLAN-SANDBOX"

            assert [entry.args[:2] for entry in request.call_args_list] == [
                ("GET", "/v1/catalogs/products"),
                ("POST", "/v1/catalogs/products"),
                ("POST", "/v1/billing/plans"),
            ]
            product_payload = request.call_args_list[1].kwargs["json_body"]
            assert product_payload["name"] == paypal_sdk.PRODUCT_NAME
            plan_payload = request.call_args_list[2].kwargs["json_body"]
            assert plan_payload["product_id"] == "PROD-SANDBOX"
            assert plan_payload["billing_cycles"][0]["pricing_scheme"]["fixed_price"] == {
                "currency_code": "JPY",
                "value": "1980",
            }
            assert json.loads(cache_file.read_text(encoding="utf-8")) == {
                "sandbox": {
                    "product_id": "PROD-SANDBOX",
                    "plan_id": "PLAN-SANDBOX",
                }
            }


def test_existing_product_and_plan_are_validated_and_reused():
    with tempfile.TemporaryDirectory() as temp_dir:
        cache_file = Path(temp_dir) / "paypal_resource_ids.json"
        cache_file.write_text(
            json.dumps(
                {
                    "sandbox": {
                        "product_id": "PROD-SANDBOX",
                        "plan_id": "PLAN-SANDBOX",
                    }
                }
            ),
            encoding="utf-8",
        )
        with patch.object(paypal_sdk, "RESOURCE_IDS_CACHE_FILE", cache_file):
            with patch.object(
                paypal_sdk,
                "_request",
                side_effect=[
                    _api_response({"id": "PROD-SANDBOX"}),
                    _api_response(
                        {
                            "id": "PLAN-SANDBOX",
                            "product_id": "PROD-SANDBOX",
                            "status": "ACTIVE",
                            "billing_cycles": [
                                {
                                    "tenure_type": "REGULAR",
                                    "frequency": {
                                        "interval_unit": "MONTH",
                                        "interval_count": 1,
                                    },
                                    "pricing_scheme": {
                                        "fixed_price": {
                                            "currency_code": "JPY",
                                            "value": "1980",
                                        }
                                    },
                                }
                            ],
                        }
                    ),
                ],
            ) as request:
                assert paypal_sdk.get_or_create_plan() == "PLAN-SANDBOX"

    assert [entry.args[1] for entry in request.call_args_list] == [
        "/v1/catalogs/products/PROD-SANDBOX",
        "/v1/billing/plans/PLAN-SANDBOX",
    ]


def test_invalid_cached_product_is_recreated():
    with tempfile.TemporaryDirectory() as temp_dir:
        cache_file = Path(temp_dir) / "paypal_resource_ids.json"
        cache_file.write_text(
            json.dumps({"sandbox": {"product_id": "PROD-OLD"}}),
            encoding="utf-8",
        )
        with patch.object(paypal_sdk, "RESOURCE_IDS_CACHE_FILE", cache_file):
            with patch.object(
                paypal_sdk,
                "_request",
                side_effect=[
                    _invalid_resource_error(),
                    _api_response({"products": []}),
                    _api_response({"id": "PROD-NEW"}),
                ],
            ) as request:
                assert paypal_sdk.get_or_create_product() == "PROD-NEW"

        assert [entry.args[1] for entry in request.call_args_list] == [
            "/v1/catalogs/products/PROD-OLD",
            "/v1/catalogs/products",
            "/v1/catalogs/products",
        ]
        assert json.loads(cache_file.read_text(encoding="utf-8")) == {
            "sandbox": {"product_id": "PROD-NEW"}
        }


def test_invalid_product_reference_during_plan_creation_is_retried():
    with tempfile.TemporaryDirectory() as temp_dir:
        cache_file = Path(temp_dir) / "paypal_resource_ids.json"
        with patch.object(paypal_sdk, "RESOURCE_IDS_CACHE_FILE", cache_file):
            with patch.object(paypal_sdk, "_request") as request:
                request.side_effect = [
                    _api_response({"products": []}),
                    _api_response({"id": "PROD-OLD"}),
                    _invalid_resource_error(),
                    _api_response({"products": []}),
                    _api_response({"id": "PROD-NEW"}),
                    _api_response({"id": "PLAN-NEW", "status": "ACTIVE"}),
                ]

                assert paypal_sdk.get_or_create_plan() == "PLAN-NEW"

        assert [entry.args[:2] for entry in request.call_args_list] == [
            ("GET", "/v1/catalogs/products"),
            ("POST", "/v1/catalogs/products"),
            ("POST", "/v1/billing/plans"),
            ("GET", "/v1/catalogs/products"),
            ("POST", "/v1/catalogs/products"),
            ("POST", "/v1/billing/plans"),
        ]
        assert request.call_args_list[2].kwargs["json_body"]["product_id"] == "PROD-OLD"
        assert request.call_args_list[5].kwargs["json_body"]["product_id"] == "PROD-NEW"
        assert json.loads(cache_file.read_text(encoding="utf-8")) == {
            "sandbox": {"product_id": "PROD-NEW", "plan_id": "PLAN-NEW"}
        }


def test_invalid_cached_plan_is_recreated():
    with tempfile.TemporaryDirectory() as temp_dir:
        cache_file = Path(temp_dir) / "paypal_resource_ids.json"
        cache_file.write_text(
            json.dumps(
                {
                    "sandbox": {
                        "product_id": "PROD-SANDBOX",
                        "plan_id": "PLAN-OLD",
                    }
                }
            ),
            encoding="utf-8",
        )
        with patch.object(paypal_sdk, "RESOURCE_IDS_CACHE_FILE", cache_file):
            with patch.object(
                paypal_sdk,
                "_request",
                side_effect=[
                    _api_response({"id": "PROD-SANDBOX"}),
                    _invalid_resource_error(),
                    _api_response({"id": "PLAN-NEW", "status": "ACTIVE"}),
                ],
            ) as request:
                assert paypal_sdk.get_or_create_plan() == "PLAN-NEW"

        assert [entry.args[1] for entry in request.call_args_list] == [
            "/v1/catalogs/products/PROD-SANDBOX",
            "/v1/billing/plans/PLAN-OLD",
            "/v1/billing/plans",
        ]
        assert json.loads(cache_file.read_text(encoding="utf-8")) == {
            "sandbox": {"product_id": "PROD-SANDBOX", "plan_id": "PLAN-NEW"}
        }


def test_product_creation_failure_is_not_hidden():
    failure = requests.HTTPError("PayPal product creation failed")
    with tempfile.TemporaryDirectory() as temp_dir:
        cache_file = Path(temp_dir) / "paypal_resource_ids.json"
        with patch.object(paypal_sdk, "RESOURCE_IDS_CACHE_FILE", cache_file):
            with patch.object(
                paypal_sdk,
                "_request",
                side_effect=[_api_response({"products": []}), failure],
            ):
                try:
                    paypal_sdk.get_or_create_product()
                    raise AssertionError("Product creation failure should propagate")
                except requests.HTTPError as error:
                    assert error is failure
        assert not cache_file.exists()


def test_product_and_plan_ids_are_separated_by_environment():
    with tempfile.TemporaryDirectory() as temp_dir:
        cache_file = Path(temp_dir) / "paypal_resource_ids.json"
        with patch.object(paypal_sdk, "RESOURCE_IDS_CACHE_FILE", cache_file):
            with patch.dict("os.environ", {"PAYPAL_ENV": "sandbox"}):
                paypal_sdk._store_resource_id("sandbox", "product_id", "PROD-S")
                paypal_sdk._store_resource_id("sandbox", "plan_id", "PLAN-S")
            with patch.dict("os.environ", {"PAYPAL_ENV": "live"}):
                paypal_sdk._store_resource_id("live", "product_id", "PROD-L")
                paypal_sdk._store_resource_id("live", "plan_id", "PLAN-L")

        assert json.loads(cache_file.read_text(encoding="utf-8")) == {
            "sandbox": {"product_id": "PROD-S", "plan_id": "PLAN-S"},
            "live": {"product_id": "PROD-L", "plan_id": "PLAN-L"},
        }


def test_requests_obtain_token_first_and_send_bearer_auth():
    response = _api_response({"products": []})
    order = []

    def token():
        order.append("token")
        return "mock-access-token"

    def send_request(method, url, **kwargs):
        order.append("request")
        assert kwargs["headers"]["Authorization"] == "Bearer mock-access-token"
        assert url == "https://api-m.sandbox.paypal.com/v1/catalogs/products"
        return response

    with patch.object(paypal_sdk, "get_access_token", side_effect=token):
        with patch.object(paypal_sdk.requests, "request", side_effect=send_request):
            assert paypal_sdk._request("GET", "/v1/catalogs/products") is response

    assert order == ["token", "request"]


def test_live_environment_uses_live_api_url():
    response = _api_response({"products": []})
    with patch.dict("os.environ", {"PAYPAL_ENV": "live"}):
        with patch.object(paypal_sdk, "get_access_token", return_value="mock-access-token"):
            with patch.object(paypal_sdk.requests, "request", return_value=response) as request:
                assert paypal_sdk._request("GET", "/v1/catalogs/products") is response

    assert request.call_args.args[1] == "https://api-m.paypal.com/v1/catalogs/products"
