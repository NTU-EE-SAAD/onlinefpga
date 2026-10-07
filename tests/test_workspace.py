import json
from types import SimpleNamespace

import pytest

from tests.conftest import sign_in
from portal import service
from portal.db import get_db


def live_rental(app):
    with app.app_context():
        rental = service.book(1, 1, 15)
        service.tick()
        get_db().execute("UPDATE devices SET driver='pynq',jupyter_url='http://192.0.2.1:9090' WHERE id=1")
        return rental


def test_proxy_access_requires_owner_live_rental(client, app, monkeypatch):
    rental = live_rental(app)
    monkeypatch.setattr('requests.Session.request', lambda *a, **k: pytest.fail('No board connection permitted'))
    assert client.get(f'/lab/{rental}/api').status_code == 401
    sign_in(client, 2)
    assert client.get(f'/lab/{rental}/api').status_code == 404
    sign_in(client)
    clock = service.now()
    monkeypatch.setattr(service, 'now', lambda: clock + 901)
    assert client.get(f'/lab/{rental}/api').status_code == 403


def test_proxy_server_token_cookie_and_origin(client, app, monkeypatch):
    rental = live_rental(app)
    sign_in(client)
    calls = []

    def upstream(self, method, url, **kwargs):
        calls.append((method, url, kwargs))
        return SimpleNamespace(content=b'{"version":"6.4.0"}', status_code=200,
                               headers={'Content-Type': 'application/json', 'Set-Cookie': 'secret=bad'})

    monkeypatch.setattr('requests.Session.request', upstream)
    response = client.get(f'/lab/{rental}/api?token=attacker')
    assert response.status_code == 200
    assert 'Set-Cookie' not in response.headers
    assert response.headers['Cache-Control'] == 'no-store'
    method, url, kwargs = calls[0]
    assert 'token=' not in url
    assert 'Cookie' not in kwargs['headers']
    assert kwargs['headers']['Authorization'].startswith('token ')
    assert not kwargs['allow_redirects']
    assert client.post(f'/lab/{rental}/api/kernels', json={}).status_code == 403
    assert client.post(f'/lab/{rental}/api/kernels', json={}, headers={'Origin': 'https://evil.example'}).status_code == 403
    assert client.post(f'/lab/{rental}/api/kernels', json={}, headers={'Origin': 'http://localhost'}).status_code == 200


def test_return_revokes_proxy_without_waiting_for_scheduler(client, app, monkeypatch):
    rental = live_rental(app)
    sign_in(client)
    with app.app_context():
        service.finish(1, rental)
    monkeypatch.setattr('requests.Session.request', lambda *a, **k: pytest.fail('No board connection permitted'))
    assert client.get(f'/lab/{rental}/tree').status_code == 403


def test_proxy_rechecks_after_slow_response(client, app, monkeypatch):
    rental = live_rental(app)
    sign_in(client)
    clock = service.now()

    def upstream(*args, **kwargs):
        monkeypatch.setattr(service, 'now', lambda: clock + 901)
        return SimpleNamespace(content=b'private', status_code=200, headers={})

    monkeypatch.setattr('requests.Session.request', upstream)
    response = client.get(f'/lab/{rental}/api')
    assert response.status_code == 403
    assert b'private' not in response.data


def test_proxy_rejects_traversal_and_cross_origin_get(client, app, monkeypatch):
    rental = live_rental(app)
    sign_in(client)
    monkeypatch.setattr('requests.Session.request', lambda *a, **k: pytest.fail('No board connection permitted'))
    assert client.get(f'/lab/{rental}/../api').status_code == 400
    assert client.get(f'/lab/{rental}/api', headers={'Origin':'http://evil.example'}).status_code == 403


def test_notebook_html_and_redirect_do_not_expose_board_token(client, app, monkeypatch):
    rental = live_rental(app)
    sign_in(client)
    with app.app_context():
        secret = get_db().execute('SELECT access_secret FROM rentals WHERE id=?',(rental,)).fetchone()[0]
    monkeypatch.setattr('requests.Session.request',lambda *a, **k: SimpleNamespace(
        content=f'<body data-token="{secret}"></body>'.encode(),status_code=302,
        headers={'Content-Type':'text/html','Location':f'/lab/{rental}/tree?token={secret}'}))
    response=client.get(f'/lab/{rental}/')
    assert secret not in response.get_data(as_text=True)
    assert response.headers['Location']==f'/lab/{rental}/tree'


def test_disabled_account_and_changed_password_revoke_proxy(client,app,monkeypatch):
    rental=live_rental(app)
    sign_in(client)
    monkeypatch.setattr('requests.Session.request',lambda *a, **k: pytest.fail('No board connection permitted'))
    with app.app_context():
        get_db().execute('UPDATE users SET session_version=session_version+1 WHERE id=1')
    assert client.get(f'/lab/{rental}/api').status_code==401
    sign_in(client)
    with app.app_context():
        get_db().execute('UPDATE users SET session_version=1,enabled=0 WHERE id=1')
    assert client.get(f'/lab/{rental}/api').status_code==401
