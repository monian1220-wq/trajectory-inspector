"""
Anthropic Messages API 风格的适配器。

它和 OpenAI 格式的差别主要体现在三处：
    1. 工具调用不是放在 message.tool_calls 里，而是 content 数组里的一个 block
    2. 工具结果是一条独立的 user 消息（type=tool_result 的 block），
       而不是一条独立的顶层记录
    3. 参数 arguments 本身就是字典（不需要二次解析）

★ 为什么我要把"差别"写在这里而不是省略？
  因为面试官问"你怎么支持多格式"时，你要能具体说出差别在哪。
  只写"我做了个适配器层"，说不清差别的，一听就是没真的做过。
"""

from __future__ import annotations

from .base import BaseAdapter, parse_arguments
from ..models import Step, ToolCall, ToolResult, Trajectory


class AnthropicAdapter(BaseAdapter):
    name = "anthropic"

    def can_handle(self, records: list[dict]) -> bool:
        if not records:
            return False
        for record in records:
            content = record.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") in ("tool_use", "tool_result"):
                    return True
        return False

    def parse(self, records: list[dict]) -> Trajectory:
        # 第一遍：把 tool_result block 收集起来
        results: dict[str, ToolResult] = {}
        for record in records:
            for block in self._blocks(record):
                if block.get("type") != "tool_result":
                    continue
                call_id = block.get("tool_use_id")
                if call_id:
                    results[call_id] = ToolResult(
                        ok=block.get("is_error") is not True,
                        result=block.get("content"),
                        error=str(block.get("content")) if block.get("is_error") else None,
                    )

        # 第二遍：把 assistant 消息里的 tool_use block 变成 Step
        steps: list[Step] = []
        for record in records:
            if record.get("role") != "assistant":
                continue
            blocks = self._blocks(record)
            calls: list[ToolCall] = []
            texts: list[str] = []
            for block in blocks:
                if block.get("type") == "tool_use":
                    call_id = block.get("id")
                    calls.append(ToolCall(
                        call_id=call_id,
                        tool=block.get("name"),
                        raw_arguments=block.get("input"),
                        args=parse_arguments(block.get("input")),
                        result=results.get(call_id),
                    ))
                elif block.get("type") == "text":
                    texts.append(block.get("text") or "")

            usage = record.get("usage") or {}
            steps.append(Step(
                index=len(steps),
                content="\n".join(t for t in texts if t) or None,
                prompt_tokens=usage.get("input_tokens", 0) or 0,
                completion_tokens=usage.get("output_tokens", 0) or 0,
                calls=calls,
            ))

        return Trajectory(adapter=self.name, steps=steps)

    @staticmethod
    def _blocks(record: dict) -> list[dict]:
        content = record.get("content")
        if isinstance(content, list):
            return [b for b in content if isinstance(b, dict)]
        return []
