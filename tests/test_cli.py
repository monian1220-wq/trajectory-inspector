"""
CLI 与 diff 的端到端测试。

从这里开始，测的就不是"某个函数对不对"，而是"作为一个工具，它用起来对不对"：
    - 退出码对不对（CI 门禁全靠它）
    - 输出里有没有关键信息
    - 两条轨迹的分歧定位准不准

★ 为什么退出码这么重要？
  因为 GitHub Actions 判断"这一步成不成功"的唯一依据就是退出码。
  退出码约定错了，门禁就是个摆设。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trajectory_inspector.adapters import load_trajectory
from trajectory_inspector.cli import EXIT_BLOCKED, EXIT_INPUT_ERROR, EXIT_OK, main
from trajectory_inspector.diff import diff_trajectories

TRAJECTORY_DIR = Path(__file__).parent / "fixtures" / "trajectories"


def fixture(name: str) -> Path:
    return TRAJECTORY_DIR / f"{name}.jsonl"


# ============================================================
# parse 命令
# ============================================================
class TestParseCommand:
    def test_prints_step_table(self, capsys):
        code = main(["parse", str(fixture("01_repeat_loop"))])
        output = capsys.readouterr().out

        assert code == EXIT_OK
        assert "Step" in output and "Tool" in output
        assert "get_order" in output
        assert "最终回答" in output

    def test_json_output_is_valid(self, capsys):
        code = main(["parse", str(fixture("05_healthy")), "--json"])
        payload = json.loads(capsys.readouterr().out)

        assert code == EXIT_OK
        assert payload["adapter"] == "openai"
        assert payload["metrics"]["steps"] == 3
        assert payload["metrics"]["calls"] == 3
        assert payload["metrics"]["tool_calls_by_name"] == {
            "get_order": 1, "check_refund_policy": 1, "refund_order": 1,
        }
        assert payload["final_answer"].startswith("Refund of 200 CNY completed")


# ============================================================
# check 命令：退出码约定（CI 门禁的基础）
# ============================================================
class TestCheckCommand:
    def test_healthy_trajectory_exits_zero(self, capsys):
        code = main(["check", str(fixture("05_healthy"))])
        output = capsys.readouterr().out

        assert code == EXIT_OK
        assert "没有发现异常" in output

    def test_error_level_anomaly_exits_one(self, capsys):
        """有 ERROR 级异常 → 退出码 1 → CI 变红。"""
        code = main(["check", str(fixture("01_repeat_loop"))])
        output = capsys.readouterr().out

        assert code == EXIT_BLOCKED
        assert "repeat_call" in output
        assert "退出码 1" in output

    def test_warn_only_does_not_block_at_error_threshold(self, capsys):
        """
        ★ 分级的意义就在这里：只有 warn 级问题时不该阻断合并。
          如果什么都阻断，团队会习惯性忽略红灯，门禁就失效了。
        """
        code = main(["check", str(fixture("04_context_bloat"))])
        output = capsys.readouterr().out

        assert code == EXIT_OK, "只有 warn 级异常时不该阻断"
        assert "context_bloat" in output

    def test_warn_blocks_when_threshold_lowered(self):
        code = main(["check", str(fixture("04_context_bloat")), "--fail-on", "warn"])
        assert code == EXIT_BLOCKED

    def test_markdown_output_has_table(self, capsys):
        main(["check", str(fixture("03_silent_failure")), "--markdown"])
        output = capsys.readouterr().out

        assert "## Agent 轨迹检查报告" in output
        assert "| 级别 | 规则 |" in output
        assert "silent_tool_failure" in output

    def test_json_output_exposes_severity_summary(self, capsys):
        main(["check", str(fixture("03_silent_failure")), "--json"])
        payload = json.loads(capsys.readouterr().out)

        assert payload["max_severity"] == "error"
        assert payload["anomaly_summary"] == {"error": 1}
        assert payload["anomalies"][0]["tool"] == "check_refund_policy"


# ============================================================
# 输入错误处理
# ============================================================
class TestInputErrors:
    def test_missing_file_exits_two(self, capsys):
        """
        文件不存在时，CLI 约定是**抛出 SystemExit(2)** 而不是 return 2。

        这是有意的（见 cli.py 的 _load）：在真实终端里，
        SystemExit 会让 python 进程以退出码 2 结束；而在单元测试里
        可以直接断言它，比"看返回值"更接近真实行为。
        """
        with pytest.raises(SystemExit) as excinfo:
            main(["check", "no_such_file.jsonl"])

        assert excinfo.value.code == EXIT_INPUT_ERROR
        assert "找不到文件" in capsys.readouterr().err

    def test_unrecognized_format_exits_two(self, tmp_path, capsys):
        path = tmp_path / "weird.jsonl"
        path.write_text('{"hello": "world"}\n', encoding="utf-8")

        with pytest.raises(SystemExit) as excinfo:
            main(["check", str(path)])

        assert excinfo.value.code == EXIT_INPUT_ERROR
        assert "格式无法识别" in capsys.readouterr().err


# ============================================================
# diff 命令
# ============================================================
class TestDiffCommand:
    def test_first_divergence_is_located(self):
        base, _ = load_trajectory(fixture("05_healthy"))
        candidate, _ = load_trajectory(fixture("01_repeat_loop"))
        result = diff_trajectories(base, candidate)

        assert result.first_divergence is not None
        assert result.first_divergence.index == 1
        assert result.first_divergence.base.startswith("check_refund_policy")
        assert result.first_divergence.candidate.startswith("get_order")

    def test_same_trajectory_has_no_divergence(self):
        base, _ = load_trajectory(fixture("05_healthy"))
        same, _ = load_trajectory(fixture("05_healthy"))
        result = diff_trajectories(base, same)

        assert result.first_divergence is None
        assert result.subsequent == []
        assert result.metric_deltas["calls"] == 0

    def test_metric_deltas_are_signed(self):
        """指标变化要能看出方向 —— 这是判断"改好了还是改坏了"的依据。"""
        base, _ = load_trajectory(fixture("05_healthy"))
        candidate, _ = load_trajectory(fixture("04_context_bloat"))
        result = diff_trajectories(base, candidate)

        assert result.metric_deltas["calls"] > 0
        assert result.metric_deltas["steps"] > 0
        assert result.metric_deltas["total_prompt_tokens"] > 0

    def test_cli_diff_exits_zero_even_with_divergence(self, capsys):
        """
        ★ 设计决定：diff 命令不因为有分歧就返回失败。

        为什么？因为"有分歧"是你改了 Prompt 的**预期结果**，
        不是错误。用退出码表达分歧，会让 CI 天天红，
        然后所有人就都不看红灯了。
        """
        code = main(["diff", str(fixture("05_healthy")), str(fixture("01_repeat_loop"))])
        output = capsys.readouterr().out

        assert code == EXIT_OK
        assert "第一次分歧" in output

    def test_cli_diff_reports_identical_sequences(self, capsys):
        main(["diff", str(fixture("05_healthy")), str(fixture("05_healthy"))])
        assert "完全一致" in capsys.readouterr().out


# ============================================================
# report 命令
# ============================================================
class TestReportCommand:
    def test_writes_markdown_file(self, tmp_path):
        target = tmp_path / "report.md"
        code = main(["report", str(fixture("01_repeat_loop")),
                     "--format", "markdown", "--output", str(target)])

        assert code == EXIT_OK
        content = target.read_text(encoding="utf-8")
        assert "Agent 轨迹检查报告" in content
        assert "repeat_call" in content

    def test_writes_json_file(self, tmp_path):
        target = tmp_path / "report.json"
        main(["report", str(fixture("01_repeat_loop")),
              "--format", "json", "--output", str(target)])

        payload = json.loads(target.read_text(encoding="utf-8"))
        assert payload["anomalies"][0]["rule"] == "repeat_call"


# ============================================================
# 规则配置可切换
# ============================================================
class TestRulesFilePath:
    def test_custom_rules_file_changes_result(self, tmp_path, capsys):
        """把 repeat_call 关掉之后，同一个文件就不该再报这条规则。"""
        rules_path = tmp_path / "rules.yaml"
        rules_path.write_text(
            "repeat_call:\n  enabled: false\n", encoding="utf-8"
        )
        code = main(["--rules", str(rules_path), "check", str(fixture("01_repeat_loop"))])
        output = capsys.readouterr().out

        assert code == EXIT_OK, "关掉唯一的报错规则后就不该阻断了"
        assert "repeat_call" not in output

    def test_missing_rules_file_falls_back_to_defaults(self, capsys):
        """配置文件不存在时用默认值，不报错 —— clone 下来就能跑。"""
        code = main(["--rules", "definitely/not/here.yaml", "check", str(fixture("05_healthy"))])
        assert code == EXIT_OK
