"""飞花令游戏核心逻辑：归一化、判重、状态机、战报、持久化。

纯逻辑模块，不依赖 maibot_sdk，可独立单测。设计要点见 PLAN.md §4/§5：
- 宁滥勿缺：漏记诗句会导致 bot 当众重复（核心失败），误记只是少一句可选诗；
- 整句与分句互判重：整条消息归一化后（≤20 字）与各分句一起进入判重集合；
- 持久化带 last_activity，配合惰性过期检查防止重启后的僵尸局污染上下文。
"""

from __future__ import annotations

import json
import os
import re
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from .t2s_table import to_simplified

# 诗句判重单元的长度区间（归一化后）
MIN_UNIT_LEN = 4
MAX_UNIT_LEN = 20
# ③ 号 Hook 的整条超长护栏：bot 出站消息归一化后超过该长度直接忽略（战报/长闲聊）
MAX_OUTBOUND_WHOLE_LEN = 50
# 历史局保留数量
HISTORY_LIMIT = 10
# bot 自己在排行榜上的显示名
BOT_SPEAKER_ID = "__bot__"
BOT_DISPLAY_NAME = "bot"

_SPLIT_RE = re.compile(
    r"[，。！？；、,\.\?\!;:\s…—·\"'“”‘’（）()《》〈〉【】\[\]！？]+"
)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")


# ── 归一化与判重单元 ────────────────────────────────────────────────


def normalize(text: str) -> str:
    """只保留 CJK 汉字并统一为简体。"""
    cjk_only = "".join(_CJK_RE.findall(text or ""))
    return to_simplified(cjk_only)


def extract_units(raw: str, keyword: str) -> list[str]:
    """提取判重单元：按标点/空白切分 → 各段归一化 → 4~20 字且含令字（保序去重）。"""
    units: list[str] = []
    seen: set[str] = set()
    for seg in _SPLIT_RE.split(raw or ""):
        norm = normalize(seg)
        if (
            MIN_UNIT_LEN <= len(norm) <= MAX_UNIT_LEN
            and keyword in norm
            and norm not in seen
        ):
            seen.add(norm)
            units.append(norm)
    return units


def collect_keys(raw: str, keyword: str) -> list[str]:
    """一条发言贡献的全部判重键：分句单元 + 整句（若整句 ≤20 字且含令字）。"""
    keys = extract_units(raw, keyword)
    whole = normalize(raw)
    if (
        MIN_UNIT_LEN <= len(whole) <= MAX_UNIT_LEN
        and keyword in whole
        and whole not in keys
    ):
        keys.append(whole)
    return keys


# ── 数据结构 ───────────────────────────────────────────────────────


@dataclass
class PoemEntry:
    """一次有效发言。keys 是它贡献的全部判重键，text 是代表性展示文本。"""

    text: str
    keys: list[str]
    speaker_id: str
    speaker_name: str
    timestamp: float


@dataclass
class Game:
    """一局进行中的飞花令。used_keys 不落盘，加载时从 entries 重建。"""

    session_id: str
    keyword: str
    start_time: float
    last_activity: float
    entries: list[PoemEntry] = field(default_factory=list)
    used_keys: set[str] = field(default_factory=set)

    def rebuild_keys(self) -> None:
        self.used_keys = {k for e in self.entries for k in e.keys}

    def player_entry_count(self) -> int:
        """非 bot 的有效句数（主动接令的计数基准，其他 bot 也算玩家）。"""
        return sum(1 for e in self.entries if e.speaker_id != BOT_SPEAKER_ID)

    def leaderboard(self) -> list[tuple[str, int]]:
        """按有效句数降序的 (显示名, 句数) 排行。"""
        counter = Counter(e.speaker_name or e.speaker_id for e in self.entries)
        return counter.most_common()


# ── 状态机 ─────────────────────────────────────────────────────────


