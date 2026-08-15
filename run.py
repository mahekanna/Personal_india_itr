#!/usr/bin/env python3
"""Start the local web application.

    python run.py            # http://127.0.0.1:8000
    python run.py --port 9000 --reload

It binds to localhost only. Your Form 16 has no business being reachable from
the network.
"""

from __future__ import annotations

import argparse
import webbrowser


def main() -> None:
    parser = argparse.ArgumentParser(description="Personal India ITR")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--reload", action="store_true",
                        help="Reload on code changes, for development.")
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    import uvicorn

    url = f"http://{args.host}:{args.port}"
    print(f"\n  Personal India ITR is starting at {url}")
    print("  Everything stays on this machine. Press Ctrl+C to stop.\n")

    if not args.no_browser and not args.reload:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - a headless box has no browser
            pass

    uvicorn.run(
        "app.main:app", host=args.host, port=args.port, reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
