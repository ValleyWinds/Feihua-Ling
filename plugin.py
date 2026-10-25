"""飞花令插件入口。

群聊飞花令游戏：记录所有已用诗句，并在 bot 每次生成回复前把清单注入
extra_prompt，保证 bot 接令时绝不重复任何人（包括自己）说过的诗句。
完整设计见 PLAN.md（开发期文档，发布前删除）。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from maibot_sdk import MaiBotPlugin

from . import game as G
from .config import FeihuaLingConfig
from .game import GameStore
from .handlers import HandlersMixin


class FeihuaLingPlugin(HandlersMixin, MaiBotPlugin):
    """飞花令插件主类（Command 与 Hook 处理器在 HandlersMixin 中）。"""

    config_model = FeihuaLingConfig

    def __init__(self) -> None:
        super().__init__()
        # 运行时状态由 on_load 初始化（依赖 ctx.paths）
        self._store: GameStore | None = None
        self._store_lock = asyncio.Lock()
        # 自发消息指纹（dict 当有序集用，FIFO 淘汰），防止功能性文本被 ③ 号 Hook 误记录
        self._fingerprints: dict[str, None] = {}

    # ── 生命周期（三者必须全覆盖，缺一拒载）────────────────────────

    async def on_load(self) -> None:
        data_dir = Path(str(self.ctx.paths.data_dir))
        self._store = GameStore(data_dir / "games.json")
        self._store.load()
        # 惰性过期的第一触点：启动时清理僵尸局（关机期间早已超时的局静默收官）
        now = time.time()
        timeout = self.config.game.timeout_minutes
        expired = [
            sid
            for sid, g in self._store.active.items()
            if G.is_expired(g, now, timeout)
        ]
        for sid in expired:
            self._store.finish(sid, "timeout_on_load", now)
        self.ctx.logger.info(
            f"飞花令插件已加载：活跃局 {len(self._store.active)}，历史 {len(self._store.history)}，"
            f"启动清理僵尸局 {len(expired)}"
        )

    async def on_unload(self) -> None:
        if self._store is not None:
            self._store.save()
        self.ctx.logger.info("飞花令插件已卸载")

    async def on_config_update(
        self, scope: str, config_data: dict, version: str
    ) -> None:
        """配置热重载。self.config 已由 Runner 注入新值，各处理器即时读取，无需缓存。"""
        self.ctx.logger.info(f"飞花令配置更新: scope={scope}, version={version}")


def create_plugin() -> MaiBotPlugin:
    """MaiBot 插件工厂函数。"""
    return FeihuaLingPlugin()
