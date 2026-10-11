"""
异常检测规则引擎。

设计原则（三条，每条都有理由）：
    1. **规则之间不互相依赖**：每条规则自己看自己，加一条新规则不会影响老的。
       这样才能放心地一条条加规则 —— 这是能持续演进的前提。
    2. **宁可少报，不要用重复告警淹没用户**：同一个问题只报一次。
       一个工具如果对同一个 bug 报 5 条告警，用户会直接把整个工具关掉。
    3. **每条告警都要能说清"为什么"**：message 里必须带具体数字和位置，
       因为这是给人排查用的，不是给机器分类用的。
"""

from __future__ import annotations

from .config import RulesConfig
from .models import (
    Anomaly,
    AnomalyReport,
    Trajectory,
    call_signature,
    severity_rank,
)

# ============================================================
# 参数相似度工具
# ============================================================
def levenshtein(a: str, b: str) -> int:
    """
    编辑距离：把 a 改成 b 最少需要几步。

    这是入门级动态规划，代码只有十来行。
    ★ 这个函数值得你逐行读懂 —— 它是"序列对齐"类问题的地基，
      第 3 周的版本 diff 也会用到同样的思想。
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    previous = list(range(len(b) + 1))
    for i, char_a in enumerate(a, start=1):
        current = [i]
        for j, char_b in enumerate(b, start=1):
            current.append(min(
                previous[j] + 1,                  # 删除
                current[j - 1] + 1,               # 插入
                previous[j - 1] + (char_a != char_b),   # 替换
            ))
        previous = current
    return previous[-1]


def _loose(value) -> str:
    """宽松归一化：忽略大小写、首尾空格、连续空格。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(int(value)) if float(value).is_integer() else str(value)
    return " ".join(str(value).strip().lower().split())


def _skeleton(value) -> str:
    """骨架归一化：只保留字母数字和中文，去掉所有标点。"""
    import re
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]", "", _loose(value))


def values_similar(left, right, threshold: float) -> bool:
    """判断两个参数值是否"看起来差不多"。"""
    text_left, text_right = _loose(left), _loose(right)
    if text_left == text_right:
        return True
    # 去掉标点后相等 → 同一个东西的不同写法（"A-100" 和 "A100"）
    skeleton_left, skeleton_right = _skeleton(left), _skeleton(right)
    if skeleton_left and skeleton_left == skeleton_right:
        return True
    longest = max(len(text_left), len(text_right))
    if longest == 0:
        return True
    return levenshtein(text_left, text_right) / longest >= threshold


def arguments_similar(left: dict, right: dict, threshold: float) -> bool:
    """
    判断两组参数是否"差不多"。

    只比较两边都有的键 —— 参数个数不同时不判定为微扰，
    因为那更可能是"这次多带了一个参数"，不算同一次调用。
    """
    shared = [key for key in left if key in right]
    if not shared:
        return False
    return all(values_similar(left[key], right[key], threshold) for key in shared)


# ============================================================
# 各条规则的实现
# ============================================================
def check_truncated_arguments(trajectory: Trajectory, rules: RulesConfig) -> list[Anomaly]:
    """规则：arguments 为 null —— 通常是模型输出被 max_tokens 截断。"""
    if not rules.truncated_arguments.enabled:
        return []
    out: list[Anomaly] = []
    for index, (step_index, call) in enumerate(trajectory.all_calls):
        if call.raw_arguments is None:
            out.append(Anomaly(
                rule="truncated_arguments",
                severity=rules.truncated_arguments.severity,
                message=f"工具 {call.tool} 的 arguments 为 null（可能被 max_tokens 截断）",
                call_index=index,
                step=step_index,
                tool=call.tool,
            ))
    return out


