"""组件处理器：1 个 @Command + 3 个 @HookHandler，以 mixin 形式由 plugin.py 组合。

数据流闭环（见 PLAN.md §3）：
① chat.receive.after_process 记群友诗句 + 重复提示
② maisaka.replyer.before_request 注入已用诗句清单（extra_prompt）
③ send_service.after_send 记 bot 自己的诗句 + 重复吐槽（先排除插件自发消息）
④ @Command fhl 开局/结束/状态/历史
⑤ 可选主动接令：llm.generate → 本地校验 → 发送 → 记录

运行时属性（_store / _store_lock / _fingerprints）由 plugin.py 初始化。
"""

from __future__ import annotations

import random
import time
from typing import Any

from maibot_sdk import Command, HookHandler
from maibot_sdk.types import HookMode

from . import game as G
from .game import BOT_DISPLAY_NAME, BOT_SPEAKER_ID, MAX_OUTBOUND_WHOLE_LEN, Game

USAGE = (
    "🎋 飞花令用法：\n"
    "/fhl —— 随机令字开局\n"
    "/fhl <字> —— 指定令字开局（如 /fhl 花）\n"
    "/fhl end —— 结束本局并出战报\n"
    "/fhl status —— 查看当前局\n"
    "/fhl history —— 查看上一局战报"
)

# 自发消息指纹保留条数（FIFO）
FINGERPRINT_LIMIT = 300


def _extract_message_text(message: dict) -> str:
    """提取消息纯文本：processed_plain_text 优先，raw_message 组件列表兜底。

    raw_message 是 [{"type": "text", "data": "..."}] 形态，必须按 type=="text"
    过滤，否则图片 URL 等非正文会参与匹配。
    """
    raw_text = str(message.get("processed_plain_text") or "")
    if raw_text:
        return raw_text
    raw_msg = message.get("raw_message")
    if isinstance(raw_msg, list):
        parts = [
            seg.get("data", "")
            for seg in raw_msg
            if isinstance(seg, dict) and seg.get("type") == "text"
        ]
        return "".join(str(p) for p in parts if isinstance(p, str))
    return ""


def _extract_user(message: dict) -> tuple[str, str]:
    """user_id 嵌套在 message_info.user_info.user_id，昵称字段名防御式多取。"""
    info = message.get("message_info") or {}
    user_info = info.get("user_info") or {}
    uid = str(user_info.get("user_id") or message.get("user_id") or "")
    name = str(
        user_info.get("user_nickname")
        or user_info.get("nickname")
        or user_info.get("user_name")
        or uid
    )
    return uid, name


def _extract_session_id(message: dict) -> str:
    """会话 ID 防御式提取（出/入站序列化结构可能不同，开发期对真实结构核对）。"""
    info = message.get("message_info") or {}
    group_info = info.get("group_info") or {}
    return str(
        info.get("session_id")
        or message.get("session_id")
        or info.get("stream_id")
        or message.get("stream_id")
        or group_info.get("group_id")
        or ""
    )


