from .conftest import sign_in
from portal import service


def test_public_monochrome_portal(client):
    page = client.get('/').get_data(as_text=True)
    assert 'FPGA Lab' in page
    assert '電機系遠端實驗室' not in page
    assert 'board-illustration' not in page
    assert 'type="search"' not in page
    assert 'data-theme-toggle' in page
    assert 'theme.js' in page
    assert '指南與須知' in page
    assert '<dialog' not in page


def test_authenticated_booking_dialogs(client):
    sign_in(client)
    page = client.get('/').get_data(as_text=True)
    assert '<dialog' in page
    assert 'data-open-booking' in page
    assert 'data-scheduled-time hidden' in page
    assert 'test-csrf' in page
    assert 'name="minutes"' in page


def test_guide_and_rules_share_page(client):
    page = client.get('/guide').get_data(as_text=True)
    assert '<h1>使用指南與借用須知</h1>' in page
    assert 'id="rules"' in page
    assert 'id="workspace"' in page


def test_existing_booking_disables_home_submit(app, client):
    sign_in(client)
    with app.app_context():
        service.book(1, 1, 15)
    page = client.get('/').get_data(as_text=True)
    assert '你已有一筆借用或預約' in page
    assert 'type="submit" disabled' in page


def test_device_uses_same_booking_controls(client):
    sign_in(client)
    page = client.get('/devices/1').get_data(as_text=True)
    assert 'data-booking-form' in page
    assert 'data-scheduled-time hidden' in page
    assert '預約結束時間不因準備延遲而延長' in page
