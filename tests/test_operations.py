import sqlite3

from portal.db import get_db
from portal.hardware import PynqDriver
from tests.conftest import sign_in
import pytest


def test_admin_can_add_unique_simulated_device(client, app):
    data = sign_in(client,4)
    for _ in range(2):
        assert client.post("/admin/devices", data={**data,"slug":"pynq-04","name":"PYNQ 04"}).status_code == 302
    with app.app_context():
        rows = get_db().execute("SELECT * FROM devices WHERE slug='pynq-04'").fetchall()
        assert len(rows) == 1
        assert rows[0]["driver"] == "simulated"


def test_backup_live_database(app,tmp_path):
    target = tmp_path / "backup.sqlite3"
    result = app.test_cli_runner().invoke(args=["backup-db",str(target)])
    assert result.exit_code == 0
    with sqlite3.connect(target) as conn:
        assert conn.execute("SELECT count(*) FROM users").fetchone()[0] == 4
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert app.test_cli_runner().invoke(args=["backup-db",str(target)]).exit_code != 0


def test_init_db_preserves_inventory(app):
    with app.app_context():
        get_db().execute("UPDATE devices SET name='Custom Device' WHERE id=1")
    result = app.test_cli_runner().invoke(args=["init-db"])
    assert result.exit_code == 0
    with app.app_context():
        assert get_db().execute("SELECT name FROM devices WHERE id=1").fetchone()[0] == "Custom Device"


def test_cli_password_reset_revokes_existing_session(client,app):
    sign_in(client)
    result = app.test_cli_runner().invoke(args=["reset-password","student1@example.com"],
                                         input="new-secret-password\nnew-secret-password\n")
    assert result.exit_code == 0
    assert client.get("/api/status").status_code == 401


def test_hardware_driver_requires_explicit_enable(app,monkeypatch):
    import paramiko
    monkeypatch.setattr(paramiko,"SSHClient",lambda:pytest.fail("Must not attempt SSH"))
    with app.app_context(), pytest.raises(RuntimeError,match="disabled"):
        PynqDriver().prepare({"driver":"pynq"},{"access_secret":"secret"})


def test_hardware_driver_requires_credentials_before_network(app,monkeypatch):
    import paramiko
    app.config["ENABLE_HARDWARE"] = True
    monkeypatch.setattr(paramiko,"SSHClient",lambda:pytest.fail("Must not attempt SSH"))
    with app.app_context(), pytest.raises(RuntimeError,match="configured"):
        PynqDriver().prepare({"driver":"pynq","host":"192.0.2.1"},{"access_secret":"secret"})


def test_admin_can_add_kv260_and_cli_sets_model(client, app):
    data=sign_in(client,4)
    client.post('/admin/devices',data={**data,'slug':'kria-01','name':'Kria','model':'KV260'})
    result=app.test_cli_runner().invoke(args=['configure-device','kria-01','--host','192.0.2.2',
                                            '--ssh-user','ubuntu','--model','KV260','--jupyter-url','http://192.0.2.2:9090'])
    assert result.exit_code==0, result.output
    with app.app_context():
        row=get_db().execute("SELECT * FROM devices WHERE slug='kria-01'").fetchone()
        assert row['model']=='KV260' and row['maintenance']==1 and row['driver']=='pynq'
    bad=app.test_cli_runner().invoke(args=['configure-device','kria-01','--host','192.0.2.2',
                                          '--jupyter-url','http://other.example:9090/escape'])
    assert bad.exit_code!=0
