// 用法：node tools/gen-fixtures.js
// 作用：重新生成 tests/fixtures/trajectories/*.jsonl（5 条测试轨迹）
// ★ 重新生成后请接着跑一次 node tools/verify-rules.js，同步更新 expected.json
const fs = require("fs");
const path = require("path");

const projectRoot = path.join(__dirname, "..");
const outDir = path.join(projectRoot, "tests", "fixtures", "trajectories");
fs.mkdirSync(outDir, { recursive: true });

let clock = 0;
const nextTime = () => `2025-01-01T00:00:${String(clock++).padStart(2, "0")}Z`;

// 构造工具调用 + 结果的辅助函数
function call(id, name, argsObj) {
  return {
    call: {
      id,
      type: "function",
      function: { name, arguments: JSON.stringify(argsObj) },
    },
    argsObj,
  };
}

// 一个 step = 一次 LLM 调用
function step({ content = null, calls = [], results = [], usage }) {
  const time = nextTime();
  const out = [];
  out.push({
    type: "llm_response",
    timestamp: time,
    step: step.counter++,
    usage,
    message: {
      role: "assistant",
      content,
      tool_calls: calls.length
        ? calls.map(c => c.call)
        : null,
    },
  });
  for (let i = 0; i < calls.length; i++) {
    const r = results[i] || {};
    out.push({
      type: "tool_result",
      timestamp: time,
      call_id: calls[i].call.id,
      tool: calls[i].call.function.name,
      ok: r.ok !== false,
      duration_ms: r.duration_ms ?? 120,
      error: r.error ?? null,
      result: r.result ?? null,
    });
  }
  return out;
}

function makeTrajectory() {
  step.counter = 0;
  clock = 0;
  return [];
}

function write(name, records, meta) {
  const file = path.join(outDir, `${name}.jsonl`);
  const body = records.map(r => JSON.stringify(r)).join("\n") + "\n";
  fs.writeFileSync(file, body, "utf8");
  const steps = records.filter(r => r.type === "llm_response").length;
  const calls = records.filter(r => r.type === "tool_result").length;
  console.log(`${name}.jsonl  ${String(records.length).padStart(2)} 条记录 / ${steps} 步 / ${calls} 次工具调用`);
  console.log(`    期望异常：${meta}`);
  return file;
}

// ============================================================
// fixture 1：重复调用循环（连续 3 次完全相同的调用）
// ============================================================
{
  const recs = makeTrajectory();
  for (let i = 0; i < 3; i++) {
    recs.push(...step({
      calls: [call(`c${i + 1}`, "get_order", { order_id: "A100" })],
      results: [{ result: { order_id: "A100", status: "paid", days_since_purchase: 22, amount: 320 } }],
      usage: { prompt_tokens: 820 + i * 90, completion_tokens: 45 },
    }));
  }
  recs.push(...step({
    content: "Your order is past the 15-day refund window, so it cannot be refunded automatically.",
    usage: { prompt_tokens: 1120, completion_tokens: 38 },
  }));
  write("01_repeat_loop", recs, "REPEAT_CALL x1（下标 [0,1,2]）");
}

// ============================================================
// fixture 2：参数微扰循环（order_id 相同但被反复改写格式）
// ============================================================
{
  const recs = makeTrajectory();
  const variants = [
    { order_id: "A100" },
    { order_id: "a100" },
    { order_id: " A100 " },
    { order_id: "A-100" },
  ];
  variants.forEach((v, i) => {
    recs.push(...step({
      calls: [call(`c${i + 1}`, "get_order", v)],
      results: [{ result: { order_id: "A100", status: "paid", days_since_purchase: 9, amount: 150 } }],
      usage: { prompt_tokens: 700 + i * 70, completion_tokens: 40 },
    }));
  });
  recs.push(...step({
    content: "I still cannot find that order. Please double-check the order number.",
    usage: { prompt_tokens: 1010, completion_tokens: 28 },
  }));
  write("02_arg_jitter_loop", recs, "ARG_JITTER x1（4 个变体，签名不同所以 REPEAT 抓不到）");
}

// ============================================================
// fixture 3：静默失败（工具报错，不重试也不告知）
// ============================================================
{
  const recs = makeTrajectory();
  recs.push(...step({
    content: "Let me check the policy.",
    calls: [call("c1", "check_refund_policy", { order_id: "B200" })],
    results: [{ ok: false, error: "upstream timeout", result: null, duration_ms: 30000 }],
    usage: { prompt_tokens: 600, completion_tokens: 35 },
  }));
  recs.push(...step({
    content: "Your order B200 is eligible for a refund. I have processed it.",
    usage: { prompt_tokens: 720, completion_tokens: 30 },
  }));
  write("03_silent_failure", recs, "SILENT_TOOL_FAILURE x1（工具 check_refund_policy 失败后被忽略）");
}

// ============================================================
// fixture 4：上下文膨胀 + 步数失控
// ============================================================
{
  const recs = makeTrajectory();
  const tools = ["search_policy", "get_order", "check_refund_policy"];
  for (let i = 0; i < 16; i++) {
    const tool = tools[i % tools.length];
    // prompt_tokens 指数增长：800, 1200, 1800, 2700, 4050, 6075, ...
    const prompt = Math.round(800 * Math.pow(1.5, i));
    recs.push(...step({
      calls: [call(`c${i + 1}`, tool, { order_id: `A${100 + i}`, query: `issue ${i}` })],
      results: [{ result: { status: "ok", note: `result ${i}` }, duration_ms: 100 + i }],
      usage: { prompt_tokens: prompt, completion_tokens: 40 },
    }));
  }
  recs.push(...step({
    content: "I could not fully resolve this. Please contact support.",
    usage: { prompt_tokens: Math.round(800 * Math.pow(1.5, 16)), completion_tokens: 25 },
  }));
  const total = recs.filter(r => r.type === "llm_response")
    .reduce((a, r) => a + r.usage.prompt_tokens, 0);
  write("04_context_bloat", recs, `CONTEXT_BLOAT x1（prompt_tokens 从 800 涨到 ${Math.round(800 * Math.pow(1.5, 16))}）+ 总 token ${total}`);
}

// ============================================================
// fixture 5：健康轨迹（对照组，必须零告警）
// ============================================================
{
  const recs = makeTrajectory();
  recs.push(...step({
    content: "I need the order details first.",
    calls: [
      call("c1", "get_order", { order_id: "A100" }),
      call("c2", "check_refund_policy", { order_id: "A100" }),
    ],
    results: [
      { result: { order_id: "A100", status: "paid", days_since_purchase: 7, amount: 200 } },
      { result: { eligible: true, reason: "within 15 days" } },
    ],
    usage: { prompt_tokens: 500, completion_tokens: 60 },
  }));
  recs.push(...step({
    content: "Your order is eligible. Proceeding with the refund.",
    calls: [call("c3", "refund_order", { order_id: "A100", idempotency_key: "k-001" })],
    results: [{ result: { refunded: true, amount: 200 }, duration_ms: 240 }],
    usage: { prompt_tokens: 640, completion_tokens: 42 },
  }));
  recs.push(...step({
    content: "Refund of 200 CNY completed. It will appear in 1-3 business days.",
    usage: { prompt_tokens: 720, completion_tokens: 28 },
  }));
  write("05_healthy", recs, "无（对照组，期望零 ERROR / 零 WARN）");
}

console.log("\n生成完毕。");
