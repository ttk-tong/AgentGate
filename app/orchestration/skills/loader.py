"""技能加载：扫描 SKILL.md → 解析 front-matter + 正文 → Skill（plan/07 §3、§4）。

目录风格（与 Claude Code skill 一致，便于手写/分发）：

    skills/
    └── invoice_processing/
        └── SKILL.md      # front-matter 元数据 + 正文领域提示

SKILL.md 结构：

    ---
    name: invoice_processing
    version: 1.2.0
    triggers: [发票, 报销, invoice]
    tools: [kb_search, sql_query]
    ---
    正文：注入 system 的领域提示片段……

为不引入 YAML 依赖，front-matter 用极简解析：`key: value`，value 支持
`[a, b, c]` 内联列表与标量。正文（--- 之后的全部）作为 Skill.prompt。
解析纯字符串处理，无 IO 之外副作用，可离线测试。
"""
from __future__ import annotations

import os

from app.domain.skill import Skill

_FRONT_MATTER_FENCE = "---"
# front-matter 里按列表解释的键（其余按标量）
_LIST_KEYS = {"triggers", "tools", "requires_scopes"}
_BOOL_KEYS = {"always_on"}
_INT_KEYS = {"max_context_tokens"}


def parse_skill_md(text: str) -> Skill:
    """解析一个 SKILL.md 文本为 Skill。无 front-matter 则视为纯正文提示。"""
    front, body = _split_front_matter(text)
    meta = _parse_front_matter(front)

    if "name" not in meta:
        raise ValueError("SKILL.md 缺少必填字段 name")

    return Skill(
        name=str(meta["name"]),
        version=str(meta.get("version", "0.0.0")),
        description=str(meta.get("description", "")),
        triggers=meta.get("triggers", []),
        tools=meta.get("tools", []),
        requires_scopes=meta.get("requires_scopes", []),
        model_hint=meta.get("model_hint") or None,
        max_context_tokens=int(meta.get("max_context_tokens", 2000)),
        always_on=bool(meta.get("always_on", False)),
        prompt=body.strip(),
    )


def _split_front_matter(text: str) -> tuple[str, str]:
    """切出 front-matter 与正文。首行是 --- 时，取到下一个 --- 之间为 front-matter。"""
    stripped = text.lstrip("﻿\n")  # 去 BOM/前导空行
    if not stripped.startswith(_FRONT_MATTER_FENCE):
        return "", text
    rest = stripped[len(_FRONT_MATTER_FENCE):]
    end = rest.find("\n" + _FRONT_MATTER_FENCE)
    if end == -1:
        return "", text
    front = rest[:end]
    body = rest[end + len("\n" + _FRONT_MATTER_FENCE):]
    return front, body


def _parse_front_matter(front: str) -> dict:
    """极简 key: value 解析。list 键支持 [a, b] 内联；其余按标量。"""
    meta: dict = {}
    for raw in front.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if key in _LIST_KEYS:
            meta[key] = _parse_list(value)
        elif key in _BOOL_KEYS:
            meta[key] = value.lower() in ("true", "1", "yes")
        elif key in _INT_KEYS:
            meta[key] = int(value) if value.isdigit() else 2000
        else:
            meta[key] = value
    return meta


def _parse_list(value: str) -> list[str]:
    """解析 `[a, b, c]` 或逗号分隔的内联列表。"""
    v = value.strip()
    if v.startswith("[") and v.endswith("]"):
        v = v[1:-1]
    return [item.strip().strip("'\"") for item in v.split(",") if item.strip()]


def discover_skill_files(root: str) -> list[str]:
    """扫描 root 下所有子目录的 SKILL.md，返回路径列表（稳定按名排序）。"""
    found: list[str] = []
    if not os.path.isdir(root):
        return found
    for entry in sorted(os.listdir(root)):
        manifest = os.path.join(root, entry, "SKILL.md")
        if os.path.isfile(manifest):
            found.append(manifest)
    # 也允许 root 直接放 SKILL.md
    direct = os.path.join(root, "SKILL.md")
    if os.path.isfile(direct):
        found.append(direct)
    return found


def load_skill_file(path: str) -> Skill:
    """读并解析一个 SKILL.md 文件。"""
    with open(path, encoding="utf-8") as f:
        return parse_skill_md(f.read())
