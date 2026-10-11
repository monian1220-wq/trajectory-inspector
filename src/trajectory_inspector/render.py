"""渲染层：把结构化结果变成人能看的东西。"""

from __future__ import annotations

from .models import AnomalyReport, Trajectory, TrajectoryDiff

# 严重级别的显示标记。故意用纯 ASCII：
# 中文和 emoji 在终端里算"两个字符宽"，而 Python 的格式化宽度按"一个字符"算，
# 混用会导致表格错位。想用中文标记的话得引入 wcwidth 这类库。
SEVERITY_TAG = {
    "critical": "[CRIT]",
    "error": "[ERR ]",
    "warn": "[WARN]",
    "info": "[INFO]",
}


def _column_widths(rows: list[list[str]], headers: list[str],
                   minimums: list[int] | None = None) -> list[int]:
    """
    算出每一列该多宽。

    ★ 为什么不写死宽度？
      因为真实数据里工具名长度完全不可控（check_refund_policy 有 19 个字符）。
      写死宽度会被长内容"顶开"，导致整张表错位。
      正确做法是先扫一遍数据取最大值 —— 这也正是 rich 这类库内部做的事。
    """
    minimums = minimums or [0] * len(headers)
    widths = [max(len(h), m) for h, m in zip(headers, minimums)]
    for row in rows:
        for index, cell in enumerate(row):
            if index < len(widths):
                widths[index] = max(widths[index], len(cell))
    return widths


def _render_row(cells: list[str], widths: list[int]) -> str:
    padded = [cell.ljust(width) for cell, width in zip(cells, widths)]
    return "  ".join(padded).rstrip()


def _truncate(text: str, width: int) -> str:
    """过长就截断并加省略号 —— 表格宁可少显示，也不要撑破终端宽度。"""
    if len(text) <= width:
        return text
    return text[: max(0, width - 3)] + "..."


def render_trajectory(trajectory: Trajectory, show_content: bool = True,
                      max_result_width: int = 40) -> str:
    """
    把轨迹渲染成一张按调用顺序排列的表格。

    max_result_width：结果列的宽度上限。工具返回值可能很长（一整个订单对象），
    全打出来会把表格撑得没法看 —— 所以截断。想看完整内容用 --json。
    """
    lines: list[str] = []

    headers = ["Step", "Tool", "Args", "Result", "Flag"]
    rows: list[list[str]] = []

    # 先把"重复调用"的位置算出来，好在表里打标记
    from .config import RulesConfig
    from .rules import check_repeat_call
    flagged = set()
    for anomaly in check_repeat_call(trajectory, RulesConfig()):
        flagged.update(anomaly.detail.get("indices", []))

    call_number = 0
    for step in trajectory.steps:
        if not step.calls:
            continue
        for call in step.calls:
            arguments_text = ", ".join(
                f"{key}={value}" for key, value in call.args.items()
            ) or "-"
            result_text = call.result.summary(max_length=200) if call.result else "(no result)"
            rows.append([
                str(step.index),
                str(call.tool),
                arguments_text,
                _truncate(result_text, max_result_width),
                "!! REPEAT" if call_number in flagged else "",
            ])
            call_number += 1

    if rows:
        widths = _column_widths(rows, headers, minimums=[4, 12, 12, 10, 0])
        lines.append(_render_row(headers, widths))
        lines.append(_render_row(["-" * width for width in widths], widths))
        for row in rows:
            lines.append(_render_row(row, widths))
    else:
        lines.append("（这条轨迹没有工具调用）")

    if show_content:
        answer = trajectory.final_answer
        if answer:
            lines.append("")
            lines.append(f"最终回答：{answer}")

    return "\n".join(lines)


def render_anomalies(report: AnomalyReport) -> str:
    """把告警列表渲染成人能读的报告。"""
    lines: list[str] = []
    trajectory = report.trajectory

    lines.append(f"轨迹：{trajectory.path or '(内存)'}    格式：{trajectory.adapter}")
    lines.append(
        f"概况：{len(trajectory.steps)} 步 / {trajectory.call_count} 次工具调用 / "
        f"prompt_tokens 合计 {trajectory.total_prompt_tokens}"
    )
    if trajectory.parse_warnings:
        lines.append("")
        lines.append(f"解析警告 {len(trajectory.parse_warnings)} 条（坏行已跳过，未中断解析）：")
        for warning in trajectory.parse_warnings[:5]:
            lines.append(f"  - {warning}")
        if len(trajectory.parse_warnings) > 5:
            lines.append(f"  ... 另有 {len(trajectory.parse_warnings) - 5} 条")

    lines.append("")

    if not report.anomalies:
        lines.append("检查结果：没有发现异常。")
    else:
        counts = report.counts_by_severity()
        summary = "，".join(
            f"{SEVERITY_TAG.get(level, level)} {count} 条"
            for level, count in sorted(counts.items(), key=lambda kv: -len(kv[0]))
        )
        lines.append(f"检查结果：发现 {len(report.anomalies)} 处异常（{summary}）")
        lines.append("")
        for anomaly in report.anomalies:
            location = []
            if anomaly.step is not None:
                location.append(f"step {anomaly.step}")
            if anomaly.call_index is not None:
                location.append(f"call #{anomaly.call_index}")
            location_text = f"  ({', '.join(location)})" if location else ""
            lines.append(
                f"  {SEVERITY_TAG.get(anomaly.severity, '[' + anomaly.severity + ']')} "
                f"{anomaly.rule}{location_text}"
            )
            lines.append(f"        {anomaly.message}")

    if report.has_blocking_issue("error"):
        lines.append("")
        lines.append("结论：存在 ERROR 及以上级别的异常 —— 在 CI 里会阻断合并。")
    return "\n".join(lines)


