import re

from portal import service
from portal.db import get_db
from tests.conftest import sign_in


def csrf(client, path="/login"):
    html = client.get(path).get_data(as_text=True)
    return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)


def test_public_pages(client):
    for path in ["/", "/login", "/register", "/guide"]:
        response = client.get(path)
        assert response.status_code == 200
        assert "Content-Security-Policy" in response.headers
    assert client.get("/devices/1").status_code == 302
    assert client.get("/api/status").status_code == 401


def test_registration_login_and_password_hash(client, app):
    token = csrf(client, "/register")
    response = client.post("/register", data={"csrf_token":token,"name":"Test Student","email":"NEW@example.com",
                                              "password":"a-long-password-123","password_confirm":"a-long-password-123","terms":"yes"})
    assert response.status_code == 302
    with app.app_context():
        row = get_db().execute("SELECT * FROM users WHERE email='new@example.com'").fetchone()
        assert row and row["password_hash"] != "a-long-password-123"
        assert row["role"] == "student"
    token = csrf(client)
    assert client.post("/login", data={"csrf_token":token,"email":"new@example.com","password":"a-long-password-123"}).status_code == 302
    assert client.get("/rentals").status_code == 200


def test_csrf_and_admin_permissions(client):
    sign_in(client)
    assert client.post("/devices/1/book", data={"minutes":15}).status_code == 400
    assert client.post("/devices/1/book", data={"minutes":15,"csrf_token":"無效的令牌"}).status_code == 400
    assert client.get("/admin").status_code == 403
    assert client.post("/admin/users/2",data={"csrf_token":"test-csrf"}).status_code == 403


def test_booking_privacy_and_return(client, app):
    data = sign_in(client)
    response = client.post("/devices/1/book", data={**data,"minutes":15,"mode":"now","email":"student2@example.com"})
    assert response.status_code == 302
    with app.app_context():
        row = get_db().execute("SELECT * FROM rentals").fetchone()
        assert row["user_id"] == 1
        rental_id = row["id"]
        service.tick()
    assert client.get(f"/rentals/{rental_id}").status_code == 200
    sign_in(client,2)
    assert client.get(f"/rentals/{rental_id}").status_code == 404
    client.post(f"/rentals/{rental_id}/return", data=data)
    with app.app_context():
        assert get_db().execute("SELECT status FROM rentals").fetchone()[0] == "active"
    sign_in(client)
    client.post(f"/rentals/{rental_id}/return", data=data)
    with app.app_context():
        service.tick()
        assert get_db().execute("SELECT status FROM rentals").fetchone()[0] == "completed"


def test_status_never_exposes_secrets(client, app):
    sign_in(client)
    with app.app_context():
        rental = service.book(1, 1, 15)
        service.tick()
        secret = get_db().execute("SELECT access_secret FROM rentals WHERE id=?", (rental,)).fetchone()[0]
    response = client.get("/api/status")
    assert response.status_code == 200
    assert secret not in response.get_data(as_text=True)
    assert "password_hash" not in response.get_data(as_text=True)


def test_admin_disable_revokes_session_and_returns_board(client, app):
    student = app.test_client()
    sign_in(student)
    data = sign_in(client,4)
    with app.app_context():
        service.book(1, 1, 15)
        service.tick()
    assert client.get("/admin").status_code == 200
    client.post("/admin/users/1",data=data)
    assert student.get("/api/status").status_code == 401
    with app.app_context():
        assert get_db().execute("SELECT status FROM rentals").fetchone()[0] == "releasing"
        service.tick()
        assert get_db().execute("SELECT status FROM rentals").fetchone()[0] == "completed"


def test_password_change_revokes_other_sessions(client, app):
    data = sign_in(client)
    other = app.test_client()
    sign_in(other)
    response = client.post("/account",data={**data,"current_password":"correct-password-123","password":"new-password-12345","password_confirm":"new-password-12345"})
    assert response.status_code == 302
    assert other.get("/api/status").status_code == 401


def test_failed_cleanup_retry_can_leave_maintenance(client, app):
    data = sign_in(client,4)
    with app.app_context():
        rental = service.book(1, 1, 15)
        get_db().execute("UPDATE rentals SET status='failed' WHERE id=?", (rental,))
        get_db().execute("UPDATE devices SET maintenance=1,last_error='offline' WHERE id=1")
    client.post("/admin/devices/1/retry",data=data)
    with app.app_context():
        service.tick()
        assert get_db().execute("SELECT last_error FROM devices WHERE id=1").fetchone()[0] == ""
    client.post("/admin/devices/1",data={**data,"name":"PYNQ 01","max_minutes":120})
    with app.app_context():
        assert get_db().execute("SELECT maintenance FROM devices WHERE id=1").fetchone()[0] == 0


def test_expired_live_rental_hides_credentials(client,app,monkeypatch):
    sign_in(client)
    with app.app_context():
        rental = service.book(1,1,15)
        service.tick()
        get_db().execute("UPDATE devices SET driver='pynq',jupyter_url='http://board.example:9090' WHERE id=1")
        secret = get_db().execute("SELECT access_secret FROM rentals WHERE id=?",(rental,)).fetchone()[0]
    page = client.get(f"/rentals/{rental}").get_data(as_text=True)
    assert secret not in page
    assert f'/lab/{rental}/tree' in page
    old_now = service.now()
    monkeypatch.setattr(service,"now",lambda:old_now+901)
    page = client.get(f"/rentals/{rental}").get_data(as_text=True)
    assert secret not in page
    assert "http://board.example:9090" not in page
    assert f'/lab/{rental}/tree' not in page


def test_health_tracks_worker(client,app):
    assert client.get("/healthz").status_code == 503
    with app.app_context():
        service.tick()
    assert client.get("/healthz").status_code == 200


def test_login_rate_limit(client):
    token = csrf(client)
    for _ in range(10):
        assert client.post("/login",data={"csrf_token":token,"email":"bad@example.com","password":"invalid"}).status_code == 200
    assert client.post("/login",data={"csrf_token":token,"email":"bad@example.com","password":"invalid"}).status_code == 429
