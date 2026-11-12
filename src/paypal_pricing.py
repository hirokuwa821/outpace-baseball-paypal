"""PayPal plan pricing and currency-aware amount formatting."""

from decimal import Decimal, InvalidOperation


PAYPAL_CURRENCY_DECIMAL_PLACES = {
    "AUD": 2,
    "BHD": 3,
    "BRL": 2,
    "CAD": 2,
    "CHF": 2,
    "CLP": 0,
    "CNY": 2,
    "CZK": 2,
    "DKK": 2,
    "EUR": 2,
    "GBP": 2,
    "HKD": 2,
    "HUF": 2,
    "ILS": 2,
    "ISK": 0,
    "JOD": 3,
    "JPY": 0,
    "KRW": 0,
    "KWD": 3,
    "MXN": 2,
    "MYR": 2,
    "NOK": 2,
    "NZD": 2,
    "OMR": 3,
    "PHP": 2,
    "PLN": 2,
    "RUB": 2,
    "SEK": 2,
    "SGD": 2,
    "THB": 2,
    "TND": 3,
    "TWD": 2,
    "USD": 2,
    "VND": 0,
}

PAYPAL_CURRENCY_SYMBOLS = {
    "JPY": "¥",
    "USD": "$",
}

MONTHLY_PLAN_CURRENCY = "JPY"
MONTHLY_PLAN_AMOUNT = Decimal("1980")


def format_paypal_amount(amount: Decimal | int | str, currency_code: str) -> str:
    """Format an amount using the configured minor-unit precision for its currency."""
    currency = currency_code.strip().upper()
    try:
        decimal_places = PAYPAL_CURRENCY_DECIMAL_PLACES[currency]
    except KeyError:
        raise ValueError(f"Unsupported PayPal currency: {currency}") from None

    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError):
        raise ValueError("Amount must be a valid decimal number") from None
    if not value.is_finite() or value < 0:
        raise ValueError("Amount must be a finite, non-negative number")

    quantum = Decimal(1).scaleb(-decimal_places)
    try:
        normalized_value = value.quantize(quantum)
    except InvalidOperation:
        raise ValueError(f"{currency} amount cannot be represented") from None
    if value != normalized_value:
        raise ValueError(
            f"{currency} amounts support at most {decimal_places} decimal places"
        )
    return f"{normalized_value:.{decimal_places}f}"


def format_paypal_price(amount: Decimal | int | str, currency_code: str) -> str:
    """Format a display price with a currency symbol or code."""
    currency = currency_code.strip().upper()
    formatted_amount = format_paypal_amount(amount, currency)
    symbol = PAYPAL_CURRENCY_SYMBOLS.get(currency, f"{currency} ")
    return f"{symbol}{Decimal(formatted_amount):,}"
