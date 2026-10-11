"""
解析层测试 —— 覆盖真实数据里会遇到的"不干净"情况。

这些用例的价值在于：它们描述的是**真实会发生的意外**，不是假想。
    - 文件最后一行残缺（进程被 kill、磁盘写满）
    - arguments 被截断成 null（模型输出被 max_tokens 切断）
    - arguments 已经是字典（不同 SDK 的行为不一致）
    - 结果是 null（失败的工具调用没有返回值）
"""

from __future__ import annotations

import json

import pytest

from trajectory_inspector.adapters import (
    AnthropicAdapter,
    OpenAIAdapter,
    load_trajectory,
    parse_arguments,
)


# ============================================================
# parse_arguments：三种输入形态
# ============================================================
class TestParseArguments:
    def test_standard_json_string(self):
        """标准情况：arguments 是 JSON 字符串，需要二次解析。"""
        assert parse_arguments('{"order_id": "A100"}') == {"order_id": "A100"}

    def test_already_a_dict(self):
        """意外情况：有些 SDK 直接给字典。不处理会崩。"""
        assert parse_arguments({"order_id": "A100"}) == {"order_id": "A100"}

    def test_none_and_empty(self):
        """arguments 为 null / 空串 —— 被 max_tokens 截断时就是这样。"""
        assert parse_arguments(None) == {}
        assert parse_arguments("") == {}

    def test_truncated_json_does_not_raise(self):
        """解析不了的字符串要降级成空字典，而不是让整个工具崩掉。"""
        assert parse_arguments('{"order_id": "A10') == {}

    def test_json_array_is_not_a_dict(self):
        """协议规定必须是对象；万一给了数组，也要能安全降级。"""
        assert parse_arguments("[1, 2, 3]") == {}


# ============================================================
# OpenAI 适配器
# ============================================================
class TestOpenAIAdapter:
    def test_groups_parallel_calls_into_one_step(self):
        """
        ★ 一个 step 可以带多个并行工具调用 —— 这是最容易被写错的格式细节。

        如果实现时把每个工具调用当成一个 step，那么"第几步"这个信息就全错了，
        报告里的定位也就没法用了。
        """
        records = [
            {"type": "llm_response", "step": 0, "usage": {"prompt_tokens": 100},
             "message": {"role": "assistant", "content": None, "tool_calls": [
                 {"id": "a", "type": "function",
                  "function": {"name": "get_order", "arguments": '{"order_id": "A1"}'}},
                 {"id": "b", "type": "function",
                  "function": {"name": "get_user", "arguments": '{"user_id": "U1"}'}},
             ]}},
            {"type": "tool_result", "call_id": "a", "tool": "get_order",
             "ok": True, "result": {"status": "paid"}},
            {"type": "tool_result", "call_id": "b", "tool": "get_user",
             "ok": True, "result": {"name": "Tom"}},
        ]
        trajectory = OpenAIAdapter().parse(records)

        assert len(trajectory.steps) == 1, "两个并行调用应该属于同一个 step"
        assert trajectory.call_count == 2
        assert [c.tool for c in trajectory.steps[0].calls] == ["get_order", "get_user"]

    def test_results_are_paired_by_call_id(self):
        """结果必须靠 call_id 精确配对，不能靠顺序猜。"""
        records = [
            {"type": "llm_response", "step": 0, "message": {"role": "assistant",
             "content": None, "tool_calls": [
                 {"id": "second", "type": "function",
                  "function": {"name": "b", "arguments": "{}"}},
                 {"id": "first", "type": "function",
                  "function": {"name": "a", "arguments": "{}"}},
             ]}},
            # 故意把结果顺序打乱
            {"type": "tool_result", "call_id": "first", "tool": "a", "ok": True,
             "result": {"who": "a"}},
            {"type": "tool_result", "call_id": "second", "tool": "b", "ok": True,
             "result": {"who": "b"}},
        ]
        trajectory = OpenAIAdapter().parse(records)
        calls = trajectory.steps[0].calls
        assert calls[0].result.result == {"who": "b"}
        assert calls[1].result.result == {"who": "a"}

    def test_failed_result_with_null_payload(self):
        """失败的工具调用 result 是 null —— 不能因此崩掉。"""
        records = [
            {"type": "llm_response", "step": 0, "message": {"role": "assistant",
             "content": None, "tool_calls": [
                 {"id": "a", "type": "function",
                  "function": {"name": "x", "arguments": "{}"}}]}},
            {"type": "tool_result", "call_id": "a", "tool": "x",
             "ok": False, "error": "timeout", "result": None},
        ]
        trajectory = OpenAIAdapter().parse(records)
        result = trajectory.steps[0].calls[0].result
        assert result is not None
        assert result.ok is False
        assert "timeout" in result.summary()


