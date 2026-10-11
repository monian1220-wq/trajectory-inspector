"""
版本对比：找出两条轨迹"从哪一步开始不一样"。

这是整个工具里最有诊断价值的命令。使用场景：
    你改了 Prompt / 换了模型 / 调了检索策略，想知道**具体哪一步行为变了**。
    如果没有它，你只能靠读几百行日志猜。

★ 核心难点：两条轨迹长度不同，不能逐行比对。
  所以我们先各自压成一个"调用签名序列"，再做序列对齐。

  这里用的是**简化版**算法：逐个位置比签名，第一个不同的就是分歧点。
  它能处理"同一位置换了工具/参数"，但处理不了"插入/删除一步"的情况 ——
  那种情况需要最长公共子序列（LCS）对齐，是这个项目第 3 周可以升级的地方。

  先跑通简化版，再优化 —— 这个顺序比反过来强得多。
"""

from __future__ import annotations

from .models import CallDiff, ToolCall, Trajectory, TrajectoryDiff, call_signature


def _signatures(trajectory: Trajectory) -> list[tuple[str, ToolCall]]:
    """把轨迹压成签名序列：[(签名, 原始调用), ...]。"""
    return [
        (call_signature(call.tool, call.args), call)
        for _, call in trajectory.all_calls
    ]


def diff_trajectories(base: Trajectory, candidate: Trajectory) -> TrajectoryDiff:
    """对比两条轨迹。"""
    base_signatures = _signatures(base)
    candidate_signatures = _signatures(candidate)

    diffs: list[CallDiff] = []
    common = min(len(base_signatures), len(candidate_signatures))
    for index in range(common):
        base_signature, _ = base_signatures[index]
        candidate_signature, _ = candidate_signatures[index]
        if base_signature != candidate_signature:
            diffs.append(CallDiff(
                index=index, kind="changed",
                base=base_signature, candidate=candidate_signature,
            ))

    tail = max(len(base_signatures), len(candidate_signatures))
    for index in range(common, tail):
        if index < len(base_signatures):
            diffs.append(CallDiff(index=index, kind="removed",
                                  base=base_signatures[index][0], candidate=None))
        else:
            diffs.append(CallDiff(index=index, kind="added",
                                  base=None, candidate=candidate_signatures[index][0]))

    first = diffs[0] if diffs else None
    deltas = {
        "steps": len(candidate.steps) - len(base.steps),
        "calls": candidate.call_count - base.call_count,
        "total_prompt_tokens": (
            candidate.total_prompt_tokens - base.total_prompt_tokens
        ),
        "error_results": (
            sum(1 for _, c in candidate.all_calls if c.result and not c.result.ok)
            - sum(1 for _, c in base.all_calls if c.result and not c.result.ok)
        ),
    }

    return TrajectoryDiff(
        base_path=base.path,
        candidate_path=candidate.path,
        first_divergence=first,
        subsequent=diffs[1:],
        metric_deltas=deltas,
    )
