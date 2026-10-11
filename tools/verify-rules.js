// 用法：node tools/verify-rules.js
//
// 作用有两个：
//   1. 用一份**独立实现**（JavaScript）复现 Python 侧的检测规则，
//      把每个 fixture 应该检出什么算出来 —— 这是交叉验证。
//      两份独立实现得出同样结论，比"自己写期望值自己测"可信得多。
//   2. 把结果写进 tests/fixtures/expected.json，供 Python 测试对照。
//
// 注意：本文件里的 RULES 常量必须和 src/trajectory_inspector/config.py 的默认值一致。
//       tests/test_fixtures.py::test_python_default_rules_match_expected_rules
//       会在两边跑偏时立刻失败 —— 那就是提醒你该重新跑这个脚本了。
const fs = require("fs");
const path = require("path");

const projectRoot = path.join(__dirname, "..");
const dir = path.join(projectRoot, "tests", "fixtures", "trajectories");
const FILES = fs.readdirSync(dir).filter(f => f.endsWith(".jsonl")).sort();

// ---------- 与 Python 侧保持一致的常量 ----------
const RULES = {
  repeat_call: { max_identical_repeat: 2, severity: "error" },
  arg_jitter_loop: { runs: 4, arg_similarity: 0.7, severity: "error" },
  silent_tool_failure: {
    side_effect_tools: ["refund_order", "transfer_money", "delete_record", "send_email"],
    severity: "error",
  },
  context_bloat: { growth_factor: 3.0, min_steps: 4, severity: "warn" },
  step_explosion: { max_steps: 15, severity: "warn" },
  parallel_fanout: { max_parallel_calls: 3, severity: "info" },
  redundant_parallel_call: { severity: "warn" },
  truncated_arguments: { severity: "error" },
};
const SILENT_KEYWORDS = ["sorry", "failed", "error", "unable", "retry", "timeout",
                         "抱歉", "失败", "错误", "重试", "超时"];

// ---------- 解析：与 adapters/openai.py 同样的逻辑 ----------
function parseRecords(records) {
  const steps = [];
  const resultsById = new Map();
  for (const r of records) {
    if (r.type === "tool_result" && r.call_id) resultsById.set(r.call_id, r);
  }
  for (const r of records) {
    if (r.type !== "llm_response") continue;
    const msg = r.message || {};
    const tcs = msg.tool_calls || [];
    const step = {
      index: r.step,
      timestamp: r.timestamp || null,
      prompt_tokens: (r.usage || {}).prompt_tokens || 0,
      completion_tokens: (r.usage || {}).completion_tokens || 0,
      content: msg.content || null,
      calls: [],
    };
    for (const tc of tcs) {
      const fn = tc.function || {};
      const res = resultsById.get(tc.id) || null;
      step.calls.push({
        call_id: tc.id,
        tool: fn.name,
        raw_arguments: fn.arguments === undefined ? null : fn.arguments,
        args: parseArgs(fn.arguments),
        result: res,
      });
    }
    steps.push(step);
  }
  return { steps, resultsById };
}

function parseArgs(raw) {
  if (raw === null || raw === undefined || raw === "") return {};
  if (typeof raw === "object") return raw;
  try { return JSON.parse(raw); } catch (e) { return {}; }
}

function allCalls(traj) {
  const out = [];
  for (const s of traj.steps) for (const c of s.calls) out.push({ step: s.index, call: c });
  return out;
}

// ---------- 规则实现（与 Python 侧逐行对应）----------
// canonical：严格归一化，只吸收"确定等价"的差异（空格、浮点写法），不改大小写。
//            因为 "A100" 和 "a100" 在业务上可能真的是两个不同的单号。
function canonicalValue(v) {
  if (typeof v === "string") return v.trim();
  if (typeof v === "number") return v === Math.trunc(v) ? String(Math.trunc(v)) : String(v);
  if (typeof v === "boolean") return String(v);
  return v;
}
function canonical(args) {
  const out = {};
  for (const k of Object.keys(args).sort()) out[k] = canonicalValue(args[k]);
  return JSON.stringify(out);
}