def render_diff(diff: TrajectoryDiff) -> str:
    """渲染版本对比结果。"""
    lines: list[str] = []
    lines.append(f"基线：{diff.base_path}")
    lines.append(f"候选：{diff.candidate_path}")
    lines.append("")

    if diff.first_divergence is None:
        lines.append("工具调用序列完全一致 —— 行为层面没有分歧。")
        lines.append("（注意：这只说明『怎么调工具』没变，不代表最终回答也没变。）")
    else:
        first = diff.first_divergence
        lines.append(f"第一次分歧出现在第 {first.index} 次工具调用：")
        lines.append(f"  基线：{first.base}")
        lines.append(f"  候选：{first.candidate}")
        if diff.subsequent:
            lines.append("")
            lines.append(f"之后还有 {len(diff.subsequent)} 处分歧：")
            for item in diff.subsequent[:10]:
                lines.append(f"  #{item.index} [{item.kind}] "
                             f"{item.base}  ->  {item.candidate}")
            if len(diff.subsequent) > 10:
                lines.append(f"  ... 另有 {len(diff.subsequent) - 10} 处")

    if diff.metric_deltas:
        lines.append("")
        lines.append("指标变化：")
        # 用英文键名而不是中文标签，因为中文在终端里占两个字符宽，
        # 而 ljust 按一个字符算 —— 混排必然对不齐。这类"宽度陷阱"在
        # 做终端输出时会反复遇到，最省事的解法就是别用中文对齐。
        for key in ("steps", "calls", "total_prompt_tokens", "error_results"):
            if key not in diff.metric_deltas:
                continue
            delta = diff.metric_deltas[key]
            sign = "+" if delta > 0 else ""
            lines.append(f"  {key:<22} {sign}{delta:g}")
    return "\n".join(lines)


def render_markdown(report: AnomalyReport) -> str:
    """把检查结果输出成 Markdown，方便直接贴进 PR 评论或报告文件。"""
    trajectory = report.trajectory
    lines: list[str] = []
    lines.append("## Agent 轨迹检查报告")
    lines.append("")
    lines.append(f"- 轨迹文件：`{trajectory.path or '(内存)'}`")
    lines.append(f"- 格式适配器：`{trajectory.adapter}`")
    lines.append(f"- 规模：{len(trajectory.steps)} 步 / {trajectory.call_count} 次工具调用")
    lines.append(f"- token：prompt {trajectory.total_prompt_tokens} / "
                 f"completion {trajectory.total_completion_tokens}")
    lines.append(f"- 已启用规则：{len(report.enabled_rules)} 条")
    lines.append("")

    if not report.anomalies:
        lines.append("### 结论：未发现异常")
        return "\n".join(lines)

    lines.append("### 异常列表")
    lines.append("")
    lines.append("| 级别 | 规则 | 位置 | 说明 |")
    lines.append("|:---|:---|:---|:---|")
    for anomaly in report.anomalies:
        location_parts = []
        if anomaly.step is not None:
            location_parts.append(f"step {anomaly.step}")
        if anomaly.call_index is not None:
            location_parts.append(f"call #{anomaly.call_index}")
        lines.append(
            f"| `{anomaly.severity}` | `{anomaly.rule}` | "
            f"{', '.join(location_parts) or '-'} | {anomaly.message} |"
        )
    lines.append("")
    if report.has_blocking_issue("error"):
        lines.append("> **门禁结论**：存在 ERROR 及以上异常，CI 应阻断。")
    else:
        lines.append("> **门禁结论**：未达到阻断阈值。")
    return "\n".join(lines)


def render_tool_stats(trajectory: Trajectory) -> str:
    """工具调用次数统计 —— 一眼看出 Agent 把时间花在哪了。"""
    counts = trajectory.tool_call_counts()
    if not counts:
        return "（没有工具调用）"
    items = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    width = max(len(name) for name, _ in items)
    total = sum(counts.values())
    lines = []
    for name, count in items:
        share = count / total
        bar = "#" * max(1, round(share * 30))
        lines.append(f"  {name.ljust(width)}  {str(count).rjust(3)} 次  {bar} {share:.0%}")
    return "\n".join(lines)
