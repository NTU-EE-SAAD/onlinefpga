import hashlib
import hmac
import re
import secrets
import sqlite3
from functools import wraps

from flask import Blueprint, abort, current_app, flash, g, jsonify, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash, generate_password_hash

from .db import get_db, transaction, next_user_id
from . import service
from .student_ids import validate_student_id, STUDENT_ID_PATTERN

bp = Blueprint("web", __name__)


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not g.user:
            if request.path.startswith("/api/"):
                abort(401)
            return redirect(url_for("web.login"))
        return view(*args, **kwargs)
    return wrapped


def admin_required(view):
    @wraps(view)
    @login_required
    def wrapped(*args, **kwargs):
        if g.user["role"] != "admin":
            abort(403)
        return view(*args, **kwargs)
    return wrapped


@bp.before_app_request
def authentication():
    g.user = None
    if session.get("user_id"):
        user = get_db().execute("SELECT * FROM users WHERE id=?", (session["user_id"],)).fetchone()
        if user and user["enabled"] and user["session_version"] == session.get("version"):
            g.user = user
        else:
            session.clear()
    if request.path.startswith("/lab/"):
        # Notebook uses its own API. The proxy verifies the session, rental and Origin.
        request.max_content_length = 16 * 1024 * 1024
    elif request.method == "POST":
        submitted = request.form.get("csrf_token", request.headers.get("X-CSRF-Token", ""))
        expected = session.get("csrf", "")
        if not expected or not hmac.compare_digest(submitted.encode("utf-8"), expected.encode("utf-8")):
            abort(400, description="操作已過期，請重新整理頁面後再試。")
    if g.user and g.user["role"] == "student" and not g.user["student_id"]:
        if request.endpoint not in ("web.account", "web.logout", "static", "web.health", "web.guide"):
            if request.path.startswith(("/api/", "/lab/")):
                abort(403, description="請先在帳號設定補填學號。")
            return redirect(url_for("web.account"))


def limited(scope, maximum=10, seconds=900, identity=""):
    material = scope + ":" + (request.remote_addr or "unknown") + ":" + identity
    key = hashlib.sha256(material.encode()).hexdigest()
    clock = service.now()
    with transaction() as conn:
        row = conn.execute("SELECT * FROM rate_limits WHERE key=?", (key,)).fetchone()
        if not row or row["resets_at"] <= clock:
            conn.execute("INSERT INTO rate_limits VALUES(?,1,?) ON CONFLICT(key) DO UPDATE SET count=1,resets_at=?",
                         (key, clock + seconds, clock + seconds))
        elif row["count"] >= maximum:
            abort(429, description="操作次數過多，請稍後再試。")
        else:
            conn.execute("UPDATE rate_limits SET count=count+1 WHERE key=?", (key,))


def validate_password(password):
    if len(password) < 12 or len(password) > 128:
        raise service.RuleError("密碼需為 12 至 128 個字元。")


@bp.route("/")
def home():
    devices = service.devices_with_status()
    has_open = bool(g.user and get_db().execute(
        f"SELECT 1 FROM rentals WHERE user_id=? AND status IN ({service.OPEN})",
        (g.user["id"],)).fetchone())
    return render_template("home.html", devices=devices,
                           has_open=has_open,
                           available=sum(d["state"] == "available" for d in devices),
                           maintenance_start=current_app.config["MAINTENANCE_START"],
                           maintenance_end=current_app.config["MAINTENANCE_END"])


@bp.route("/register", methods=["GET", "POST"])
def register():
    if g.user:
        return redirect(url_for("web.home"))
    if not current_app.config["REGISTRATION_OPEN"]:
        abort(403, description="目前暫停開放註冊。")
    if request.method == "POST":
        limited("register", maximum=100, seconds=3600)
        email = request.form.get("email", "").strip().lower()
        name = request.form.get("name", "").strip()
        password = request.form.get("password", "")
        try:
            student_id = validate_student_id(request.form.get("student_id", ""))
            if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) or len(email) > 254:
                raise service.RuleError("請輸入有效的電子郵件。")
            if not 2 <= len(name) <= 60:
                raise service.RuleError("姓名需為 2 至 60 個字元。")
            validate_password(password)
            if password != request.form.get("password_confirm", ""):
                raise service.RuleError("兩次輸入的密碼不一致。")
            if request.form.get("terms") != "yes":
                raise service.RuleError("請先閱讀並同意借用須知。")
            hashed = generate_password_hash(password)
            with transaction() as conn:
                cursor = conn.execute("INSERT INTO users(id,student_id,email,name,password_hash,created_at) VALUES(?,?,?,?,?,?)",
                                      (next_user_id(conn), student_id, email, name, hashed, service.now()))
                service.event(conn, cursor.lastrowid, None, "register")
            flash("帳號已建立，請登入開始借用。", "success")
            return redirect(url_for("web.login"))
        except sqlite3.IntegrityError:
            flash("此學號或電子郵件已註冊，請登入；舊帳號可先以原信箱登入。", "error")
        except service.RuleError as exc:
            flash(str(exc), "error")
    return render_template("auth.html", registering=True, student_id_pattern=STUDENT_ID_PATTERN)


