"""Bounded callback server. Query strings and codes are never logged."""
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlsplit, parse_qs
import threading


def start_callback(runtime, port, portal=False):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def setup(self):
            super().setup()
            self.connection.settimeout(3)

        def do_GET(self):
            try:
                url = urlsplit(self.path)
                path = "/oauth/cognito/callback" if portal else "/oauth/agentcore/callback"
                if url.path != path or len(self.path) > 8192:
                    raise ValueError()
                query = parse_qs(url.query, max_num_fields=8)
                expected = {"code", "state"} if portal else {"session_id", "state"}
                if set(query) != expected or any(len(v) != 1 for v in query.values()):
                    raise ValueError()
                if portal:
                    runtime.callback(query["state"][0], query["code"][0])
                    body = b"Login received. Return to Slack app Home, click Refresh and confirm your account."
                else:
                    code = runtime.callback(query["session_id"][0], query["state"][0])
                    body = ("Authorization ready. Return to the app Home, select Enter code, "
                            "and submit this one-time code within ten minutes:\n\n" + code).encode()
                status = 200
            except Exception:
                status, body = 400, b"Authorization invalid or expired. Start again from the app Home."
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

    server = HTTPServer(("127.0.0.1", port), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server
