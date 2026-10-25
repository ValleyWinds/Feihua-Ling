"""game.py 纯逻辑测试：归一化、判重、繁简、状态机、战报、持久化往返。"""


# ── 归一化 ─────────────────────────────────────────────────────────


def test_normalize_strips_non_cjk(game_mod):
    assert game_mod.normalize("春眠不觉晓，处处闻啼鸟！hello 123") == "春眠不觉晓处处闻啼鸟"


def test_normalize_traditional_to_simplified(game_mod):
    assert game_mod.normalize("春眠不覺曉") == "春眠不觉晓"
    assert game_mod.normalize("煙籠寒水月籠沙") == "烟笼寒水月笼沙"


def test_normalize_empty(game_mod):
    assert game_mod.normalize("") == ""
    assert game_mod.normalize("!!! ... 123") == ""


# ── 判重单元提取 ───────────────────────────────────────────────────


def test_extract_units_filters(game_mod):
    # 太短 / 太长 / 不含令字 的分句都被过滤
    units = game_mod.extract_units("春眠不觉晓，床前明月光，这句很长很长很长很长很长很长很长", "月")
    assert units == ["床前明月光"]


def test_collect_keys_whole_and_segments(game_mod):
    keys = game_mod.collect_keys("春眠不觉晓，处处闻啼鸟", "春")
    assert "春眠不觉晓" in keys
    assert "处处闻啼鸟" not in keys  # 不含令字的分句不进集合
    assert "春眠不觉晓处处闻啼鸟" in keys  # 整句 ≤20 字也入集合


def test_collect_keys_whole_too_long(game_mod):
    # 整句超 20 字时只有含令字的分句入集合
    raw = "春眠不觉晓，处处闻啼鸟，夜来风雨声，花落知多少，春风又绿江南岸"
    keys = game_mod.collect_keys(raw, "春")
    assert keys == ["春眠不觉晓", "春风又绿江南岸"]
    assert game_mod.normalize(raw) not in keys


# ── 记录与判重 ─────────────────────────────────────────────────────


def _make_game(game_mod, keyword="春", now=1000.0):
    return game_mod.Game(
        session_id="s1", keyword=keyword, start_time=now, last_activity=now
    )


def test_record_new_entry(game_mod):
    g = _make_game(game_mod)
    dup, entry = game_mod.record_entry(g, "u1", "张三", "春眠不觉晓", 1001.0)
    assert dup is None and entry is not None
    assert entry.text == "春眠不觉晓"
    assert g.used_keys == {"春眠不觉晓"}
    assert g.last_activity == 1001.0


def test_record_duplicate_same_text(game_mod):
    g = _make_game(game_mod)
    game_mod.record_entry(g, "u1", "张三", "春眠不觉晓", 1001.0)
    dup, entry = game_mod.record_entry(g, "u2", "李四", "春眠不觉晓", 1002.0)
    assert dup == "春眠不觉晓" and entry is None


def test_record_duplicate_half_sentence(game_mod):
    # 半句判重："春眠不觉晓" 已说过，"春眠不觉晓，处处闻啼鸟" 判重、不计排行
    g = _make_game(game_mod)
    game_mod.record_entry(g, "u1", "张三", "春眠不觉晓", 1001.0)
    dup, entry = game_mod.record_entry(g, "u2", "李四", "春眠不觉晓，处处闻啼鸟", 1002.0)
    assert dup == "春眠不觉晓"
    assert entry is None
    assert len(g.entries) == 1
    # 组合键仍入判重集合：拦截之后"无标点连读"的变体
    assert "春眠不觉晓处处闻啼鸟" in g.used_keys
    assert g.last_activity == 1002.0  # 重复发言也算游戏活跃


def test_record_traditional_duplicate(game_mod):
    # 繁体与简体互判重
    g = _make_game(game_mod)
    game_mod.record_entry(g, "u1", "张三", "春眠不觉晓", 1001.0)
    dup, entry = game_mod.record_entry(g, "u2", "李四", "春眠不覺曉", 1002.0)
    assert dup == "春眠不觉晓" and entry is None


def test_record_partial_dup_still_records_new(game_mod):
    # 部分重复：不计排行，但全新含令字分句进判重集合（宁滥勿缺，防 bot 之后重复它）
    g = _make_game(game_mod)
    game_mod.record_entry(g, "u1", "张三", "春眠不觉晓", 1001.0)
    dup, entry = game_mod.record_entry(g, "u2", "李四", "春眠不觉晓，春江水暖鸭先知", 1002.0)
    assert dup == "春眠不觉晓"
    assert entry is None
    assert "春江水暖鸭先知" in g.used_keys
    assert len(g.entries) == 1


def test_record_ignores_non_poem(game_mod):
    g = _make_game(game_mod)
    dup, entry = game_mod.record_entry(g, "u1", "张三", "今天天气不错", 1001.0)
    assert dup is None and entry is None
    assert g.entries == []


def test_find_owner(game_mod):
    g = _make_game(game_mod)
    game_mod.record_entry(g, "u1", "张三", "春眠不觉晓", 1001.0)
    assert game_mod.find_owner(g, "春眠不觉晓") == "张三"


