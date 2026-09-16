"""消融对比：同一批题在几个「真实旋钮」下各跑一遍，出一张对比表（设计 §7）。

方案列都是**本仓库能开关的真实配置**，不是虚构的「加了重排」：

| 方案 | 怎么切 | 想证明什么 |
|---|---|---|
| A 基线 | 默认 settings + Mock | 主指标水位 |
| B 关子 Agent | `SUBAGENT_ENABLED=false` | D5 题应变为明确拒绝，D1 不受影响 |
| C 压缩更激进 | `EVAL_FORCE_COMPACT_THRESHOLD=400` | D2 触发率上升；D1 完成率不该掉 |
| D 双发 reject | `EVAL_FORCE_POLICY=reject` | D4-T04 从抢占变 409；其余不变 |

E 列（真模型换掉 Mock）第一版不做：那要单独成表，不和 A–D 混（Mock 下的数字与
真模型下的数字不可比，放一张表里会误导）。

**这张表的读法**：不看「哪一列分高」——B/C/D 都是刻意把产品调坏或调偏，分低是预期。
要看的是**变化是否落在该变的维度上**：关子 Agent 只该动 D5，双发 reject 只该动 D4。
若关子 Agent 把 D1 也带崩了，说明有不该有的耦合——那才是这张表的价值。

用法：
    python -m evals.ablation                    # 跑 A–D 四列
    python -m evals.ablation --arms A B         # 只跑指定列
"""
from __future__ import annotations

import argparse
import asyncio
import os
from dataclasses import dataclass

from evals.report import REPORTS_DIR, _DIM_LABELS, _git_sha, _pct
from evals.runner import CaseResult, run_cases
from evals.schema import load_all_cases


@dataclass(frozen=True)
class Arm:
    """一个消融方案。env 是相对基线要改的环境变量。"""

    key: str
    label: str
    env: dict[str, str]
    expect: str  # 这一列想证明什么，直接印进表格


ARMS: list[Arm] = [
    Arm("A", "A 基线", {}, "主指标水位"),
    Arm(
        "B",
        "B 关子 Agent",
        {"SUBAGENT_ENABLED": "false"},
        "D5 变拒绝；D1 不受影响",
    ),
    Arm(
        "C",
        "C 压缩更激进",
        {"EVAL_FORCE_COMPACT_THRESHOLD": "400"},
        "D2 触发率上升；D1 不该掉",
    ),
    Arm(
        "D",
        "D 双发 reject",
        {"EVAL_FORCE_POLICY": "reject"},
        "D4 双发题改判；其余不变",
    ),
]


async def _run_arm(arm: Arm) -> list[CaseResult]:
    """在 arm 的环境下跑全集。

    每列跑完把环境还原：一列没还原，后面每一列都跑在被改过的运行时上，整张表都脏。
    settings 缓存也要清——Settings 是 lru_cache 的，不清的话新环境变量读不到。
    """
    from app.config import get_settings

    saved = {k: os.environ.get(k) for k in arm.env}
    try:
        os.environ.update(arm.env)
        get_settings.cache_clear()
        return await run_cases(load_all_cases())
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        get_settings.cache_clear()


def _dim_cell(results: list[CaseResult], dim: str) -> str:
    rows = [r for r in results if r.case.dimension.value == dim]
    if not rows:
        return "—"
    return _pct(sum(1 for r in rows if r.ok), len(rows))


def render_table(runs: list[tuple[Arm, list[CaseResult]]]) -> str:
    """一行一个维度，一列一个方案。让「变化落在哪个维度」一眼可见。"""
    header = "| 能力面 | " + " | ".join(a.label for a, _ in runs) + " |"
    sep = "|---" * (len(runs) + 1) + "|"
    lines = [header, sep]
    for key, label in _DIM_LABELS:
        cells = [_dim_cell(res, key) for _, res in runs]
        lines.append(f"| {label} | " + " | ".join(cells) + " |")
    totals = [
        _pct(sum(1 for r in res if r.ok), len(res)) for _, res in runs
    ]
    lines.append("| **合计** | " + " | ".join(f"**{t}**" for t in totals) + " |")

    lines.append("")
    lines.append("| 方案 | 怎么切 | 想证明什么 |")
    lines.append("|---|---|---|")
    for arm, _ in runs:
        knob = (
            "默认 settings + Mock"
            if not arm.env
            else " ".join(f"`{k}={v}`" for k, v in arm.env.items())
        )
        lines.append(f"| {arm.label} | {knob} | {arm.expect} |")
    return "\n".join(lines)


def _diff_notes(runs: list[tuple[Arm, list[CaseResult]]]) -> str:
    """相对基线，逐题列出「谁翻了面」。表格只给数字，这里给题号。"""
    if not runs:
        return ""
    base = {r.case.id: r.ok for _, res in runs[:1] for r in res}
    out: list[str] = []
    # repeats>1 的题翻面要标出来：那可能只是时序抖动（4/5 也算不过），
    # 不一定是这个旋钮的效应。不标的话读者会把抖动读成「旋钮引入了耦合」。
    for arm, res in runs[1:]:
        flipped = [
            f"{r.case.id}（{'绿→红' if base.get(r.case.id) else '红→绿'}"
            + (f"，{r.passed}/{len(r.attempts)}，时序题可能是抖动" if len(r.attempts) > 1 else "")
            + "）"
            for r in res
            if base.get(r.case.id) is not None and r.ok != base[r.case.id]
        ]
        if flipped:
            out.append(f"- **{arm.label}** 相对基线翻面：{'、'.join(flipped)}")
        else:
            out.append(f"- **{arm.label}** 相对基线无题翻面")
    return "\n".join(out)


async def _main_async(keys: list[str]) -> int:
    arms = [a for a in ARMS if a.key in keys]
    if not arms:
        print(f"没有匹配的方案列：{keys}（可选 {[a.key for a in ARMS]}）")
        return 2

    runs: list[tuple[Arm, list[CaseResult]]] = []
    for arm in arms:
        print(f"\n===== {arm.label} =====")
        runs.append((arm, await _run_arm(arm)))

    body = f"""# AgentGate 评测消融对比

> 自动生成（`python -m evals.ablation`）。方案列都是**本仓库能开关的真实旋钮**。
> commit `{_git_sha()}`。

**读法**：不看「哪一列分高」——B/C/D 都是刻意把产品调坏或调偏，分低是预期。要看的是
**变化是否落在该变的维度上**：关子 Agent 只该动 D5，双发 reject 只该动 D4。若关子
Agent 把 D1 也带崩了，说明有不该有的耦合——那才是这张表的价值。

{render_table(runs)}

## 相对基线的翻面

{_diff_notes(runs)}
"""
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    out = REPORTS_DIR / "ablation.md"
    out.write_text(body, encoding="utf-8")
    print(f"\n消融表：{out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="AgentGate 评测消融对比")
    ap.add_argument(
        "--arms",
        nargs="*",
        default=[a.key for a in ARMS],
        help=f"要跑的方案列（默认全部：{[a.key for a in ARMS]}）",
    )
    args = ap.parse_args()
    return asyncio.run(_main_async([k.upper() for k in args.arms]))


if __name__ == "__main__":
    raise SystemExit(main())
