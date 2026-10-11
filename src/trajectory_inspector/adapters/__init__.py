"""适配器注册表：加新格式只需要在这里多注册一个类。"""

from __future__ import annotations

from pathlib import Path

from .anthropic import AnthropicAdapter
from .base import BaseAdapter, OpenAIAdapter, parse_arguments

#: 顺序有意义：先注册的优先匹配
ADAPTERS: list[type[BaseAdapter]] = [OpenAIAdapter, AnthropicAdapter]

__all__ = [
    "ADAPTERS",
    "AnthropicAdapter",
    "BaseAdapter",
    "OpenAIAdapter",
    "parse_arguments",
    "load_trajectory",
]


def load_trajectory(path: str | Path) -> tuple:
    """
    读一个轨迹文件，自动识别格式并解析。

    返回 (Trajectory, warnings)。

    如果把所有适配器都试一遍还是不认识，抛 ValueError ——
    这是**唯一**应该抛异常的地方，因为在完全看不懂输入的情况下，
    继续往下跑只会产生误导性的结论。
    """
    records, warnings = BaseAdapter.load_records(path)

    if not records:
        raise ValueError(f"{path} 里没有任何可用记录（空文件或全部是坏行）")

    for adapter_cls in ADAPTERS:
        adapter = adapter_cls()
        if adapter.can_handle(records):
            trajectory = adapter.parse(records)
            trajectory.path = str(path)
            trajectory.parse_warnings = warnings
            return trajectory, warnings

    raise ValueError(
        f"{path} 的格式无法识别。目前支持："
        + "、".join(cls.name for cls in ADAPTERS)
        + "。如果你用的是别的格式，可以在 src/trajectory_inspector/adapters/ 下加一个适配器。"
    )
