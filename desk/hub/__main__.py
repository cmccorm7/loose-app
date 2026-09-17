"""Launch the hub: start the local server and open it in a browser.

    python -m hub                 # from the desk/ directory
    python -m hub --port 8899 --no-browser

The server binds to 127.0.0.1 only. Nothing on your network can reach it.
"""

from __future__ import annotations

import argparse
import socket
import threading
import webbrowser

import uvicorn

from .app import create_app
from .config import paths as resolve_paths


def free_port(preferred: int, host: str = "127.0.0.1") -> int:
    """Use ``preferred`` if it is free, otherwise let the OS pick one."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind((host, preferred))
            return preferred
        except OSError:
            pass
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((host, 0))
        return probe.getsockname()[1]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="hub", description="YM Desk -- a local hub for trading statements."
    )
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--home", help="data directory (default ~/.ym-desk)")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--reload", action="store_true", help="for development")
    args = parser.parse_args(argv)

    port = free_port(args.port)
    where = resolve_paths(args.home)
    url = f"http://127.0.0.1:{port}"

    print(f"YM Desk\n  data  {where.home}\n  url   {url}\n")
    if port != args.port:
        print(f"  (port {args.port} was busy)\n")
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()

    uvicorn.run(
        create_app(home=args.home),
        host="127.0.0.1",
        port=port,
        log_level="warning",
        reload=args.reload,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
