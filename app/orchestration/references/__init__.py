"""引用解析：把客户端给的引用变成不可变快照。

对外暴露：build_default_resolvers / resolve_all（解析），
render_references / attach_references（落库 + 渲染进 user 消息）。
"""
from app.orchestration.references.assembler import (
    attach_references,
    render_references,
)
from app.orchestration.references.resolvers import (
    build_default_resolvers,
    resolve_all,
)

__all__ = [
    "attach_references",
    "build_default_resolvers",
    "render_references",
    "resolve_all",
]