def check_repeat_call(trajectory: Trajectory, rules: RulesConfig) -> list[Anomaly]:
    """
    规则：连续多次"工具 + 参数完全相同"。

    这是最典型的"无进展循环" —— Agent 卡在原地反复做同一件事，
    每次都烧掉一整轮 token，却什么新信息都没得到。

    ★ 归一化用**严格**的 canonical（不改大小写）。
      因为 "A100" 和 "a100" 可能真的是两个单号，混为一谈会漏报真实问题。
      "看起来差不多"的判断交给 arg_jitter_loop 那条规则。
    """
    if not rules.repeat_call.enabled:
        return []
    calls = trajectory.all_calls
    threshold = rules.repeat_call.max_identical_repeat

    # 第一遍：找出所有"连续相同"的段
    groups: list[tuple[int, int]] = []
    run_start, run_length = 0, 0
    previous_signature = None
    for index, (_, call) in enumerate(calls):
        signature = call_signature(call.tool, call.args)
        if signature == previous_signature:
            run_length += 1
        else:
            if run_length >= threshold:
                groups.append((run_start, run_length))
            run_start, run_length = index, 1
        previous_signature = signature
    if run_length >= threshold:                 # ★ 别忘了结算最后一段
        groups.append((run_start, run_length))

    # 第二遍：把段变成告警
    out: list[Anomaly] = []
    for start, length in groups:
        step_index, call = calls[start]
        out.append(Anomaly(
            rule="repeat_call",
            severity=rules.repeat_call.severity,
            message=(
                f"工具 {call.tool} 连续 {length} 次以完全相同的参数调用"
                f"（无进展循环，额外消耗 {length - 1} 轮 token）"
            ),
            call_index=start,
            step=step_index,
            tool=call.tool,
            detail={
                "call_count": length,
                "indices": list(range(start, start + length)),
            },
        ))
    return out


def check_arg_jitter_loop(trajectory: Trajectory, rules: RulesConfig) -> list[Anomaly]:
    """
    规则：连续多次"同一个工具 + 参数只有细微差别"。

    为什么需要这条规则？因为 repeat_call 抓不到它 ——
    Agent 把 "A100" 改写成 "a100"、" A100 "、"A-100"，
    每次签名都不同，看起来像"在努力尝试"，其实是同一个死循环。
    这种循环更隐蔽，也更常见（模型会"换个说法再试一次"）。
    """
    if not rules.arg_jitter_loop.enabled:
        return []
    calls = trajectory.all_calls
    threshold = rules.arg_jitter_loop.runs

    # 先算出 repeat_call 报了哪些段，避免同一个问题报两次
    repeated = {
        (anomaly.call_index, anomaly.detail.get("call_count"))
        for anomaly in check_repeat_call(trajectory, rules)
    }

    out: list[Anomaly] = []
    start, length = 0, 1
    for index in range(1, len(calls) + 1):
        same = (
            index < len(calls)
            and calls[index][1].tool == calls[start][1].tool
            # ★ 跟"这一串的起点"比，不是跟"上一个"比。
            #   跟上一个比会产生链式误判：A~B 且 B~C 不代表 A~C。
            and arguments_similar(
                calls[index][1].args, calls[start][1].args,
                rules.arg_jitter_loop.arg_similarity,
            )
        )
        if same:
            length += 1
            continue
        if length >= threshold and (start, length) not in repeated:
            step_index, call = calls[start]
            out.append(Anomaly(
                rule="arg_jitter_loop",
                severity=rules.arg_jitter_loop.severity,
                message=(
                    f"工具 {call.tool} 连续 {length} 次调用的参数只有细微差别"
                    f"（疑似改写参数反复重试，同样是无进展循环）"
                ),
                call_index=start,
                step=step_index,
                tool=call.tool,
                detail={"call_count": length,
                        "indices": list(range(start, start + length))},
            ))
        start, length = index, 1
    return out


def _mentioned_failure(trajectory: Trajectory, after_step: int, keywords: list[str]) -> bool:
    """
    判断在 after_step 之后，Agent 有没有在"纯文字回答"里提到失败。

    为什么只看"纯文字回答"（没有工具调用的那一步）？
    因为中间步骤的 content 常常只是"我接下来要做什么"的思考过程，
    把它当成"已告知用户"会造成漏报。
    """
    for step in trajectory.steps:
        if step.index <= after_step or step.calls:
            continue
        text = (step.content or "").lower()
        if any(keyword in text for keyword in keywords):
            return True
    return False


