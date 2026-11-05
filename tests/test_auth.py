import time

import pytest

from file_relay.auth import login_token, valid_login_token, session_value, valid_session


def test_login_token_is_short_lived_and_tamper_proof(config):
    now = int(time.time())
    ticket = login_token(config.token, expiry=now + 600)
    assert valid_login_token(config.token, ticket, now=now)
    assert valid_login_token(config.token, ticket, now=now + 599)
    assert not valid_login_token(config.token, ticket, now=now + 600)
    assert not valid_login_token(config.token, ticket + "a", now=now)
    assert not valid_login_token("0" * 64, ticket, now=now)
    assert config.token not in ticket
    assert login_token(config.token) != login_token(config.token)


def test_login_tickets_and_cookies_are_not_interchangeable(config):
    ticket, cookie = login_token(config.token), session_value(config.token)
    assert not valid_session(config.token, ticket)
    assert not valid_login_token(config.token, cookie)
    assert not valid_login_token(config.token, config.token)


@pytest.mark.parametrize("value", [None, 123, [], "", "错误", "." * 100, "9" * 10000])
def test_malformed_credentials_are_rejected(config, value):
    assert not valid_login_token(config.token, value)
    assert not valid_session(config.token, value)


def test_session_lifetime_can_be_configured(config):
    now = int(time.time())
    cookie = session_value(config.token, expiry=now + 300, seconds=300)
    assert valid_session(config.token, cookie, seconds=300, now=now + 299)
    assert not valid_session(config.token, cookie, seconds=300, now=now + 300)
