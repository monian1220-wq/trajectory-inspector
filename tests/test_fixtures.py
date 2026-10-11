"""
Fixture 对照测试 —— 这是整个测试套件里最重要的一组。

它做三件事：
    1. 确保 data / logic / expected 三者一致（expected.json 是独立生成的，可作为"第二意见"）
    2. 锁定"健康轨迹必须零告警"这条底线
    3. 把每条 fixture 的"应该检出什么"写成可执行的断言，而不是README里的一句话

★ 为什么要有 expected.json 而不是把期望值写在测试里？
  因为它是由独立实现（一份等价逻辑）生成的，等于一次交叉验证。
  两份独立实现得出同样的结论，比"我自己写期望值自己测"可信得多。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trajectory_inspector.adapters import load_trajectory
from trajectory_inspector.config import RulesConfig
from trajectory_inspector.rules import run_rules

FIXTURE_DIR = Path(__file__).parent / "fixtures"
TRAJECTORY_DIR = FIXTURE_DIR / "trajectories"
EXPECTED_PATH = FIXTURE_DIR / "expected.json"

with EXPECTED_PATH.open("r", encoding="utf-8") as _handle:
    EXPECTED = json.load(_handle)

FIXTURE_NAMES = sorted(EXPECTED["fixtures"].keys())


def test_all_fixture_files_have_expectations():
    """每个 .jsonl 文件都要在 expected.json 里有对应条目，反之亦然。"""
    on_disk = sorted(p.stem for p in TRAJECTORY_DIR.glob("*.jsonl"))
    assert on_disk == FIXTURE_NAMES, (
        "fixture 文件和 expected.json 不同步。\n"
        f"  磁盘上：{on_disk}\n"
        f"  期望里：{FIXTURE_NAMES}\n"
        "  加新 fixture 后，需要同步更新 expected.json。"
    )


def test_python_default_rules_match_expected_rules():
    """
    Python 侧的默认阈值必须和 expected.json 里记录的一致。

    这条测试是防"两套实现悄悄跑偏"的护栏：
    如果有人改了 config.py 的默认值却没重新生成 expected.json，
    这里会立刻失败，而不是等到某天发现检测结果对不上。
    """
    expected_rules = EXPECTED["rules"]
    actual = RulesConfig().to_dict()

    for rule_name, expected_rule in expected_rules.items():
        assert rule_name in actual, f"RulesConfig 缺少规则 {rule_name}"
        for key, expected_value in expected_rule.items():
            assert key in actual[rule_name], f"{rule_name} 缺少字段 {key}"
            assert actual[rule_name][key] == expected_value, (
                f"{rule_name}.{key} 不一致："
                f"config.py 是 {actual[rule_name][key]!r}，"
                f"expected.json 记录的是 {expected_value!r}"
            )


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_metrics_match(name: str):
    """解析出来的指标必须和期望值完全一致。"""
    trajectory, warnings = load_trajectory(TRAJECTORY_DIR / f"{name}.jsonl")
    expected = EXPECTED["fixtures"][name]
    metrics = expected["metrics"]

    assert not warnings, f"{name} 不应该有解析警告：{warnings}"
    assert len(trajectory.steps) == metrics["steps"], "步数不一致"
    assert trajectory.call_count == metrics["calls"], "工具调用次数不一致"
    assert trajectory.tool_call_counts() == metrics["tool_calls_by_name"], "工具分布不一致"
    assert trajectory.total_prompt_tokens == metrics["total_prompt_tokens"], "prompt_tokens 不一致"
    assert trajectory.total_completion_tokens == metrics["total_completion_tokens"], (
        "completion_tokens 不一致"
    )
    error_results = sum(
        1 for _, call in trajectory.all_calls
        if call.result is not None and not call.result.ok
    )
    assert error_results == metrics["error_results"], "失败结果数不一致"


@pytest.mark.parametrize("name", FIXTURE_NAMES)
def test_anomalies_match(name: str):
    """检出的异常（规则名 + 级别）必须和期望值一致。"""
    trajectory, _ = load_trajectory(TRAJECTORY_DIR / f"{name}.jsonl")
    report = run_rules(trajectory)

    actual = sorted((a.rule, a.severity) for a in report.anomalies)
    expected = sorted(
        (a["rule"], a["severity"]) for a in EXPECTED["fixtures"][name]["anomalies"]
    )
    assert actual == expected, (
        f"{name} 的检出结果和期望不符：\n  实际：{actual}\n  期望：{expected}"
    )


def test_healthy_trajectory_has_no_anomalies():
    """
    ★ 最重要的一条测试：健康轨迹必须零告警。

    为什么它最重要？因为检测工具最大的失败不是"漏报"，而是"狼来了"——
    如果正常轨迹也被报一堆问题，用户两天之内就会把整个工具关掉。
    所以你每次调阈值，都要先跑这条测试。
    """
    trajectory, _ = load_trajectory(TRAJECTORY_DIR / "05_healthy.jsonl")
    report = run_rules(trajectory)

    assert report.anomalies == [], (
        "健康轨迹出现了告警，说明某个规则的阈值过松：\n"
        + "\n".join(f"  - {a.severity}:{a.rule} {a.message}" for a in report.anomalies)
    )
    assert report.max_severity() is None
    assert not report.has_blocking_issue("error")


def test_silent_failure_is_detected_with_detail():
    """静默失败不仅要检出，还要能说清是哪个工具、什么错误。"""
    trajectory, _ = load_trajectory(TRAJECTORY_DIR / "03_silent_failure.jsonl")
    report = run_rules(trajectory)

    silent = [a for a in report.anomalies if a.rule == "silent_tool_failure"]
    assert len(silent) == 1
    anomaly = silent[0]
    assert anomaly.tool == "check_refund_policy"
    assert anomaly.step == 0
    assert anomaly.detail["error"] == "upstream timeout"
    assert anomaly.detail["is_side_effect"] is False


def test_side_effect_failure_escalates_to_critical():
    """
    有副作用的工具（退款/转账）失败后被静默忽略 → 必须升级为 critical。

    这条用内联轨迹构造，因为它是"规则行为"而不是"某个 fixture 的性质"。
    """
    import json as jsonlib

    records = [
        {"type": "llm_response", "step": 0, "usage": {"prompt_tokens": 100}, "message": {
            "role": "assistant", "content": None, "tool_calls": [
                {"id": "x", "type": "function",
                 "function": {"name": "refund_order",
                              "arguments": jsonlib.dumps({"order_id": "A1"})}}]}},
        {"type": "tool_result", "call_id": "x", "tool": "refund_order",
         "ok": False, "error": "timeout", "result": None},
        {"type": "llm_response", "step": 1, "usage": {"prompt_tokens": 120}, "message": {
            "role": "assistant", "content": "Refund completed.", "tool_calls": None}},
    ]
    trajectory = _trajectory_from_records(records)
    report = run_rules(trajectory)

    silent = [a for a in report.anomalies if a.rule == "silent_tool_failure"]
    assert len(silent) == 1
    assert silent[0].severity == "critical", "有副作用的工具失败被静默忽略，必须升级为 critical"
    assert report.has_blocking_issue("critical")


def _trajectory_from_records(records: list[dict]):
    """把内存里的记录交给适配器解析（走真实的解析路径，不绕过适配器）。"""
    from trajectory_inspector.adapters import OpenAIAdapter

    adapter = OpenAIAdapter()
    assert adapter.can_handle(records), "测试数据应该是 OpenAI 格式"
    return adapter.parse(records)
