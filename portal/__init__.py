"""OnlineFPGA borrowing portal, independent of the legacy MongoDB deployment."""
import os
import secrets
from datetime import timedelta
from pathlib import Path

from flask import Flask, session
from werkzeug.middleware.proxy_fix import ProxyFix


def create_app(test_config=None):
    root = Path(__file__).resolve().parent.parent
    app = Flask(__name__, instance_path=str(root / "instance"))
    app.config.from_mapping(
        SECRET_KEY=os.environ.get("SECRET_KEY"),
        DATABASE_PATH=os.environ.get("DATABASE_PATH", str(root / "instance/portal.sqlite3")),
        TIMEZONE=os.environ.get("TIMEZONE", "Asia/Taipei"),
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE", "false").lower() == "true",
        PERMANENT_SESSION_LIFETIME=timedelta(hours=12),
        MAX_CONTENT_LENGTH=32 * 1024,
        REGISTRATION_OPEN=os.environ.get("REGISTRATION_OPEN", "true").lower() == "true",
        MAINTENANCE_START=os.environ.get("MAINTENANCE_START", "06:00"),
        MAINTENANCE_END=os.environ.get("MAINTENANCE_END", "07:00"),
        SCHEDULER_INTERVAL=int(os.environ.get("SCHEDULER_INTERVAL", "10")),
        ENABLE_HARDWARE=os.environ.get("ENABLE_HARDWARE", "false").lower() == "true",
        FPGA_SSH_KEY=os.environ.get("FPGA_SSH_KEY", ""),
        FPGA_KNOWN_HOSTS=os.environ.get("FPGA_KNOWN_HOSTS", ""),
        FPGA_SUDO_PASSWORD=os.environ.get("FPGA_SUDO_PASSWORD", ""),
    )
    if test_config:
        app.config.update(test_config)
    Path(app.instance_path).mkdir(mode=0o700, parents=True, exist_ok=True)
    if not app.config["SECRET_KEY"]:
        # A persistent local secret keeps sessions valid across worker/restarts.
        secret_file = Path(app.instance_path) / "secret.key"
        try:
            fd = os.open(secret_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, "w") as stream:
                stream.write(secrets.token_hex(32))
        app.config["SECRET_KEY"] = secret_file.read_text().strip()
    if len(app.config["SECRET_KEY"]) < 32 or app.config["SECRET_KEY"].startswith("replace-with"):
        raise RuntimeError("SECRET_KEY 必須是至少 32 字元的隨機密鑰。")
    if os.environ.get("TRUST_PROXY", "false").lower() == "true":
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1)

    from . import db, views, cli
    db.init_app(app)
    app.register_blueprint(views.bp)
    cli.init_app(app)

    @app.context_processor
    def helpers():
        from .service import local_time, status_label
        if "csrf" not in session:
            session["csrf"] = secrets.token_urlsafe(32)
        return {"csrf_token": session["csrf"], "local_time": local_time,
                "status_label": status_label, "timezone": app.config["TIMEZONE"]}

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; font-src 'self'; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
        )
        if response.mimetype == "text/html" or response.mimetype == "application/json":
            response.headers["Cache-Control"] = "no-store"
        return response

    return app
