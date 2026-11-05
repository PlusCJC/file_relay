"""Short-lived login tickets and independent, time-limited browser sessions."""

import hashlib
import hmac
import re
import secrets
import time


LOGIN_SECONDS = 10 * 60
SESSION_SECONDS = 2 * 3600


def _issue(secret, purpose, seconds, expiry=None):
    expiry = int(time.time()) + seconds if expiry is None else expiry
    payload = f"{expiry}.{secrets.token_hex(16)}"
    message = f"file-relay:v1:{purpose}:{payload}".encode()
    signature = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return f"{payload}.{signature}"


def _valid(secret, value, purpose, seconds, now=None):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]{1,12}\.[0-9a-f]{32}\.[0-9a-f]{64}", value):
        return False
    expiry, nonce, signature = value.split(".")
    current = time.time() if now is None else now
    if not current < int(expiry) <= current + seconds + 1:
        return False
    message = f"file-relay:v1:{purpose}:{expiry}.{nonce}".encode()
    expected = hmac.new(secret.encode(), message, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def login_token(secret, seconds=LOGIN_SECONDS, expiry=None):
    return _issue(secret, "login", seconds, expiry)


def valid_login_token(secret, value, seconds=LOGIN_SECONDS, now=None):
    return _valid(secret, value, "login", seconds, now)


def session_value(secret, expiry=None, seconds=SESSION_SECONDS):
    return _issue(secret, "session", seconds, expiry)


def valid_session(secret, value, seconds=SESSION_SECONDS, now=None):
    return _valid(secret, value, "session", seconds, now)
