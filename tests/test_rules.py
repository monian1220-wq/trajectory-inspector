"""
规则的边界测试。

★ 这组测试和 test_fixtures.py 的分工：
    - test_fixtures.py：验证"真实轨迹文件"能被正确检出（端到端）
    - 本文件：验证"规则本身的边界行为"（单元级）

  边界测试更重要，因为检测工具的真正风险是**误报**：
  一个把正常轨迹也报成问题的工具，用户两天之内就会关掉它。
"""

from __future__ import annotations

import json

from trajectory_inspector.adapters import OpenAIAdapter
from trajectory_inspector.config import RulesConfig
from trajectory_inspector.models import call_signature
from trajectory_inspector.rules import (
    arguments_similar,
    check_arg_jitter_loop,
    check_context_bloat,
    check_repeat_call,
    check_redundant_parallel_call,
    check_silent_tool_failure,
    levenshtein,
    run_rules,
    values_similar,
)


# ============================================================
# 辅助：用最少的话构造一条轨迹
# ============================================================
def build(calls_per_step: list[list[tuple]], prompt_tokens: list[int] | None = None,
          results: dict[str, dict] | None = None, final: str | None = None):
    """
    构造一条轨迹。

    calls_per_step: [[("get_order", {"order_id": "A1"}), ...], ...]
    results: {call_id: {"ok": False, "error": "timeout"}}，默认全部成功
    """
    records: list[dict] = []
    counter = 0
    for step_index, calls in enumerate(calls_per_step):
        tool_calls = []
        for tool, args in calls:
            counter += 1
            call_id = f"c{counter}"
            tool_calls.append({
                "id": call_id, "type": "function",
                "function": {"name": tool, "arguments": json.dumps(args)},
            })
        tokens = (prompt_tokens or [100] * len(calls_per_step))[step_index]
        records.append({
            "type": "llm_response", "step": step_index,
            "usage": {"prompt_tokens": tokens, "completion_tokens": 10},
            "message": {"role": "assistant", "content": None, "tool_calls": tool_calls},
        })
        for index, item in enumerate(tool_calls):
            call_id = item["id"]
            spec = (results or {}).get(call_id, {})
            records.append({
                "type": "tool_result", "call_id": call_id,
                "tool": item["function"]["name"],
                "ok": spec.get("ok", True),
                "error": spec.get("error"),
                "result": spec.get("result", {}),
            })
    if final is not None:
        records.append({
            "type": "llm_response", "step": len(calls_per_step),
            "usage": {"prompt_tokens": 100, "completion_tokens": 10},
            "message": {"role": "assistant", "content": final, "tool_calls": None},
        })
    return OpenAIAdapter().parse(records)


# ============================================================
# 编辑距离
# ============================================================
class TestLevenshtein:
    def test_basic_cases(self):
        assert levenshtein("", "") == 0
        assert levenshtein("abc", "abc") == 0
        assert levenshtein("abc", "") == 3
        assert levenshtein("kitten", "sitting") == 3    # 教科书经典用例

    def test_short_id_is_sensitive(self):
        """
        ★ 这条测试记录了一个真实的设计约束：
          "A100" 改成 "A-100" 只差 1 个字符，但因为字符串短，
          相似度只有 0.75 —— 所以 arg_similarity 阈值不能设高于 0.75，
          否则最常见的"改写参数重试"会被漏掉。
        """
        assert levenshtein("a100", "a-100") == 1
        similarity = 1 - levenshtein("a100", "a-100") / 5
        assert similarity == 0.8
        assert RulesConfig().arg_jitter_loop.arg_similarity <= 0.8


