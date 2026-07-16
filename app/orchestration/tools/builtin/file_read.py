"""file_read：只读工具（plan/04 §9）。

读取工作目录下的文本文件。只读 + 并发安全 → 可与其他只读工具并行成批。
做了两点约束：限制在 base_dir 内（防目录穿越）、输出超阈值截断（防撑爆上下文）。
"""
from __future__ import annotations

import os

from app.domain.tool import ToolContext, ToolResult, ToolSpec
from app.orchestration.tools.base import BaseTool

_MAX_BYTES = 8192  # 输出截断阈值，见 plan/04 §5


class FileReadTool(BaseTool):
    spec = ToolSpec(
        name="file_read",
        description="读取工作目录下一个文本文件的内容。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "相对工作目录的文件路径"},
            },
            "required": ["path"],
        },
        is_read_only=True,
        is_concurrency_safe=True,
        idempotent=True,
    )

    def __init__(self, base_dir: str):
        self._base = os.path.realpath(base_dir)

    async def call(self, args: dict, ctx: ToolContext, on_progress=None) -> ToolResult:
        rel = str(args.get("path", ""))
        target = os.path.realpath(os.path.join(self._base, rel))
        # 防目录穿越：解析后的路径必须仍在 base_dir 内
        if target != self._base and not target.startswith(self._base + os.sep):
            return ToolResult(
                ok=False, error="path escapes base dir", error_code="forbidden_path"
            )
        if not os.path.isfile(target):
            return ToolResult(
                ok=False, error=f"not a file: {rel}", error_code="not_found"
            )
        try:
            with open(target, encoding="utf-8", errors="replace") as f:
                data = f.read(_MAX_BYTES + 1)
        except OSError as e:  # noqa: BLE001
            return ToolResult(ok=False, error=str(e), error_code="io_error", is_retryable=True)

        truncated = len(data) > _MAX_BYTES
        content = data[:_MAX_BYTES]
        if truncated:
            content += "\n…[truncated]"
        return ToolResult(
            ok=True,
            content=content,
            meta={"path": rel, "truncated": truncated},
        )