def record_entry(
    game: Game,
    speaker_id: str,
    speaker_name: str,
    raw: str,
    now: float,
) -> tuple[str | None, PoemEntry | None]:
    """记录一条发言，返回 (重复的判重键, 新增条目)。

    - 发言含重复（任一单元已用过）→ 不计入排行条目，但全新单元仍进判重集合
      （宁滥勿缺——组合键能拦截「无标点连读」的变体重复）；
    - 只要识别为诗句（含有效单元）就刷新 last_activity；
    - 发言不含有效单元时返回 (None, None)，不刷新活动时间。
    """
    units = extract_units(raw, game.keyword)
    if not units:
        return None, None
    whole = normalize(raw)
    if (
        MIN_UNIT_LEN <= len(whole) <= MAX_UNIT_LEN
        and game.keyword in whole
        and whole not in units
    ):
        all_keys = units + [whole]
    else:
        all_keys = units

    dup_key = next((k for k in all_keys if k in game.used_keys), None)

    new_keys: list[str] = []
    for k in all_keys:
        if k not in game.used_keys and k not in new_keys:
            new_keys.append(k)
    game.used_keys.update(new_keys)
    game.last_activity = now

    entry: PoemEntry | None = None
    if new_keys and dup_key is None:
        entry = PoemEntry(
            text=new_keys[0],
            keys=new_keys,
            speaker_id=speaker_id,
            speaker_name=speaker_name,
            timestamp=now,
        )
        game.entries.append(entry)
    return dup_key, entry


def find_owner(game: Game, key: str) -> str:
    """返回某个判重键的首个发言者显示名。"""
    for e in game.entries:
        if key in e.keys:
            return e.speaker_name or e.speaker_id
    return "某人"


def is_expired(game: Game, now: float, timeout_minutes: int) -> bool:
    return now - game.last_activity >= timeout_minutes * 60


def validate_bot_poem(text: str, keyword: str, used_keys: set[str]) -> tuple[bool, str]:
    """主动作诗的本地校验：含令字的有效分句 + 不与已用集合重复。"""
    units = extract_units(text, keyword)
    if not units:
        return False, "没有找到含令字的 4~20 字分句"
    for u in units:
        if u in used_keys:
            return False, f"「{u}」已经有人说过"
    return True, ""


# ── 文案生成 ───────────────────────────────────────────────────────


def build_injection(game: Game, max_entries_hint: int) -> str:
    """注入 bot 回复请求的额外要求（条件式措辞，防止把日常聊天带偏）。"""
    lines = "\n".join(f"{i}.{e.text}" for i, e in enumerate(game.entries, 1))
    head = (
        f"\n【飞花令进行中·令字「{game.keyword}」】若本轮你要接令作诗，诗句必须含"
        f"「{game.keyword}」字，且不得与以下任何已用诗句重复（整句或其中的分句都不行）；"
        f"若只是日常聊天，请忽略本清单：\n{lines}"
    )
    if len(game.entries) >= max_entries_hint:
        head += (
            f"\n（已用诗句已达 {len(game.entries)} 条，含「{game.keyword}」的诗句可能已枯竭，"
            f"建议自然收官）"
        )
    return head


def build_report(game: Game) -> str:
    """收官战报。"""
    minutes = max(1, round((game.last_activity - game.start_time) / 60))
    lines = [f"🌸 飞花令「{game.keyword}」收官！共 {len(game.entries)} 句诗，历时约 {minutes} 分钟"]
    board = game.leaderboard()
    if board:
        rank_lines = [f"{i}. {name} ×{count}" for i, (name, count) in enumerate(board, 1)]
        lines.append("🏅 排行：\n" + "\n".join(rank_lines))
    else:
        lines.append("本局没有人对出诗句……")
    return "\n".join(lines)


def build_status(game: Game) -> str:
    """当前局状态。"""
    minutes = max(0, round((time.time() - game.start_time) / 60))
    players = len({e.speaker_id for e in game.entries}) or "暂无"
    return (
        f"🎋 本局飞花令令字：「{game.keyword}」，已用 {len(game.entries)} 句诗，"
        f"{players} 人参与，已进行约 {minutes} 分钟"
    )


