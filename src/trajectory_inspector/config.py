"""
规则阈值配置。

★ 重要：这里的默认值不是随手写的，是拿 tests/fixtures 里 5 条真实轨迹调出来的。
   调整阈值前请先跑 pytest —— 改完要保证 05_healthy 依然零告警。

为什么把阈值放在配置文件而不是写死在代码里？
因为"什么算异常"是业务判断，会随场景变化（客服 Agent 和代码 Agent 的合理步数完全不同）。
外置之后，换场景不用改代码。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from pathlib import Path

import yaml

DEFAULT_RULES_PATH = Path("config/rules.yaml")


@dataclass
class RepeatCallRule:
    enabled: bool = True
    #: 连续多少次"工具+参数完全相同"才算循环（2 = 出现第二次就算）
    max_identical_repeat: int = 2
    severity: str = "error"


@dataclass
class ArgJitterLoopRule:
    enabled: bool = True
    #: 连续多少次"参数只有细微差别"才算循环
    runs: int = 4
    #: 参数相似度阈值（0~1）。注意短字符串很敏感：
    #  "A100" vs "A-100" 的编辑距离相似度只有 0.75，所以阈值不能设太高。
    arg_similarity: float = 0.7
    severity: str = "error"


@dataclass
class SilentToolFailureRule:
    enabled: bool = True
    #: 一旦这些工具失败后被静默忽略，直接升级为 critical
    side_effect_tools: list[str] = field(
        default_factory=lambda: ["refund_order", "transfer_money", "delete_record", "send_email"]
    )
    severity: str = "error"


@dataclass
class ContextBloatRule:
    enabled: bool = True
    #: 一段 >= min_steps 的窗口里，prompt_tokens 首尾比值超过这个数就告警
    growth_factor: float = 3.0
    min_steps: int = 4
    severity: str = "warn"


@dataclass
class StepExplosionRule:
    enabled: bool = True
    max_steps: int = 15
    severity: str = "warn"


@dataclass
class ParallelFanoutRule:
    enabled: bool = True
    max_parallel_calls: int = 3
    severity: str = "info"


@dataclass
class RedundantParallelCallRule:
    enabled: bool = True
    severity: str = "warn"


@dataclass
class TruncatedArgumentsRule:
    enabled: bool = True
    severity: str = "error"


@dataclass
class RulesConfig:
    repeat_call: RepeatCallRule = field(default_factory=RepeatCallRule)
    arg_jitter_loop: ArgJitterLoopRule = field(default_factory=ArgJitterLoopRule)
    silent_tool_failure: SilentToolFailureRule = field(default_factory=SilentToolFailureRule)
    context_bloat: ContextBloatRule = field(default_factory=ContextBloatRule)
    step_explosion: StepExplosionRule = field(default_factory=StepExplosionRule)
    parallel_fanout: ParallelFanoutRule = field(default_factory=ParallelFanoutRule)
    redundant_parallel_call: RedundantParallelCallRule = field(
        default_factory=RedundantParallelCallRule
    )
    truncated_arguments: TruncatedArgumentsRule = field(
        default_factory=TruncatedArgumentsRule
    )
    #: 判断"Agent 是否告知用户失败了"的关键词表
    silent_keywords: list[str] = field(
        default_factory=lambda: [
            "sorry", "failed", "error", "unable", "retry", "timeout",
            "抱歉", "失败", "错误", "重试", "超时",
        ]
    )

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "RulesConfig":
        """
        从字典构造配置。只认识的键会被使用，多余的键直接忽略 ——
        这样配置文件里可以写注释性的额外字段，不会让程序崩掉。
        """
        config = cls()
        for f in fields(cls):
            if f.name not in data:
                continue
            value = data[f.name]
            current = getattr(config, f.name)
            if hasattr(current, "__dataclass_fields__"):      # 嵌套的规则配置
                sub = type(current)()
                for sf in fields(type(current)):
                    if isinstance(value, dict) and sf.name in value:
                        setattr(sub, sf.name, value[sf.name])
                setattr(config, f.name, sub)
            else:
                setattr(config, f.name, value)
        return config


def load_rules(path: str | Path | None = None) -> RulesConfig:
    """
    读取规则配置。

    找不到文件时**不报错**，直接用默认值 —— 这样用户 clone 下来就能跑，
    不需要先准备配置文件。
    """
    target = Path(path) if path else DEFAULT_RULES_PATH
    if not target.exists():
        return RulesConfig()
    with target.open("r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle) or {}
    return RulesConfig.from_dict(data)
