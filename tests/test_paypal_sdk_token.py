# -*- coding: utf-8 -*-
"""Tests for PayPal SDK token handling (mocked)"""
import datetime
import builtins
import types
import pytest
from unittest import mock

# Import the module under test
from paypal_integration.src import paypal_sdk

# Helper to create a fake response object
class DummyResponse:
    def __init__(self, json_data, status_code=200):
        self._json = json_data
        self.status_code = status_code
        self.text = str(json_data)
        self.headers = {}
    def json(self):
        return self._json
    def raise_for_status(self):
        if not (200 <= self.status_code < 300):
            raise requests.HTTPError(f"{self.status_code}", response=self)

# Patch requests.post used in _get_access_token
@mock.patch('src.paypal_sdk.requests.post')
def test_normal_token_acquisition(mock_post):
    now = datetime.datetime.now(datetime.timezone.utc)
    mock_post.return_value = DummyResponse({
        "access_token": "valid-token",
        "expires_in": 3600,
    })
    # Clear cache
    paypal_sdk._token_cache.clear()
    token = paypal_sdk.get_access_token()
    assert token == "valid-token"
    # Cache should contain dict with expiry
    cached = paypal_sdk._token_cache[paypal_sdk.get_paypal_env()]
    assert isinstance(cached, dict)
    assert cached["token"] == "valid-token"
    assert isinstance(cached["expires_at"], datetime.datetime)
    assert cached["expires_at"] > now

@mock.patch('src.paypal_sdk.requests.post')
def test_empty_token_rejected(mock_post):
    mock_post.return_value = DummyResponse({"access_token": "", "expires_in": 3600})
    paypal_sdk._token_cache.clear()
    with pytest.raises(RuntimeError, match="Invalid access token"):
        paypal_sdk.get_access_token()
    # cache must stay empty
    assert not paypal_sdk._token_cache

@mock.patch('src.paypal_sdk.requests.post')
def test_none_token_rejected(mock_post):
    mock_post.return_value = DummyResponse({"access_token": None, "expires_in": 3600})
    paypal_sdk._token_cache.clear()
    with pytest.raises(RuntimeError, match="Invalid access token"):
        paypal_sdk.get_access_token()
    assert not paypal_sdk._token_cache

@mock.patch('src.paypal_sdk.requests.post')
def test_cache_reuse_within_ttl(mock_post):
    mock_post.return_value = DummyResponse({"access_token": "cached-token", "expires_in": 3600})
    paypal_sdk._token_cache.clear()
    first = paypal_sdk.get_access_token()
    # second call should not trigger another post
    mock_post.reset_mock()
    second = paypal_sdk.get_access_token()
    assert second == first
    mock_post.assert_not_called()

@mock.patch('src.paypal_sdk.requests.post')
def test_token_refresh_on_expiry(mock_post):
    # First token expires in 1 second
    mock_post.side_effect = [
        DummyResponse({"access_token": "short-lived", "expires_in": 1}),
        DummyResponse({"access_token": "new-token", "expires_in": 3600}),
    ]
    paypal_sdk._token_cache.clear()
    token1 = paypal_sdk.get_access_token()
    assert token1 == "short-lived"
    # Fast forward time by monkeypatching datetime
    real_utcnow = datetime.datetime.now
    class FakeNow:
        @staticmethod
        def now(tz=None):
            return real_utcnow(tz) + datetime.timedelta(seconds=2)
    with mock.patch('src.paypal_sdk.datetime.datetime', new=FakeNow):
        token2 = paypal_sdk.get_access_token()
    assert token2 == "new-token"
    # Ensure second request was made
    assert mock_post.call_count == 2

# Ensure _request does not call PayPal when token missing/invalid
@mock.patch('src.paypal_sdk.requests.request')
@mock.patch('src.paypal_sdk.get_access_token')
def test_request_blocks_on_missing_token(mock_get_token, mock_request):
    mock_get_token.side_effect = RuntimeError("invalid token")
    with pytest.raises(RuntimeError):
        paypal_sdk._request('GET', '/v1/some/endpoint')
    mock_request.assert_not_called()