// valueSimilar：用于"参数微扰"判定，这里才允许忽略大小写和细微差别。
function loose(v) {
  if (typeof v === "number") return canonicalValue(v);
  return String(v).trim().toLowerCase().replace(/\s+/g, " ");
}
// 更激进的归一化：去掉所有非字母数字字符。用于识别 "A-100" / "A100" / " A100 " 是同一个东西。
function skeleton(v) {
  return loose(v).replace(/[^a-z0-9\u4e00-\u9fa5]/g, "");
}
function valueSimilar(a, b) {
  const s1 = loose(a), s2 = loose(b);
  if (s1 === s2) return true;
  // 去掉标点后相等 → 判定为同一参数的不同写法
  const k1 = skeleton(a), k2 = skeleton(b);
  if (k1 !== "" && k1 === k2) return true;
  const longer = Math.max(s1.length, s2.length);
  if (longer === 0) return true;
  return lev(s1, s2) / longer >= RULES.arg_jitter_loop.arg_similarity;
}
function lev(a, b) {
  const m = a.length, n = b.length;
  let prev = Array.from({ length: n + 1 }, (_, j) => j);
  for (let i = 1; i <= m; i++) {
    const cur = [i];
    for (let j = 1; j <= n; j++) {
      cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a[i - 1] === b[j - 1] ? 0 : 1));
    }
    prev = cur;
  }
  return prev[n];
}
function argsSimilar(x, y) {
  const keys = Object.keys(x).filter(k => k in y);
  if (keys.length === 0) return false;
  return keys.every(k => valueSimilar(x[k], y[k]));
}

