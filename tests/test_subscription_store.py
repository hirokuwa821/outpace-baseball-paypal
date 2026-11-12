import os
import tempfile
import time
import datetime
import sqlite3

import importlib

# Import the module
import sys, os
sys.path.append(os.path.join(os.path.dirname(__file__), "..", "src"))
import subscription_store

def _setup_temp_db():
    # Create a temporary file for the SQLite DB
    fd, path = tempfile.mkstemp(suffix='.db')
    os.close(fd)
    # Override the DB_PATH in the module
    subscription_store.DB_PATH = path
    # Ensure fresh init
    subscription_store.init_db()
    return path

def test_init_db_creates_tables():
    db_path = _setup_temp_db()
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    # Check tables exist
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='subscriptions'")
    assert cur.fetchone() is not None
    cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='pending'")
    assert cur.fetchone() is not None
    conn.close()
    os.remove(db_path)

def test_database_path_fingerprint_is_stable_and_path_specific():
    original_path = subscription_store.DB_PATH
    try:
        subscription_store.DB_PATH = r'C:\paypal\subscriptions.db'
        first = subscription_store.get_database_path_fingerprint()
        assert first == subscription_store.get_database_path_fingerprint()

        subscription_store.DB_PATH = r'C:\paypal\other.db'
        assert first != subscription_store.get_database_path_fingerprint()
    finally:
        subscription_store.DB_PATH = original_path

def test_upsert_and_get_by_subscription():
    db_path = _setup_temp_db()
    subscription_store.upsert('sub_123', 'user_abc', 'ACTIVE')
    row = subscription_store.get_by_subscription('sub_123')
    assert row is not None
    assert row['subscription_id'] == 'sub_123'
    assert row['user_id'] == 'user_abc'
    assert row['status'] == 'ACTIVE'
    os.remove(db_path)

def test_store_and_consume_pending_successful():
    db_path = _setup_temp_db()
    token = 'testtoken'
    subscription_store.store_pending(token, 'user_xyz')
    # Immediate consumption should succeed
    user = subscription_store.consume_pending(token)
    assert user == 'user_xyz'
    # Token should no longer exist
    assert subscription_store.consume_pending(token) is None
    os.remove(db_path)

def test_consume_pending_expired():
    db_path = _setup_temp_db()
    token = 'expiretoken'
    # Insert with old timestamp manually
    now = (
        datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        - datetime.timedelta(hours=25)
    ).isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO pending (token, user_id, created_at) VALUES (?, ?, ?)", (token, 'user_old', now))
    conn.commit()
    conn.close()
    # Consumption should return None (expired) and delete the row
    assert subscription_store.consume_pending(token) is None
    # Ensure token removed
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute("SELECT * FROM pending WHERE token = ?", (token,))
    assert cur.fetchone() is None
    conn.close()
    os.remove(db_path)

def test_get_pending_user_validates_without_consuming():
    db_path = _setup_temp_db()
    token = 'returntoken'
    subscription_store.store_pending(token, 'user_return')

    assert subscription_store.get_pending_user(token) == 'user_return'
    assert subscription_store.get_pending_user(token) == 'user_return'
    assert subscription_store.consume_pending(token) == 'user_return'
    assert subscription_store.get_pending_user(token) is None
    os.remove(db_path)

def test_get_pending_user_rejects_and_removes_expired_token():
    db_path = _setup_temp_db()
    token = 'expiredreturntoken'
    created_at = (
        datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        - datetime.timedelta(hours=25)
    ).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO pending (token, user_id, created_at) VALUES (?, ?, ?)",
            (token, 'user_old', created_at),
        )
        conn.commit()
    finally:
        conn.close()

    assert subscription_store.get_pending_user(token) is None
    assert subscription_store.consume_pending(token) is None
    os.remove(db_path)

def test_inspect_pending_token_reports_safe_states():
    db_path = _setup_temp_db()
    subscription_store.store_pending('found-token', 'user_found')
    assert subscription_store.inspect_pending_token('found-token') == (
        'found',
        'user_found',
    )
    assert subscription_store.inspect_pending_token('unknown-token') == (
        'missing',
        None,
    )

    expired_time = (
        datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
        - datetime.timedelta(hours=25)
    ).isoformat()
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO pending (token, user_id, created_at) VALUES (?, ?, ?)",
        ('expired-token', 'user_expired', expired_time),
    )
    conn.commit()
    conn.close()
    assert subscription_store.inspect_pending_token('expired-token') == (
        'expired',
        None,
    )

    assert subscription_store.consume_pending('found-token') == 'user_found'
    assert subscription_store.inspect_pending_token('found-token') == (
        'consumed',
        None,
    )
    os.remove(db_path)

def test_persist_verified_subscription_consumes_token_atomically():
    db_path = _setup_temp_db()
    subscription_store.store_pending('atomic-token', 'user_atomic')

    assert subscription_store.persist_verified_subscription(
        'atomic-token', 'user_atomic', 'sub_atomic', 'ACTIVE'
    )
    assert subscription_store.get_pending_user('atomic-token') is None
    row = subscription_store.get_by_subscription('sub_atomic')
    assert row is not None
    assert row['user_id'] == 'user_atomic'
    assert row['status'] == 'ACTIVE'
    assert subscription_store.inspect_pending_token('atomic-token') == (
        'consumed',
        None,
    )
    os.remove(db_path)

def test_persist_verified_subscription_rejects_wrong_owner():
    db_path = _setup_temp_db()
    subscription_store.store_pending('atomic-token', 'user_atomic')

    assert not subscription_store.persist_verified_subscription(
        'atomic-token', 'other_user', 'sub_atomic', 'ACTIVE'
    )
    assert subscription_store.get_by_subscription('sub_atomic') is None
    assert subscription_store.get_pending_user('atomic-token') == 'user_atomic'
    os.remove(db_path)

def test_token_one_time_use():
    db_path = _setup_temp_db()
    token = 'onceToken'
    subscription_store.store_pending(token, 'user_one')
    first = subscription_store.consume_pending(token)
    second = subscription_store.consume_pending(token)
    assert first == 'user_one'
    assert second is None
    os.remove(db_path)

def test_get_by_user_returns_most_recent():
    db_path = _setup_temp_db()
    # Insert two subscriptions for same user with different timestamps
    subscription_store.upsert('sub_a', 'user_dup', 'ACTIVE')
    time.sleep(0.1)
    subscription_store.upsert('sub_b', 'user_dup', 'CANCELLED')
    row = subscription_store.get_by_user('user_dup')
    assert row['subscription_id'] == 'sub_b'
    os.remove(db_path)
