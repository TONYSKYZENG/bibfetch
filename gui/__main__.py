"""Run the GUI with ``python3 -m gui``."""

from __future__ import annotations

import sys


def main() -> int:
    try:
        import tkinter  # noqa: F401
    except ModuleNotFoundError as exc:
        print(
            "无法启动 GUI：当前 Python 缺少 tkinter。\n"
            "macOS Homebrew 可运行 `brew install python-tk@3.14`，"
            "或改用自带 tkinter 的 python.org 版本。",
            file=sys.stderr,
        )
        return 1
    from .app import main as run_app

    return run_app()


if __name__ == "__main__":
    raise SystemExit(main())
