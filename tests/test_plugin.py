"""SDK 测试注入点：真实 SDK、只假最外层 RPC 通道。

跑法（本地有任意可用 Python 即可，不需要 MaiBot 宿主）：

    uv run --with pytest --with pydantic python -m pytest

其中 maibot_sdk 取自本地源码仓库或已安装的 SDK；详见 references/plugin-testing.md 第 1 层。
"""

import asyncio
import json

from maibot_sdk.context import PluginContext

PLUGIN_ID = "valleywinds.feihua-ling"


def make_message(text, session_id="s1", user_id="u1", nickname="张三"):
    """构造 chat.receive / send_service Hook 的序列化 message dict。"""
    return {
        "processed_plain_text": text,
        "message_info": {
            "session_id": session_id,
            "user_info": {"user_id": user_id, "user_nickname": nickname},
        },
    }


def make_plugin(plugin_module, tmp_path, sent_texts=None, llm_replies=None, game_cfg=None):
    """组装带 fake RPC 的插件实例并初始化运行时状态。"""
    sent = sent_texts if sent_texts is not None else []
    llm_replies = llm_replies if llm_replies is not None else []

    async def fake_rpc(method, plugin_id, payload):
        assert method == "cap.call"
        cap = payload["capability"]
        if cap == "send.text":
            sent.append(payload["args"]["text"])
            return {"success": True}
        if cap == "llm.generate":
            reply = llm_replies.pop(0) if llm_replies else {"success": False}
            return reply if isinstance(reply, dict) and "success" in reply else {"success": True, "content": str(reply)}
        raise AssertionError(f"未预期的能力调用: {cap}")

    plugin = plugin_module.create_plugin()
    plugin._set_context(PluginContext(PLUGIN_ID, rpc_call=fake_rpc))

    cfg = {"plugin": {"config_version": "1.0.0"}, "game": {}}
    cfg["game"].update(game_cfg or {})
    plugin.set_plugin_config(cfg)

    store = plugin_module.game.GameStore(tmp_path / "games.json")
    store.load()
    plugin._store = store
    return plugin


def run(coro):
    return asyncio.run(coro)


# ── 配置注入 ───────────────────────────────────────────────────────


def test_config_injection(plugin_module, tmp_path):
    plugin = make_plugin(plugin_module, tmp_path, game_cfg={"enabled": False, "timeout_minutes": 3})
    assert plugin.config.plugin.config_version == "1.0.0"
    assert plugin.config.game.enabled is False
    assert plugin.config.game.timeout_minutes == 3
    assert plugin.config.game.default_keywords == ["花", "月", "风", "雪", "春", "江", "水", "山"]


# ── Command：/fhl ──────────────────────────────────────────────────


def test_cmd_start_sends_rules(plugin_module, tmp_path):
    sent = []
    plugin = make_plugin(plugin_module, tmp_path, sent_texts=sent)
    ok, msg, level = run(plugin.cmd_fhl(stream_id="s1", matched_groups={"arg": "花"}))
    assert ok is True and level == 1
    assert "令字：「花」" in sent[0]
    assert "s1" in plugin._store.active
    assert plugin._store.active["s1"].keyword == "花"


def test_cmd_start_random_keyword(plugin_module, tmp_path):
    sent = []
    plugin = make_plugin(plugin_module, tmp_path, sent_texts=sent)
    run(plugin.cmd_fhl(stream_id="s1", matched_groups=None))
    assert plugin._store.active["s1"].keyword in plugin.config.game.default_keywords


def test_cmd_start_while_running(plugin_module, tmp_path):
    sent = []
    plugin = make_plugin(plugin_module, tmp_path, sent_texts=sent)
    run(plugin.cmd_fhl(stream_id="s1", matched_groups={"arg": "花"}))
    run(plugin.cmd_fhl(stream_id="s1", matched_groups={"arg": "月"}))
    assert "进行中" in sent[-1]
    assert plugin._store.active["s1"].keyword == "花"


def test_cmd_end_publishes_report(plugin_module, tmp_path):
    sent = []
    plugin = make_plugin(plugin_module, tmp_path, sent_texts=sent)
    run(plugin.cmd_fhl(stream_id="s1", matched_groups={"arg": "花"}))
    run(plugin.on_player_message(message=make_message("花间一壶酒", nickname="张三")))
    ok, _, level = run(plugin.cmd_fhl(stream_id="s1", matched_groups={"arg": "end"}))
    assert level == 1
    assert any("张三 ×1" in t for t in sent)
    assert "s1" not in plugin._store.active
    # 历史可查
    ok, _, _ = run(plugin.cmd_fhl(stream_id="s1", matched_groups={"arg": "history"}))
    assert any("上一局战报" in t for t in sent)