class HandlersMixin:
    """Command 与 Hook 处理器集合。"""

    # ── 内部辅助 ──────────────────────────────────────────────────

    def _game_cfg(self):
        return self.config.game

    async def _send(self, text: str, stream_id: str) -> bool:
        """插件功能性发送：先登记归一化指纹，防止被 ③ 号 Hook 误记录/误吐槽。"""
        self._remember_fingerprint(text)
        try:
            result = await self.ctx.send.text(text=text, stream_id=stream_id)
            return bool(result)
        except Exception:
            self.ctx.logger.exception("send.text 失败")
            return False

    def _remember_fingerprint(self, text: str) -> None:
        fp = G.normalize(text)
        if not fp:
            return
        self._fingerprints[fp] = None
        while len(self._fingerprints) > FINGERPRINT_LIMIT:
            self._fingerprints.popitem(last=False)

    def _is_own_message(self, raw_text: str) -> bool:
        return G.normalize(raw_text) in self._fingerprints

    def _expire_if_needed(self, session_id: str) -> Game | None:
        """惰性过期检查，超时局静默收官。必须在 _store_lock 内调用。"""
        g = self._store.active.get(session_id)
        if g is None:
            return None
        now = time.time()
        if G.is_expired(g, now, self._game_cfg().timeout_minutes):
            self._store.finish(session_id, "timeout", now)
            self.ctx.logger.info(
                f"飞花令会话 {session_id} 令字「{g.keyword}」超时收官（{len(g.entries)} 句）"
            )
            return None
        return g

    # ── ④ Command：/fhl ───────────────────────────────────────────

    @Command(
        name="fhl",
        pattern=r"^/fhl(?:\s+(?P<arg>.+))?$",
        description="飞花令游戏：/fhl 开局、/fhl end 结束、/fhl status 状态",
    )
    async def cmd_fhl(self, **kwargs: Any) -> tuple[bool, str, int]:
        stream_id = str(kwargs.get("stream_id") or "")
        if not stream_id:
            return True, "no stream", 1
        arg = str((kwargs.get("matched_groups") or {}).get("arg") or "").strip()

        if not self._game_cfg().enabled:
            await self._send("🎋 飞花令功能当前已关闭", stream_id)
            return True, "disabled", 1

        parts = arg.split()
        first = parts[0].lower() if parts else ""

        if first in ("end", "stop"):
            return await self._cmd_finish(stream_id)
        if first == "status":
            return await self._cmd_status(stream_id)
        if first in ("history", "last"):
            return await self._cmd_history(stream_id)
        if first in ("help", "帮助"):
            await self._send(USAGE, stream_id)
            return True, "usage", 1
        if first == "start":
            keyword = parts[1] if len(parts) > 1 else ""
            return await self._cmd_start(stream_id, keyword)
        if not first:
            return await self._cmd_start(stream_id, "")

        # 剩余形态：单个汉字 → 指定令字开局；其他 → 用法
        norm = G.normalize(arg)
        if len(norm) == 1:
            return await self._cmd_start(stream_id, norm)
        await self._send(USAGE, stream_id)
        return True, "usage", 1

    async def _cmd_start(self, stream_id: str, keyword: str) -> tuple[bool, str, int]:
        now = time.time()
        async with self._store_lock:
            if self._expire_if_needed(stream_id) is not None:
                game = self._store.active[stream_id]
                await self._send(
                    f"🎋 本局进行中（令字「{game.keyword}」），/fhl end 结束后再开新局",
                    stream_id,
                )
                return True, "already running", 1
            pool = [k for k in self._game_cfg().default_keywords if k]
            chosen = keyword or (random.choice(pool) if pool else "花")
            game = Game(
                session_id=stream_id,
                keyword=chosen,
                start_time=now,
                last_activity=now,
            )
            self._store.start(game)

        timeout = self._game_cfg().timeout_minutes
        await self._send(
            f"🎋 飞花令开局！令字：「{chosen}」\n"
            f"规则：说一句含「{chosen}」字的真实古诗词（4~20 字），整句或半句说过都算重复；"
            f"{timeout} 分钟无新诗句自动收官。\n"
            f"来吧，说出含「{chosen}」的诗句，bot 也会接令～",
            stream_id,
        )
        return True, f"started keyword={chosen}", 1

    async def _cmd_finish(self, stream_id: str) -> tuple[bool, str, int]:
        async with self._store_lock:
            game = self._expire_if_needed(stream_id)
            if game is None:
                await self._send("当前没有进行中的飞花令局", stream_id)
                return True, "no game", 1
            self._store.finish(stream_id, "manual", time.time())
        await self._send(G.build_report(game), stream_id)
        return True, "finished", 1

    async def _cmd_status(self, stream_id: str) -> tuple[bool, str, int]:
        async with self._store_lock:
            game = self._expire_if_needed(stream_id)
            if game is None:
                await self._send("当前没有进行中的飞花令局，/fhl 开一局？", stream_id)
                return True, "no game", 1
            await self._send(G.build_status(game), stream_id)
        return True, "status", 1

    async def _cmd_history(self, stream_id: str) -> tuple[bool, str, int]:
        report = self._store.last_report()
        if report is None:
            await self._send("还没有完成过任何一局飞花令", stream_id)
            return True, "no history", 1
        await self._send(f"📖 上一局战报：\n{report}", stream_id)
        return True, "history", 1

    # ── ① 入站：记群友诗句 ────────────────────────────────────────

    @HookHandler(
        "chat.receive.after_process",
        mode=HookMode.OBSERVE,
        name="feihua_player_recorder",
    )
    async def on_player_message(self, **kwargs: Any) -> None:
        """观察一切入站消息（不区分发送者身份，其他 bot 的诗句同样记录）。"""
        if not self._game_cfg().enabled:
            return
        message = kwargs.get("message")
        if not isinstance(message, dict):
            return
        text = _extract_message_text(message)
        if not text:
            return
        session_id = _extract_session_id(message)
        if not session_id:
            return
        user_id, user_name = _extract_user(message)

        async with self._store_lock:
            game = self._expire_if_needed(session_id)
            if game is None:
                return
            dup_key, entry = G.record_entry(
                game, user_id, user_name, text, time.time()
            )
            if entry is None and dup_key is None:
                return
            self._store.save()

        if dup_key:
            owner = G.find_owner(game, dup_key)
            await self._send(
                f"🚫「{dup_key}」这句 {owner} 已经说过啦，换一句吧～", session_id
            )
        if entry is not None and self._game_cfg().bot_proactive:
            await self._maybe_proactive(session_id)

    # ── ② 回复生成前：注入已用诗句清单 ──────────────────────────────

    @HookHandler(
        "maisaka.replyer.before_request",
        mode=HookMode.BLOCKING,
        name="feihua_used_injector",
    )
    async def inject_used_poems(self, **kwargs: Any) -> dict[str, Any] | None:
        """extra_prompt 追加注入（追加不覆盖，整份 kwargs 带回）。无局返回 None 零干扰。"""
        if not self._game_cfg().enabled:
            return None
        session_id = str(kwargs.get("session_id") or "")
        if not session_id:
            return None
        async with self._store_lock:
            game = self._expire_if_needed(session_id)
            if game is None or not game.entries:
                return None
            injection = G.build_injection(game, self._game_cfg().max_entries_hint)
        kwargs["extra_prompt"] = (kwargs.get("extra_prompt") or "") + injection
        return {"action": "continue", "modified_kwargs": kwargs}

    # ── ③ 出站：记 bot 自己的诗句 ──────────────────────────────────

    @HookHandler(
        "send_service.after_send",
        mode=HookMode.OBSERVE,
        name="feihua_bot_recorder",
    )
    async def on_bot_message(self, **kwargs: Any) -> None:
        if not self._game_cfg().enabled:
            return
        if not kwargs.get("sent"):
            return
        message = kwargs.get("message")
        if not isinstance(message, dict):
            return
        text = _extract_message_text(message)
        if not text:
            return
        # 自发消息排除：指纹命中 → 插件功能性文本（提示/战报/主动诗句，均已自行入账）
        if self._is_own_message(text):
            return
        # 整条超长护栏：战报/长闲聊不可能是单句接令
        if len(G.normalize(text)) > MAX_OUTBOUND_WHOLE_LEN:
            return
        session_id = _extract_session_id(message)
        if not session_id:
            return

        async with self._store_lock:
            game = self._expire_if_needed(session_id)
            if game is None:
                return
            dup_key, entry = G.record_entry(
                game, BOT_SPEAKER_ID, BOT_DISPLAY_NAME, text, time.time()
            )
            if entry is None and dup_key is None:
                return
            self._store.save()

        if dup_key and entry is None and self._game_cfg().bot_wrong_reply_hint:
            await self._send("😆 bot 这句已经说过啦，罚酒一杯！", session_id)

    # ── ⑤ 可选主动接令 ────────────────────────────────────────────

    async def _maybe_proactive(self, session_id: str) -> None:
        """每 N 条玩家诗句后主动作诗一句：生成 → 校验 → 发送 → 入账。"""
        every_n = max(1, self._game_cfg().bot_proactive_every_n)
        async with self._store_lock:
            game = self._expire_if_needed(session_id)
            if game is None or game.player_entry_count() % every_n != 0:
                return

        failure_hint = ""
        for _ in range(3):
            async with self._store_lock:
                game = self._expire_if_needed(session_id)
                if game is None:
                    return
                messages = G.build_poem_messages(game, failure_hint)
            try:
                result = await self.ctx.llm.generate(
                    prompt=messages, task_name="utils"
                )
            except Exception:
                self.ctx.logger.exception("主动接令 llm.generate 失败")
                return
            if not isinstance(result, dict) or not result.get("success"):
                continue
            poem = str(
                result.get("content") or result.get("response") or result.get("text") or ""
            ).strip()
            if not poem:
                continue

            async with self._store_lock:
                game = self._expire_if_needed(session_id)
                if game is None:
                    return
                ok, reason = G.validate_bot_poem(poem, game.keyword, game.used_keys)
                if ok:
                    _, entry = G.record_entry(
                        game,
                        BOT_SPEAKER_ID,
                        BOT_DISPLAY_NAME,
                        poem,
                        time.time(),
                    )
                    if entry is not None:
                        self._store.save()
            if ok:
                # 走 _send 登记指纹：③ 会跳过这条，记录已在这里完成
                await self._send(poem, session_id)
                return
            failure_hint = f"上一次输出不合格（{reason}），请换一句。"

        await self._send("😵 bot 江郎才尽，接不上令了，甘拜下风……", session_id)
