"""把 CaseResult 汇总成报告：一份 markdown 表 + 一份原始 json。

设计 §7。两个产物分工不同：

- `evals/reports/latest.md`——**提交进库**。人读的表：按维度、按层、按 stop_reason
  出分布，附失败题的第一条失败原因。它是这套评测集对外的「产品」。
- `evals/reports/raw-*.json`——**gitignore**。机器相关的原始数字（耗时受本机负载
  影响，进库只会制造无意义的 diff）。留着是为了消融对比时能和另一次跑做差。

刻意不做的事：不算「总分」。32 道题的算术平均没有意义——刁难层跑红是设计里
允许的结果（§7「允许、甚至鼓励难看的数字」），把它摊进一个百分比反而藏住了
「哪一面弱」。表按维度和层出分布，弱在哪里一眼能看出来。
"""
from __future__ import annotations

import json
import platform
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from evals.runner import CaseResult

REPORTS_DIR = Path(__file__).parent / "reports"

# 维度代号 → 中文名，表里按这个顺序出行。与设计 §3 的七个能力面一致。
_DIM_LABELS: list[tuple[str, str]] = [
    ("tools", "D1 工具编排"),
    ("compact", "D2 上下文压缩"),
    ("memory", "D3 长期记忆"),
    ("convo", "D4 对话状态"),
    ("subagent", "D5 子 Agent 治理"),
    ("safety", "D6 安全与鉴权"),
    ("resilience", "D7 韧性"),
]

_LAYER_LABELS: list[tuple[str, str]] = [
    ("basic", "基础"),
    ("twist", "绕弯"),
    ("hard", "刁难"),
]


def _git_sha() -> str:
    """当前 commit 短 sha。数字要能追溯到代码版本，否则消融对比没有意义。"""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        return out.stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001 — 报告不该因为拿不到 sha 就失败
        return "unknown"


def _pct(n: int, total: int) -> str:
    if not total:
        return "—"
    return f"{n}/{total} ({100 * n / total:.0f}%)"


def _fmt_ms(ms: float) -> str:
    return f"{ms:.0f}ms" if ms < 1000 else f"{ms / 1000:.1f}s"


def _by_dimension(results: list[CaseResult]) -> str:
    """按能力面出一行：通过数、平均工具步数、平均耗时。

    「轨迹正确率」在本仓库就是**这道题的过程层断言全过**——工具名序列、参数、
    步数上界、无孤儿都在 expect 里，所以 ok 本身即轨迹正确，不再单列一个指标。
    """
    lines = [
        "| 能力面 | 通过 | 平均 tool 步数 | 平均耗时 |",
        "|---|---|---|---|",
    ]
    for key, label in _DIM_LABELS:
        rows = [r for r in results if r.case.dimension.value == key]
        if not rows:
            continue
        n_ok = sum(1 for r in rows if r.ok)
        avg_tools = sum(r.avg_tool_uses for r in rows) / len(rows)
        avg_ms = sum(r.avg_ms for r in rows) / len(rows)
        lines.append(
            f"| {label} | {_pct(n_ok, len(rows))} | {avg_tools:.1f} | {_fmt_ms(avg_ms)} |"
        )
    n_ok_all = sum(1 for r in results if r.ok)
    lines.append(f"| **合计** | **{_pct(n_ok_all, len(results))}** | | |")
    return "\n".join(lines)


def _by_layer(results: list[CaseResult]) -> str:
    """按 5:3:2 分层出一行。刁难层低是预期，不是 bug。"""
    lines = ["| 层 | 题数 | 通过 |", "|---|---|---|"]
    for key, label in _LAYER_LABELS:
        rows = [r for r in results if r.case.layer.value == key]
        if not rows:
            continue
        n_ok = sum(1 for r in rows if r.ok)
        lines.append(f"| {label} | {len(rows)} | {_pct(n_ok, len(rows))} |")
    return "\n".join(lines)


def _stop_reason_dist(results: list[CaseResult]) -> str:
    """stop_reason 分布——设计 §4 说这就是运行时评测的标签体系。"""
    counter: Counter[str] = Counter()
    for r in results:
        for a in r.attempts:
            counter[a.stop_reason or "(none)"] += 1
    if not counter:
        return "_（无数据）_"
    lines = ["| stop_reason | 次数 |", "|---|---|"]
    for reason, n in counter.most_common():
        lines.append(f"| `{reason}` | {n} |")
    return "\n".join(lines)