function runRules(traj) {
  const anomalies = [];
  const add = (rule, severity, message, loc = {}) =>
    anomalies.push({ rule, severity, message, ...loc });
  const calls = allCalls(traj);

  // 1. 截断参数
  calls.forEach(({ step, call }, idx) => {
    if (call.raw_arguments === null) {
      add("truncated_arguments", RULES.truncated_arguments.severity,
        `工具 ${call.tool} 的 arguments 为 null（可能被截断）`, { call_index: idx, step });
    }
  });

  // 2. 重复调用
  let runStart = 0, runLen = 0, prev = null;
  const groups = [];
  calls.forEach(({ call }, idx) => {
    const sig = call.tool + "|" + canonical(call.args);
    if (sig === prev) runLen++;
    else { if (runLen >= RULES.repeat_call.max_identical_repeat) groups.push([runStart, runLen]); runStart = idx; runLen = 1; }
    prev = sig;
  });
  if (runLen >= RULES.repeat_call.max_identical_repeat) groups.push([runStart, runLen]);
  for (const [start, len] of groups) {
    const tool = calls[start].call.tool;
    add("repeat_call", RULES.repeat_call.severity,
      `工具 ${tool} 连续 ${len} 次以完全相同的参数调用（无进展循环）`,
      { call_index: start, call_count: len, tool, indices: Array.from({ length: len }, (_, k) => start + k) });
  }

  // 3. 参数微扰循环：连续多次调用"同一个工具 + 参数只是细微差别"
  //    用"与这一串起点比较"而不是"与上一次比较"，避免链式误判。
  let jStart = 0, jLen = 1;
  for (let i = 1; i <= calls.length; i++) {
    const same = i < calls.length
      && calls[i].call.tool === calls[jStart].call.tool
      && argsSimilar(calls[i].call.args, calls[jStart].call.args);
    if (same) { jLen++; continue; }
    // 如果这一串恰好是完全相同的调用，说明 repeat_call 已经报过了，不重复报
    const alreadyReported = groups.some(([s, l]) => s === jStart && l === jLen);
    if (jLen >= RULES.arg_jitter_loop.runs && !alreadyReported) {
      add("arg_jitter_loop", RULES.arg_jitter_loop.severity,
        `工具 ${calls[jStart].call.tool} 连续 ${jLen} 次调用的参数只有细微差别（疑似改写参数反复重试）`,
        { call_index: jStart, call_count: jLen, tool: calls[jStart].call.tool,
          indices: Array.from({ length: jLen }, (_, k) => jStart + k) });
    }
    jStart = i; jLen = 1;
  }

  // 4. 静默失败
  calls.forEach(({ step, call }, idx) => {
    const res = call.result;
    if (!res || res.ok !== false) return;
    const later = calls.slice(idx + 1);
    const retried = later.some(l => l.call.tool === call.tool);
    // 提及判定：失败之后是否有"纯文字回答"提到了失败
    let said = false;
    for (const s of traj.steps) {
      if (s.index <= step) continue;
      if (s.calls.length > 0) continue;
      const text = (s.content || "").toLowerCase();
      if (SILENT_KEYWORDS.some(k => text.includes(k))) { said = true; break; }
    }
    if (retried || said) return;
    const isSideEffect = RULES.silent_tool_failure.side_effect_tools.includes(call.tool);
    add("silent_tool_failure", isSideEffect ? "critical" : RULES.silent_tool_failure.severity,
      `工具 ${call.tool} 返回失败（${res.error || "unknown"}）后既未重试也未告知用户`,
      { call_index: idx, step, tool: call.tool, is_side_effect: isSideEffect });
  });

  // 5. 上下文膨胀：找出"整条轨迹里累积增长最猛的一段"，只报一次。
  //    只看单步倍数会漏掉指数增长（每步 1.5x 时单步永远不超阈值），
  //    所以判定标准是：一段 >=4 步里，首尾总量涨了 >=3 倍。
  //    同一个问题的多个窗口只报一次 —— 宁可少报，不要用重复告警淹没用户。
  const pts = traj.steps.map(s => s.prompt_tokens);
  let worst = null;
  for (let start = 0; start + RULES.context_bloat.min_steps <= pts.length; start++) {
    for (let end = start + RULES.context_bloat.min_steps; end <= pts.length; end++) {
      const ratio = pts[end - 1] / (pts[start] || 1);
      if (ratio >= RULES.context_bloat.growth_factor && (!worst || ratio > worst.ratio)) {
        worst = { start, end, ratio };
      }
    }
  }
  if (worst) {
    add("context_bloat", RULES.context_bloat.severity,
      `prompt_tokens 从第 ${traj.steps[worst.start].index} 步的 ${pts[worst.start]} `
      + `增长到第 ${traj.steps[worst.end - 1].index} 步的 ${pts[worst.end - 1]}`
      + `（${worst.ratio.toFixed(1)}x）`,
      { step: traj.steps[worst.start].index, end_step: traj.steps[worst.end - 1].index,
        start_tokens: pts[worst.start], end_tokens: pts[worst.end - 1],
        growth: Number(worst.ratio.toFixed(2)) });
  }

  // 6. 步数失控
  if (traj.steps.length > RULES.step_explosion.max_steps) {
    add("step_explosion", RULES.step_explosion.severity,
      `轨迹共 ${traj.steps.length} 步，超过阈值 ${RULES.step_explosion.max_steps}`,
      { step_count: traj.steps.length });
  }

  // 7. 并行扇出
  const byStep = new Map();
  for (const c of calls) byStep.set(c.step, (byStep.get(c.step) || 0) + 1);
  for (const [step, n] of byStep) {
    if (n > RULES.parallel_fanout.max_parallel_calls) {
      add("parallel_fanout", RULES.parallel_fanout.severity,
        `第 ${step} 步一次性发起 ${n} 个工具调用，超过阈值 ${RULES.parallel_fanout.max_parallel_calls}`,
        { step, call_count: n });
    }
  }

  // 8. 同一步内冗余并行调用
  for (const s of traj.steps) {
    if (s.calls.length < 2) continue;
    const seen = new Map();
    for (let i = 0; i < s.calls.length; i++) {
      const c = s.calls[i];
      const key = c.tool + "|" + canonical(c.args);
      if (seen.has(key)) {
        add("redundant_parallel_call", RULES.redundant_parallel_call.severity,
          `第 ${s.index} 步内 ${c.tool} 被重复发起（与同一步内第 ${seen.get(key)} 个调用参数完全相同）`,
          { step: s.index, tool: c.tool });
      } else seen.set(key, i);
    }
  }

  return anomalies;
}

