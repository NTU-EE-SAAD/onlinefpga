"""Durable, transactional allocation. All timestamps are UTC epoch seconds."""
import secrets
import time
from datetime import datetime, timedelta, time as day_time
from zoneinfo import ZoneInfo

from flask import current_app

from .db import get_db, transaction

OPEN = "'reserved','preparing','active','releasing'"
RUNNING = "'preparing','active','releasing'"
LABELS = {"reserved": "已預約", "preparing": "準備中", "active": "使用中",
          "releasing": "回收中", "completed": "已結束", "cancelled": "已取消", "failed": "設備異常"}


class RuleError(ValueError):
    pass


def now():
    return int(time.time())


def status_label(value):
    return LABELS.get(value, value)


def local_time(value, fmt="%m/%d %H:%M"):
    if value is None:
        return "—"
    return datetime.fromtimestamp(value, ZoneInfo(current_app.config["TIMEZONE"])).strftime(fmt)


def parse_start(value):
    try:
        dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo(current_app.config["TIMEZONE"]))
        return int(dt.timestamp())
    except (ValueError, TypeError):
        raise RuleError("請選擇有效的預約日期與時間。")


def maintenance_overlap(start, end):
    zone = ZoneInfo(current_app.config["TIMEZONE"])
    a = day_time.fromisoformat(current_app.config["MAINTENANCE_START"])
    b = day_time.fromisoformat(current_app.config["MAINTENANCE_END"])
    if a == b:
        return False
    first = datetime.fromtimestamp(start, zone).date() - timedelta(days=1)
    last = datetime.fromtimestamp(end, zone).date()
    day = first
    while day <= last:
        left = datetime.combine(day, a, zone)
        right = datetime.combine(day + timedelta(days=1 if b <= a else 0), b, zone)
        if start < right.timestamp() and end > left.timestamp():
            return True
        day += timedelta(days=1)
    return False


def event(conn, user_id, rental_id, action, detail="", at=None):
    conn.execute("INSERT INTO events(user_id,rental_id,action,detail,created_at) VALUES(?,?,?,?,?)",
                 (user_id, rental_id, action, detail, now() if at is None else at))


def book(user_id, device_id, minutes, start=None):
    clock = now()
    immediate = start is None
    start = clock if immediate else start
    try:
        minutes = int(minutes)
    except (ValueError, TypeError):
        raise RuleError("請選擇有效的借用時間。")
    if not immediate and start < clock + 60:
        raise RuleError("預約開始時間至少需在一分鐘之後。")
    if start > clock + 14 * 86400:
        raise RuleError("目前可預約未來 14 天內的時段。")
    with transaction() as conn:
        user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        device = conn.execute("SELECT * FROM devices WHERE id=?", (device_id,)).fetchone()
        if not user or not user["enabled"]:
            raise RuleError("帳號目前無法借用。")
        if not device or device["maintenance"]:
            raise RuleError("設備目前維護中，請選擇其他設備。")
        if device["driver"] == "pynq" and not current_app.config["ENABLE_HARDWARE"]:
            raise RuleError("實體設備尚未啟用。")
        if minutes < 15 or minutes > device["max_minutes"] or minutes % 15:
            raise RuleError(f"借用時間需為 15 分鐘的倍數，最長 {device['max_minutes']} 分鐘。")
        end = start + minutes * 60
        if maintenance_overlap(start, end):
            raise RuleError("此時段與每日維護時間重疊，請選擇其他時間。")
        if conn.execute(f"SELECT 1 FROM rentals WHERE user_id=? AND status IN ({OPEN})", (user_id,)).fetchone():
            raise RuleError("每個帳號同時只能有一筆借用或預約，請先結束或取消目前的紀錄。")
        conflict = conn.execute(
            f"SELECT 1 FROM rentals WHERE device_id=? AND status IN ({OPEN}) "
            "AND starts_at < ? AND ends_at > ?", (device_id, end, start)).fetchone()
        busy = conn.execute(
            f"SELECT 1 FROM rentals WHERE device_id=? AND status IN ({RUNNING})",
            (device_id,)).fetchone() if immediate else None
        if conflict or busy:
            raise RuleError("此時段已被借用或設備正在回收，請選擇其他時段。")
        cursor = conn.execute(
            "INSERT INTO rentals(user_id,device_id,starts_at,ends_at,status,created_at,updated_at) "
            "VALUES(?,?,?,?,'reserved',?,?)", (user_id, device_id, start, end, clock, clock))
        rental_id = cursor.lastrowid
        event(conn, user_id, rental_id, "borrow" if immediate else "reserve", at=clock)
        return rental_id