@bp.route("/login", methods=["GET", "POST"])
def login():
    if g.user:
        return redirect(url_for("web.home"))
    if request.method == "POST":
        limited("login", maximum=100)
        identifier = request.form.get("identifier", request.form.get("email", "")).strip()
        limited("login-account", maximum=10, identity=identifier.lower())
        if "@" in identifier:
            # Email login is retained only for legacy accounts and standalone admins.
            user = get_db().execute("SELECT * FROM users WHERE email=? AND student_id IS NULL",
                                    (identifier.lower(),)).fetchone()
        else:
            user = get_db().execute("SELECT * FROM users WHERE student_id=? COLLATE NOCASE",
                                    (identifier,)).fetchone()
        password = request.form.get("password", "")
        # Perform a password hash check for nonexistent accounts as well.
        if "DUMMY_HASH" not in current_app.config:
            current_app.config["DUMMY_HASH"] = generate_password_hash(secrets.token_urlsafe(20))
        hashed = user["password_hash"] if user else current_app.config["DUMMY_HASH"]
        if len(password) <= 128 and check_password_hash(hashed, password) and user and user["enabled"]:
            session.clear()
            session.update(user_id=user["id"], version=user["session_version"], csrf=secrets.token_urlsafe(32))
            session.permanent = True
            target = "web.account" if user["role"] == "student" and not user["student_id"] else "web.home"
            return redirect(url_for(target))
        flash("學號或密碼不正確，或帳號已停用。舊帳號尚未補填學號時可使用原信箱登入。", "error")
    return render_template("auth.html", registering=False)


@bp.post("/logout")
@login_required
def logout():
    session.clear()
    return redirect(url_for("web.home"))


@bp.get("/devices/<int:device_id>")
@login_required
def device(device_id):
    device = next((d for d in service.devices_with_status() if d["id"] == device_id), None)
    if not device:
        abort(404)
    open_rental = get_db().execute(f"SELECT 1 FROM rentals WHERE user_id=? AND status IN ({service.OPEN})",
                                   (g.user["id"],)).fetchone()
    return render_template("device.html", device=device, has_open=bool(open_rental))


@bp.post("/devices/<int:device_id>/book")
@login_required
def book(device_id):
    limited("book", maximum=20, seconds=60)
    try:
        mode = request.form.get("mode", "now")
        if mode not in ("now", "scheduled"):
            raise service.RuleError("請選擇借用方式。")
        start = service.parse_start(request.form.get("starts_at", "")) if mode == "scheduled" else None
        rental_id = service.book(g.user["id"], device_id, request.form.get("minutes"), start)
        flash("預約已建立；開始時間到達後，排程會自動準備設備。" if start else "借用已受理，排程正在準備設備。", "success")
        return redirect(url_for("web.rental", rental_id=rental_id))
    except service.RuleError as exc:
        flash(str(exc), "error")
        return redirect(url_for("web.device", device_id=device_id))


@bp.get("/rentals")
@login_required
def rentals():
    return render_template("rentals.html", rentals=service.rentals_for(g.user["id"]))


@bp.get("/rentals/<int:rental_id>")
@login_required
def rental(rental_id):
    row = get_db().execute("SELECT r.*,d.name AS device_name,d.model,d.driver,d.jupyter_url "
                           "FROM rentals r JOIN devices d ON d.id=r.device_id WHERE r.id=? AND r.user_id=?",
                           (rental_id, g.user["id"])).fetchone()
    if not row:
        abort(404)
    can_access = row["status"] == "active" and row["ends_at"] > service.now()
    return render_template("rental.html", rental=row, can_access=can_access)


@bp.post("/rentals/<int:rental_id>/return")
@login_required
def return_rental(rental_id):
    try:
        service.finish(g.user["id"], rental_id)
        flash("操作已受理。設備回收完成後會重新開放借用。", "success")
    except service.RuleError as exc:
        flash(str(exc), "error")
    return redirect(url_for("web.rental", rental_id=rental_id))


