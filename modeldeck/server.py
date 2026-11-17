import argparse
import json
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast

from .guard import GuardError, SwapGuard, execute_gated, jev_gate
from .state import Store

STATIC = Path(__file__).with_name('static')


def make_server(store, guard, port=8866):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args) -> None:
            pass

        def allowed_host(self):
            port = cast(ThreadingHTTPServer, self.server).server_port
            hosts = {'127.0.0.1:' + str(port), 'localhost:' + str(port)}
            return self.headers.get('Host') in hosts

        def send(self, status, body, content_type='application/json'):
            raw = body if isinstance(body, bytes) else json.dumps(body, allow_nan=False).encode()
            self.send_response(status)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
            self.end_headers()
            try:
                self.wfile.write(raw)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            if not self.allowed_host():
                return self.send(403, {'error': 'Foreign Host rejected'})
            path = self.path.split('?', 1)[0]
            if path == '/api/state':
                return self.send(200, store.snapshot())
            names = {'/': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css', '/icon.svg': 'icon.svg'}
            if path not in names:
                return self.send(404, {'error': 'Not found'})
            file = STATIC / names[path]
            return self.send(200, file.read_bytes(), mimetypes.guess_type(file)[0] or 'application/octet-stream')

        def do_POST(self):
            if not self.allowed_host():
                return self.send(403, {'error': 'Foreign Host rejected'})
            origin = 'http://' + self.headers.get('Host', '')
            if self.headers.get('Origin') != origin or self.headers.get('X-ModelDeck-CSRF') != store.csrf:
                return self.send(403, {'error': 'Same-origin request and CSRF token required'})
            if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                return self.send(415, {'error': 'JSON required'})
            try:
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= 4096:
                    return self.send(413, {'error': 'Invalid request size'})
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise TypeError('Object required')
            except (ValueError, TypeError):
                return self.send(400, {'error': 'Request must be a bounded JSON object with a valid Content-Length.'})
            try:
                if not guard:
                    return self.send(503, {'error': 'Swap guard unavailable'})
                if self.path == '/api/swap/prepare':
                    return self.send(200, guard.prepare(data.get('profile')))
                if self.path == '/api/swap/commit':
                    return self.send(202, guard.commit(data.get('ticket'), data.get('confirmation')))
                return self.send(404, {'error': 'Not found'})
            except GuardError as exc:
                return self.send(409, {'error': str(exc)})

    return ThreadingHTTPServer(('127.0.0.1', port), Handler)


def main():
    parser = argparse.ArgumentParser(description='Loopback-only fleet model monitor')
    parser.add_argument('--port', type=int, default=8866)
    parser.add_argument('--data-dir', type=Path)
    args = parser.parse_args()
    store = Store(args.data_dir)
    guard = SwapGuard(store.swap_state, execute_gated, jev_gate, store.directory / 'pending-swap.json')
    server = make_server(store, guard, args.port)
    collector = threading.Thread(target=store.loop, daemon=True)
    collector.start()
    print('ModelDeck listening on http://127.0.0.1:' + str(server.server_port), flush=True)
    try:
        server.serve_forever()
    finally:
        store.stop.set()
        server.server_close()


if __name__ == '__main__':
    main()
