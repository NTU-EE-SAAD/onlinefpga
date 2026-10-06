import fcntl
import os
import signal
import time
from pathlib import Path
from urllib.parse import urlsplit

import click
from werkzeug.security import generate_password_hash

from .db import get_db, initialize, transaction
from . import service


def init_app(app):
    @app.cli.command("init-db")
    def init_db():
        initialize()
        click.echo("資料庫已初始化；僅建立模擬設備，沒有連線或操作 FPGA。")

    @app.cli.command("create-admin")
    @click.option("--email", prompt=True)
    @click.option("--name", default="管理員", prompt=True)
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
    def create_admin(email, name, password):
        from .views import validate_password
        try:
            validate_password(password)
            email = email.strip().lower()
            if "@" not in email or not name.strip():
                raise service.RuleError("請輸入有效的姓名與電子郵件。")
            with transaction() as conn:
                if conn.execute("SELECT 1 FROM users WHERE email=?", (email,)).fetchone():
                    raise service.RuleError("帳號已存在；請使用 promote-admin。")
                conn.execute("INSERT INTO users(email,name,password_hash,role,created_at) VALUES(?,?,?,'admin',?)",
                             (email, name, generate_password_hash(password), service.now()))
            click.echo("管理員已建立。")
        except service.RuleError as exc:
            raise click.ClickException(str(exc))

    @app.cli.command("promote-admin")
    @click.argument("email")
    def promote_admin(email):
        with transaction() as conn:
            cursor = conn.execute("UPDATE users SET role='admin',session_version=session_version+1 WHERE email=?", (email.strip().lower(),))
            if not cursor.rowcount:
                raise click.ClickException("帳號不存在。")
        click.echo("已提升為管理員，請重新登入。")

    @app.cli.command("reset-password")
    @click.argument("email")
    @click.option("--password", prompt=True, hide_input=True, confirmation_prompt=True)
    def reset_password(email, password):
        from .views import validate_password
        try:
            validate_password(password)
        except service.RuleError as exc:
            raise click.ClickException(str(exc))
        with transaction() as conn:
            cursor = conn.execute("UPDATE users SET password_hash=?,session_version=session_version+1 WHERE email=?",
                                 (generate_password_hash(password), email.strip().lower()))
            if not cursor.rowcount:
                raise click.ClickException("帳號不存在。")
        click.echo("密碼已重設，原有登入 session 已撤銷。")

    @app.cli.command("configure-device")
    @click.argument("slug")
    @click.option("--host", required=True)
    @click.option("--ssh-port", default=22, type=click.IntRange(1, 65535))
    @click.option("--ssh-user", default="xilinx")
    @click.option("--jupyter-url", required=True)
    def configure_device(slug, host, ssh_port, ssh_user, jupyter_url):
        """Register a physical PYNQ; remains under maintenance until explicitly enabled."""
        parts = urlsplit(jupyter_url)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            raise click.ClickException("Jupyter URL 必須是有效的 http/https 網址，不可含帳密。")
        with transaction() as conn:
            row = conn.execute("SELECT * FROM devices WHERE slug=?", (slug,)).fetchone()
            if not row:
                raise click.ClickException("設備不存在。")
            if conn.execute(f"SELECT 1 FROM rentals WHERE device_id=? AND status IN ({service.OPEN})", (row["id"],)).fetchone():
                raise click.ClickException("設備仍有借用或預約，請先結束所有紀錄。")
            conn.execute("UPDATE devices SET driver='pynq',host=?,ssh_port=?,ssh_user=?,jupyter_url=?,maintenance=1 WHERE id=?",
                         (host, ssh_port, ssh_user, jupyter_url, row["id"]))
        click.echo("實體設備已登記且維持維護中；此命令不會連線到 FPGA。")

    @app.cli.command("backup-db")
    @click.argument("destination", type=click.Path())
    def backup_db(destination):
        import sqlite3
        target = Path(destination).resolve()
        if target == Path(app.config["DATABASE_PATH"]).resolve() or target.exists():
            raise click.ClickException("請指定尚不存在的備份檔案。")
        target.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(fd)
        with sqlite3.connect(target) as backup:
            get_db().backup(backup)
        click.echo(f"備份已建立：{target}")

    @app.cli.command("scheduler")
    @click.option("--once", is_flag=True, help="Process one iteration and exit.")
    def scheduler(once):
        if app.config["SCHEDULER_INTERVAL"] < 1 or app.config["SCHEDULER_INTERVAL"] > 60:
            raise click.ClickException("SCHEDULER_INTERVAL 必須介於 1 與 60 秒。")
        lock_path = Path(app.config["DATABASE_PATH"]).resolve().with_suffix(".scheduler.lock")
        with lock_path.open("a") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise click.ClickException("已有排程程序執行中。")
            if once:
                service.tick()
                click.echo("排程已執行一次。")
                return
            stop = False
            def shutdown(_signum, _frame):
                nonlocal stop
                stop = True
            signal.signal(signal.SIGTERM, shutdown)
            signal.signal(signal.SIGINT, shutdown)
            click.echo("排程已啟動。")
            while not stop:
                started = time.monotonic()
                try:
                    service.tick()
                except Exception:
                    app.logger.exception("Scheduler iteration failed")
                remaining = app.config["SCHEDULER_INTERVAL"] - (time.monotonic() - started)
                while remaining > 0 and not stop:
                    time.sleep(min(remaining, 0.5))
                    remaining -= 0.5