def finish(user_id, rental_id, admin=False):
    with transaction() as conn:
        row = conn.execute("SELECT * FROM rentals WHERE id=?", (rental_id,)).fetchone()
        if not row or (row["user_id"] != user_id and not admin):
            raise RuleError("找不到這筆借用紀錄。")
        if row["status"] in ("completed", "cancelled", "failed", "releasing"):
            return
        if row["status"] == "preparing":
            raise RuleError("設備正在準備，請稍後再歸還。")
        target = "cancelled" if row["status"] == "reserved" else "releasing"
        conn.execute("UPDATE rentals SET status=?,updated_at=?,claimed_at=NULL,attempts=0,reason=?,"
                     "finished_at=? WHERE id=?", (target, now(), "admin" if admin else "user",
                                                now() if target == "cancelled" else None, rental_id))
        event(conn, user_id, rental_id, "force_return" if admin else "return")


def devices_with_status():
    conn = get_db()
    result = []
    for device in conn.execute("SELECT * FROM devices ORDER BY id"):
        item = dict(device)
        running = conn.execute(f"SELECT * FROM rentals WHERE device_id=? AND status IN ({RUNNING})",
                               (device["id"],)).fetchone()
        pending = conn.execute("SELECT * FROM rentals WHERE device_id=? AND status='reserved' "
                               "AND starts_at<=? AND ends_at>? ORDER BY starts_at LIMIT 1",
                               (device["id"], now(), now())).fetchone() if not running else None
        item["state"] = ("maintenance" if device["maintenance"] else running["status"] if running
                         else "preparing" if pending else "available")
        item["available_at"] = running["ends_at"] if running else pending["ends_at"] if pending else None
        item["reservations"] = conn.execute(
            "SELECT starts_at,ends_at FROM rentals WHERE device_id=? AND status='reserved' "
            "AND ends_at>? ORDER BY starts_at LIMIT 8", (device["id"], now())).fetchall()
        result.append(item)
    return result


def rentals_for(user_id=None, limit=50):
    condition = "WHERE r.user_id=?" if user_id else ""
    params = (user_id, limit) if user_id else (limit,)
    return get_db().execute(
        "SELECT r.*,d.name AS device_name,d.model,d.driver,d.jupyter_url,u.name AS user_name,u.email "
        "FROM rentals r JOIN devices d ON d.id=r.device_id JOIN users u ON u.id=r.user_id "
        f"{condition} ORDER BY r.created_at DESC,r.id DESC LIMIT ?", params).fetchall()


