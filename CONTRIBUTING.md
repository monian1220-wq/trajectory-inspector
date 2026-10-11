# 参与贡献

谢谢你有兴趣！这个项目还很小，**任何一条反馈都比一个 star 更有价值**。

---

## 最有价值的四种贡献（按价值排序）

### 1. 报告"误报" ⭐⭐⭐⭐⭐

**检测工具最大的失败不是漏报，而是误报。** 如果正常轨迹也被报一堆问题，用户两天之内就会把工具关掉。

所以如果你拿自己的正常轨迹跑出了告警，请务必告诉我。[开一个误报 issue](../../issues/new?template=false_positive.yml) 就行，模板会引导你说清"为什么我认为这是正常的"。

### 2. 分享你遇到的坏轨迹 ⭐⭐⭐⭐⭐

这个项目现在只有 5 条**构造出来的**测试轨迹，它们代表不了真实世界的复杂度。

如果你手上有真实的失败案例（脱敏后），贴出来会非常有帮助 —— 它可以变成一条新的测试用例，让工具对真实数据更可靠。

### 3. 新增一个适配器 ⭐⭐⭐⭐

目前只支持 OpenAI 和 Anthropic 两种格式。如果你用的框架（LangChain / AutoGen / Dify / 自研）导出的轨迹格式不一样，**加一个适配器只要一个文件**：

```python
# src/trajectory_inspector/adapters/your_format.py
from .base import BaseAdapter, parse_arguments
from ..models import Step, ToolCall, ToolResult, Trajectory

class YourFormatAdapter(BaseAdapter):
    name = "your_format"

    def can_handle(self, records: list[dict]) -> bool:
        """判断这批记录是不是我认识的格式。"""
        ...

    def parse(self, records: list[dict]) -> Trajectory:
        """翻译成统一的中间格式。★ 这里不要做任何异常判断，那是 rules 层的事。"""
        ...
```

然后在 `adapters/__init__.py` 的 `ADAPTERS` 列表里注册一行，再补一个测试就行。

**核心逻辑一行都不用改** —— 这就是当初把适配层单独拆出来的目的。

### 4. 新增一条检测规则 ⭐⭐⭐

见下面的详细步骤。

---

## 开发环境

```bash
git clone https://github.com/monian1220-wq/trajectory-inspector.git
cd trajectory-inspector

python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux

pip install -e ".[dev]"
python -m pytest
```

运行时依赖只有 PyYAML —— 这是刻意的设计，为了让任何人 clone 下来就能跑，也为了让 CI 足够快。

---

## 新增一条检测规则的步骤

1. **先在 `tests/fixtures/trajectories/` 里造一条会触发它的轨迹**
   （或者修改 `tools/gen-fixtures.js` 重新生成）。
   ★ 顺序很重要：**先有"应该检出什么"，再写实现。** 反了就变成"照着实现写测试"。

2. **在 `config.py` 里加一个规则配置 dataclass**，并在 `config/rules.yaml` 里写上默认值和注释
   —— 阈值必须可配置，不能写死在代码里。

3. **在 `rules.py` 里实现判定函数**，签名固定为：

   ```python
   def check_your_rule(trajectory: Trajectory, rules: RulesConfig) -> list[Anomaly]:
       ...
   ```

   三条约定：
   - **规则之间互不依赖**（不要读别的规则的结果，唯一例外是 `arg_jitter_loop` 会去重 `repeat_call` 已报的段）
   - **message 里必须带具体数字和位置** —— 这是给人排查用的，不是给机器分类用的
   - **同一个问题只报一次**，不做重复告警

4. **在 `ALL_RULES` 里注册一行。**

5. **补测试**：既要测"能检出"，**更要测"不误报"**。
   参考 `tests/test_rules.py`，那里大部分用例都在防误报。

6. **跑一遍全量检查**：

   ```bash
   python -m pytest
   node tools/verify-rules.js      # 需要 Node.js，重新生成 expected.json
   python -m pytest                # 再跑一次，确认对照通过
   ```

   ★ **`05_healthy` 必须零告警。** 这是第一条底线，破了要说明理由。

---

## 提交规范

提交信息用 `类型(范围): 说明` 的格式：

```
feat(rules): 新增 parallel_fanout 规则，检测单步发起过多工具调用
fix(render): 修正结果列过长导致表格撑破终端宽度
test(adapters): 补充 arguments 被截断的解析用例
docs: README 补充发布检查清单
```

常见的类型：`feat` / `fix` / `test` / `docs` / `refactor` / `chore` / `ci` / `tune`。

**范围**填模块名：`rules` / `adapters` / `render` / `cli` / `diff` / `config`。

---

## 一些约定

| 约定 | 原因 |
|:---|:---|
| 文本文件用 **LF 行尾、末尾留空行** | 有 `.editorconfig` 管着，别让 diff 里全是噪音 |
| 配置文件和 `.gitignore` 尽量用 **纯 ASCII 注释** | 跨编码环境下中文注释有被写坏的风险（真发生过） |
| 终端输出里**不要用中文对齐** | 中文占两个字符宽，而 Python 按一个算，必然错位 |
| 新增依赖要慎重 | 运行时依赖只有 PyYAML 是卖点之一，加依赖要有充分理由 |
| 主动写"已知局限" | 说明边界比假装完备更专业 |

---

## 关于新手

**这个项目对新手友好，欢迎第一次贡献。**

`tests/test_rules.py` 和 `rules.py` 里的注释写得比较细，是刻意为你准备的。
如果哪里看不懂，**直接开 issue 问** —— "这里我没看懂"本身就是文档需要改进的信号，
比默默放弃有价值得多。

如果需要帮助，也可以在 issue 里说明你是新手，我会尽量给更详细的指引。
