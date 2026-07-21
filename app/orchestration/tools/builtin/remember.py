"""remember：把用户明确要求记住的信息写入长期记忆（plan/06 §4.1 显式写入）。

写工具（有副作用），单独串行成批。不在 call 内直接落库，而是产出 ContextMutation，
交由 Loop 的应用器按序写入 MemoryService（避免并发竞态、保证确定性）。

kind 让模型区分事实/偏好/事件；scope 默认 user（跨会话记住），由 Loop 结合
会话的 external_user 决定 scope_key，工具本身不接触租户/用户标识（防越权）。
"""
from __future__ import annotations

from app.domain.tool import ContextMutation, ToolContext, ToolResult, ToolSpec
from app.orchestration.tools.base import BaseTool

_ALLOWED_KINDS = {"fact", "preference", "event"}


class RememberTool(BaseTool):
    spec = ToolSpec(
        name="remember",
        description=(
            "把用户明确要求记住的稳定信息写入长期记忆，供以后跨会话调用。"
            "适用于用户偏好、稳定事实、重要事件；不要用来记临时的一次性内容。"
        ),
        parameters={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "要记住的内容（自然语言）"},
                "kind": {
                    "type": "string",
                    "enum": sorted(_ALLOWED_KINDS),
                    "description": "记忆类型：fact 事实 / preference 偏好 / event 事件",
                },
            },
            "required": ["content"],
        },
        is_read_only=False,        # 写工具：单独串行成批
        is_concurrency_safe=False,
        mutates_context=True,      # 有副作用：延迟按序应用
    )

    async def call(self, args: dict, ctx: ToolContext, on_progress=None) -> ToolResult:
        content = str(args.get("content", "")).strip()
        kind = str(args.get("kind") or "preference")
        if kind not in _ALLOWED_KINDS:
            kind = "preference"
        if not content:
            return ToolResult(
                ok=False,
                content={"error": "empty content", "code": "invalid_args"},
                error="remember requires non-empty content",
                error_code="invalid_args",
            )
        # 副作用描述成 mutation，交给 Loop 应用器写 MemoryService。
        # scope/scope_key 由 Loop 结合会话上下文补全，工具不接触用户标识。
        mutation = ContextMutation(
            tool_call_id="",
            kind="remember",
            payload={"content": content, "kind": kind},
        )
        return ToolResult(
            ok=True,
            content={"remembered": content, "kind": kind},
            display=f"已记住（{kind}）：{content}",
            mutation=mutation,
            meta={"len": len(content)},
        )