def tick(clock=None, driver_factory=None):
    """One recoverable scheduler iteration. Run from a single locked worker."""
    from .hardware import driver_for
    factory = driver_factory or driver_for
    clock = now() if clock is None else clock
    with transaction() as conn:
        conn.execute("INSERT INTO worker_state VALUES(1,?) ON CONFLICT(id) DO UPDATE SET heartbeat=?", (clock, clock))
        conn.execute("DELETE FROM rate_limits WHERE resets_at<?", (clock,))
        expired = conn.execute("SELECT * FROM rentals WHERE status='active' AND ends_at<=?", (clock,)).fetchall()
        for row in expired:
            conn.execute("UPDATE rentals SET status='releasing',claimed_at=NULL,attempts=0,reason='expired',updated_at=? WHERE id=?", (clock, row["id"]))
            event(conn, row["user_id"], row["id"], "expire", at=clock)
        missed = conn.execute("SELECT * FROM rentals WHERE status='reserved' AND ends_at<=?", (clock,)).fetchall()
        for row in missed:
            conn.execute("UPDATE rentals SET status='cancelled',reason='missed',finished_at=?,updated_at=? WHERE id=?", (clock, clock, row["id"]))
            event(conn, row["user_id"], row["id"], "missed", at=clock)
    # Release old allocations before activating the next reservation.
    process_jobs("releasing", clock, factory)
    with transaction() as conn:
        rows = conn.execute("SELECT r.*,d.maintenance,d.driver FROM rentals r JOIN devices d ON d.id=r.device_id "
                            "WHERE r.status='reserved' AND r.starts_at<=? AND r.ends_at>? ORDER BY r.starts_at,r.id",
                            (clock, clock)).fetchall()
        for row in rows:
            if row["maintenance"] or (row["driver"] == "pynq" and not current_app.config["ENABLE_HARDWARE"]):
                continue
            if conn.execute(f"SELECT 1 FROM rentals WHERE device_id=? AND status IN ({RUNNING})", (row["device_id"],)).fetchone():
                continue
            conn.execute("UPDATE rentals SET status='preparing',updated_at=?,access_secret=? WHERE id=?",
                         (clock, secrets.token_urlsafe(18), row["id"]))
    process_jobs("preparing", clock, factory)


def process_jobs(state, clock, factory):
    ids = get_db().execute("SELECT id FROM rentals WHERE status=? AND (claimed_at IS NULL OR claimed_at<=?)",
                           (state, clock - 180)).fetchall()
    for item in ids:
        with transaction() as conn:
            row = conn.execute("SELECT * FROM rentals WHERE id=? AND status=? AND (claimed_at IS NULL OR claimed_at<=?)",
                               (item["id"], state, clock - 180)).fetchone()
            if not row:
                continue
            conn.execute("UPDATE rentals SET claimed_at=?,attempts=attempts+1 WHERE id=?", (clock, row["id"]))
            device = dict(conn.execute("SELECT * FROM devices WHERE id=?", (row["device_id"],)).fetchone())
            rental = dict(row)
        # Never hold the DB write lock while doing network I/O.
        try:
            driver = factory(device)
            if state == "preparing":
                driver.prepare(device, rental)
            else:
                driver.release(device, rental)
        except Exception:
            current_app.logger.exception("Device operation failed for rental %s", rental["id"])
            with transaction() as conn:
                # Quarantine instead of making a possibly occupied board available.
                conn.execute("UPDATE devices SET maintenance=1,last_error=? WHERE id=?",
                             ("設備操作失敗，需由管理員檢查後重試回收。", device["id"]))
                conn.execute("UPDATE rentals SET status='failed',error=?,updated_at=?,finished_at=?,access_secret='' WHERE id=?",
                             ("設備操作失敗；請聯絡管理員。", clock, clock, rental["id"]))
                event(conn, rental["user_id"], rental["id"], "hardware_failed", state, clock)
            continue
        with transaction() as conn:
            if state == "preparing":
                # A long provisioning operation must never grant an expired lease.
                current = max(clock, now())
                user = conn.execute("SELECT enabled FROM users WHERE id=?", (rental["user_id"],)).fetchone()
                target = "active" if rental["ends_at"] > current and user["enabled"] else "releasing"
                conn.execute("UPDATE rentals SET status=?,claimed_at=NULL,attempts=0,updated_at=? WHERE id=? AND status=?",
                             (target, current, rental["id"], state))
                event(conn, rental["user_id"], rental["id"], "activate", at=current)
            else:
                conn.execute("UPDATE rentals SET status='completed',finished_at=?,updated_at=?,access_secret='',claimed_at=NULL WHERE id=? AND status=?",
                             (clock, clock, rental["id"], state))
                event(conn, rental["user_id"], rental["id"], "released", at=clock)
                conn.execute("UPDATE devices SET last_error='' WHERE id=?", (device["id"],))
