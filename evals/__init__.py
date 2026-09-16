"""AgentGate 运行时评测集（见 docs/superpowers/specs/2026-09-12-runtime-eval-set-design.md）。

这个包是仓库的**测量仪器**，不是运行时的一部分——刻意放在 app/ 之外。

评测对象是「运行时行为」而非「答案质量」：provider 固定为 MockProvider，
答案好不好不由本仓库的代码决定，能被本仓库决定的是编排、分批、落库、
取消/引导/引用、闸门与优雅失败。
"""
