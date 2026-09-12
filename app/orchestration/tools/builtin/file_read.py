"""file_read：只读工具（plan/04 §9）。

读取工作目录下的文本文件。只读 + 并发安全 → 可与其他只读工具并行成批。
路径判定与截断复用共享沙箱（file_sandbox），与引用 resolver 同一套实现——
两份会漂移，而漂移的后果是目录穿越。
"""
from __future__ import annotations

import os

from app.domain.tool import ToolContext, ToolResult, ToolSpec
from app.orchestration.tools.base import BaseTool
from app.orchestration.tools.builtin.file_sandbox import read_sandboxed


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
        r = read_sandboxed(self._base, rel)
        if not r.ok:
            return ToolResult(
                ok=False,
                error=r.error,
                error_code=r.error_code,
                is_retryable=r.error_code == "io_error",
            )
        return ToolResult(
            ok=True,
            content=r.content,
            meta={"path": rel, "truncated": r.truncated},
        )