@bp.route("/account", methods=["GET", "POST"])
@login_required
def account():
    if request.method == "POST":
        if request.form.get("action") == "bind_student_id":
            limited("bind-student-id", maximum=10, identity=str(g.user["id"]))
            try:
                if g.user["student_id"]:
                    raise service.RuleError("學號已設定，若需更正請聯絡管理員。")
                if not check_password_hash(g.user["password_hash"], request.form.get("current_password", "")):
                    raise service.RuleError("目前密碼不正確。")
                student_id = validate_student_id(request.form.get("student_id", ""))
                with transaction() as conn:
                    cursor = conn.execute("UPDATE users SET student_id=?,session_version=session_version+1 "
                                          "WHERE id=? AND student_id IS NULL", (student_id, g.user["id"]))
                    if not cursor.rowcount:
                        raise service.RuleError("學號已設定，請重新登入。")
                    service.event(conn, g.user["id"], None, "student_id_bind")
                session.clear()
                flash("學號已設定，請改用學號登入。", "success")
                return redirect(url_for("web.login"))
            except sqlite3.IntegrityError:
                flash("此學號已被使用，請確認或聯絡管理員。", "error")
            except service.RuleError as exc:
                flash(str(exc), "error")
            return render_template("account.html", student_id_pattern=STUDENT_ID_PATTERN)
        limited("password", maximum=10, identity=str(g.user["id"]))
        try:
            if not check_password_hash(g.user["password_hash"], request.form.get("current_password", "")):
                raise service.RuleError("目前密碼不正確。")
            password = request.form.get("password", "")
            validate_password(password)
            if password != request.form.get("password_confirm"):
                raise service.RuleError("兩次輸入的密碼不一致。")
            with transaction() as conn:
                conn.execute("UPDATE users SET password_hash=?,session_version=session_version+1 WHERE id=?",
                             (generate_password_hash(password), g.user["id"]))
                service.event(conn, g.user["id"], None, "password_change")
            session.clear()
            flash("密碼已更新，請重新登入。", "success")
            return redirect(url_for("web.login"))
        except service.RuleError as exc:
            flash(str(exc), "error")
    return render_template("account.html", student_id_pattern=STUDENT_ID_PATTERN)


@bp.get("/guide")
def guide():
    return render_template("guide.html")


@bp.get("/api/status")
@login_required
def status():
    devices = service.devices_with_status()
    data = [{"id": d["id"], "state": d["state"], "available_at": d["available_at"]} for d in devices]
    rentals = [{"id": r["id"], "status": r["status"], "ends_at": r["ends_at"]}
               for r in service.rentals_for(g.user["id"])]
    heartbeat = get_db().execute("SELECT heartbeat FROM worker_state WHERE id=1").fetchone()
    return jsonify(devices=data, rentals=rentals, server_time=service.now(),
                   scheduler_online=bool(heartbeat and heartbeat["heartbeat"] > service.now() - max(60, current_app.config["SCHEDULER_INTERVAL"] * 3)))


@bp.get("/healthz")
def health():
    get_db().execute("SELECT 1 FROM users LIMIT 1").fetchone()
    row = get_db().execute("SELECT heartbeat FROM worker_state WHERE id=1").fetchone()
    worker_ok = bool(row and row["heartbeat"] > service.now() - max(60, current_app.config["SCHEDULER_INTERVAL"] * 3))
    return jsonify(web="ok", scheduler="ok" if worker_ok else "offline"), 200 if worker_ok else 503


@bp.get("/admin")
@admin_required
def admin():
    conn = get_db()
    return render_template("admin.html", devices=service.devices_with_status(),
                           rentals=service.rentals_for(limit=100),
                           users=conn.execute("SELECT id,name,student_id,email,role,enabled,created_at FROM users ORDER BY id DESC LIMIT 200").fetchall(),
                           events=conn.execute("SELECT e.*,u.email FROM events e LEFT JOIN users u ON u.id=e.user_id ORDER BY e.id DESC LIMIT 40").fetchall())


@bp.post("/admin/devices/<int:device_id>")
@admin_required
def update_device(device_id):
    try:
        name = request.form.get("name", "").strip()
        minutes = int(request.form.get("max_minutes", "120"))
        if not 2 <= len(name) <= 60 or minutes < 15 or minutes > 480 or minutes % 15:
            raise service.RuleError("設備名稱需為 2 至 60 字；時限需為 15 至 480 分鐘且為 15 的倍數。")
        maintenance = int(request.form.get("maintenance") == "yes")
        with transaction() as conn:
            device = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
            if not device:
                abort(404)
            if not maintenance and device["last_error"]:
                raise service.RuleError("設備有未解決的回收異常，請先執行重試回收。")
            conn.execute("UPDATE devices SET name=?,max_minutes=?,maintenance=? WHERE id=?",
                         (name, minutes, maintenance, device_id))
            service.event(conn, g.user["id"], None, "device_update", device["slug"])
        flash("設備設定已更新。維護狀態會阻止新的借用與預約啟用。", "success")
    except (ValueError, service.RuleError) as exc:
        flash(str(exc) if isinstance(exc, service.RuleError) else "設定格式不正確。", "error")
    return redirect(url_for("web.admin"))


