"""支持通过 python -m seekra 启动同一个命令行入口。"""

from .terminal.cli import main


if __name__ == "__main__":
    raise SystemExit(main())
