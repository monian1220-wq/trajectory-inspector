"""
核心数据模型。

设计要点（这是整个项目最重要的一个决定）：
    不同厂商的轨迹格式完全不一样（OpenAI / Anthropic / 自己写的 Agent），
    所以我们先定义一套**自己的中间格式**，让所有适配器都归一化到它。
    下游的规则、diff、报告只认这套格式，不认任何厂商格式。

    加一个新格式 = 加一个适配器文件，**不用改核心逻辑一行**。
    这就是"面向接口编程"最实际的好处。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


# ============================================================
# 严重级别：决定这条告警能不能阻断合并
# ============================================================
SEVERITY_ORDER = {"info": 0, "warn": 1, "error": 2, "critical": 3}


def severity_rank(severity: str) -> int:
    return SEVERITY_ORDER.get(severity, 0)


# ============================================================
# 归一化后的轨迹结构
# ============================================================
@dataclass
class ToolResult:
    """一次工具调用的返回值。"""

    ok: bool = True
    result: Any = None
    error: str | None = None
    duration_ms: int | None = None

    def summary(self, max_length: int = 60) -> str:
        """压成一句人能看的话，用于终端表格。"""
        if not self.ok:
            return f"FAILED: {self.error or 'unknown error'}"
        if self.result is None:
            return "None"
        if isinstance(self.result, dict):
            parts = [f"{k}={v}" for k, v in list(self.result.items())[:3]]
            text = ", ".join(parts)
        else:
            text = str(self.result)
        return text if len(text) <= max_length else text[: max_length - 3] + "..."


@dataclass
class ToolCall:
    """一次工具调用，以及它的返回值（如果配对上了）。"""

    call_id: str | None
    tool: str | None
    #: 原始 arguments。协议规定它是【JSON 字符串】，但真实数据里可能是
    #: null、空串、甚至已经是字典 —— 三种情况都要能处理。
    raw_arguments: Any = None
    args: dict = field(default_factory=dict)
    result: ToolResult | None = None


@dataclass
class Step:
    """一步 = 一次 LLM 调用（可能同时发起多个工具调用）。"""

    index: int
    timestamp: str | None = None
    content: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    calls: list[ToolCall] = field(default_factory=list)

    @property
    def is_tool_round(self) -> bool:
        """这一步是不是在调工具（而不是在给最终回答）。"""
        return len(self.calls) > 0


@dataclass
class Trajectory:
    """一整条轨迹。"""

    path: str | None = None
    adapter: str = "unknown"
    steps: list[Step] = field(default_factory=list)
    #: 解析过程中遇到的坏行，不中断解析，只记录下来
    parse_warnings: list[str] = field(default_factory=list)

    # ---------- 派生属性 ----------
    @property
    def all_calls(self) -> list[tuple[int, ToolCall]]:
        """
        把所有工具调用按顺序拉平，返回 [(所属 step 的 index, ToolCall), ...]。

        为什么要拉平？因为"连续重复调用"这类规则关心的是**调用序列**，
        而调用可能分布在多个 step 里。拉平之后，规则实现会简单很多。
        """
        flat: list[tuple[int, ToolCall]] = []
        for step in self.steps:
            for call in step.calls:
                flat.append((step.index, call))
        return flat

    @property
    def call_count(self) -> int:
        return sum(len(step.calls) for step in self.steps)

    @property
    def total_prompt_tokens(self) -> int:
        return sum(step.prompt_tokens for step in self.steps)

    @property
    def total_completion_tokens(self) -> int:
        return sum(step.completion_tokens for step in self.steps)

    @property
    def final_answer(self) -> str | None:
        """最后一条"没有调工具、只有文字"的回答，就是最终答案。"""
        for step in reversed(self.steps):
            if step.content and not step.calls:
                return step.content
        return None

    def tool_call_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for _, call in self.all_calls:
            name = call.tool or "<unknown>"
            counts[name] = counts.get(name, 0) + 1
        return counts

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "adapter": self.adapter,
            "metrics": {
                "steps": len(self.steps),
                "calls": self.call_count,
                "tool_calls_by_name": self.tool_call_counts(),
                "error_results": sum(
                    1 for _, call in self.all_calls
                    if call.result is not None and not call.result.ok
                ),
                "total_prompt_tokens": self.total_prompt_tokens,
                "total_completion_tokens": self.total_completion_tokens,
            },
            "final_answer": self.final_answer,
            "parse_warnings": self.parse_warnings,
        }


# ============================================================
# 告警
# ============================================================
@dataclass
class Anomaly:
    """一条被检测出来的异常。"""

    rule: str
    severity: str
    message: str
    #: 第几次工具调用（从 0 开始），没有则 None
    call_index: int | None = None
    #: 第几步，没有则 None
    step: int | None = None
    tool: str | None = None
    #: 规则自己带的附加信息（比如重复次数、增长率）
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "rule": self.rule,
            "severity": self.severity,
            "message": self.message,
            "call_index": self.call_index,
            "step": self.step,
            "tool": self.tool,
            "detail": self.detail,
        }


@dataclass
class AnomalyReport:
    """一条轨迹的完整检查结果。"""

    trajectory: Trajectory
    anomalies: list[Anomaly] = field(default_factory=list)
    #: 已经开启的规则名，用于在报告里说明"检查了什么"
    enabled_rules: list[str] = field(default_factory=list)

    def counts_by_severity(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for anomaly in self.anomalies:
            counts[anomaly.severity] = counts.get(anomaly.severity, 0) + 1
        return counts

    def max_severity(self) -> str | None:
        if not self.anomalies:
            return None
        return max(self.anomalies, key=lambda a: severity_rank(a.severity)).severity

    def has_blocking_issue(self, threshold: str = "error") -> bool:
        """是否有达到或超过阈值的告警 —— 这就是 CI 门禁的判断依据。"""
        floor = severity_rank(threshold)
        return any(severity_rank(a.severity) >= floor for a in self.anomalies)

    def to_dict(self) -> dict:
        return {
            **self.trajectory.to_dict(),
            "enabled_rules": self.enabled_rules,
            "anomaly_summary": self.counts_by_severity(),
            "anomalies": [a.to_dict() for a in self.anomalies],
            "max_severity": self.max_severity(),
        }


# ============================================================
# 版本差异对比
# ============================================================
@dataclass
class CallDiff:
    """调用序列上的一处差异。"""

    index: int
    kind: str          # "changed" | "added" | "removed"
    base: str | None = None
    candidate: str | None = None

    def to_dict(self) -> dict:
        return {"index": self.index, "kind": self.kind,
                "base": self.base, "candidate": self.candidate}


@dataclass
class TrajectoryDiff:
    """两条轨迹的对比结果。"""

    base_path: str | None = None
    candidate_path: str | None = None
    #: 调用序列第一次出现分歧的位置（None = 没有分歧）
    first_divergence: CallDiff | None = None
    #: 后续所有分歧
    subsequent: list[CallDiff] = field(default_factory=list)
    #: 指标变化
    metric_deltas: dict[str, float] = field(default_factory=dict)

    @property
    def base_call_count(self) -> int:
        return int(self.metric_deltas.get("base_calls", 0))

    def to_dict(self) -> dict:
        return {
            "base_path": self.base_path,
            "candidate_path": self.candidate_path,
            "first_divergence": self.first_divergence.to_dict() if self.first_divergence else None,
            "subsequent": [d.to_dict() for d in self.subsequent],
            "metric_deltas": self.metric_deltas,
        }


def canonical_arguments(args: dict) -> str:
    """
    把参数字典压成一个稳定的字符串（"签名"）。

    ★ 这里有个容易搞错的地方，值得单独说：
      归一化要**保守**。我们只吸收"确定等价"的差异：
        - 字符串前后的空格（" A100 " → "A100"）
        - 数字的浮点写法（320.0 → "320"）
      但**不把大小写统一**，因为 "A100" 和 "a100" 在业务上
      可能是两个不同的订单号 —— 把它们当成一样，会漏报真实问题。

      "忽略大小写和细微差别"这件事，交给 arg_jitter_loop 那条规则去做，
      因为那条规则本来就是在找"看起来差不多但又不完全一样"的调用。
      两条规则用两种严格度，这是刻意的设计。
    """
    normalized = {key: _canonical_value(args[key]) for key in sorted(args)}
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))


def _canonical_value(value: Any) -> Any:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        # 320.0 和 320 应该被视为同一个值
        return str(int(value)) if value.is_integer() else str(value)
    if isinstance(value, str):
        return value.strip()
    return value


def call_signature(tool: str | None, args: dict) -> str:
    """一次工具调用的完整签名：工具名 + 参数。"""
    return f"{tool}|{canonical_arguments(args)}"
