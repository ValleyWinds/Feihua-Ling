"""飞花令插件配置模型。

基于 MaiBot SDK PluginConfigBase，声明 config_model 后 self.config 返回强类型实例，
WebUI Schema 自动生成。所有字段必须有默认值；[plugin].config_version 为强校验字段。
[plugin].enabled 是宿主保留键（WebUI 启停开关与激活门禁共用），模型不声明；
业务级总开关放在 [game].enabled。
"""

from __future__ import annotations

from typing import ClassVar

from maibot_sdk import Field, PluginConfigBase

CONFIG_SCHEMA_VERSION = "1.0.0"


class PluginSection(PluginConfigBase):
    """配置版本信息（自动维护）。"""

    __ui_label__: ClassVar[str] = "插件设置"

    config_version: str = Field(
        default=CONFIG_SCHEMA_VERSION,
        description="配置版本，请勿手动修改。",
        json_schema_extra={"disabled": True, "hidden": True, "label": "配置版本", "order": 99},
    )


class GameSection(PluginConfigBase):
    """飞花令游戏行为设置。"""

    __ui_label__: ClassVar[str] = "游戏设置"
    __ui_order__: ClassVar[int] = 1

    enabled: bool = Field(
        default=True,
        description="飞花令总开关。关闭后不再记录诗句、不再向 bot 注入清单，命令只回复已关闭。",
        json_schema_extra={"label": "启用飞花令"},
    )
    timeout_minutes: int = Field(
        default=10,
        description="一局游戏在多少分钟内没有新诗句后自动收官（静默结束，不发送消息）。",
        json_schema_extra={"label": "超时收官（分钟）"},
    )
    max_entries_hint: int = Field(
        default=50,
        description="已用诗句累计超过多少条后，提示 bot 该令字的诗句可能已枯竭、建议收官。",
        json_schema_extra={"label": "枯竭提示线（条）"},
    )
    bot_proactive: bool = Field(
        default=False,
        description="开启后 bot 会主动接令：每累计若干条玩家诗句后自动作诗一句。默认关闭，bot 只在被点名时接令。",
        json_schema_extra={"label": "bot 主动接令"},
    )
    bot_proactive_every_n: int = Field(
        default=5,
        description="bot 主动接令的间隔：每累计多少条玩家诗句后 bot 跟一句。",
        json_schema_extra={"label": "主动接令间隔（条）"},
    )
    bot_wrong_reply_hint: bool = Field(
        default=True,
        description="bot 说了重复诗句时，是否当场发提示（罚酒一杯）。",
        json_schema_extra={"label": "bot 重复提示"},
    )
    llm_verify: bool = Field(
        default=False,
        description="是否用 LLM 校验玩家诗句真伪（更慢且消耗模型额度，默认关闭）。",
        json_schema_extra={"label": "LLM 校验诗句真伪"},
    )
    default_keywords: list[str] = Field(
        default_factory=lambda: ["花", "月", "风", "雪", "春", "江", "水", "山"],
        description="开局未指定令字时，从这个池子里随机抽取令字。",
        json_schema_extra={"label": "随机令字池"},
    )


class FeihuaLingConfig(PluginConfigBase):
    """飞花令插件完整配置。"""

    __ui_label__: ClassVar[str] = "飞花令"

    plugin: PluginSection = Field(default_factory=PluginSection)
    game: GameSection = Field(default_factory=GameSection)
