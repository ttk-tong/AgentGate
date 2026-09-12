"""工作目录内的安全文本读取（file_read 工具与引用 resolver 共用）。

抽成共享模块的理由：引用解析也要读文件。如果两边各写一套路径判定，两份实现
就会漂移——而其中一份漂移的后果是目录穿越，这是安全缺陷而不是风格问题。

两条约束（原 file_read 的语义，逐字保留）：
- 限制在 base_dir 内（realpath 解析后再比较，防 `..` 与符号链接穿越）。
- 输出超阈值截断（防撑爆上下文）。
"""
from __future__ import annotations

import os

from pydantic import BaseModel

MAX_READ_BYTES = 8192
TRUNCATION_SUFFIX = "\n…[truncated]"


class SandboxRead(BaseModel):
    ok: bool
    content: str = ""
    truncated: bool = False
    error_code: str | None = None
    error: str | None = None


def read_sandboxed(
    base_dir: str, rel_path: str, *, max_bytes: int = MAX_READ_BYTES
) -> SandboxRead:
    """读 base_dir 下的一个文本文件。越界/不存在/IO 失败都返回 ok=False。"""
    base = os.path.realpath(base_dir)
    target = os.path.realpath(os.path.join(base, rel_path))
    # 防目录穿越：解析后的路径必须仍在 base_dir 内
    if target != base and not target.startswith(base + os.sep):
        return SandboxRead(
            ok=False, error_code="forbidden_path", error="path escapes base dir"
        )
    if not os.path.isfile(target):
        return SandboxRead(ok=False, error_code="not_found", error=f"not a file: {rel_path}")
    try:
        with open(target, encoding="utf-8", errors="replace") as f:
            data = f.read(max_bytes + 1)
    except OSError as e:
        return SandboxRead(ok=False, error_code="io_error", error=str(e))

    truncated = len(data) > max_bytes
    content = data[:max_bytes] + (TRUNCATION_SUFFIX if truncated else "")
    return SandboxRead(ok=True, content=content, truncated=truncated)
