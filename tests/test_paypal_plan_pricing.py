import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

PROJECT_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_DIR / "src"))

import i18n
import paypal_sdk
import subscription_store
from paypal_pricing import format_paypal_amount

# Route all plan-quarantine DB access in this module to a temp file, never the real DB.
_TEMP_DB_DIR = tempfile.TemporaryDirectory()
_DB_PATCH = patch.object(
    subscription_store, "DB_PATH", str(Path(_TEMP_DB_DIR.name) / "test_subscriptions.db")
)
_DB_PATCH.start()
subscription_store.init_db()


def test_plan_request_uses_jpy_integer_amount():
    response = MagicMock()
    response.json.return_value = {"id": "P-TEST", "status": "ACTIVE"}

    with tempfile.TemporaryDirectory() as temp_dir:
        cache_file = Path(temp_dir) / "paypal_resource_ids.json"
        with patch.object(paypal_sdk, "RESOURCE_IDS_CACHE_FILE", cache_file):
            with patch.object(paypal_sdk, "get_or_create_product", return_value="PROD-TEST"):
                with patch.object(paypal_sdk, "_request", return_value=response) as request:
                    assert paypal_sdk.get_or_create_plan() == "P-TEST"
        assert '"plan_id": "P-TEST"' in cache_file.read_text(encoding="utf-8")

    request.assert_called_once()
    method, endpoint = request.call_args.args
    payload = request.call_args.kwargs["json_body"]
    assert payload["product_id"] == "PROD-TEST"
    assert method == "POST"
    assert endpoint == "/v1/billing/plans"
    assert payload["billing_cycles"][0]["pricing_scheme"]["fixed_price"] == {
        "currency_code": "JPY",
        "value": "1980",
    }


def test_subscription_uses_the_created_plan():
    response = MagicMock()
    approve_url = "https://www.sandbox.paypal.com/webapps/billing/subscriptions?ba_token=BA-TEST"
    response.json.return_value = {
        "links": [{"rel": "approve", "href": approve_url}]
    }

    with patch.object(paypal_sdk, "get_paypal_env", return_value="sandbox"):
        with patch.object(paypal_sdk, "get_or_create_plan", return_value="P-TEST"):
            with patch.object(paypal_sdk, "_request", return_value=response) as request:
                assert (
                    paypal_sdk.create_subscription(
                        "user", "https://example.test/return", "https://example.test/cancel"
                    )
                    == approve_url
                )

    payload = request.call_args.kwargs["json_body"]
    assert payload["plan_id"] == "P-TEST"


def test_pricing_precision_and_display_match():
    assert format_paypal_amount("1980", "JPY") == "1980"
    assert format_paypal_amount("12.34", "USD") == "12.34"
    assert format_paypal_amount("1.234", "KWD") == "1.234"
    assert format_paypal_amount("1", "CLP") == "1"

    for language in ("ja", "en", "es", "ko", "zh"):
        plans = i18n.get_subscription_plans(lang=language)
        pro_plan = next(plan for plan in plans if plan["id"] == "Pro")
        assert pro_plan["price"] == "¥1,980"

    try:
        format_paypal_amount("1.2", "JPY")
        raise AssertionError("JPY amounts with fractional digits should be rejected")
    except ValueError:
        pass
    try:
        format_paypal_amount("1.00", "XXX")
        raise AssertionError("Unconfigured currencies should be rejected")
    except ValueError:
        pass