# ============================================================
# 参数相似度
# ============================================================
class TestValuesSimilar:
    def test_case_and_space_insensitive(self):
        threshold = 0.7
        assert values_similar("A100", "a100", threshold)
        assert values_similar("A100", " A100 ", threshold)
        assert values_similar("A100", "A-100", threshold)     # 去掉标点后相等

    def test_genuinely_different_values_are_not_similar(self):
        threshold = 0.7
        assert not values_similar("A100", "B900", threshold)
        assert not values_similar("order-100", "order-999", threshold)

    def test_numeric_representations(self):
        threshold = 0.7
        assert values_similar(320, 320.0, threshold)
        assert values_similar("320", 320, threshold)


class TestArgumentsSimilar:
    def test_only_shared_keys_are_compared(self):
        """参数个数不同不算微扰 —— 那更可能是"这次多带了一个参数"。"""
        assert arguments_similar({"a": 1}, {"a": 1, "b": 2}, 0.7)

    def test_no_shared_keys_is_not_similar(self):
        assert not arguments_similar({"a": 1}, {"b": 1}, 0.7)


# ============================================================
# repeat_call 的边界
# ============================================================
class TestRepeatCall:
    def test_two_identical_calls_is_a_loop(self):
        trajectory = build([[("get_order", {"order_id": "A1"})],
                            [("get_order", {"order_id": "A1"})]])
        anomalies = check_repeat_call(trajectory, RulesConfig())
        assert len(anomalies) == 1
        assert anomalies[0].detail["call_count"] == 2
        assert anomalies[0].detail["indices"] == [0, 1]

    def test_loop_at_the_very_end_is_still_detected(self):
        """
        ★ 这条测试防的是最容易漏的一个 bug：忘了在循环结束后"结算最后一段"。

        构造：一次正常调用 + 两次相同调用（重复出现在最后）。
        如果实现里只在"签名变化时"结算，这组重复就会被漏掉。
        """
        trajectory = build([[("a", {"i": 1})],
                            [("b", {"i": 2})],
                            [("b", {"i": 2})]])
        anomalies = check_repeat_call(trajectory, RulesConfig())
        assert len(anomalies) == 1
        assert anomalies[0].detail["indices"] == [1, 2]

    def test_case_difference_is_not_a_repeat(self):
        """
        ★ 归一化必须保守："A100" 和 "a100" 可能是两个不同的单号。
          把它们当成重复会漏报真实问题。
          "看起来差不多"的判断交给 arg_jitter_loop。
        """
        trajectory = build([[("get_order", {"order_id": "A100"})],
                            [("get_order", {"order_id": "a100"})]])
        assert check_repeat_call(trajectory, RulesConfig()) == []

    def test_key_order_does_not_matter(self):
        """{"a":1,"b":2} 和 {"b":2,"a":1} 是同一次调用。"""
        assert call_signature("t", {"a": 1, "b": 2}) == call_signature("t", {"b": 2, "a": 1})
        trajectory = build([[("t", {"a": 1, "b": 2})], [("t", {"b": 2, "a": 1})]])
        assert len(check_repeat_call(trajectory, RulesConfig())) == 1

    def test_float_and_int_are_the_same_value(self):
        trajectory = build([[("t", {"amount": 320})], [("t", {"amount": 320.0})]])
        assert len(check_repeat_call(trajectory, RulesConfig())) == 1

    def test_different_tools_are_not_a_repeat(self):
        trajectory = build([[("a", {"i": 1})], [("b", {"i": 1})]])
        assert check_repeat_call(trajectory, RulesConfig()) == []