// ---------- 差异对比 ----------
function firstDivergence(a, b) {
  const sa = allCalls(a).map(x => x.call.tool + "|" + canonical(x.call.args));
  const sb = allCalls(b).map(x => x.call.tool + "|" + canonical(x.call.args));
  const n = Math.min(sa.length, sb.length);
  for (let i = 0; i < n; i++) if (sa[i] !== sb[i]) return { index: i, a: sa[i], b: sb[i] };
  if (sa.length !== sb.length) return { index: n, a: sa[n] ?? null, b: sb[n] ?? null };
  return null;
}

// ---------- 跑全部 fixture ----------
const expected = { rules: RULES, fixtures: {} };
const summary = [];

for (const f of FILES) {
  const name = f.replace(/\.jsonl$/, "");
  const raw = fs.readFileSync(path.join(dir, f), "utf8");
  const records = raw.replace(/\r\n/g, "\n").split("\n").filter(l => l.trim()).map(l => JSON.parse(l));
  const traj = parseRecords(records);
  traj.call_count = traj.steps.reduce((a, s) => a + s.calls.length, 0);
  traj.total_prompt_tokens = traj.steps.reduce((a, s) => a + s.prompt_tokens, 0);
  traj.total_completion_tokens = traj.steps.reduce((a, s) => a + s.completion_tokens, 0);
  const anomalies = runRules(traj);
  const metrics = {
    steps: traj.steps.length,
    calls: traj.call_count,
    tool_calls_by_name: {},
    error_results: allCalls(traj).filter(x => x.call.result && x.call.result.ok === false).length,
    total_prompt_tokens: traj.total_prompt_tokens,
    total_completion_tokens: traj.total_completion_tokens,
  };
  for (const { call } of allCalls(traj)) {
    metrics.tool_calls_by_name[call.tool] = (metrics.tool_calls_by_name[call.tool] || 0) + 1;
  }

  expected.fixtures[name] = {
    record_count: records.length,
    metrics,
    anomalies: anomalies.map(a => ({ rule: a.rule, severity: a.severity })),
    anomaly_detail: anomalies,
  };

  summary.push({ name, steps: metrics.steps, calls: metrics.calls,
                 anomalies: anomalies.map(a => `${a.severity}:${a.rule}`).join(", ") || "(无)" });
}

// 差异对比的固定用例：healthy(旧版) vs repeat_loop，期望在第 1 次调用就分歧
{
  const load = n => parseRecords(fs.readFileSync(path.join(dir, n + ".jsonl"), "utf8")
    .replace(/\r\n/g, "\n").split("\n").filter(l => l.trim()).map(l => JSON.parse(l)));
  const healthy = load("05_healthy");
  const repeat = load("01_repeat_loop");
  const bloated = load("04_context_bloat");
  expected.diff_cases = [
    { name: "healthy_vs_repeat", base: "05_healthy", candidate: "01_repeat_loop",
      divergence: firstDivergence(healthy, repeat) },
    { name: "bloat_vs_healthy", base: "04_context_bloat", candidate: "05_healthy",
      divergence: firstDivergence(bloated, healthy) },
    { name: "self_vs_self", base: "05_healthy", candidate: "05_healthy",
      divergence: firstDivergence(healthy, healthy) },
  ];
}

// 末尾补一个换行：这是文本文件的通用约定（POSIX 标准），
// 也让 git diff 和编辑器不会显示"文件末尾缺换行"的警告。
fs.writeFileSync(path.join(projectRoot, "tests", "fixtures", "expected.json"),
  JSON.stringify(expected, null, 2) + "\n", "utf8");

console.log("fixture 检出结果：");
for (const s of summary) {
  console.log(`  ${s.name.padEnd(20)} ${String(s.steps).padStart(2)} 步 / ${String(s.calls).padStart(2)} 调用  ->  ${s.anomalies}`);
}
console.log("\n差异对比用例：");
for (const c of expected.diff_cases) {
  console.log(`  ${c.name.padEnd(20)} ${JSON.stringify(c.divergence)}`);
}
console.log("\n已写出 tests/fixtures/expected.json");
