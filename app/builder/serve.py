"""Serve built projects so Stephanie can view them on her phone. Step 1: static files
over the LAN. (Vite dev serving = step 2; cloudflared tunnel = step 3.)
"""

import functools
import http.server
import socket
import socketserver
import threading
from pathlib import Path

from app.config import settings

# name -> (server, thread, port, url) for the servers we've started, so they can be listed/stopped.
_servers: dict[str, tuple] = {}


class _ReusableServer(socketserver.TCPServer):
    allow_reuse_address = True


def lan_ip() -> str:
    """This machine's LAN IP (so a phone on the same wifi can reach it)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def _free_port(base: int) -> int:
    for port in range(base, base + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:  # nothing listening → free
                return port
    return base


def serve_static(project_dir: Path, *, name: str | None = None) -> str:
    """Serve a directory of static files on the LAN; returns the URL. Re-serving a name
    replaces the old server."""
    name = name or project_dir.name
    stop_server(name)
    port = _free_port(settings.builder_port_base)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(project_dir))
    httpd = _ReusableServer(("0.0.0.0", port), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    url = f"http://{lan_ip()}:{port}"
    _servers[name] = (httpd, thread, port, url)
    return url


def stop_server(name: str) -> None:
    entry = _servers.pop(name, None)
    if entry:
        server = entry[0]
        server.shutdown()
        server.server_close()


def running() -> dict[str, str]:
    return {name: entry[3] for name, entry in _servers.items()}
