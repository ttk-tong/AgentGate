"""迁移执行辅助：程序内把 schema 升到最新（dev 自动建表用）。

生产仍走命令行 `alembic upgrade head`（可控、可审计）；这里只给 dev 便利：
应用启动时若 app_env=dev 且开启 auto_migrate，则自动 upgrade 到 head，省去
手动跑迁移。

注意：Alembic 的在线迁移内部会 `asyncio.run(...)`（见 alembic/env.py），不能在
已运行的事件循环里直接调用。因此这里用 `alembic.command.upgrade`（同步 API），
并由调用方放到工作线程执行（`asyncio.to_thread`），避免与 lifespan 的事件循环冲突。
"""
from __future__ import annotations

import os

from alembic import command
from alembic.config import Config

from app.observability.logging import get_logger

_log = get_logger("migrate")


def _alembic_config() -> Config:
    """定位仓库根的 alembic.ini，构造 Alembic Config。"""
    # 本文件在 app/persistence/ 下，根目录是上两级
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
    ini_path = os.path.join(root, "alembic.ini")
    cfg = Config(ini_path)
    # script_location 在 ini 里是相对路径（alembic），补成绝对，保证任意工作目录都能找到
    cfg.set_main_option("script_location", os.path.join(root, "alembic"))
    return cfg


def upgrade_to_head() -> None:
    """同步执行 `alembic upgrade head`。放在工作线程里调用（见模块 docstring）。"""
    cfg = _alembic_config()
    command.upgrade(cfg, "head")
    _log.info("db.migrated", target="head")
