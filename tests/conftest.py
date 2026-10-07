from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from werkzeug.security import generate_password_hash

from portal import create_app, service
from portal.db import initialize, get_db


@pytest.fixture
def app(tmp_path, monkeypatch):
    clock = int(datetime(2026, 10, 8, 10, tzinfo=ZoneInfo("Asia/Taipei")).timestamp())
    monkeypatch.setattr(service, "now", lambda: clock)
    app = create_app({"TESTING": True, "SECRET_KEY": "test-secret-" * 4,
                      "DATABASE_PATH": str(tmp_path / "test.sqlite3"), "ENABLE_HARDWARE": False})
    with app.app_context():
        initialize()
        for i in range(1, 5):
            get_db().execute("INSERT INTO users(id,email,name,password_hash,role,created_at) VALUES(?,?,?,?,?,?)",
                             (i, f"student{i}@example.com", f"Student {i}", generate_password_hash("correct-password-123"),
                              "admin" if i == 4 else "student", clock))
            get_db().execute("UPDATE users SET student_id=? WHERE id=?", (f"B1590100{i}", i))
    return app


@pytest.fixture
def client(app):
    return app.test_client()


def sign_in(client, user=1):
    with client.session_transaction() as session:
        session.update(user_id=user, version=1, csrf="test-csrf")
    return {"csrf_token": "test-csrf"}
