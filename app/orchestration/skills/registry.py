"""技能注册表（plan/07 §4）。

启动时扫描 skills/ 目录、解析 SKILL.md、构建 SkillRegistry。校验：技能引用的
工具必须在 ToolRegistry 存在，否则拒绝加载并告警——避免激活后暴露不存在的工具。

注册表只管「有哪些技能、各自声明什么」；「本轮激活哪些」是 selector 的职责。
"""
from __future__ import annotations

from app.domain.skill import Skill
from app.observability.logging import get_logger
from app.orchestration.skills.loader import discover_skill_files, load_skill_file

_log = get_logger("skills")


class SkillRegistry:
    """进程内技能全集。按名注册/查找，导出激活所需的工具与提示。"""

    def __init__(self) -> None:
        self._skills: dict[str, Skill] = {}

    def register(self, skill: Skill, *, known_tools: set[str] | None = None) -> Skill:
        """注册一个技能。known_tools 给定时校验引用的工具都存在。"""
        if known_tools is not None:
            missing = [t for t in skill.tools if t not in known_tools]
            if missing:
                raise ValueError(f"技能 {skill.name} 引用了不存在的工具: {missing}")
        self._skills[skill.name] = skill
        return skill

    def load_dir(self, root: str, *, known_tools: set[str] | None = None) -> list[str]:
        """扫描目录加载全部 SKILL.md。返回成功加载的技能名列表。

        单个技能解析/校验失败只告警跳过，不影响其余技能加载（稳健优先）。
        """
        loaded: list[str] = []
        for path in discover_skill_files(root):
            try:
                skill = load_skill_file(path)
                self.register(skill, known_tools=known_tools)
                loaded.append(skill.name)
            except Exception as e:
                _log.warning("skill.load_failed", path=path, error=str(e))
        _log.info("skill.loaded", count=len(loaded), names=loaded)
        return loaded

    def get(self, name: str) -> Skill | None:
        return self._skills.get(name)

    def names(self) -> list[str]:
        return list(self._skills)

    def all(self) -> list[Skill]:
        return list(self._skills.values())

    def always_on(self) -> list[Skill]:
        return [s for s in self._skills.values() if s.always_on]
