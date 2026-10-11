"""让 `python -m trajectory_inspector` 可以运行。"""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