def test_cmd_unknown_arg_shows_usage(plugin_module, tmp_path):
    sent = []
    plugin = make_plugin(plugin_module, tmp_path, sent_texts=sent)
    run(plugin.cmd_fhl(stream_id="s1", matched_groups={"arg": "乱七八糟"}))
    assert any("/fhl" in t for t in sent)


def test_cmd_disabled(plugin_module, tmp_path):
    sent = []
    plugin = make_plugin(plugin_module, tmp_path, sent_texts=sent, game_cfg={"enabled": False})
    run(plugin.cmd_fhl(stream_id="s1", matched_groups={"arg": "花"}))
    assert "已关闭" in sent[0]
    assert not plugin._store.active


# ── ① 入站记录 + 重复提示 ──────────────────────────────────────────


def test_player_message_records_poem(plugin_module, tmp_path):
    sent = []
    plugin = make_plugin(plugin_module, tmp_path, sent_texts=sent)
    run(plugin.cmd_fhl(stream_id="s1", matched_groups={"arg": "春"}))
    run(plugin.on_player_message(message=make_message("春眠不觉晓", user_id="u1", nickname="张三")))
    g = plugin._store.active["s1"]
    assert g.entries[0].text == "春眠不觉晓"
    assert g.entries[0].speaker_name == "张三"


def test_player_duplicate_gets_hint(plugin_module, tmp_path):
    sent = []
    plugin = make_plugin(plugin_module, tmp_path, sent_texts=sent)
    run(plugin.cmd_fhl(stream_id="s1", matched_groups={"arg": "春"}))
    run(plugin.on_player_message(message=make_message("春眠不觉晓", user_id="u1", nickname="张三")))
    run(plugin.on_player_message(message=make_message("春眠不觉晓", user_id="u2", nickname="李四")))
    assert any("张三 已经说过" in t for t in sent)


def test_player_message_no_game_ignored(plugin_module, tmp_path):
    plugin = make_plugin(plugin_module, tmp_path)
    run(plugin.on_player_message(message=make_message("春眠不觉晓")))
    assert plugin._store.active == {}


def test_disabled_hook_ignores_messages(plugin_module, tmp_path):
    plugin = make_plugin(plugin_module, tmp_path, game_cfg={"enabled": False})
    run(plugin.cmd_fhl(stream_id="s1", matched_groups={"arg": "春"}))  # 开局也被关
    plugin._store.active["s1"] = plugin_module.game.Game(
        session_id="s1", keyword="春", start_time=1.0, last_activity=1.0
    )
    run(plugin.on_player_message(message=make_message("春眠不觉晓")))
    assert plugin._store.active["s1"].entries == []


# ── ② extra_prompt 注入 ────────────────────────────────────────────


def test_inject_extra_prompt(plugin_module, tmp_path):
    plugin = make_plugin(plugin_module, tmp_path)
    run(plugin.cmd_fhl(stream_id="s1", matched_groups={"arg": "春"}))
    run(plugin.on_player_message(message=make_message("春眠不觉晓", nickname="张三")))

    kwargs = {
        "hook_name": "maisaka.replyer.before_request",
        "session_id": "s1",
        "request_type": "reply",
        "task_name": "replyer",
        "model_name": "",
        "extra_prompt": "已有要求",
        "attempt": 1,
        "retry_count": 0,
        "max_retries": 3,
        "reply_message_id": "m1",
        "reply_reason": "",
        "selected_expression_ids": [],
        "reply_tool_args": {},
    }
    result = run(plugin.inject_used_poems(**dict(kwargs)))
    assert result is not None and result["action"] == "continue"
    # modified_kwargs 整体替换：extra_prompt 为追加且其余键原样带回（含 hook_name）
    modified = result["modified_kwargs"]
    assert modified["extra_prompt"].startswith("已有要求")
    assert "春眠不觉晓" in modified["extra_prompt"]
    assert modified["session_id"] == "s1"
    assert modified["reply_message_id"] == "m1"
    assert modified["hook_name"] == "maisaka.replyer.before_request"


def test_inject_no_game_returns_none(plugin_module, tmp_path):
    plugin = make_plugin(plugin_module, tmp_path)
    result = run(plugin.inject_used_poems(session_id="other"))
    assert result is None


def test_inject_game_without_entries_returns_none(plugin_module, tmp_path):
    plugin = make_plugin(plugin_module, tmp_path)
    run(plugin.cmd_fhl(stream_id="s1", matched_groups={"arg": "春"}))
    result = run(plugin.inject_used_poems(session_id="s1"))
    assert result is None


# ── ③ 出站记录 + 自发消息排除 ──────────────────────────────────────


def test_bot_message_records_poem(plugin_module, tmp_path):
    sent = []
    plugin = make_plugin(plugin_module, tmp_path, sent_texts=sent)
    run(plugin.cmd_fhl(stream_id="s1", matched_groups={"arg": "春"}))
    run(plugin.on_bot_message(message=make_message("春风又绿江南岸"), sent=True))
    g = plugin._store.active["s1"]
    assert g.entries[0].speaker_id == plugin_module.game.BOT_SPEAKER_ID