def check_silent_tool_failure(trajectory: Trajectory, rules: RulesConfig) -> list[Anomaly]:
    """
    规则：工具失败了，但 Agent 既不重试、也不告诉用户。

    ★ 这条是所有规则里**最危险**的一条，也是我认为这个工具最有价值的检测项。
      无进展循环只是浪费钱；静默失败会让用户拿到错误结论 ——
      "我帮你查过了，可以退款"，而实际上那次查询超时失败了。

      如果失败的工具是"有副作用的"（退款、转账），直接升级为 critical：
      因为那意味着用户可能基于错误信息做了决定。

    判定过程（就是"把模糊直觉翻译成可执行规则"的过程）：
        ① 找到一个 ok=false 的工具结果
        ② 往后看：同一个工具有没有再被调用？（在重试 → 不算静默）
        ③ 再往后看：Agent 有没有在回答里提到失败？（告知了 → 不算静默）
        ④ 两条都没有 → 静默失败
    """
    if not rules.silent_tool_failure.enabled:
        return []
    calls = trajectory.all_calls
    side_effect_tools = set(rules.silent_tool_failure.side_effect_tools)

    out: list[Anomaly] = []
    for index, (step_index, call) in enumerate(calls):
        result = call.result
        if result is None or result.ok:
            continue

        retried = any(
            later_call.tool == call.tool
            for _, later_call in calls[index + 1:]
        )
        if retried:
            continue
        if _mentioned_failure(trajectory, step_index, rules.silent_keywords):
            continue

        is_side_effect = (call.tool or "") in side_effect_tools
        out.append(Anomaly(
            rule="silent_tool_failure",
            severity="critical" if is_side_effect else rules.silent_tool_failure.severity,
            message=(
                f"工具 {call.tool} 返回失败（{result.error or 'unknown'}）后"
                f"既未重试也未告知用户，Agent 直接给出了结论"
            ),
            call_index=index,
            step=step_index,
            tool=call.tool,
            detail={"error": result.error, "is_side_effect": is_side_effect},
        ))
    return out


def check_context_bloat(trajectory: Trajectory, rules: RulesConfig) -> list[Anomaly]:
    """
    规则：prompt_tokens 在某一段里持续暴涨。

    ★ 为什么判定"一段窗口的首尾比值"，而不是"单步倍数"？
      因为指数增长时单步倍数看着很温和（每步 1.5x），
      但 10 步之后总量就是 57 倍 —— 单步判定完全抓不到。
      实测：一个 1.5x/步 的轨迹，单步阈值设 2.0 永远不会触发。

      另外只报"最猛的那一段"，不做重复告警。
    """
    if not rules.context_bloat.enabled:
        return []
    points = [step.prompt_tokens for step in trajectory.steps]
    minimum = rules.context_bloat.min_steps
    worst: tuple[int, int, float] | None = None

    for start in range(len(points) - minimum + 1):
        for end in range(start + minimum, len(points) + 1):
            base = points[start] or 1
            ratio = points[end - 1] / base
            if ratio >= rules.context_bloat.growth_factor:
                if worst is None or ratio > worst[2]:
                    worst = (start, end, ratio)

    if worst is None:
        return []
    start, end, ratio = worst
    return [Anomaly(
        rule="context_bloat",
        severity=rules.context_bloat.severity,
        message=(
            f"prompt_tokens 从第 {trajectory.steps[start].index} 步的 {points[start]} "
            f"增长到第 {trajectory.steps[end - 1].index} 步的 {points[end - 1]}"
            f"（{ratio:.1f}x），上下文正在失控"
        ),
        step=trajectory.steps[start].index,
        detail={
            "end_step": trajectory.steps[end - 1].index,
            "start_tokens": points[start],
            "end_tokens": points[end - 1],
            "growth": round(ratio, 2),
        },
    )]


