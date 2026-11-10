"""Run the GUI with ``python3 -m gui``."""

from __future__ import annotations

import subprocess
import sys


def _ensure_pyqt() -> bool:
    try:
        import PyQt5  # noqa: F401
        return True
    except ModuleNotFoundError:
        pass

    print("检测到当前环境尚未安装 PyQt5，正在自动安装依赖...", file=sys.stderr)
    try:
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install", "PyQt5>=5.15.0"],
            stdout=sys.stdout,
            stderr=sys.stderr,
        )
        print("PyQt5 依赖自动安装完成！", file=sys.stderr)
        return True
    except Exception as exc:
        print(
            f"自动安装依赖失败：{exc}\n"
            f"请手动运行：pip install PyQt5",
            file=sys.stderr,
        )
        return False


def main() -> int:
    if not _ensure_pyqt():
        return 1

    from .app import main as run_app

    return run_app()


if __name__ == "__main__":
    raise SystemExit(main())