def test_own_functional_message_excluded(plugin_module, tmp_path):
    """插件功能性发送（重复提示等）登记指纹后，③ 不得把它记成 bot 诗句。"""
    sent = []
    plugin = make_plugin(plugin_module, tmp_path, sent_texts=sent)
    run(plugin.cmd_fhl(stream_id="s1", matched_groups={"arg": "春"}))
    # 模拟：① 收到玩家重复 → 插件发出提示（走 _send 登记指纹）
    run(plugin.on_player_message(message=make_message("春眠不觉晓", nickname="张三")))
    run(plugin.on_player_message(message=make_message("春眠不觉晓", user_id="u2", nickname="李四")))
    hint_text = next(t for t in sent if "已经说过" in t)
    # ③ 现在观察到这条提示消息（出站回环）
    run(plugin.on_bot_message(message=make_message(hint_text), sent=True))
    g = plugin._store.active["s1"]
    assert all("已经说过" not in e.text for e in g.entries)
    assert not any(t for t in sent if "罚酒" in t)  # 也不应触发吐槽


def test_bot_duplicate_gets_tease(plugin_module, tmp_path):
    sent = []
    plugin = make_plugin(plugin_module, tmp_path, sent_texts=sent)
    run(plugin.cmd_fhl(stream_id="s1", matched_groups={"arg": "春"}))
    run(plugin.on_player_message(message=make_message("春眠不觉晓", nickname="张三")))
    run(plugin.on_bot_message(message=make_message("春眠不觉晓"), sent=True))
    assert any("罚酒" in t for t in sent)


def test_overlong_outbound_ignored(plugin_module, tmp_path):
    """整条超 50 字的 bot 消息（战报形态）不进入记录。"""
    plugin = make_plugin(plugin_module, tmp_path)
    run(plugin.cmd_fhl(stream_id="s1", matched_groups={"arg": "春"}))
    report_like = "🌸 飞花令收官！\n" + "\n".join(f"{i}.春诗句编号{i:02d}" for i in range(20))
    run(plugin.on_bot_message(message=make_message(report_like), sent=True))
    assert plugin._store.active["s1"].entries == []


# ── ⑤ 主动接令 ─────────────────────────────────────────────────────


def test_proactive_poem(plugin_module, tmp_path):
    sent = []
    plugin = make_plugin(
        plugin_module,
        tmp_path,
        sent_texts=sent,
        llm_replies=["春江水暖鸭先知"],
        game_cfg={"bot_proactive": True, "bot_proactive_every_n": 1},
    )
    run(plugin.cmd_fhl(stream_id="s1", matched_groups={"arg": "春"}))
    run(plugin.on_player_message(message=make_message("春眠不觉晓", nickname="张三")))
    # 主动诗句已发送并入账
    assert "春江水暖鸭先知" in sent
    g = plugin._store.active["s1"]
    assert any(e.speaker_id == plugin_module.game.BOT_SPEAKER_ID for e in g.entries)


def test_proactive_retries_then_gives_up(plugin_module, tmp_path):
    sent = []
    plugin = make_plugin(
        plugin_module,
        tmp_path,
        sent_texts=sent,
        llm_replies=["春眠不觉晓", "春眠不觉晓", "春眠不觉晓"],  # 全是重复句
        game_cfg={"bot_proactive": True, "bot_proactive_every_n": 1},
    )
    run(plugin.cmd_fhl(stream_id="s1", matched_groups={"arg": "春"}))
    run(plugin.on_player_message(message=make_message("春眠不觉晓", nickname="张三")))
    assert any("江郎才尽" in t for t in sent)


# ── 持久化文件 ─────────────────────────────────────────────────────


def test_games_json_written(plugin_module, tmp_path):
    plugin = make_plugin(plugin_module, tmp_path)
    run(plugin.cmd_fhl(stream_id="s1", matched_groups={"arg": "春"}))
    run(plugin.on_player_message(message=make_message("春眠不觉晓", nickname="张三")))
    data = json.loads((tmp_path / "games.json").read_text(encoding="utf-8"))
    assert "s1" in data["active"]
    assert data["active"]["s1"]["keyword"] == "春"


def test_lazy_expiry_on_inject(plugin_module, tmp_path):
    """超时局在注入触点被静默收官，不再污染上下文。"""
    plugin = make_plugin(plugin_module, tmp_path)
    run(plugin.cmd_fhl(stream_id="s1", matched_groups={"arg": "春"}))
    # 模拟一局早已超时（timeout_minutes 默认 10 分钟）
    plugin._store.active["s1"].last_activity -= 3600
    result = run(plugin.inject_used_poems(session_id="s1"))
    assert result is None
    assert "s1" not in plugin._store.active
    assert plugin._store.history  # 进了历史
