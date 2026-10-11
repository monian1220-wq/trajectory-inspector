"""
Trajectory Inspector —— Agent 轨迹检查器。

一个**零外部依赖**的命令行工具：读取 Agent 运行轨迹，
自动检测异常行为，对比两个版本找出"第一次分歧在哪一步"。

快速开始见仓库根目录的 README.md。
"""

__version__ = "0.1.0"

from .adapters import load_trajectory
from .config import RulesConfig, load_rules
from .diff import diff_trajectories
from .models import Anomaly, AnomalyReport, Step, ToolCall, ToolResult, Trajectory
from .rules import run_rules

__all__ = [
    "__version__",
    "load_trajectory",
    "load_rules",
    "run_rules",
    "diff_trajectories",
    "RulesConfig",
    "Trajectory",
    "Step",
    "ToolCall",
    "ToolResult",
    "Anomaly",
    "AnomalyReport",
]