@bp.post("/admin/devices")
@admin_required
def add_device():
    try:
        slug = request.form.get("slug", "").strip().lower()
        name = request.form.get("name", "").strip()
        model = request.form.get("model", "PYNQ-Z2")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,39}", slug) or not 2 <= len(name) <= 60:
            raise service.RuleError("代號需為 2–40 個小寫英文字母、數字或連字號；名稱需為 2–60 字。")
        with transaction() as conn:
            if model not in ("PYNQ-Z2", "KV260"):
                raise service.RuleError("請選擇支援的板型。")
            conn.execute("INSERT INTO devices(slug,name,model,description) VALUES(?,?,?,?)",
                         (slug, name, model, "適合數位邏輯、Python 與 FPGA 加速實驗。"))
            service.event(conn, g.user["id"], None, "device_add", slug)
        flash("已新增模擬設備。之後可透過 server CLI 設定實體連線。", "success")
    except sqlite3.IntegrityError:
        flash("設備代號已存在。", "error")
    except service.RuleError as exc:
        flash(str(exc), "error")
    return redirect(url_for("web.admin"))


@bp.post("/admin/rentals/<int:rental_id>/return")
@admin_required
def force_return(rental_id):
    try:
        service.finish(g.user["id"], rental_id, admin=True)
        flash("強制歸還已受理。", "success")
    except service.RuleError as exc:
        flash(str(exc), "error")
    return redirect(url_for("web.admin"))


@bp.post("/admin/devices/<int:device_id>/retry")
@admin_required
def retry_device(device_id):
    with transaction() as conn:
        device = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        if not device:
            abort(404)
        if conn.execute(f"SELECT 1 FROM rentals WHERE device_id=? AND status IN ({service.RUNNING})", (device_id,)).fetchone():
            flash("設備仍有執行中的操作，請稍後再試。", "error")
        else:
            row = conn.execute("SELECT * FROM rentals WHERE device_id=? AND status='failed' ORDER BY id DESC LIMIT 1", (device_id,)).fetchone()
            if row:
                if conn.execute(f"SELECT 1 FROM rentals WHERE user_id=? AND status IN ({service.OPEN})", (row["user_id"],)).fetchone():
                    flash("原借用者已有其他借用，請先取消該筆借用再重試。", "error")
                else:
                    conn.execute("UPDATE rentals SET status='releasing',claimed_at=NULL,attempts=0,error='',finished_at=NULL,updated_at=? WHERE id=?", (service.now(), row["id"]))
                    service.event(conn, g.user["id"], row["id"], "retry_cleanup")
                    flash("已排入重試回收。成功後可手動解除設備維護。", "success")
            else:
                flash("沒有需要重試的回收紀錄。", "error")
    return redirect(url_for("web.admin"))


@bp.post("/admin/users/<int:user_id>")
@admin_required
def update_user(user_id):
    if user_id == g.user["id"]:
        flash("無法停用目前登入的管理員。", "error")
        return redirect(url_for("web.admin"))
    with transaction() as conn:
        user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if not user:
            abort(404)
        enabled = 0 if user["enabled"] else 1
        conn.execute("UPDATE users SET enabled=?,session_version=session_version+1 WHERE id=?", (enabled, user_id))
        if not enabled:
            conn.execute("UPDATE rentals SET status='cancelled',reason='disabled',updated_at=?,finished_at=? WHERE user_id=? AND status='reserved'", (service.now(), service.now(), user_id))
            conn.execute("UPDATE rentals SET status='releasing',reason='disabled',claimed_at=NULL,updated_at=? WHERE user_id=? AND status='active'", (service.now(), user_id))
        service.event(conn, g.user["id"], None, "user_enable" if enabled else "user_disable", user["email"])
    flash("帳號狀態已更新。", "success")
    return redirect(url_for("web.admin"))


@bp.app_errorhandler(400)
@bp.app_errorhandler(401)
@bp.app_errorhandler(403)
@bp.app_errorhandler(404)
@bp.app_errorhandler(413)
@bp.app_errorhandler(429)
def error_page(error):
    messages = {401: "請先登入。", 403: "你沒有權限存取此頁面。", 404: "找不到這個頁面。",
                413: "送出的資料過大。", 429: "操作次數過多，請稍後再試。", 400: "操作無效，請重新整理後再試。"}
    message = error.description if error.code in (400, 403, 429) and not error.description.startswith(("The ", "You ")) else messages[error.code]
    if request.path.startswith("/api/"):
        return jsonify(error=message), error.code
    return render_template("error.html", code=error.code, message=message), error.code