# ============================================================
# arg_jitter_loop 的边界
# ============================================================
class TestArgJitterLoop:
    def test_four_variants_of_the_same_id(self):
        trajectory = build([[("get_order", {"order_id": v})] for v in
                            ["A100", "a100", " A100 ", "A-100"]])
        anomalies = check_arg_jitter_loop(trajectory, RulesConfig())
        assert len(anomalies) == 1
        assert anomalies[0].detail["call_count"] == 4
        # 注意：这组调用不会触发 repeat_call，因为签名不同 —— 这正是两条规则的分工
        assert check_repeat_call(trajectory, RulesConfig()) == []

    def test_three_variants_is_below_threshold(self):
        trajectory = build([[("get_order", {"order_id": v})] for v in
                            ["A100", "a100", "A-100"]])
        assert check_arg_jitter_loop(trajectory, RulesConfig()) == []

    def test_no_chain_misjudgement(self):
        """
        ★ 防"链式误判"：A~B 且 B~C 不代表 A~C。

        构造：第一个参数和第三个参数差得很远，中间那个"桥接"它们。
        如果实现是"跟上一个比"，就会把这三个误判成一段。
        正确实现是"跟这一串的起点比"。
        """
        trajectory = build([[("t", {"v": "aaaa"})],
                            [("t", {"v": "aaab"})],
                            [("t", {"v": "aabb"})],
                            [("t", {"v": "zzzz"})]])
        # 起点是 "aaaa"："aaaa"~"aaab" 相似，"aabb" 与 "aaaa" 相似度 0.5 < 0.7
        # 所以这一串只有 2 个，达不到阈值 4
        assert check_arg_jitter_loop(trajectory, RulesConfig()) == []

    def test_exact_repeats_are_not_double_reported(self):
        """完全相同的重复由 repeat_call 负责，arg_jitter_loop 不重复报。"""
        trajectory = build([[("get_order", {"order_id": "A1"})] for _ in range(4)])
        assert len(check_repeat_call(trajectory, RulesConfig())) == 1
        assert check_arg_jitter_loop(trajectory, RulesConfig()) == []


# ============================================================
# silent_tool_failure 的边界
# ============================================================
class TestSilentToolFailure:
    def test_ignored_failure_is_detected(self):
        trajectory = build([[("check_refund_policy", {"order_id": "B2"})]],
                           results={"c1": {"ok": False, "error": "timeout"}},
                           final="Your order is eligible.")
        anomalies = check_silent_tool_failure(trajectory, RulesConfig())
        assert len(anomalies) == 1
        assert anomalies[0].detail["error"] == "timeout"

    def test_retry_is_not_silent(self):
        """失败了但 Agent 又试了一次 —— 不算静默。"""
        trajectory = build([[("check_refund_policy", {"order_id": "B2"})],
                            [("check_refund_policy", {"order_id": "B2"})]],
                           results={"c1": {"ok": False, "error": "timeout"}},
                           final="Eventually it worked.")
        assert check_silent_tool_failure(trajectory, RulesConfig()) == []

    def test_mentioning_the_failure_is_not_silent(self):
        """Agent 在回答里说了"系统出错了" —— 不算静默。"""
        trajectory = build([[("check_refund_policy", {"order_id": "B2"})]],
                           results={"c1": {"ok": False, "error": "timeout"}},
                           final="Sorry, the policy service timed out. Please try again later.")
        assert check_silent_tool_failure(trajectory, RulesConfig()) == []

    def test_successful_call_is_never_silent(self):
        trajectory = build([[("check_refund_policy", {"order_id": "B2"})]],
                           final="All good.")
        assert check_silent_tool_failure(trajectory, RulesConfig()) == []

    def test_intermediate_thinking_is_not_treated_as_disclosure(self):
        """
        ★ 一个容易写错的地方：中间步骤的 content（"Let me check..."）
          只是模型的思考过程，不能当成"已经告知用户"。
          只有"纯文字回答"才算告知。
        """
        records = [
            {"type": "llm_response", "step": 0, "message": {
                "role": "assistant",
                # 这句话里含 "error" 这个词，但它不是给用户的告知
                "content": "There may be an error, let me retry the check.",
                "tool_calls": [{"id": "c1", "type": "function",
                                "function": {"name": "check_refund_policy",
                                             "arguments": "{}"}}]}},
            {"type": "tool_result", "call_id": "c1", "tool": "check_refund_policy",
             "ok": False, "error": "timeout", "result": None},
            {"type": "llm_response", "step": 1, "message": {
                "role": "assistant", "content": "It is eligible.", "tool_calls": None}},
        ]
        trajectory = OpenAIAdapter().parse(records)
        anomalies = check_silent_tool_failure(trajectory, RulesConfig())
        assert len(anomalies) == 1, "中间步骤的思考不应被当作已告知用户"


