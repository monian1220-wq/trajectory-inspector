"""
适配器基类。

职责边界（很重要，别搞混）：
    适配器只做一件事：把【某种厂商格式】翻译成【我们的中间格式 Step】。
    它**不做**任何异常判断 —— 那是 rules 层的事。

为什么要有适配器层？因为"能读别人的轨迹"是这个工具的价值前提。
如果解析逻辑写死成自己 Agent 的格式，这个工具就只能给自己用。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

from ..models import Step, ToolCall, ToolResult, Trajectory


def parse_arguments(raw: Any) -> dict:
    """
    把 tool_call 的 arguments 变成字典。

    ★ 这是所有做工具调用的人都会踩一次的坑：
      协议规定 arguments 是【JSON 字符串】，所以它需要**第二次** json.loads。
      但真实世界不守规矩，必须同时兼容三种情况。

    三种情况：
      - 标准：'{"order_id": "A100"}'  → 解析成字典（注意这里又解析了一次！）
      - 意外：已经是字典 {...}         → 原样返回
      - 空值：None / ""               → 返回空字典

    如果只按"字符串"处理，遇到字典就会崩，报错信息是：
        TypeError: the JSON object must be str, bytes or bytearray, not dict
    如果压根忘了二次解析，报错信息是：
        AttributeError: 'str' object has no attribute 'get'
    记住这两条报错，能帮你省下大量排查时间。
    """
    if raw is None or raw == "":
        return {}
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            # arguments 被截断了（模型输出到一半断了），这是真实会发生的
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


class BaseAdapter(ABC):
    """所有适配器的基类。"""

    #: 适配器名字，会显示在报告里
    name: str = "base"

    @abstractmethod
    def can_handle(self, records: list[dict]) -> bool:
        """判断这批记录是不是我认识的格式。"""
        raise NotImplementedError

    @abstractmethod
    def parse(self, records: list[dict]) -> Trajectory:
        """把原始记录翻译成 Trajectory。"""
        raise NotImplementedError

    # ---------- 公共工具，子类可以直接用 ----------
    @staticmethod
    def load_records(path: str | Path) -> tuple[list[dict], list[str]]:
        """
        读 JSONL 文件，返回 (记录列表, 警告列表)。

        关键行为：**遇到坏行不要抛异常中断**，而是跳过并记录警告。
        为什么？因为轨迹文件经常是"跑到一半崩了"留下的，
        最后一行残缺是常态。工具在这种输入下直接崩掉，就没法用了。

        对应到工程原则：解析外部输入时，能恢复就恢复，能降级就降级。
        """
        records: list[dict] = []
        warnings: list[str] = []
        file_path = Path(path)
        with file_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                text = line.strip()
                if not text:
                    continue
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError as error:
                    warnings.append(f"第 {line_number} 行不是合法 JSON，已跳过（{error.msg}）")
                    continue
                if not isinstance(parsed, dict):
                    warnings.append(f"第 {line_number} 行不是对象，已跳过")
                    continue
                records.append(parsed)
        return records, warnings

    @staticmethod
    def collect_tool_results(records: list[dict]) -> dict[str, ToolResult]:
        """
        先把所有工具结果按 call_id 收集起来。

        为什么要先收集一遍？因为轨迹里"请求"和"结果"是分开的两行，
        而我们要把结果挂到对应的调用上。先建索引再配对，
        比在循环里到处找要清晰得多。这是很常用的数据处理套路。
        """
        results: dict[str, ToolResult] = {}
        for record in records:
            if record.get("type") != "tool_result":
                continue
            call_id = record.get("call_id")
            if not call_id:
                continue
            results[call_id] = ToolResult(
                ok=record.get("ok", True) is not False,
                result=record.get("result"),
                error=record.get("error"),
                duration_ms=record.get("duration_ms"),
            )
        return results

    @staticmethod
    def build_tool_call(raw_call: dict, results: dict[str, ToolResult]) -> ToolCall:
        """把一条原始 tool_call 翻译成 ToolCall，并配上结果。"""
        function = raw_call.get("function") or {}
        call_id = raw_call.get("id")
        return ToolCall(
            call_id=call_id,
            tool=function.get("name"),
            raw_arguments=function.get("arguments"),
            args=parse_arguments(function.get("arguments")),
            result=results.get(call_id),
        )


class OpenAIAdapter(BaseAdapter):
    """
    OpenAI Chat Completions 风格的工具调用轨迹。

    认识的格式（每行一个 JSON）：
        {"type":"llm_response","step":0,"usage":{...},"message":{"role":"assistant",
         "content":null,"tool_calls":[{"id":"c1","function":{"name":"...","arguments":"..."}}]}}
        {"type":"tool_result","call_id":"c1","tool":"...","ok":true,"result":{...}}
    """

    name = "openai"

    def can_handle(self, records: list[dict]) -> bool:
        if not records:
            return False
        for record in records:
            if record.get("type") == "llm_response":
                message = record.get("message")
                # 必须是"assistant 消息 + tool_calls 结构"才算 OpenAI 风格
                if isinstance(message, dict) and "tool_calls" in message:
                    return True
        return False

    def parse(self, records: list[dict]) -> Trajectory:
        results = self.collect_tool_results(records)
        steps: list[Step] = []

        for record in records:
            if record.get("type") != "llm_response":
                continue
            message = record.get("message") or {}
            usage = record.get("usage") or {}
            calls = [
                self.build_tool_call(raw, results)
                for raw in (message.get("tool_calls") or [])
            ]
            steps.append(Step(
                index=record.get("step", len(steps)),
                timestamp=record.get("timestamp"),
                content=message.get("content"),
                prompt_tokens=usage.get("prompt_tokens", 0) or 0,
                completion_tokens=usage.get("completion_tokens", 0) or 0,
                calls=calls,
            ))

        return Trajectory(adapter=self.name, steps=steps)