def test_player_entry_count_excludes_bot(game_mod):
    g = _make_game(game_mod)
    game_mod.record_entry(g, "u1", "张三", "春眠不觉晓", 1001.0)
    game_mod.record_entry(g, game_mod.BOT_SPEAKER_ID, "bot", "春江水暖鸭先知", 1002.0)
    assert g.player_entry_count() == 1
    assert len(g.entries) == 2


# ── 过期与校验 ─────────────────────────────────────────────────────


def test_is_expired(game_mod):
    g = _make_game(game_mod, now=1000.0)
    assert not game_mod.is_expired(g, 1000.0 + 599, 10)
    assert game_mod.is_expired(g, 1000.0 + 600, 10)


def test_validate_bot_poem(game_mod):
    used = {"春眠不觉晓"}
    ok, _ = game_mod.validate_bot_poem("春江水暖鸭先知", "春", used)
    assert ok
    ok, reason = game_mod.validate_bot_poem("春眠不觉晓", "春", used)
    assert not ok and "已经有人说过" in reason
    ok, reason = game_mod.validate_bot_poem("白日依山尽", "春", used)
    assert not ok and "令字" in reason


# ── 文案 ───────────────────────────────────────────────────────────


def test_build_injection_wording(game_mod):
    g = _make_game(game_mod)
    game_mod.record_entry(g, "u1", "张三", "春眠不觉晓", 1001.0)
    text = game_mod.build_injection(g, max_entries_hint=50)
    assert "令字「春」" in text
    assert "忽略本清单" in text  # 条件式措辞，防日常聊天被带偏
    assert "1.春眠不觉晓" in text
    assert "枯竭" not in text  # 未达提示线


def test_build_injection_exhaust_hint(game_mod):
    g = _make_game(game_mod)
    # 用汉字池生成唯一诗句键（归一化会丢弃数字，不能用编号）
    chars = "风花雪月山水云天江河湖海"
    for i in range(50):
        key = "春" + chars[i % 11] + chars[(i // 11) % 11] + chars[i % 7] + "诗"
        dup, entry = game_mod.record_entry(g, "u1", "张三", key, 1001.0 + i)
        assert entry is not None, f"第 {i} 条应记录成功: {key}"
    text = game_mod.build_injection(g, max_entries_hint=50)
    assert "枯竭" in text


def test_build_report_leaderboard(game_mod):
    g = _make_game(game_mod)
    game_mod.record_entry(g, "u1", "张三", "春眠不觉晓", 1001.0)
    game_mod.record_entry(g, "u1", "张三", "春江水暖鸭先知", 1002.0)
    game_mod.record_entry(g, game_mod.BOT_SPEAKER_ID, "bot", "春色满园关不住", 1003.0)
    report = game_mod.build_report(g)
    assert "「春」" in report
    assert "张三 ×2" in report
    assert "bot ×1" in report


def test_build_poem_messages_with_failure_hint(game_mod):
    g = _make_game(game_mod)
    messages = game_mod.build_poem_messages(g, "上一次输出不合格")
    assert messages[0]["role"] == "system"
    assert "「春」" in messages[1]["content"]
    assert "上一次输出不合格" in messages[1]["content"]


# ── 持久化 ─────────────────────────────────────────────────────────


def test_store_roundtrip(tmp_path, game_mod):
    store = game_mod.GameStore(tmp_path / "games.json")
    store.load()
    g = _make_game(game_mod)
    game_mod.record_entry(g, "u1", "张三", "春眠不觉晓，处处闻啼鸟", 1001.0)
    store.start(g)

    store2 = game_mod.GameStore(tmp_path / "games.json")
    store2.load()
    assert "s1" in store2.active
    g2 = store2.active["s1"]
    assert g2.keyword == "春"
    # used_keys 从 entries 重建，判重能力完整恢复
    assert "春眠不觉晓" in g2.used_keys
    dup, _ = game_mod.record_entry(g2, "u2", "李四", "春眠不觉晓", 2000.0)
    assert dup == "春眠不觉晓"


def test_store_finish_and_history(tmp_path, game_mod):
    store = game_mod.GameStore(tmp_path / "games.json")
    store.load()
    g = _make_game(game_mod)
    game_mod.record_entry(g, "u1", "张三", "春眠不觉晓", 1001.0)
    store.start(g)

    finished = store.finish("s1", "manual", 2000.0)
    assert finished is not None
    assert "s1" not in store.active
    assert len(store.history) == 1

    store2 = game_mod.GameStore(tmp_path / "games.json")
    store2.load()
    assert not store2.active
    report = store2.last_report()
    assert report and "春眠不觉晓" not in report  # 战报是排行不含诗句原文
    assert "张三 ×1" in report


def test_store_history_limit(tmp_path, game_mod):
    store = game_mod.GameStore(tmp_path / "games.json")
    store.load()
    for i in range(game_mod.HISTORY_LIMIT + 3):
        g = game_mod.Game(
            session_id=f"s{i}", keyword="春", start_time=1.0, last_activity=2.0
        )
        store.start(g)
        store.finish(f"s{i}", "manual", 3.0)
    assert len(store.history) == game_mod.HISTORY_LIMIT
