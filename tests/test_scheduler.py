from concurrent.futures import ThreadPoolExecutor

import pytest

from portal import service
from portal.db import get_db, transaction


def test_borrow_expire_and_release(app):
    with app.app_context():
        start = service.now()
        rental = service.book(1, 1, 15)
        assert service.devices_with_status()[0]["state"] == "preparing"
        service.tick(start)
        assert get_db().execute("SELECT status FROM rentals WHERE id=?", (rental,)).fetchone()[0] == "active"
        service.tick(start + 900)
        row = get_db().execute("SELECT * FROM rentals WHERE id=?", (rental,)).fetchone()
        assert row["status"] == "completed"
        assert row["access_secret"] == ""
        assert row["reason"] == "expired"


def test_return_not_available_until_cleanup(app):
    with app.app_context():
        rental = service.book(1, 1, 15)
        service.tick()
        service.finish(1, rental)
        with pytest.raises(service.RuleError):
            service.book(2, 1, 15)
        service.tick()
        assert service.devices_with_status()[0]["state"] == "available"
        service.book(2, 1, 15)


def test_schedule_and_back_to_back(app):
    with app.app_context():
        start = service.now() + 3600
        first = service.book(1, 1, 15, start)
        second = service.book(2, 1, 15, start + 900)
        service.tick(start - 1)
        assert get_db().execute("SELECT status FROM rentals WHERE id=?", (first,)).fetchone()[0] == "reserved"
        service.tick(start)
        service.tick(start + 900)
        rows = dict(get_db().execute("SELECT id,status FROM rentals"))
        assert rows[first] == "completed"
        assert rows[second] == "active"


def test_schedule_conflict_and_one_per_user(app):
    with app.app_context():
        start = service.now() + 3600
        service.book(1, 1, 60, start)
        with pytest.raises(service.RuleError, match="已被借用"):
            service.book(2, 1, 15, start + 900)
        with pytest.raises(service.RuleError, match="每個帳號"):
            service.book(1, 2, 15, start + 7200)


def test_concurrent_claim_only_one_wins(app):
    def claim(user):
        with app.app_context():
            try:
                return service.book(user, 1, 15)
            except service.RuleError:
                return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, [1, 2]))
    assert sum(result is not None for result in results) == 1


def test_same_user_concurrent_different_boards(app):
    def claim(device):
        with app.app_context():
            try:
                return service.book(1, device, 15)
            except service.RuleError:
                return None
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(claim, [1, 2]))
    assert sum(result is not None for result in results) == 1


@pytest.mark.parametrize("minutes", [0, 14, 16, 121, "bad"])
def test_duration_validation(app, minutes):
    with app.app_context(), pytest.raises(service.RuleError):
        service.book(1, 1, minutes)


def test_maintenance_and_booking_horizon(app):
    with app.app_context():
        # Test overnight maintenance as well as ordinary daytime maintenance.
        start = service.parse_start("2026-10-09T05:45")
        with pytest.raises(service.RuleError, match="維護"):
            service.book(1, 1, 30, start)
        app.config.update(MAINTENANCE_START="23:00", MAINTENANCE_END="01:00")
        assert service.maintenance_overlap(service.parse_start("2026-10-08T23:30"), service.parse_start("2026-10-09T00:30"))
        with pytest.raises(service.RuleError, match="14 天"):
            service.book(1, 1, 30, service.now() + 15 * 86400)


def test_worker_restart_cancels_missed_reservation(app):
    with app.app_context():
        start = service.now() + 60
        rental = service.book(1, 1, 15, start)
        service.tick(start + 901)
        row = get_db().execute("SELECT * FROM rentals WHERE id=?", (rental,)).fetchone()
        assert row["status"] == "cancelled"
        assert row["reason"] == "missed"


def test_failed_cleanup_quarantines_device(app):
    class Broken:
        def prepare(self, *_args):
            raise RuntimeError("offline")
        def release(self, *_args):
            raise RuntimeError("offline")
    with app.app_context():
        rental = service.book(1, 1, 15)
        service.tick(driver_factory=lambda _: Broken())
        assert get_db().execute("SELECT status FROM rentals WHERE id=?", (rental,)).fetchone()[0] == "failed"
        assert service.devices_with_status()[0]["state"] == "maintenance"
        with pytest.raises(service.RuleError, match="維護"):
            service.book(2, 1, 15)


def test_worker_recovers_stale_claim(app):
    with app.app_context():
        rental = service.book(1, 1, 15)
        get_db().execute("UPDATE rentals SET status='preparing',claimed_at=?,access_secret='test' WHERE id=?", (service.now(), rental))
        service.tick(service.now() + 179)
        assert get_db().execute("SELECT status FROM rentals WHERE id=?", (rental,)).fetchone()[0] == "preparing"
        service.tick(service.now() + 181)
        assert get_db().execute("SELECT status FROM rentals WHERE id=?", (rental,)).fetchone()[0] == "active"


def test_disabled_user_during_provision_does_not_get_access(app):
    class DisableOnPrepare:
        def prepare(self, device, rental):
            get_db().execute("UPDATE users SET enabled=0 WHERE id=?", (rental["user_id"],))
    with app.app_context():
        rental = service.book(1, 1, 15)
        service.tick(driver_factory=lambda _: DisableOnPrepare())
        assert get_db().execute("SELECT status FROM rentals WHERE id=?", (rental,)).fetchone()[0] == "releasing"


def test_real_hardware_disabled(app):
    with app.app_context():
        get_db().execute("UPDATE devices SET driver='pynq' WHERE id=1")
        with pytest.raises(service.RuleError, match="尚未啟用"):
            service.book(1, 1, 15)
