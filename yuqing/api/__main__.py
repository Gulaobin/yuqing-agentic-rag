"""启动接口与聊天页：python -m yuqing.api [--port 8000]，浏览器打开 http://127.0.0.1:8000"""

from __future__ import annotations

import argparse


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m yuqing.api")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    import uvicorn

    uvicorn.run("yuqing.api.app:app", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
