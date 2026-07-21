"""技能子系统（plan/07）。

- Skill 领域模型（domain/skill.py）：声明式清单 + 领域提示片段。
- loader：扫描 skills/ 目录，解析 SKILL.md（front-matter + 正文）为 Skill。
- registry：进程内技能表，加载时校验引用的工具都在 ToolRegistry 存在。
- selector：两级激活——静态 always_on + 一级 trigger 关键词命中，命中过多/需语义
  判断时用注入的小模型裁决（二级），再按 scope 权限过滤。

设计延续可离线测原则：front-matter 解析、trigger 命中、scope 过滤都是纯函数，
小模型选择器可注入桩，`pytest` 不碰磁盘/LLM 即可验证（loader 也支持从内存文本解析）。
"""
from __future__ import annotations

from app.orchestration.skills.loader import parse_skill_md
from app.orchestration.skills.registry import SkillRegistry
from app.orchestration.skills.selector import select_skills

__all__ = ["SkillRegistry", "parse_skill_md", "select_skills"]
