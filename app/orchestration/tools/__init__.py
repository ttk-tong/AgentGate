"""工具子系统：注册表、基类、内置工具（见 plan/04）。"""
from __future__ import annotations

import os

from app.orchestration.tools.base import BaseTool, ToolRegistry
from app.orchestration.tools.builtin.file_read import FileReadTool
from app.orchestration.tools.builtin.kb_search import KbSearchTool
from app.orchestration.tools.builtin.note_append import NoteAppendTool
from app.orchestration.tools.builtin.remember import RememberTool
from app.orchestration.tools.builtin.weather import WeatherTool

# 注意：SpawnAgentTool **不** 在此顶层 import。它间接依赖 tool_executor，与
# tool_executor 对 tools.base 的依赖会成环。改由 `attach_spawn_agent` 惰性导入。

__all__ = [
    "BaseTool",
    "ToolRegistry",
    "build_default_registry",
    "attach_spawn_agent",
]


def build_default_registry(file_base_dir: str | None = None) -> ToolRegistry:
    """装配内置工具集：三个只读（可并行）+ 两个写（串行：便签、记忆）。

    注意：`spawn_agent` **不** 在此处注册。它依赖运行期的 `SubagentRunner`
    （携带父 session_id 与 provider 引用），构造有生命周期问题——每请求新建。
    上层（chat._build_loop）在拿到 SubagentRunner 后调 `attach_spawn_agent`
    补挂进本注册表。
    """
    reg = ToolRegistry()
    reg.register(FileReadTool(base_dir=file_base_dir or os.getcwd()))
    reg.register(KbSearchTool())
    reg.register(WeatherTool())
    reg.register(NoteAppendTool())
    reg.register(RememberTool())
    return reg


def attach_spawn_agent(registry: ToolRegistry, runner) -> None:
    """把 `spawn_agent` 工具挂进注册表，注入 runner（plan/03 §8）。

    惰性导入 SpawnAgentTool，避免 tools/__init__ ↔ tool_executor 的循环依赖。
    runner 为 None 时挂一个「明确报错」的桩，方便调试期看到工具已声明但不可用。
    """
    from app.orchestration.tools.builtin.spawn_agent import SpawnAgentTool

    registry.register(SpawnAgentTool(runner=runner))
