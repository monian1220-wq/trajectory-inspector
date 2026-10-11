#!/usr/bin/env python3
"""
推送到 GitHub 之前运行一次：把仓库里的 YOUR_USERNAME 占位符换成你的真实用户名。

用法：

    python setup.py 你的GitHub用户名

为什么需要它：README、CONTRIBUTING 和 issue 模板里有几处链接要带上用户名
（CI 徽章、clone 地址、Discussion 链接）。手工改容易漏，漏掉的地方在
GitHub 上会显示成坏掉的徽章或 404 链接。

为什么用 Python 而不是 PowerShell 脚本？
    因为 Windows 默认的 PowerShell 执行策略会拒绝运行本地 .ps1 文件
    （报 "在此系统上禁止运行脚本"），而 Python 没有这个限制。

可选参数：
    --check    只检查还有没有占位符，不做修改（CI 或推送前自查用）
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

#: 要替换的占位符。写成列表是因为历史上出现过两种写法 ——
#: 统一成英文的一个是更好的做法，但脚本要能容忍旧的残留。
PLACEHOLDERS = ["YOUR_USERNAME", "你的用户名"]

TARGETS = [
    "README.md",
    "CONTRIBUTING.md",
    ".github/ISSUE_TEMPLATE/config.yml",
    ".github/ISSUE_TEMPLATE/bug_report.yml",
    ".github/ISSUE_TEMPLATE/false_positive.yml",
]

# GitHub 用户名的规则：字母数字和连字符，不能以连字符开头或结尾，最长 39 字符
USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9]|-(?=[A-Za-z0-9])){0,38}$")


def count_placeholders() -> list[tuple[Path, int]]:
    found: list[tuple[Path, int]] = []
    for relative in TARGETS:
        path = ROOT / relative
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        occurrences = sum(text.count(marker) for marker in PLACEHOLDERS)
        if occurrences:
            found.append((path, occurrences))
    return found


def replace_all(username: str) -> int:
    changed = 0
    for relative in TARGETS:
        path = ROOT / relative
        if not path.exists():
            print(f"  跳过（文件不存在）：{relative}")
            continue

        text = path.read_text(encoding="utf-8")
        occurrences = sum(text.count(marker) for marker in PLACEHOLDERS)
        if not occurrences:
            print(f"  无需改动：{relative}")
            continue

        updated = text
        for marker in PLACEHOLDERS:
            updated = updated.replace(marker, username)
        # 保持 LF 行尾和末尾换行 —— 别让这次替换制造出一大堆无意义的 diff
        updated = updated.replace("\r\n", "\n")
        if not updated.endswith("\n"):
            updated += "\n"

        path.write_text(updated, encoding="utf-8", newline="\n")
        print(f"  已更新：{relative}（{occurrences} 处）")
        changed += 1
    return changed


def main(argv: list[str]) -> int:
    if "--check" in argv:
        remaining = count_placeholders()
        if not remaining:
            print("没有残留的 YOUR_USERNAME 占位符。")
            return 0
        print("发现残留的占位符（这些链接在 GitHub 上会是坏的）：")
        for path, occurrences in remaining:
            print(f"  {path.relative_to(ROOT)}  {occurrences} 处")
        print("\n运行 `python setup.py 你的GitHub用户名` 来替换。")
        return 1

    args = [a for a in argv if not a.startswith("-")]
    if len(args) != 1:
        print(__doc__)
        return 2

    username = args[0]
    if not USERNAME_PATTERN.match(username):
        print(f"错误：{username!r} 不像一个合法的 GitHub 用户名。")
        print("  规则：只能用字母、数字和连字符，不能以连字符开头或结尾，最长 39 个字符。")
        return 2

    print(f"把占位符替换为：{username}\n")
    changed = replace_all(username)

    print()
    if changed:
        print(f"完成，共修改 {changed} 个文件。")
        print("建议接着执行：")
        print("  git add -A")
        print('  git commit -m "chore: 替换为真实仓库地址"')
    else:
        print("没有需要替换的占位符（可能已经设置过了）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
