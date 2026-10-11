"""
命令行入口。

    python -m trajectory_inspector parse  <trajectory.jsonl>
    python -m trajectory_inspector check  <trajectory.jsonl> [--fail-on error]
    python -m trajectory_inspector diff   <base.jsonl> <candidate.jsonl>
    python -m trajectory_inspector report <trajectory.jsonl> --format markdown --output report.md

设计约定：check 命令在发现达到阈值的异常时，**退出码为 1**。
这一条约定了 CI 门禁能不能工作的全部 —— GitHub Actions 靠退出码判断成败。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .adapters import load_trajectory
from .config import RulesConfig, load_rules
from .diff import diff_trajectories
from .render import render_anomalies, render_diff, render_markdown, render_tool_stats, render_trajectory
from .rules import run_rules

EXIT_OK = 0
EXIT_BLOCKED = 1
EXIT_INPUT_ERROR = 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trajectory-inspector",
        description="Agent 轨迹检查器：把『哪一步不对』变成可自动检测的结论。",
    )
    parser.add_argument("--rules", metavar="PATH",
                        help="规则配置文件路径（默认 config/rules.yaml，不存在则用内置默认值）")
    sub = parser.add_subparsers(dest="command", required=True)

    p_parse = sub.add_parser("parse", help="解析并打印轨迹时间线")
    p_parse.add_argument("trajectory")
    p_parse.add_argument("--stats", action="store_true", help="额外打印工具调用次数统计")
    p_parse.add_argument("--json", action="store_true", help="输出 JSON 而不是表格")

    p_check = sub.add_parser("check", help="检测异常（CI 里用这个）")
    p_check.add_argument("trajectory")
    p_check.add_argument("--fail-on", default="error",
                         choices=["info", "warn", "error", "critical"],
                         help="达到这个级别就返回退出码 1（默认 error）")
    p_check.add_argument("--json", action="store_true", help="输出 JSON")
    p_check.add_argument("--markdown", action="store_true", help="输出 Markdown（适合贴 PR 评论）")

    p_diff = sub.add_parser("diff", help="对比两条轨迹，找出第一次分歧")
    p_diff.add_argument("base")
    p_diff.add_argument("candidate")
    p_diff.add_argument("--json", action="store_true", help="输出 JSON")

    p_report = sub.add_parser("report", help="生成报告文件")
    p_report.add_argument("trajectory")
    p_report.add_argument("--format", default="markdown", choices=["markdown", "json"])
    p_report.add_argument("--output", "-o", help="写到文件（不指定则打印到终端）")

    return parser


def _load(path: str) -> tuple:
    """统一的加载入口，把"文件不存在"和"格式不认识"翻译成友好提示。"""
    try:
        return load_trajectory(path)
    except FileNotFoundError:
        print(f"错误：找不到文件 {path}", file=sys.stderr)
        raise SystemExit(EXIT_INPUT_ERROR)
    except ValueError as error:
        print(f"错误：{error}", file=sys.stderr)
        raise SystemExit(EXIT_INPUT_ERROR)


def _print_warnings(warnings: list[str]) -> None:
    if warnings:
        print(f"（注意：有 {len(warnings)} 行坏数据被跳过，未中断解析）", file=sys.stderr)


def command_parse(args, rules: RulesConfig) -> int:
    trajectory, warnings = _load(args.trajectory)
    _print_warnings(warnings)
    if args.json:
        print(json.dumps(trajectory.to_dict(), ensure_ascii=False, indent=2))
        return EXIT_OK
    print(render_trajectory(trajectory))
    if args.stats:
        print()
        print("工具调用统计：")
        print(render_tool_stats(trajectory))
    return EXIT_OK


def command_check(args, rules: RulesConfig) -> int:
    trajectory, warnings = _load(args.trajectory)
    _print_warnings(warnings)
    report = run_rules(trajectory, rules)

    if args.json:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    elif args.markdown:
        print(render_markdown(report))
    else:
        print(render_anomalies(report))

    if report.has_blocking_issue(args.fail_on):
        if not (args.json or args.markdown):
            print()
            print(f"退出码 1（因为存在 >= {args.fail_on} 级别的异常，CI 会判定失败）")
        return EXIT_BLOCKED
    return EXIT_OK


def command_diff(args, rules: RulesConfig) -> int:
    base, base_warnings = _load(args.base)
    candidate, candidate_warnings = _load(args.candidate)
    _print_warnings(base_warnings + candidate_warnings)

    result = diff_trajectories(base, candidate)
    if args.json:
        print(json.dumps(result.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(render_diff(result))
    # 注意：diff 命令**不**用退出码表达"有分歧"。
    # 有分歧是正常现象（你改了 Prompt 当然会有分歧），只有检查异常才该阻断。
    return EXIT_OK


def command_report(args, rules: RulesConfig) -> int:
    trajectory, warnings = _load(args.trajectory)
    report = run_rules(trajectory, rules)
    body = (json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
            if args.format == "json" else render_markdown(report))

    if args.output:
        Path(args.output).write_text(body + "\n", encoding="utf-8")
        print(f"报告已写入 {args.output}")
    else:
        print(body)

    if warnings:
        _print_warnings(warnings)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    rules = load_rules(args.rules)

    handlers = {
        "parse": command_parse,
        "check": command_check,
        "diff": command_diff,
        "report": command_report,
    }
    return handlers[args.command](args, rules)


if __name__ == "__main__":
    raise SystemExit(main())