# ============================================================
# context_bloat 的边界
# ============================================================
class TestContextBloat:
    def test_healthy_growth_is_not_flagged(self):
        """正常轨迹 4 步涨 22% —— 这是健康的上下文累积，不该报警。"""
        trajectory = build([[("t", {"i": i})] for i in range(4)],
                           prompt_tokens=[820, 910, 1000, 1120])
        assert check_context_bloat(trajectory, RulesConfig()) == []

    def test_moderate_growth_is_not_flagged(self):
        trajectory = build([[("t", {"i": i})] for i in range(5)],
                           prompt_tokens=[500, 600, 700, 800, 900])
        assert check_context_bloat(trajectory, RulesConfig()) == []

    def test_exponential_growth_is_flagged(self):
        """指数增长：单步只有 1.5x，4 步却涨了 5 倍。"""
        trajectory = build([[("t", {"i": i})] for i in range(5)],
                           prompt_tokens=[800, 1200, 1800, 2700, 4050])
        anomalies = check_context_bloat(trajectory, RulesConfig())
        assert len(anomalies) == 1
        assert anomalies[0].detail["growth"] >= 3.0

    def test_only_worst_window_is_reported(self):
        """同一个问题的多个窗口只报一次 —— 不用重复告警淹没用户。"""
        trajectory = build([[("t", {"i": i})] for i in range(10)],
                           prompt_tokens=[int(800 * 1.5 ** i) for i in range(10)])
        assert len(check_context_bloat(trajectory, RulesConfig())) == 1

    def test_short_trace_is_not_flagged(self):
        """步数不足 min_steps 时不判定（样本太少，结论不可信）。"""
        trajectory = build([[("t", {"i": i})] for i in range(3)],
                           prompt_tokens=[100, 500, 5000])
        assert check_context_bloat(trajectory, RulesConfig()) == []


# ============================================================
# redundant_parallel_call 的边界
# ============================================================
class TestRedundantParallelCall:
    def test_same_call_twice_in_one_step(self):
        trajectory = build([[("get_order", {"order_id": "A1"}),
                             ("get_order", {"order_id": "A1"})]])
        anomalies = check_redundant_parallel_call(trajectory, RulesConfig())
        assert len(anomalies) == 1
        assert anomalies[0].step == 0

    def test_two_different_calls_in_one_step_is_fine(self):
        """一步里并行调两个不同的工具是正常且推荐的。"""
        trajectory = build([[("get_order", {"order_id": "A1"}),
                             ("get_user", {"user_id": "U1"})]])
        assert check_redundant_parallel_call(trajectory, RulesConfig()) == []


# ============================================================
# 规则开关
# ============================================================
class TestRuleToggles:
    def test_disabled_rule_produces_nothing(self):
        trajectory = build([[("get_order", {"order_id": "A1"})],
                            [("get_order", {"order_id": "A1"})]])
        config = RulesConfig()
        config.repeat_call.enabled = False
        report = run_rules(trajectory, config)
        assert [a.rule for a in report.anomalies] == []
        assert "repeat_call" not in report.enabled_rules

    def test_critical_blocks_at_critical_threshold_only(self):
        """critical 级异常在 error 阈值下也阻断，在 critical 阈值下同样阻断。"""
        trajectory = build([[("refund_order", {"order_id": "A1"})]],
                           results={"c1": {"ok": False, "error": "timeout"}},
                           final="Refund done.")
        report = run_rules(trajectory, RulesConfig())
        assert report.max_severity() == "critical"
        assert report.has_blocking_issue("error")
        assert report.has_blocking_issue("critical")
