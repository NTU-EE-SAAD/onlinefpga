import sqlite3

import pytest

from portal import create_app, service
from portal.db import get_db, initialize, transaction, next_user_id
from portal.student_ids import validate_student_id
from tests.test_web import csrf
from tests.conftest import sign_in


@pytest.mark.parametrize("value,expected", [
    ("b15901001", "B15901001"), ("R15921001", "R15921001"),
    ("D15943001", "D15943001"), ("B15a01101", "B15A01101"),
    ("B15b01001", "B15B01001"), ("R15K41001", "R15K41001"),
])
def test_regular_ids_and_letter_department_codes(value, expected):
    assert validate_student_id(value) == expected


@pytest.mark.parametrize("value", [
    "", "B1590100", "B159010001", "X15901001", "T15901101", "P15921001",
    "Ｂ15901001", "B１５901001", "B15901 01", "B1590100x", "B15901001\nX",
    "B15999001", "B15G01001", "B15D01001", "B15901A01", "B15921001", "R15901001",
])
def test_reject_invalid_or_unsupported_ids(value):
    with pytest.raises(service.RuleError):
        validate_student_id(value)


def registration(client, student_id="B15901009", email="new@example.com"):
    return client.post("/register", data={
        "csrf_token": csrf(client, "/register"), "student_id": student_id,
        "email": email, "name": "Test Student", "password": "long-student-password",
        "password_confirm": "long-student-password", "terms": "yes",
    })


def test_server_validation_and_case_insensitive_uniqueness(client, app):
    assert registration(client, "X15901009").status_code == 200
    assert registration(client, "B15999009").status_code == 200
    assert registration(client).status_code == 302
    assert registration(client, "b15901009", "other@example.com").status_code == 200
    assert registration(client, "B15901008", "new@example.com").status_code == 200
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM users WHERE student_id='B15901009'").fetchone()[0] == 1
    token = csrf(client)
    # A newly registered student must use their student ID, not their contact email.
    assert client.post("/login", data={"csrf_token": token, "identifier": "new@example.com",
                                       "password": "long-student-password"}).status_code == 200
    assert client.get("/api/status").status_code == 401
    assert client.post("/login", data={"csrf_token": token, "identifier": "b15901009",
                                       "password": "long-student-password"}).status_code == 302
    assert client.get("/api/status").status_code == 200


def test_email_is_still_required(client, app):
    assert registration(client, email="").status_code == 200
    with app.app_context():
        assert get_db().execute("SELECT COUNT(*) FROM users").fetchone()[0] == 4


def test_removed_account_ids_are_not_reused(app):
    with app.app_context(), transaction() as conn:
        highest = next_user_id(conn)
        conn.execute("DELETE FROM users")
        assert next_user_id(conn) > highest


def test_legacy_account_requires_id_and_revokes_old_sessions(client, app):
    with app.app_context():
        get_db().execute("UPDATE users SET student_id=NULL WHERE id=1")
    data = sign_in(client)
    assert client.get("/devices/1").location.endswith("/account")
    assert client.get("/api/status").status_code == 403
    assert client.get("/lab/1/tree").status_code == 403
    assert client.post("/account", data={**data, "action": "bind_student_id", "student_id": "B15901001",
                                        "current_password": "wrong-password"}).status_code == 200
    other = app.test_client()
    sign_in(other)
    assert client.post("/account", data={**data, "action": "bind_student_id", "student_id": "B15901001",
                                        "current_password": "correct-password-123"}).status_code == 302
    assert other.get("/api/status").status_code == 401


def test_existing_database_migration_preserves_accounts(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE users(id INTEGER PRIMARY KEY,email TEXT UNIQUE NOT NULL,name TEXT NOT NULL,"
                     "password_hash TEXT NOT NULL,role TEXT DEFAULT 'student',enabled INTEGER DEFAULT 1,"
                     "session_version INTEGER DEFAULT 1,created_at INTEGER NOT NULL)")
        conn.execute("INSERT INTO users(id,email,name,password_hash,created_at) VALUES(42,'old@example.com','Old','hash',1)")
    app = create_app({"TESTING": True, "SECRET_KEY": "test-secret-" * 4, "DATABASE_PATH": str(path)})
    with app.app_context():
        initialize()
        initialize()
        user = get_db().execute("SELECT * FROM users WHERE id=42").fetchone()
        assert user["email"] == "old@example.com" and user["student_id"] is None
        with transaction() as conn:
            conn.execute("DELETE FROM users")
            assert next_user_id(conn) == 43
