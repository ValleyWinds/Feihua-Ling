# 开发文档（architecture）

> 本目录放**开发向文档**——贡献者与 AI 协作者看这里，README 面向用户，内容不要混。
> 本文件随代码一起维护；规范的代码 + 完善的文档 = 协作难度大幅降低。

## 模块结构

- `plugin.py` — 入口：生命周期三方法（缺一拒载）、运行时状态初始化（GameStore / asyncio.Lock / 自发消息指纹表）、组装 HandlersMixin
- `handlers.py` — 组件层：`/fhl` Command + 3 个 HookHandler + 主动接令流程 + 出/入站消息字段的防御式提取
- `game.py` — 纯逻辑：归一化、判重单元提取、记录语义、状态机、战报/注入文案、GameStore（games.json 读写，原子替换写）
- `t2s_table.py` — 繁→简映射表（纯数据，743 条，一对多取最常用映射）+ `to_simplified()`
- `config.py` — 配置模型：`[plugin]` 节（config_version）+ `[game]` 节（8 项，全默认值中文 label）
- `tests/` — pytest：conftest 隔离加载 plugin.py（相对导入安全姿势）+ 纯逻辑测试 + SDK 注入点测试（fake RPC）

## 数据流

```
群友消息 ─► ① chat.receive.after_process（OBSERVE）
              记录诗句：提取含令字的 4~20 字分句（+整句组合键）→ 判重
              ├─ 全新 → 入账（排行计分）+ 刷新 last_activity + 落盘
              ├─ 含重复 → 发"这句 XX 已经说过啦"（不计分，但新组合键仍入集合）
              └─ bot_proactive 开启且玩家句数达 N → 主动接令（llm.generate
                 task_name="utils" → 本地校验 3 次重试 → 发送 → 入账）

bot 回复 ─► ② maisaka.replyer.before_request（BLOCKING）
              extra_prompt 追加注入"已用诗句清单"（条件式措辞：接令才受约束，
              日常聊天忽略），整份 kwargs 带回；无活跃局返回 None 零干扰

bot 发送 ─► ③ send_service.after_send（OBSERVE，纯观察位）
              自发排除（指纹命中 / 整条归一化 >50 字）→ 提取诗句 → 记录
              bot 说了重复诗句（无任何新单元）→ 发"罚酒一杯"吐槽

用户命令 ─► ④ /fhl（start [<字>] / end|stop / status / history|last）
              惰性过期检查的触点之一；结束局移入 history（保留最近 10 局）
```

持久化：`ctx.paths.data_dir / "games.json"`，内存 dict + 每条新诗句后落盘（tmp + os.replace 原子写），并发由插件层 `asyncio.Lock` 互斥。

## 设计取舍

- **为什么用 `extra_prompt` 注入而不是别的**：清单是动态游戏状态，`maisaka.context.append` 常驻注入会堆积过时条目；`before_model_request` 改 `items` 要处理 `item_schema_version` 严格校验，杀鸡用牛刀。`extra_prompt` 由 Host 包成`【额外回复要求】`追加，且多插件共存时我们做追加不覆盖。
- **宁滥勿缺的记录策略**：漏记诗句 → bot 当众重复（本插件要消灭的核心失败）；误记只是少一句可选诗。所以聊天中含令字的短句也会被顺带记录，可接受。
- **整句 + 分句双层判重键**："春眠不觉晓"与"春眠不觉晓，处处闻啼鸟"互判重；重复发言的整句组合键仍入集合，拦截之后"无标点连读"的变体，但不计排行分。
- **简繁统一在本地做，不交给 LLM**：判重是这个插件的命根子，必须是确定性逻辑；LLM 做字符级比对不可靠，且主动作诗的本地校验依赖同一个集合。归一化输出即清单行文本，LLM 读简体无碍。
- **不走 `ctx.db`**：`db.save` 不会自动建表，自定义模型无官方注册通道；JSON 文件简单可靠。
- **惰性过期防僵尸局**：MaiBot 重启后早已超时的局若继续占着活跃位，每次回复都会注入过时清单污染上下文。三个触点（on_load / 注入前 / 入站记录时）检查 `now - last_activity > timeout` 即静默收官；判定基准是最后诗句时间而非进程启动时间，快速重启不误杀正在玩的局。
- **自发消息排除两层**：`after_send` 无法区分消息来源，插件自己的提示/战报会被误观察——战报分句"1. 春眠不觉晓（张三）"完全符合记录条件会污染集合。主层：功能性发送前登记归一化指纹（FIFO 300 条），观察时命中即跳过；辅层：整条归一化 >50 字直接跳过（战报必然远超，正常接令回复 15~30 字极少超）。
- **主动接令的发送也走指纹登记**：诗句由主动流程自行入账，③ 跳过它——否则会双重记录并触发误吐槽。
- **`[game].enabled` 而非复用 `[plugin].enabled`**：后者是宿主保留键（WebUI 启停 + 激活门禁共用），业务模型声明它会产生冗余开关。业务开关关闭时三个 Hook 全跳过（不记录、不注入、不提示），进行中的局冻结在 games.json 等重开。

## 给接手者（含 AI）的提示

- **归一化规则与 games.json 的兼容性**：落盘存的是归一化后的 keys。若改动 `normalize` / `extract_units` / 繁简表，旧数据的键按旧规则生成，新会话按新规则判重，两者可能对不上——升级规则时考虑版本化或清理旧局。
- **两个已知待实测核对项**（离线测不出）：① 三个 Hook 的 OBSERVE 模式是否被注册期校验放行（不允许会**显式报错拒载**，预案：降级 BLOCKING + 立即 `return None`）；② `send_service.after_send` 的 message dict 里会话字段的真实键名——目前四层防御式提取（session_id / stream_id / group_info.group_id）。
- **Hook kwargs 坑位**：① 的 user_id 嵌套在 `message.message_info.user_info.user_id`；`raw_message` 是组件 dict 列表，必须按 `type=="text"` 过滤（否则图片 URL 参与匹配）；② 的 `modified_kwargs` 是整体替换，整份带回且别删 `hook_name`。
- **Command 三元组**：`result[0]` 被 Host 忽略，intercept 非 0 即拦截，用户可见回复必须自己调 `ctx.send.text()`。
- **`llm.generate` 没有 `system_prompt` 参数**（被 `**kwargs` 吞掉），system 走消息列表；返回完整 dict 先看 `success` 再取 `content`。
- **测试跑法**：`PYTHONPATH=<本地 maibot-plugin-sdk 仓库> uv run --with pytest --with pydantic python -m pytest`（系统 Python 无 SDK 依赖）。
- 预期扩展点：LLM 校验诗句真伪（`game.llm_verify` 已留配置位）、`/fhl` 子命令扩展、双字令等玩法变体。
