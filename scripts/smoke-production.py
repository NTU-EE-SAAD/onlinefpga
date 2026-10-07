"""Validate the production Nginx template with isolated HTTPS, account and Notebook traffic."""
import argparse
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import requests
from flask import Flask, request
from flask_sock import Sock
from playwright.sync_api import sync_playwright, expect
from werkzeug.security import generate_password_hash
from werkzeug.serving import make_server
from portal import create_app, service
from portal.db import initialize, get_db


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1',0))
        return sock.getsockname()[1]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--nginx',required=True,type=Path)
    args=parser.parse_args()
    root=Path(__file__).resolve().parent.parent
    nginx=args.nginx.resolve()
    with tempfile.TemporaryDirectory(prefix='onlinefpga-production-') as folder:
        work=Path(folder)
        http_port,tls_port,app_port,board_port=[free_port() for _ in range(4)]
        certificate,key=work/'test.crt',work/'test.key'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes','-days','1',
                        '-subj','/CN=mks.ntuee.org','-addext','subjectAltName=DNS:mks.ntuee.org',
                        '-keyout',str(key),'-out',str(certificate)],check=True,capture_output=True)
        app=create_app({'DATABASE_PATH':str(work/'test.sqlite3'),'SECRET_KEY':secrets.token_hex(32),
                        'SESSION_COOKIE_SECURE':True,'ENABLE_HARDWARE':False,
                        'MAINTENANCE_START':'00:00','MAINTENANCE_END':'00:00'})
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app=ProxyFix(app.wsgi_app,x_for=1,x_proto=1,x_host=1)
        @app.get('/deployment-test/headers')
        def forwarded_headers():
            return {'ip':request.remote_addr,'scheme':request.scheme,'host':request.host}
        password=secrets.token_urlsafe(24)
        with app.app_context():
            initialize()
            conn=get_db()
            conn.execute('INSERT INTO users(id,email,name,password_hash,created_at) VALUES(?,?,?,?,?)',
                         (1,'production-test@example.com','部署測試',generate_password_hash(password),service.now()))
            conn.execute("UPDATE users SET student_id='B15901001' WHERE id=1")
            rental=service.book(1,1,15)
            service.tick()
            secret=conn.execute('SELECT access_secret FROM rentals WHERE id=?',(rental,)).fetchone()[0]
            conn.execute("UPDATE devices SET driver='pynq',jupyter_url=? WHERE id=1",(f'http://127.0.0.1:{board_port}',))
        board=Flask('mock-notebook')
        @board.get('/lab/<int:rental_id>/tree')
        def tree(rental_id):
            assert request.headers.get('Authorization')=='token '+secret
            assert 'session' not in request.cookies
            return '<h1>Notebook proxy ready</h1>'
        @Sock(board).route('/lab/<int:rental_id>/api/kernels/test/channels')
        def channels(ws,rental_id):
            assert request.headers.get('Authorization')=='token '+secret
            while True:
                message=ws.receive()
                if message is None:break
                ws.send(message)
        servers=[make_server('127.0.0.1',app_port,app,threaded=True),make_server('127.0.0.1',board_port,board,threaded=True)]
        for server in servers:
            threading.Thread(target=server.serve_forever,daemon=True).start()
        text=(root/'deploy/nginx-mks.conf').read_text()
        text=text.replace('listen 80;',f'listen 127.0.0.1:{http_port};').replace('listen [::]:80;','')
        text=text.replace('listen 443 ssl;',f'listen 127.0.0.1:{tls_port} ssl;').replace('listen [::]:443 ssl;','')
        text=text.replace('@CERTIFICATE@',str(certificate)).replace('@PRIVATE_KEY@',str(key))
        text=text.replace('/var/lib/onlinefpga-acme',str(work/'acme')).replace('127.0.0.1:8000',f'127.0.0.1:{app_port}')
        # Test TLS uses a high port; preserve it in forwarded Host/Origin checks.
        text=text.replace('proxy_set_header Host mks.ntuee.org;','proxy_set_header Host $http_host;')
        text=text.replace('proxy_set_header X-Forwarded-Host mks.ntuee.org;','proxy_set_header X-Forwarded-Host $http_host;')
        (work/'acme/.well-known/acme-challenge').mkdir(parents=True)
        (work/'acme/.well-known/acme-challenge/probe').write_text('acme-ok')
        wrapper=f'pid {work}/nginx.pid; error_log {work}/error.log; events {{}} http {{ access_log off; '
        for category in ('client_body','proxy','fastcgi','uwsgi','scgi'):
            wrapper+=f'{category}_temp_path {work}/{category}; '
        config=work/'nginx.conf';config.write_text(wrapper+text+'\n}')
        subprocess.run([str(nginx),'-p',str(work)+'/', '-c',str(config),'-t'],check=True,capture_output=True)
        process=subprocess.Popen([str(nginx),'-p',str(work)+'/', '-c',str(config),'-g','daemon off;'],stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
        try:
            client=requests.Session();client.trust_env=False
            for _ in range(50):
                try:
                    client.get(f'http://127.0.0.1:{http_port}/',headers={'Host':'mks.ntuee.org'},timeout=1,allow_redirects=False)
                    break
                except requests.ConnectionError:
                    if process.poll() is not None:
                        raise RuntimeError(process.stderr.read().decode())
                    time.sleep(.1)
            else:raise RuntimeError('Test Nginx did not become ready')
            response=client.get(f'http://127.0.0.1:{http_port}/',headers={'Host':'mks.ntuee.org'},allow_redirects=False)
            assert response.status_code==301 and response.headers['Location']=='https://mks.ntuee.org/'
            response=client.get(f'http://127.0.0.1:{http_port}/.well-known/acme-challenge/probe',headers={'Host':'mks.ntuee.org'})
            assert response.text=='acme-ok'
            base=f'https://mks.ntuee.org:{tls_port}'
            with sync_playwright() as p:
                browser=p.chromium.launch(args=['--host-resolver-rules=MAP mks.ntuee.org 127.0.0.1','--no-proxy-server'])
                context=browser.new_context(ignore_https_errors=True)
                page=context.new_page();errors=[];page.on('pageerror',lambda e:errors.append(str(e)))
                page.goto(base+'/login');page.get_by_label('學號',exact=True).fill('B15901001')
                page.get_by_label('密碼',exact=True).fill(password);page.get_by_role('button',name='登入',exact=True).click()
                expect(page).to_have_url(base+'/')
                assert next(c for c in context.cookies() if c['name']=='session')['secure']
                headers=page.evaluate('''async () => (await fetch('/deployment-test/headers', {
                    headers: {'CF-Connecting-IP':'198.51.100.5','X-Forwarded-For':'198.51.100.6'}
                })).json()''')
                assert headers=={'ip':'127.0.0.1','scheme':'https','host':f'mks.ntuee.org:{tls_port}'},headers
                page.goto(base+f'/lab/{rental}/tree')
                expect(page.get_by_role('heading',name='Notebook proxy ready')).to_be_visible()
                result=page.evaluate('''url => new Promise((resolve,reject) => {
                    const ws=new WebSocket(url); ws.onopen=()=>ws.send('kernel-through-nginx');
                    ws.onmessage=e=>{resolve(e.data);ws.close()}; ws.onerror=()=>reject('WebSocket failed');
                })''',base.replace('https://','wss://')+f'/lab/{rental}/api/kernels/test/channels')
                assert result=='kernel-through-nginx'
                assert not errors,errors
                browser.close()
            print('PASS: Nginx syntax, ACME route, HTTPS redirect, secure login, forwarded headers, Notebook HTTP and WebSocket.')
        finally:
            process.terminate();process.wait(timeout=10)
            for server in servers:server.shutdown();server.server_close()


if __name__=='__main__':main()
