"""Real-browser flow against an isolated temporary DB; never touches deployment data."""
import os
import socket
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from playwright.sync_api import expect, sync_playwright


def main():
    root = Path(__file__).resolve().parent.parent
    output = root / "test-results"
    output.mkdir(exist_ok=True)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    with tempfile.TemporaryDirectory(prefix="onlinefpga-browser-") as temporary:
        env = {**os.environ, "DATABASE_PATH": str(Path(temporary) / "browser.sqlite3"),
               "SECRET_KEY": "browser-test-only-secret-" * 3, "ENABLE_HARDWARE": "false",
               "COOKIE_SECURE": "false", "TRUST_PROXY": "false", "REGISTRATION_OPEN": "true",
               "TIMEZONE": "Asia/Taipei", "MAINTENANCE_START": "00:00", "MAINTENANCE_END": "00:00",
               "SCHEDULER_INTERVAL": "1"}
        command = [sys.executable, "-m", "flask", "--app", "portal"]
        subprocess.run(command + ["init-db"], cwd=root, env=env, check=True, capture_output=True)
        processes = []
        with (output / "browser-server.log").open("w") as log:
            try:
                processes.append(subprocess.Popen([sys.executable, "-m", "gunicorn", "--workers", "2", "--bind",
                                                    f"127.0.0.1:{port}", "portal:create_app()"], cwd=root, env=env, stdout=log, stderr=log))
                processes.append(subprocess.Popen(command + ["scheduler"], cwd=root, env=env, stdout=log, stderr=log))
                for _ in range(100):
                    try:
                        with urlopen(base + "/healthz", timeout=1) as response:
                            if response.status == 200:
                                break
                    except OSError:
                        time.sleep(0.1)
                else:
                    raise RuntimeError("Isolated browser server did not start")

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch()
                    context = browser.new_context(viewport={"width": 1440, "height": 1000})
                    page = context.new_page()
                    errors = []
                    page.on("pageerror", lambda error: errors.append(str(error)))
                    page.goto(base)
                    expect(page.locator(".device-card")).to_have_count(3)
                    page.screenshot(path=str(output / "desktop.png"), full_page=True)
                    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
                    page.set_viewport_size({"width": 390, "height": 844})
                    page.screenshot(path=str(output / "mobile.png"), full_page=True)
                    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
                    page.set_viewport_size({"width": 1440, "height": 1000})

                    page.goto(base + "/register")
                    page.get_by_label("姓名", exact=True).fill("測試同學")
                    page.get_by_label("電子郵件", exact=True).fill("browser@example.com")
                    page.get_by_label("密碼", exact=True).fill("browser-password-1234")
                    page.get_by_label("確認密碼", exact=True).fill("browser-password-1234")
                    page.locator("input[name=terms]").check()
                    page.get_by_role("button", name="建立帳號").click()
                    expect(page).to_have_url(base + "/login")
                    page.get_by_label("電子郵件", exact=True).fill("browser@example.com")
                    page.get_by_label("密碼", exact=True).fill("browser-password-1234")
                    page.get_by_role("button", name="登入", exact=True).click()
                    expect(page).to_have_url(base + "/")

                    page.goto(base + "/devices/1")
                    page.locator("select[name=minutes]").select_option("15")
                    page.get_by_role("button", name="確認借用").click()
                    rental_url = page.url
                    expect(page.locator("[data-rental-state]")).to_have_text("使用中", timeout=20000)
                    expect(page.get_by_role("button", name="尚未連接 Jupyter")).to_be_disabled()
                    expect(page.locator("[data-countdown]")).to_contain_text(":")
                    assert "計算中" not in page.locator("[data-countdown]").inner_text()
                    page.on("dialog", lambda dialog: dialog.accept())
                    page.get_by_role("button", name="提前歸還").click()
                    expect(page.locator("[data-rental-state]")).to_have_text("已結束", timeout=20000)

                    page.goto(base + "/devices/2")
                    page.get_by_label("預約時段", exact=True).check()
                    expect(page.locator("input[name=starts_at]")).to_be_visible()
                    planned = (datetime.now(ZoneInfo("Asia/Taipei")) + timedelta(days=1)).replace(hour=10, minute=0, second=0, microsecond=0)
                    page.locator("input[name=starts_at]").fill(planned.strftime("%Y-%m-%dT%H:%M"))
                    page.get_by_role("button", name="確認預約").click()
                    expect(page.locator("[data-rental-state]")).to_have_text("已預約")
                    page.get_by_role("button", name="取消預約").click()
                    expect(page.locator("[data-rental-state]")).to_have_text("已取消")

                    subprocess.run(command + ["promote-admin", "browser@example.com"], cwd=root, env=env, check=True, capture_output=True)
                    page.goto(base + "/login")
                    page.get_by_label("電子郵件", exact=True).fill("browser@example.com")
                    page.get_by_label("密碼", exact=True).fill("browser-password-1234")
                    page.get_by_role("button", name="登入", exact=True).click()
                    page.goto(base + "/admin")
                    expect(page.get_by_role("heading", name="Makerspace 管理", exact=True)).to_be_visible()
                    expect(page.locator(".admin-device-grid article")).to_have_count(3)
                    page.set_viewport_size({"width": 390, "height": 844})
                    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
                    page.goto(base + "/account")
                    assert not page.evaluate("document.documentElement.scrollWidth > innerWidth")
                    page.get_by_label("目前密碼", exact=True).fill("browser-password-1234")
                    page.get_by_label("新密碼", exact=True).fill("changed-browser-password-123")
                    page.get_by_label("確認新密碼", exact=True).fill("changed-browser-password-123")
                    page.get_by_role("button", name="更新密碼").click()
                    expect(page).to_have_url(base + "/login")
                    assert not errors, errors
                    browser.close()
                print("PASS: desktop/mobile layout, registration, login, real scheduler activation, countdown, return, reservation, cancellation, admin, password change; no JavaScript errors.")
            finally:
                for process in reversed(processes):
                    process.terminate()
                for process in processes:
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()


if __name__ == "__main__":
    main()
