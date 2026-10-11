# Trajectory Inspector

> **Agent 轨迹检查器**：读一条 Agent 的运行轨迹，自动检出**无进展循环**、**静默失败**、**上下文膨胀**，
> 并对比两个版本找出**"第一次分歧出现在哪一步"**。
>
> 不依赖任何 LLM API、不需要向量库、零外部服务 —— clone 下来立刻能跑。

![python](https://img.shields.io/badge/python-3.10%2B-blue)
![deps](https://img.shields.io/badge/runtime%20deps-PyYAML%20only-brightgreen)
![rules](https://img.shields.io/badge/检测规则-8%20条-informational)
![fixtures](https://img.shields.io/badge/测试轨迹-5%20条-informational)
[![trajectory-check](https://github.com/monian1220-wq/trajectory-inspector/actions/workflows/check.yml/badge.svg)](https://github.com/monian1220-wq/trajectory-inspector/actions/workflows/check.yml)
![license](https://img.shields.io/badge/license-MIT-green)

---

## 这个工具解决什么问题

Agent 出错的时候，你面对的是几百行日志，靠肉眼找"哪一步开始不对"。更要命的是**有些错误长得不像错误**：

```jsonl
// Agent 回复"请提供订单号"，但它在同一轮里已经调用了退款工具
{"type":"llm_response","message":{"content":"请提供订单号","tool_calls":[{"function":{"name":"refund_order",...}}]}}
```

这种"嘴上说的和实际做的不一致"，只看最终回答永远发现不了 —— 最终回答是对的，用户也不会投诉。
但它已经在后台执行了一次不可撤销的操作。

**这个工具把这类问题变成可自动检测、可阻断发布的结论。**

### 它检测的三类问题，以及为什么优先级不同

| 级别 | 规则 | 问题性质 | 为什么要单独分级 |
|:---|:---|:---|:---|
| `critical` | `silent_tool_failure`（有副作用的工具） | 用户拿到错误结论，可能造成真实资损 | 必须阻断发布 |
| `error` | `repeat_call` / `arg_jitter_loop` / `truncated_arguments` | 浪费钱和延迟，或行为已经不对 | 阻断发布 |
| `warn` | `context_bloat` / `step_explosion` / `redundant_parallel_call` | 效率问题，任务可能仍然完成 | 只告警，不阻断 |
| `info` | `parallel_fanout` | 值得看一眼的现象 | 不阻断 |

> ★ 分级的真正意义：**如果什么都阻断，团队会习惯性忽略红灯，门禁就失效了。**
> 只有"会造成真实损失"的问题才配得上阻断合并。

---

## 实测效果

5 条测试轨迹，每条都埋了**一种**已知故障（`05_healthy` 是对照组，必须零告警）：

| 轨迹 | 规模 | 检出结果 |
|:---|:---|:---|
| `01_repeat_loop` | 4 步 / 3 次调用 | `[ERR] repeat_call` — 工具 get_order 连续 **3** 次以完全相同的参数调用 |
| `02_arg_jitter_loop` | 5 步 / 4 次调用 | `[ERR] arg_jitter_loop` — 参数被反复改写成 4 个变体（A100 / a100 / " A100 " / A-100） |
| `03_silent_failure` | 2 步 / 1 次调用 | `[ERR] silent_tool_failure` — check_refund_policy 超时失败后既未重试也未告知用户 |
| `04_context_bloat` | 17 步 / 16 次调用 | `[WARN] context_bloat` — prompt_tokens 从 800 涨到 525473（**656.8x**）<br>`[WARN] step_explosion` — 共 17 步超过阈值 15 |
| `05_healthy` | 3 步 / 3 次调用 | **（无异常）** |

★ 注意 `02_arg_jitter_loop`：它**不会**触发 `repeat_call`，因为四次调用的参数签名各不相同。
这正是需要两条独立规则的原因 —— 只做"签名比对"会漏掉这种最常见的循环形态。

真实输出样例：

```
$ python -m trajectory_inspector check tests/fixtures/trajectories/01_repeat_loop.jsonl
轨迹：tests/fixtures/trajectories/01_repeat_loop.jsonl    格式：openai
概况：4 步 / 3 次工具调用 / prompt_tokens 合计 3850
检查结果：发现 1 处异常（[ERR ] 1 条）

  [ERR ] repeat_call  (step 0, call #0)
        工具 get_order 连续 3 次以完全相同的参数调用（无进展循环，额外消耗 2 轮 token）

结论：存在 ERROR 及以上级别的异常 —— 在 CI 里会阻断合并。
退出码 1（因为存在 >= error 级别的异常，CI 会判定失败）
```

---

## 快速开始

```bash
git clone https://github.com/monian1220-wq/trajectory-inspector.git
cd trajectory-inspector

# 建议用虚拟环境
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

pip install -e ".[dev]"
```

### 四条命令

```bash
# 1. 看时间线：把轨迹渲染成一张表
python -m trajectory_inspector parse tests/fixtures/trajectories/01_repeat_loop.jsonl --stats

# 2. 查异常：CI 里用这条，有 error 级异常时退出码为 1
python -m trajectory_inspector check tests/fixtures/trajectories/01_repeat_loop.jsonl

# 3. 比版本：找出"第一次分歧在哪一步"
python -m trajectory_inspector diff \
  tests/fixtures/trajectories/05_healthy.jsonl \
  tests/fixtures/trajectories/01_repeat_loop.jsonl

# 4. 出报告：Markdown 适合直接贴进 PR 评论
python -m trajectory_inspector report tests/fixtures/trajectories/03_silent_failure.jsonl \
  --format markdown --output reports/silent.md
```

装好之后也可以用短命令 `trajectory-inspector`（由 `pyproject.toml` 的 `console_scripts` 注册）。

### diff 的输出长这样

```
$ python -m trajectory_inspector diff 05_healthy.jsonl 01_repeat_loop.jsonl
基线：tests/fixtures/trajectories/05_healthy.jsonl
候选：tests/fixtures/trajectories/01_repeat_loop.jsonl

第一次分歧出现在第 1 次工具调用：
  基线：check_refund_policy|{"order_id":"A100"}
  候选：get_order|{"order_id":"A100"}

指标变化：
  calls               +1
  steps               +1
  total_prompt_tokens +1990
```

> 这就是它最有诊断价值的地方：**你改了 Prompt / 换了模型之后，不用读几百行日志，
> 直接看"哪一步的行为第一次不一样了"。**

---

## 检测规则

| 规则 | 检测什么 | 默认阈值 |
|:---|:---|:---|
| `truncated_arguments` | `arguments` 为 null（通常被 max_tokens 截断） | — |
| `repeat_call` | 连续 N 次"工具 + 参数完全相同" | 2 次 |
| `arg_jitter_loop` | 连续 N 次"参数只有细微差别" | 4 次，相似度 ≥ 0.7 |
| `silent_tool_failure` | 工具失败后既不重试也不告知用户 | 副作用工具升级为 critical |
| `context_bloat` | 一段窗口内 `prompt_tokens` 累积暴涨 | ≥4 步内涨 3 倍 |
| `step_explosion` | 总步数超阈值 | 15 步 |
| `parallel_fanout` | 单步发起过多并行调用 | 3 个 |
| `redundant_parallel_call` | 同一步内重复发起相同调用 | — |

阈值全部可在 [`config/rules.yaml`](config/rules.yaml) 里改，**不用动代码**。

### 三个不为直觉所动的设计决定

**1. `repeat_call` 的归一化是保守的，`arg_jitter_loop` 才是宽松的**

`repeat_call` 只吸收"确定等价"的差异（首尾空格、`320.0` 与 `320`），**故意不统一大小写** ——
因为 `A100` 和 `a100` 在业务上可能是两个不同的订单号，混为一谈会漏报真实问题。
而"忽略大小写和细微差别"这件事交给 `arg_jitter_loop`，因为那条规则本来就是在找
"看起来差不多但又不完全一样"的调用。**两条规则用两种严格度，是刻意的分工。**

**2. 上下文膨胀看"窗口累积倍数"，不看"单步倍数"**

指数增长时单步倍数看着很温和（每步 1.5x），但 10 步之后总量是 57 倍 —— 单步判定完全抓不到。
实测：阈值设成 `单步 2.0x` 时，一条每步涨 1.5 倍的轨迹永远不会被检出。

**3. 同一个问题只报一次**

`context_bloat` 只报"最猛的那一段"，不做重复告警。
因为一个 bug 报 5 条告警，用户会直接把整个工具关掉 —— **检测工具最大的失败不是漏报，是狼来了。**

---

## 架构

```
                      ┌─────────────────────┐
   trajectory.jsonl ──▶│  Adapters 适配层     │  多种格式 → 统一中间格式
                      │  openai / anthropic  │
                      └──────────┬──────────┘
                                 ▼
                      ┌─────────────────────┐
                      │  models.Trajectory   │  Step → ToolCall → ToolResult
                      │  （归一化的中间格式） │
                      └──────────┬──────────┘
                    ┌────────────┴────────────┐
                    ▼                         ▼
          ┌───────────────────┐     ┌──────────────────┐
          │  rules 规则引擎    │     │  diff 版本对比    │
          │  8 条独立规则      │     │  序列对齐找分歧点  │
          └─────────┬─────────┘     └────────┬─────────┘
                    └────────────┬───────────┘
                                 ▼
                      ┌─────────────────────┐
                      │  render 渲染层       │  table / markdown / json
                      └──────────┬──────────┘
                                 ▼
                      ┌─────────────────────┐
                      │  cli → 退出码        │ → CI 门禁
                      └─────────────────────┘
```

### 三个分层决定，每个都有理由

**适配层隔离格式差异**：不同厂商的轨迹格式完全不一样（OpenAI 把工具调用放在 `message.tool_calls`，
Anthropic 放在 `content` 数组的 block 里，参数一个是 JSON 字符串一个是字典）。
我们定义一套自己的中间格式，让所有适配器归一化到它。
**加一个新格式 = 加一个适配器文件，核心逻辑一行不用改。**

**规则之间互不依赖**：每条规则自己看自己（`check_xxx(trajectory, rules)`），加新规则不影响老的。
这样才能放心地一条条加规则 —— 这是工具能持续演进的前提。

**渲染层与逻辑层分离**：同一份 `AnomalyReport` 可以输出成终端表格、Markdown、JSON 三种形态，
逻辑层不需要知道谁会消费它。

---

## 项目结构

```
trajectory-inspector/
├── src/trajectory_inspector/
│   ├── models.py            # 中间格式：Step / ToolCall / ToolResult / Trajectory / Anomaly
│   ├── config.py            # 规则阈值（可从 YAML 覆盖）
│   ├── adapters/
│   │   ├── base.py          # 适配器基类 + OpenAI 风格实现 + arguments 安全解析
│   │   └── anthropic.py     # Anthropic 风格实现
│   ├── rules.py             # 8 条检测规则 + 编辑距离
│   ├── diff.py              # 版本对比，定位首次分歧
│   ├── render.py            # 表格 / Markdown 渲染
│   └── cli.py               # 命令行入口与退出码约定
├── config/rules.yaml        # 阈值配置（可调，不用改代码）
├── tests/
│   ├── fixtures/
│   │   ├── trajectories/    # 5 条测试轨迹，每条埋一种已知故障
│   │   └── expected.json    # 由独立实现生成的期望结果
│   ├── test_fixtures.py     # 端到端：文件 → 检出结果
│   ├── test_adapters.py     # 解析层：脏数据、并行调用、多格式
│   ├── test_rules.py        # 规则边界（重点测"不误报"）
│   └── test_cli.py          # 退出码、输出格式、diff
├── tools/
│   ├── gen-fixtures.js      # 重新生成测试轨迹
│   └── verify-rules.js      # 独立实现交叉验证 + 生成 expected.json
└── .github/workflows/check.yml
```

---

## 关于 expected.json：一次交叉验证

`tests/fixtures/expected.json` 不是手写的，也不是 Python 生成的 ——
它来自 [`tools/verify-rules.js`](tools/verify-rules.js)，一份用 JavaScript
**独立实现**的等价检测逻辑。

为什么值得这样做？因为"自己写期望值、自己测自己"等于没测。
两份独立实现在同一批数据上得出同样的结论，可信度高得多。
而且它会先于 Python 代码存在 —— **先确定"应该检出什么"，再写实现**，顺序反了就很容易
变成"照着实现写测试"。

`tests/test_fixtures.py::test_python_default_rules_match_expected_rules`
会在两边的阈值悄悄跑偏时立刻失败，提醒你重新生成。

```bash
# 改了规则或加了 fixture 之后：
node tools/gen-fixtures.js      # 需要 Node.js，仅用于生成数据
node tools/verify-rules.js
python -m pytest
```

---

## 开发

```bash
# ★ 先确认你站在项目目录里：dir（Windows）或 ls 应该能看到 README.md、src、tests
python -m pytest
```

跑测试时如果看到一大堆 `PermissionError: 拒绝访问` 和几万个"收集到的测试"，
**说明你在用户主目录里运行了 pytest**（它会去扫描整个用户目录）。
切换目录再跑就好 —— 见本文件顶部的「快速开始」。

```bash
python -m pytest -v                      # 想看每个用例的名字
python -m pytest tests/test_rules.py     # 只跑规则测试
python -m pytest -k "healthy"            # 只跑名字里带 healthy 的
```

装好之后还可以用短命令：

```bash
trajectory-inspector check tests/fixtures/trajectories/01_repeat_loop.jsonl
```

### 推送前的两件事

```bash
# 1. 把 README/CONTRIBUTING 里的 monian1220-wq 换成你的 GitHub 用户名（只需做一次）
python setup.py 你的GitHub用户名
python setup.py --check          # 自查还有没有残留

# 2. 全量测试
python -m pytest
```

贡献方式见 [CONTRIBUTING.md](CONTRIBUTING.md)。

---

## CI 门禁

[`.github/workflows/check.yml`](.github/workflows/check.yml) 会在每次 push / PR 时：

1. 跑全部单元测试与 fixture 对照
2. 对每条示例轨迹执行 `check --fail-on error`
3. 上传 Markdown 报告作为构建产物

**退出码约定**（CI 门禁的全部基础）：

| 退出码 | 含义 |
|:---|:---|
| `0` | 没有达到阈值的异常 |
| `1` | 检出了 `--fail-on` 级别及以上的异常 → 阻断合并 |
| `2` | 输入有问题（文件不存在 / 格式不认识） |

> ★ 为什么 `diff` 命令**不**因为有分歧而返回 1？
> 因为"有分歧"是你改 Prompt 的**预期结果**，不是错误。
> 用退出码表达分歧会让 CI 天天变红，然后所有人就都不看红灯了。

---

## 已知局限 / 下一步

主动写清楚边界，比假装完备更专业：

- **diff 用的是简化对齐**：逐个位置比签名，能定位"同一位置换了工具/参数"，
  但处理不了"插入/删除一步"造成的错位。**下一步升级为最长公共子序列（LCS）对齐。**
- **"无进展"的判定还很粗糙**：目前只看"签名相同/相似"。
  真正的"无进展"应该看**状态有没有变化**（同样的工具用不同参数反复查也可能毫无进展）。
  这需要一个"状态指纹"的概念 —— 是下一个大版本的方向。
- **静默失败的"已告知"判定靠关键词表**：表不全、没考虑多语言转述、
  也没考虑"用户压根没看懂"的情况。更稳的做法是用 LLM 判定，但那会引入成本和不确定性，
  所以当前选择"确定性关键词 + 明确记录误判边界"。
- **只有两个适配器**：OpenAI 与 Anthropic 风格。LangChain / AutoGen 的导出格式还没支持。
- **没有真实线上轨迹的验证**：5 条 fixture 都是构造的。
  真实轨迹的形态更脏、更长，规则的阈值需要用真实数据重新校准。

---

## License

MIT

---

## 开发与发布检查清单

改完代码、准备推送之前，照着过一遍：

- [ ] `python -m pytest` 全绿（尤其确认 `test_healthy_trajectory_has_no_anomalies` 通过）
- [ ] 改了阈值的话，`node tools/verify-rules.js` 重新生成 `expected.json`
- [ ] 5 条示例轨迹的检出结果符合预期（`check` 一遍）
- [ ] 没有提交任何密钥（`git diff --cached` 扫一眼）
- [ ] 提交信息按主题拆开，不用一个 `update` 打发
- [ ] 文档里的命令和输出与实际一致（README 里的示例输出最容易过期）
