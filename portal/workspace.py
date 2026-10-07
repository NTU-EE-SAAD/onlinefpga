"""Rental-gated Notebook HTTP and WebSocket proxy; board tokens stay on the server."""
import queue
import threading
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import requests
import websocket
from flask import abort, g, request, Response
from flask_sock import Sock
from simple_websocket import ConnectionClosed
from werkzeug.exceptions import HTTPException

from .db import get_db
from . import service


def rental_access(rental_id):
    if not g.user:
        abort(401)
    row = get_db().execute(
        "SELECT r.*,d.driver,d.jupyter_url,u.enabled,u.session_version "
        "FROM rentals r JOIN devices d ON d.id=r.device_id JOIN users u ON u.id=r.user_id "
        "WHERE r.id=? AND r.user_id=?", (rental_id, g.user["id"])).fetchone()
    if not row:
        abort(404)
    if (row["status"] != "active" or row["ends_at"] <= service.now()
            or not row["enabled"] or row["session_version"] != g.user["session_version"]
            or row["driver"] != "pynq" or not row["access_secret"]):
        abort(403)
    return row


def check_origin(required=False):
    origin = request.headers.get("Origin")
    if (required and not origin) or (origin and origin != request.host_url.rstrip("/")):
        abort(403)


def upstream_url(row, path, socket=False):
    if any(part in (".", "..") for part in path.split("/")) or "\\" in path:
        abort(400)
    base = urlsplit(row["jupyter_url"])
    if base.scheme not in ("http", "https") or not base.hostname or base.username or base.query or base.fragment:
        abort(503)
    scheme = ("wss" if base.scheme == "https" else "ws") if socket else base.scheme
    query = urlencode([(k, v) for k, v in request.args.items(multi=True) if k != "token"])
    return urlunsplit((scheme, base.netloc, f"/lab/{row['id']}/" + quote(path, safe="/"), query, ""))


def init_app(app):
    @app.route("/lab/<int:rental_id>/", defaults={"path": ""}, methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
    @app.route("/lab/<int:rental_id>/<path:path>", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"])
    def notebook_http(rental_id, path):
        row = rental_access(rental_id)
        check_origin(required=request.method not in ("GET", "HEAD"))
        headers = {"Authorization": "token " + row["access_secret"], "Accept-Encoding": "identity"}
        for name in ("Content-Type", "Accept", "Range", "If-None-Match"):
            if name in request.headers:
                headers[name] = request.headers[name]
        try:
            # Never forward the portal session cookie or follow board redirects.
            with requests.Session() as client:
                client.trust_env = False
                result = client.request(request.method, upstream_url(row, path), headers=headers,
                                        data=request.get_data(), timeout=(5, 45), allow_redirects=False)
        except requests.RequestException:
            abort(502, description="工作空間暫時無法連線，請稍後重試。")
        # Recheck after slow requests; returned rentals must not deliver further data.
        rental_access(rental_id)
        content = result.content
        if any(kind in result.headers.get("Content-Type", "") for kind in ("text/", "json", "javascript")):
            # Notebook 6 embeds its configured token in HTML data attributes and links.
            # Browser requests authenticate to this proxy with the website session instead.
            content = content.replace(row["access_secret"].encode(), b"")
        response = Response(content, status=result.status_code)
        for name in ("Content-Type", "Content-Disposition", "Content-Range", "Accept-Ranges", "ETag"):
            if name in result.headers:
                response.headers[name] = result.headers[name]
        if "Location" in result.headers:
            location = urlsplit(result.headers["Location"])
            if location.path.startswith(f"/lab/{rental_id}/"):
                query = urlencode([(k, v) for k, v in parse_qsl(location.query) if k != "token"])
                response.headers["Location"] = urlunsplit(("", "", location.path, query, ""))
            else:
                abort(502)
        return response

    sock = Sock(app)

    @sock.route("/lab/<int:rental_id>/<path:path>")
    def notebook_socket(ws, rental_id, path):
        upstream = None
        try:
            row = rental_access(rental_id)
            check_origin(required=True)
            upstream = websocket.create_connection(
                upstream_url(row, path, socket=True), timeout=5,
                header=["Authorization: token " + row["access_secret"]],
                suppress_origin=True, http_no_proxy=["*"], enable_multithread=True)
            upstream.settimeout(1)
            messages = queue.Queue(maxsize=64)
            stop = threading.Event()

            def receive_board():
                try:
                    while not stop.is_set():
                        try:
                            opcode, data = upstream.recv_data()
                        except websocket.WebSocketTimeoutException:
                            continue
                        if opcode == websocket.ABNF.OPCODE_CLOSE:
                            break
                        messages.put(data if opcode == websocket.ABNF.OPCODE_BINARY else data.decode("utf-8"), timeout=2)
                except (websocket.WebSocketException, OSError, queue.Full):
                    pass
                finally:
                    stop.set()

            reader = threading.Thread(target=receive_board, daemon=True)
            reader.start()
            try:
                while not stop.is_set():
                    rental_access(rental_id)  # Includes expiry, return, disabled accounts and session revocation.
                    while not messages.empty():
                        ws.send(messages.get_nowait())
                    message = ws.receive(timeout=0.2)
                    if message is not None:
                        upstream.send(message, opcode=websocket.ABNF.OPCODE_BINARY if isinstance(message, bytes) else websocket.ABNF.OPCODE_TEXT)
            finally:
                stop.set()
                upstream.close()
                reader.join(timeout=2)
        except (ConnectionClosed, websocket.WebSocketException, OSError, HTTPException):
            pass
        finally:
            if upstream is not None:
                upstream.close()
            ws.close()
