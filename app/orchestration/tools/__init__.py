"""工具子系统：注册表、基类、内置工具（见 plan/04）。"""
from __future__ import annotations

import os

from app.orchestration.tools.base import BaseTool, ToolRegistry
from app.orchestration.tools.builtin.file_read import FileReadTool
from app.orchestration.tools.builtin.kb_search import KbSearchTool
from app.orchestration.tools.builtin.note_append import NoteAppendTool
from app.orchestration.tools.builtin.weather import WeatherTool

__all__ = ["BaseTool", "ToolRegistry", "build_default_registry"]


def build_default_registry(file_base_dir: str | None = None) -> ToolRegistry:
    """装配阶段 2 的内置工具集：三个只读（可并行）+ 一个写（串行）。"""
    reg = ToolRegistry()
    reg.register(FileReadTool(base_dir=file_base_dir or os.getcwd()))
    reg.register(KbSearchTool())
    reg.register(WeatherTool())
    reg.register(NoteAppendTool())
    return reg