def _stability(results: list[CaseResult]) -> str:
    """只列 repeats>1 的题：并发/时序题的 5 次通过率。"""
    rows = [r for r in results if len(r.attempts) > 1]
    if not rows:
        return "_（本次没有 repeats>1 的题）_"
    lines = ["| 题 | 通过率 |", "|---|---|"]
    for r in rows:
        lines.append(
            f"| {r.case.id} {r.case.title} | {r.passed}/{len(r.attempts)}"
            f" ({100 * r.stability:.0f}%) |"
        )
    return "\n".join(lines)


def _failures(results: list[CaseResult]) -> str:
    """失败题清单。只出第一条失败原因：一道题的后续失败往往是同一个根因的回声。"""
    bad = [r for r in results if not r.ok]
    if not bad:
        return "_本次全绿。_"
    lines = []
    for r in bad:
        first = r.first_failures[0] if r.first_failures else "(无失败详情)"
        lines.append(f"- **{r.case.id}** {r.case.title}\n  - {first}")
    return "\n".join(lines)


def render_markdown(results: list[CaseResult], *, scope: str) -> str:
    """渲染人读的那份报告。scope 记下这次跑的范围（--all / --dimension x）。"""
    now = datetime.now(timezone.utc).astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
    return f"""# AgentGate 运行时评测报告

> 自动生成（`python -m evals.runner --all`）。评测对象是**运行时行为**，不是答案质量：
> provider 固定 Mock，所以「回复好不好」不在评测范围内；能被本仓库决定的是编排、
> 分批、落库、取消/引导/引用、闸门与优雅失败。边界见 `evals/README.md`。

- 跑的范围：`{scope}`
- 时间：{now}
- commit：`{_git_sha()}`
- 环境：{platform.system()} {platform.release()} / Python {platform.python_version()} / Mock provider + PG + Redis

## 按能力面

{_by_dimension(results)}

## 按分层

{_by_layer(results)}

刁难层通过率低是**设计允许的结果**，不是 CI 红：那一层收的就是取消中途、越权、
注入、闸门这类「优雅失败」题，其中一部分钉的是已知产品缺口。

设计（§2）的目标配比是 5:3:2（16/10/6）。实际题本偏向 twist/hard，如实报出而不是
回头给题重贴标签——分层是给「难度分布」看的，改标签能让配比好看，但会让「哪一层
真的在兜底」这件事失真。

## stop_reason 分布

{_stop_reason_dist(results)}

## 稳定性（repeats > 1）

{_stability(results)}

## 未通过的题

{_failures(results)}
"""


def write_reports(results: list[CaseResult], *, scope: str) -> tuple[Path, Path]:
    """写两份产物，返回 (markdown 路径, json 路径)。

    markdown 固定叫 latest.md（进库、可 diff）；json 带时间戳（不进库，留作对比）。
    """
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    md_path = REPORTS_DIR / "latest.md"
    md_path.write_text(render_markdown(results, scope=scope), encoding="utf-8")

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    json_path = REPORTS_DIR / f"raw-{stamp}.json"
    payload = {
        "scope": scope,
        "commit": _git_sha(),
        "generated_at": stamp,
        "cases": [
            {
                "id": r.case.id,
                "dimension": r.case.dimension.value,
                "layer": r.case.layer.value,
                "title": r.case.title,
                "ok": r.ok,
                "passed": r.passed,
                "repeats": len(r.attempts),
                "avg_tool_uses": r.avg_tool_uses,
                "avg_ms": r.avg_ms,
                "attempts": [
                    {
                        "ok": a.ok,
                        "http_status": a.http_status,
                        "stop_reason": a.stop_reason,
                        "tool_uses": a.tool_uses,
                        "elapsed_ms": a.elapsed_ms,
                        "usage": a.usage,
                        "failures": a.failures,
                    }
                    for a in r.attempts
                ],
            }
            for r in results
        ],
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return md_path, json_path