def check_step_explosion(trajectory: Trajectory, rules: RulesConfig) -> list[Anomaly]:
    """规则：总步数超过阈值 —— 任务可能陷入长尾，或根本没收敛。"""
    if not rules.step_explosion.enabled:
        return []
    total = len(trajectory.steps)
    if total <= rules.step_explosion.max_steps:
        return []
    return [Anomaly(
        rule="step_explosion",
        severity=rules.step_explosion.severity,
        message=f"轨迹共 {total} 步，超过阈值 {rules.step_explosion.max_steps}",
        detail={"step_count": total},
    )]


def check_parallel_fanout(trajectory: Trajectory, rules: RulesConfig) -> list[Anomaly]:
    """规则：某一步一次性发起过多工具调用（通常是模型没想清楚就开始乱调）。"""
    if not rules.parallel_fanout.enabled:
        return []
    out: list[Anomaly] = []
    for step in trajectory.steps:
        count = len(step.calls)
        if count > rules.parallel_fanout.max_parallel_calls:
            out.append(Anomaly(
                rule="parallel_fanout",
                severity=rules.parallel_fanout.severity,
                message=(
                    f"第 {step.index} 步一次性发起 {count} 个工具调用，"
                    f"超过阈值 {rules.parallel_fanout.max_parallel_calls}"
                ),
                step=step.index,
                detail={"call_count": count},
            ))
    return out


def check_redundant_parallel_call(trajectory: Trajectory, rules: RulesConfig) -> list[Anomaly]:
    """
    规则：同一步内发起了参数完全相同的重复调用。

    这个场景很真实：模型在一步里并行发两个一样的请求（比如把同一个订单查两遍）。
    它比"跨步重复"更浪费，因为两个请求是同时打出去的，纯粹白花。
    """
    if not rules.redundant_parallel_call.enabled:
        return []
    out: list[Anomaly] = []
    for step in trajectory.steps:
        if len(step.calls) < 2:
            continue
        seen: dict[str, int] = {}
        for position, call in enumerate(step.calls):
            key = call_signature(call.tool, call.args)
            if key in seen:
                out.append(Anomaly(
                    rule="redundant_parallel_call",
                    severity=rules.redundant_parallel_call.severity,
                    message=(
                        f"第 {step.index} 步内 {call.tool} 被重复发起"
                        f"（与同一步内第 {seen[key]} 个调用参数完全相同）"
                    ),
                    step=step.index,
                    tool=call.tool,
                    detail={"duplicate_of": seen[key]},
                ))
            else:
                seen[key] = position
    return out


# ============================================================
# 规则注册表
# ============================================================
#: 加一条新规则 = 写一个函数 + 在这里注册一行。核心逻辑不用动。
ALL_RULES = [
    ("truncated_arguments", check_truncated_arguments),
    ("repeat_call", check_repeat_call),
    ("arg_jitter_loop", check_arg_jitter_loop),
    ("silent_tool_failure", check_silent_tool_failure),
    ("context_bloat", check_context_bloat),
    ("step_explosion", check_step_explosion),
    ("parallel_fanout", check_parallel_fanout),
    ("redundant_parallel_call", check_redundant_parallel_call),
]


def run_rules(trajectory: Trajectory, rules: RulesConfig | None = None) -> AnomalyReport:
    """跑一遍所有规则，返回报告。"""
    config = rules or RulesConfig()
    anomalies: list[Anomaly] = []
    enabled: list[str] = []

    for name, checker in ALL_RULES:
        rule_config = getattr(config, name, None)
        if rule_config is not None and not getattr(rule_config, "enabled", True):
            continue
        enabled.append(name)
        anomalies.extend(checker(trajectory, config))

    # 按严重级别排序，让人一眼看到最该处理的
    anomalies.sort(key=lambda a: (-severity_rank(a.severity),
                                  a.call_index if a.call_index is not None else 10 ** 6,
                                  a.step if a.step is not None else 10 ** 6))
    return AnomalyReport(trajectory=trajectory, anomalies=anomalies, enabled_rules=enabled)
