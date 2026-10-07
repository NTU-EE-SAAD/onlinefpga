"""Opt-in live-board acceptance with isolated accounts/DB; requires boards under maintenance."""
import argparse
import json
import os
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.request import urlopen

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from playwright.sync_api import expect, sync_playwright
import websocket
from werkzeug.security import generate_password_hash
from portal import create_app, service
from portal.db import get_db, initialize
from portal.hardware import PynqDriver


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--allow-board-control', action='store_true', help='Restart real board Notebook services')
    args = parser.parse_args()
    if not args.allow_board_control:
        parser.error('This test controls physical boards; specify --allow-board-control after putting them under maintenance.')
    root = Path(__file__).resolve().parent.parent
    original = create_app()
    with original.app_context():
        devices = [dict(d) for d in get_db().execute("SELECT * FROM devices WHERE driver='pynq' ORDER BY id")]
        assert devices and all(d['maintenance'] for d in devices), 'Put all physical boards under maintenance first'
        assert not get_db().execute("SELECT 1 FROM rentals WHERE status IN ('preparing','active','releasing')").fetchone()
    password = secrets.token_urlsafe(24)
    output = root / 'test-results'
    output.mkdir(exist_ok=True)
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        port = listener.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    with tempfile.TemporaryDirectory(prefix='onlinefpga-hardware-') as temporary:
        database = str(Path(temporary) / 'acceptance.sqlite3')
        env = {**os.environ, 'DATABASE_PATH': database, 'SECRET_KEY': secrets.token_hex(32),
               'ENABLE_HARDWARE': 'true', 'SCHEDULER_INTERVAL': '1', 'COOKIE_SECURE': 'false',
               'MAINTENANCE_START': '00:00', 'MAINTENANCE_END': '00:00'}
        app = create_app({'DATABASE_PATH': database})
        with app.app_context():
            initialize()
            conn = get_db()
            conn.execute('DELETE FROM devices')
            for device in devices:
                values = {**device, 'maintenance': 0}
                conn.execute('INSERT INTO devices (' + ','.join(values) + ') VALUES (' + ','.join('?' for _ in values) + ')', tuple(values.values()))
            conn.execute('INSERT INTO users(id,email,name,password_hash,created_at) VALUES(?,?,?,?,?)',
                         (900000001, 'hardware-test@example.com', '設備驗收', generate_password_hash(password), int(time.time())))
        processes = []
        with (output / 'hardware-server.log').open('w') as log:
            try:
                processes.append(subprocess.Popen([sys.executable, '-m', 'gunicorn', '--workers', '2', '--threads', '16',
                                                   '--worker-class', 'gthread', '--bind', f'127.0.0.1:{port}', 'portal:create_app()'],
                                                  cwd=root, env=env, stdout=log, stderr=log))
                processes.append(subprocess.Popen([sys.executable, '-m', 'flask', '--app', 'portal', 'scheduler'],
                                                  cwd=root, env=env, stdout=log, stderr=log))
                for _ in range(100):
                    try:
                        with urlopen(base + '/healthz', timeout=1):
                            break
                    except OSError:
                        time.sleep(.1)
                else:
                    raise RuntimeError('Test server did not start')
                with sync_playwright() as p:
                    browser = p.chromium.launch()
                    context = browser.new_context(viewport={'width':1440,'height':1000})
                    page = context.new_page()
                    page.goto(base+'/login')
                    page.get_by_label('電子郵件', exact=True).fill('hardware-test@example.com')
                    page.get_by_label('密碼', exact=True).fill(password)
                    page.get_by_role('button',name='登入',exact=True).click()
                    expect(page).to_have_url(base+'/')
                    page.on('dialog',lambda d:d.accept())
                    page.screenshot(path=str(output/'real-inventory.png'),full_page=True)
                    for index, device in enumerate(devices):
                        page.goto(base+f"/devices/{device['id']}")
                        page.locator('select[name=minutes]').select_option('15')
                        page.get_by_role('button',name='確認借用').click()
                        expect(page.locator('[data-rental-state]')).to_have_text('使用中', timeout=150000)
                        rental_url = page.url
                        rental_id = int(rental_url.rsplit('/',1)[1])
                        prefix = base+f'/lab/{rental_id}/'
                        workspace = context.new_page()
                        errors=[]
                        workspace.on('pageerror',lambda e:errors.append(e.stack))
                        assert workspace.goto(prefix+'tree').status==200
                        expect(workspace.locator('#new-dropdown-button')).to_be_visible(timeout=30000)
                        headers={'Origin':base}
                        response=context.request.post(prefix+'api/contents',data={'type':'notebook'},headers=headers)
                        assert response.ok, response.text()
                        name=response.json()['path']
                        notebook={'cells':[{'cell_type':'code','execution_count':None,'metadata':{},'outputs':[],
                                           'source':"import socket, pynq\nprint('BOARD:', socket.gethostname())\nprint('PYNQ:', pynq.__version__)\nprint('DEVICES:', [type(d).__name__ for d in pynq.Device.devices])"}],
                                  'metadata':{'kernelspec':{'display_name':'Python 3','language':'python','name':'python3'}},
                                  'nbformat':4,'nbformat_minor':4}
                        response=context.request.put(prefix+'api/contents/'+name,data={'type':'notebook','format':'json','content':notebook},headers=headers)
                        assert response.ok, response.text()
                        workspace.goto(prefix+'notebooks/'+name)
                        expect(workspace.locator('.CodeMirror')).to_contain_text('import socket', timeout=30000)
                        workspace.wait_for_function('window.Jupyter && Jupyter.notebook && Jupyter.notebook.kernel && Jupyter.notebook.kernel.is_connected()', timeout=60000)
                        workspace.screenshot(path=str(output/(device['slug']+'-before-execute.png')),full_page=True)
                        print(device['model']+' browser kernel connected',flush=True)
                        workspace.evaluate('Jupyter.notebook.execute_all_cells()')
                        expect(workspace.locator('.output')).to_contain_text('DEVICES:',timeout=120000)
                        result=workspace.locator('.output').inner_text()
                        assert 'PYNQ: 2.7.0' in result, result
                        assert 'DEVICES: []' not in result, result
                        print(device['model']+' kernel result:\n'+result,flush=True)
                        assert not errors, errors
                        workspace.screenshot(path=str(output/(device['slug']+'-notebook.png')),full_page=True)
                        kernels=context.request.get(prefix+'api/kernels').json()
                        assert kernels
                        cookies='; '.join(c['name']+'='+c['value'] for c in context.cookies(base))
                        ws=websocket.create_connection(prefix.replace('http://','ws://')+'api/kernels/'+kernels[0]['id']+'/channels?session_id='+secrets.token_hex(16),
                                                        origin=base,cookie=cookies,timeout=5,http_no_proxy=['*'])
                        assert context.request.get(prefix+'api').ok
                        assert context.request.delete(prefix+'api/contents/'+name,headers=headers).ok
                        if index==0:
                            page.get_by_role('button',name='提前歸還').click()
                        else:
                            with sqlite3.connect(database) as conn:
                                conn.execute('UPDATE rentals SET ends_at=? WHERE id=?',(int(time.time())-1,rental_id))
                        assert context.request.get(prefix+'api').status==403
                        deadline=time.monotonic()+6
                        closed=False
                        while time.monotonic()<deadline:
                            try:
                                if ws.recv()=='':
                                    closed=True;break
                            except websocket.WebSocketConnectionClosedException:
                                closed=True;break
                            except websocket.WebSocketTimeoutException:
                                pass
                        ws.close();assert closed,'WebSocket survived return/expiry'
                        page.goto(rental_url)
                        expect(page.locator('[data-rental-state]')).to_have_text('已結束',timeout=60000)
                        workspace.close()
                        print(device['model']+' return/expiry and WebSocket revocation passed',flush=True)
                    browser.close()
            finally:
                for process in processes:
                    process.terminate()
                for process in processes:
                    try: process.wait(timeout=10)
                    except subprocess.TimeoutExpired: process.kill();process.wait()
                with app.app_context():
                    for device in devices:
                        row=get_db().execute('SELECT * FROM rentals WHERE device_id=? ORDER BY id DESC LIMIT 1',(device['id'],)).fetchone()
                        if row and row['status']!='completed':
                            PynqDriver().release(device,dict(row))
    print('All live-board acceptance checks passed.',flush=True)


if __name__=='__main__':
    main()
