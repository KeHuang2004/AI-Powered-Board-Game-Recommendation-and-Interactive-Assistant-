import argparse
import os
import sys
import time
import webbrowser
from pathlib import Path
from threading import Thread

local_runtime = Path(__file__).resolve().parent / ".runtime"
if os.name == "nt" and sys.version_info[:2] == (3, 14) and local_runtime.is_dir():
    sys.path.insert(0, str(local_runtime))

try:
    import fastapi  # noqa: F401 - check both startup dependencies
    import uvicorn
except ModuleNotFoundError as error:
    raise SystemExit("请先运行 python -m pip install -r requirements.txt") from error

ROOT = Path(__file__).resolve().parent


def open_when_ready(server: uvicorn.Server, host: str, port: int) -> None:
    while not server.started:
        if server.should_exit:
            return
        time.sleep(0.05)
    browser_host = "127.0.0.1" if host in {"0.0.0.0", "::"} else host
    if ":" in browser_host:
        browser_host = f"[{browser_host}]"
    webbrowser.open(f"http://{browser_host}:{port}/")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="启动玩局网页与 API")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认仅本机")
    parser.add_argument("--port", default=8001, type=int, help="端口，默认 8001")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535:
        parser.error("端口必须在 1–65535 之间")
    if not (ROOT / "data/database/boardgames.sqlite3").is_file():
        raise SystemExit("缺少游戏目录库，请在项目目录运行 python -m backend.build_catalog")
    server = uvicorn.Server(uvicorn.Config("backend.server:app", host=args.host, port=args.port))
    if not args.no_browser:
        Thread(target=open_when_ready, args=(server, args.host, args.port), daemon=True).start()
    server.run()


if __name__ == "__main__":
    main()
