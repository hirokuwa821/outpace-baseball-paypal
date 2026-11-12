import os
import tempfile
import datetime
import sqlite3
from contextlib import closing, contextmanager
from urllib.parse import parse_qs, urlparse
from unittest import mock
from fastapi.testclient import TestClient

# Add project root and src/ to sys.path so imports work (paths derived from this file)
import sys
project_root = os.path.normpath(os.path.join(os.path.dirname(os.path.abspath(__file__)), '..'))
src_dir = os.path.join(project_root, 'src')
sys.path.append(project_root)
sys.path.append(src_dir)

# Import modules after path adjustment
import subscription_store
import paypal_server


def test_database_path_diagnostic_endpoint_compares_fingerprints_only():
    expected_fingerprint = "a" * 64
    with mock.patch(
        "paypal_server.get_database_path_fingerprint",
        return_value=expected_fingerprint,
    ):
        matching = paypal_server.database_path_match_endpoint(
            paypal_server.DatabasePathCheckRequest(fingerprint=expected_fingerprint)
        )
        different = paypal_server.database_path_match_endpoint(
            paypal_server.DatabasePathCheckRequest(fingerprint="b" * 64)
        )

    assert matching == {"database_path_match": True}
    assert different == {"database_path_match": False}


def _remove_sqlite_file(path):
    """Delete the temp DB file and any leftover rollback journal. Missing files are fine."""
    for candidate in (path, path + '-journal'):
        try:
            os.remove(candidate)
        except FileNotFoundError:
            pass


def _cleanup_temp_db(original_path, path, body_exc):
    """Restore DB_PATH and delete the temp file, independently of each other.

    Each step runs even if the other fails. If the test body already raised
    (``body_exc`` is not None), cleanup failures are attached as notes so the
    original exception is never replaced. Otherwise they are raised.
    """
    errors = []
    try:
        subscription_store.DB_PATH = original_path
    except BaseException as exc:
        errors.append(exc)
    try:
        _remove_sqlite_file(path)
    except BaseException as exc:
        errors.append(exc)

    if not errors:
        return
    if body_exc is not None:
        for exc in errors:
            body_exc.add_note(f'temp_db cleanup failed: {type(exc).__name__}: {exc}')
        return
    if len(errors) == 1:
        raise errors[0]
    raise BaseExceptionGroup('temp_db cleanup failed', errors)


@contextmanager
def temp_db():
    """Yield the path of a fresh, initialised temp SQLite DB used by subscription_store.

    On exit (success or failure) DB_PATH is restored and the file is deleted.
    Diagnostic logging is stubbed so the real logs/ file is never written.
    """
    original_path = subscription_store.DB_PATH
    fd, path = tempfile.mkstemp(suffix='.db')
    try:
        os.close(fd)
        subscription_store.DB_PATH = path
        with mock.patch('paypal_server.log_pending_token_diagnostic'):
            subscription_store.init_db()
            yield path
    except BaseException as body_exc:
        _cleanup_temp_db(original_path, path, body_exc)
        raise
    else:
        _cleanup_temp_db(original_path, path, None)

def test_import_app():
    # Verify that the FastAPI app can be imported without error
    assert hasattr(paypal_server, 'app')

def test_create_subscription_basic():
    with temp_db() as db_path:
        client = TestClient(paypal_server.app)
        with mock.patch('paypal_server.create_subscription', return_value='https://paypal.com/approval') as mock_create:
            response = client.post('/create-subscription', json={
                'user_id': 'user123',
                'return_url': 'http://localhost:8501/',
                'cancel_url': 'http://localhost:8501/cancel',
            })
            assert response.status_code == 200
            data = response.json()
            assert 'approval_url' in data and data['approval_url'] == 'https://paypal.com/approval'
            assert 'pending_token' in data
            token = data['pending_token']
            # Verify token stored in DB
            with closing(sqlite3.connect(db_path)) as conn:
                conn.row_factory = sqlite3.Row
                cur = conn.execute('SELECT user_id FROM pending WHERE token = ?', (token,))
                row = cur.fetchone()
            assert row is not None and row['user_id'] == 'user123'
            # Verify create_subscription received return_url with token
            args, kwargs = mock_create.call_args
            return_url_sent = kwargs['return_url']
            assert parse_qs(urlparse(return_url_sent).query).get('pending_token') == [token]