# ============================================================
# Anthropic 适配器
# ============================================================
class TestAnthropicAdapter:
    def test_parses_tool_use_blocks(self):
        """Anthropic 把工具调用放在 content 数组的 block 里，不是 message.tool_calls。"""
        records = [
            {"role": "assistant", "usage": {"input_tokens": 500, "output_tokens": 30},
             "content": [
                 {"type": "text", "text": "Let me check."},
                 {"type": "tool_use", "id": "t1", "name": "get_order",
                  "input": {"order_id": "A1"}},
             ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1",
                 "content": [{"type": "text", "text": "paid"}]},
            ]},
        ]
        trajectory = AnthropicAdapter().parse(records)

        assert len(trajectory.steps) == 1
        step = trajectory.steps[0]
        assert step.content == "Let me check."
        assert step.prompt_tokens == 500
        assert step.completion_tokens == 30
        assert step.calls[0].tool == "get_order"
        # Anthropic 的 input 本身就是字典，不需要二次解析
        assert step.calls[0].args == {"order_id": "A1"}
        assert step.calls[0].result is not None

    def test_error_tool_result_is_marked_not_ok(self):
        records = [
            {"role": "assistant", "content": [
                {"type": "tool_use", "id": "t1", "name": "refund", "input": {}}]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1",
                 "is_error": True, "content": "upstream 500"}]},
        ]
        trajectory = AnthropicAdapter().parse(records)
        assert trajectory.steps[0].calls[0].result.ok is False


# ============================================================
# 文件级行为
# ============================================================
class TestFileLoading:
    def test_broken_lines_are_skipped_not_fatal(self, tmp_path):
        """
        ★ 关键行为：坏行不能让整个工具崩掉。

        为什么这条重要？因为轨迹文件经常是"跑到一半崩了"留下的，
        最后一行残缺是常态。工具在这种输入上抛异常，就等于在最需要它的时候不能用。
        """
        path = tmp_path / "broken.jsonl"
        path.write_text(
            '{"type":"llm_response","step":0,"message":{"role":"assistant",'
            '"content":null,"tool_calls":[{"id":"a","function":{"name":"x",'
            '"arguments":"{}"}}]}}\n'
            '{"type":"tool_resu'                       # ← 被截断的半行
            '\n'
            '\n'
            '{"type":"tool_result","call_id":"a","tool":"x","ok":true,"result":{}}\n',
            encoding="utf-8",
        )
        trajectory, warnings = load_trajectory(path)

        assert len(warnings) == 1, "应该报告 1 条坏行警告"
        assert "第 2 行" in warnings[0]
        assert len(trajectory.steps) == 1, "好数据应该照常解析"
        assert trajectory.steps[0].calls[0].result is not None

    def test_empty_file_raises_clear_error(self, tmp_path):
        path = tmp_path / "empty.jsonl"
        path.write_text("\n\n", encoding="utf-8")
        with pytest.raises(ValueError, match="没有任何可用记录"):
            load_trajectory(path)

    def test_unknown_format_raises_helpful_error(self, tmp_path):
        """看不懂的格式要给出"怎么办"，而不是一句 JSONDecodeError。"""
        path = tmp_path / "weird.jsonl"
        path.write_text('{"foo": "bar"}\n{"foo": "baz"}\n', encoding="utf-8")
        with pytest.raises(ValueError, match="格式无法识别"):
            load_trajectory(path)

    def test_missing_file_raises_file_not_found(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            load_trajectory(tmp_path / "nope.jsonl")


# ============================================================
# 最终回答的提取
# ============================================================
def test_final_answer_is_last_text_only_step():
    """
    最终回答 = 最后一条"只有文字、没有工具调用"的 step。

    如果实现成"最后一条有 content 的 step"，就会把中间步骤的思考
    （比如 "Let me check the policy."）误当成最终回答。
    """
    records = [
        {"type": "llm_response", "step": 0, "message": {
            "role": "assistant", "content": "Let me check the policy.",
            "tool_calls": [{"id": "a", "function": {"name": "x", "arguments": "{}"}}]}},
        {"type": "tool_result", "call_id": "a", "tool": "x", "ok": True, "result": {}},
        {"type": "llm_response", "step": 1, "message": {
            "role": "assistant", "content": "It is not eligible.", "tool_calls": None}},
    ]
    trajectory = OpenAIAdapter().parse(records)
    assert trajectory.final_answer == "It is not eligible."