def build_poem_messages(game: Game, failure_hint: str = "") -> list[dict[str, str]]:
    """主动接令的 LLM 消息列表（system 走消息列表，generate 无 system_prompt 参数）。"""
    used = "\n".join(f"{i}.{e.text}" for i, e in enumerate(game.entries, 1)) or "（暂无）"
    hint = f"\n注意：{failure_hint}" if failure_hint else ""
    return [
        {
            "role": "system",
            "content": "你是飞花令游戏中的参赛选手，精通中国古典诗词，只输出真实存在的诗句。",
        },
        {
            "role": "user",
            "content": (
                f"当前飞花令令字为「{game.keyword}」。请对出一句真实存在、含「{game.keyword}」字的"
                f"中国古典诗词句子，直接输出诗句本身，不要作者、不要解释、不要引号。{hint}\n"
                f"以下是已用诗句清单，你出的句子（含其任何分句）不得与之重复：\n{used}"
            ),
        },
    ]


# ── 持久化 ─────────────────────────────────────────────────────────


def _entry_to_dict(e: PoemEntry) -> dict:
    return {
        "text": e.text,
        "keys": e.keys,
        "speaker_id": e.speaker_id,
        "speaker_name": e.speaker_name,
        "timestamp": e.timestamp,
    }


def _entry_from_dict(d: dict) -> PoemEntry:
    return PoemEntry(
        text=str(d.get("text", "")),
        keys=[str(k) for k in d.get("keys", [])],
        speaker_id=str(d.get("speaker_id", "")),
        speaker_name=str(d.get("speaker_name", "")),
        timestamp=float(d.get("timestamp", 0.0)),
    )


def _game_to_dict(g: Game, extra: dict | None = None) -> dict:
    d = {
        "session_id": g.session_id,
        "keyword": g.keyword,
        "start_time": g.start_time,
        "last_activity": g.last_activity,
        "entries": [_entry_to_dict(e) for e in g.entries],
    }
    if extra:
        d.update(extra)
    return d


def _game_from_dict(d: dict) -> Game:
    g = Game(
        session_id=str(d.get("session_id", "")),
        keyword=str(d.get("keyword", "")),
        start_time=float(d.get("start_time", 0.0)),
        last_activity=float(d.get("last_activity", 0.0)),
        entries=[_entry_from_dict(e) for e in d.get("entries", [])],
    )
    g.rebuild_keys()
    return g


class GameStore:
    """games.json 的内存态 + 落盘。调用方负责并发互斥（asyncio.Lock 在插件层）。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.active: dict[str, Game] = {}
        self.history: list[dict] = []

    def load(self) -> None:
        self.active = {}
        self.history = []
        if not self.path.exists():
            return
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        for sid, gd in (data.get("active") or {}).items():
            game = _game_from_dict(gd)
            if game.session_id and game.keyword:
                self.active[sid] = game
        self.history = [h for h in (data.get("history") or []) if isinstance(h, dict)]

    def save(self) -> None:
        payload = {
            "version": 1,
            "active": {sid: _game_to_dict(g) for sid, g in self.active.items()},
            "history": self.history[:HISTORY_LIMIT],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        os.replace(tmp, self.path)

    def start(self, game: Game) -> None:
        self.active[game.session_id] = game
        self.save()

    def finish(self, session_id: str, end_reason: str, now: float) -> Game | None:
        """把活跃局移入历史，返回该局（无局返回 None）。"""
        game = self.active.pop(session_id, None)
        if game is None:
            return None
        self.history.insert(
            0, _game_to_dict(game, {"end_time": now, "end_reason": end_reason})
        )
        del self.history[HISTORY_LIMIT:]
        self.save()
        return game

    def last_report(self) -> str | None:
        """最近一局的历史战报文本，无历史返回 None。"""
        for h in self.history:
            report = h.get("report")
            if report:
                return str(report)
            game = _game_from_dict(h)
            if game.entries or game.keyword:
                return build_report(game)
        return None