def test_create_subscription_existing_query():
    with temp_db():
        client = TestClient(paypal_server.app)
        with mock.patch('paypal_server.create_subscription', return_value='https://paypal.com/approval') as mock_create:
            response = client.post('/create-subscription', json={
                'user_id': 'userABC',
                'return_url': 'http://localhost:8501/?foo=bar',
                'cancel_url': 'http://localhost:8501/cancel',
            })
            assert response.status_code == 200
            token = response.json()['pending_token']
            # Ensure the original query param is preserved and pending_token added
            sent_url = mock_create.call_args[1]['return_url']
            assert 'foo=bar' in sent_url and f'pending_token={token}' in sent_url
            assert 'token' not in parse_qs(urlparse(sent_url).query)

def test_return_url_contains_stored_pending_token():
    request = paypal_server.CreateSubscriptionRequest(
        user_id='return_user',
        return_url='http://localhost:8502/?lang=ja',
        cancel_url='http://localhost:8502/?cancel=1',
    )
    with mock.patch('paypal_server.store_pending') as mock_store:
        with mock.patch(
            'paypal_server.create_subscription',
            return_value='https://paypal.com/approval',
        ) as mock_create:
            # Stub diagnostics so no write reaches the real logs/ file.
            # mock.patch restores the original on exit, even if the test fails.
            with mock.patch('paypal_server.log_pending_token_diagnostic'):
                response = paypal_server.create_subscription_endpoint(request)

    token = response['pending_token']
    mock_store.assert_called_once_with(token, 'return_user')
    return_url = mock_create.call_args.kwargs['return_url']
    parsed = urlparse(return_url)
    query = parse_qs(parsed.query)
    assert parsed.path == '/'
    assert query['lang'] == ['ja']
    assert query['pending_token'] == [token]
    assert 'token' not in query

def test_db_store_failure_prevents_paypal_call():
    with temp_db():
        client = TestClient(paypal_server.app)
        # Force store_pending to raise an exception
        with mock.patch('paypal_server.store_pending', side_effect=Exception('DB error')) as mock_store:
            with mock.patch('paypal_server.create_subscription') as mock_create:
                response = client.post('/create-subscription', json={
                    'user_id': 'u1',
                    'return_url': 'http://localhost/',
                    'cancel_url': 'http://localhost/cancel',
                })
                assert response.status_code == 400
                # PayPal SDK must not be called
                assert not mock_create.called

def test_paypal_sdk_failure_returns_error():
    with temp_db():
        client = TestClient(paypal_server.app)
        with mock.patch('paypal_server.create_subscription', side_effect=Exception('PayPal error')) as mock_create:
            response = client.post('/create-subscription', json={
                'user_id': 'u2',
                'return_url': 'http://localhost/',
                'cancel_url': 'http://localhost/cancel',
            })
            assert response.status_code == 400
            # Internal exception text must not reach the browser; a reference code is returned instead
            detail = response.json()['detail']
            assert 'PayPal error' not in detail
            assert 'Reference code:' in detail

def test_judge_mode_invalid_token():
    with temp_db():
        client = TestClient(paypal_server.app)
        # is_valid_judge_token returns False -> 403
        with mock.patch('paypal_server.is_valid_judge_token', return_value=False):
            response = client.post('/create-subscription', json={
                'user_id': 'u3',
                'return_url': 'http://localhost/',
                'cancel_url': 'http://localhost/cancel',
                'judge_token': 'badtoken',
            })
            assert response.status_code == 403
            assert response.json()['detail'] == 'Invalid judge token'
