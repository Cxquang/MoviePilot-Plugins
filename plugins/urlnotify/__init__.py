# -*- coding: utf-8 -*-
"""
URL 消息通知插件
外部服务通过调用 MoviePilot 的 URL 向 MoviePilot 发送消息，
再由 MoviePilot 转发到企业微信等已配置的通知渠道。
支持配置默认通知渠道，也支持每次调用时通过 channel 参数指定渠道。
"""
from datetime import datetime
from typing import List, Dict, Optional

from fastapi import Body

from app.core.config import settings
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import MessageChannel, NotificationType


class UrlNotify(_PluginBase):
    """URL 消息通知：外部服务通过 URL 调用推送消息到通知渠道"""

    # 插件元数据
    plugin_name = "UrlNotify"
    plugin_desc = ("外部服务通过 URL 调用向 MoviePilot 发送消息，"
                   "由 MoviePilot 转发到企业微信等已配置的通知渠道；"
                   "支持配置默认渠道，也支持每次调用指定渠道与消息类型。")
    plugin_version = "1.0.0"
    plugin_author = "Cxquang"
    plugin_icon = "Wecom_A.png"
    plugin_order = 100
    auth_level = 1

    # 渠道映射表（支持英文枚举名与中文名，不区分大小写，如 wechat / 微信 / WeChat）
    _CHANNEL_MAP: Dict[str, MessageChannel] = {}
    for _c in MessageChannel:
        _CHANNEL_MAP[_c.name.lower()] = _c
        _CHANNEL_MAP[_c.value.lower()] = _c
    # 消息类型映射表（如 other / 其它 / Plugin / 插件）
    _MTYPE_MAP: Dict[str, NotificationType] = {}
    for _t in NotificationType:
        _MTYPE_MAP[_t.name.lower()] = _t
        _MTYPE_MAP[_t.value.lower()] = _t

    # 表单中可选的推送渠道（排除 Web/WebAgent 站内消息类渠道）
    _FORM_CHANNELS = ["Wechat", "Telegram", "Feishu", "Slack", "Discord",
                      "QQ", "SynologyChat", "VoceChat", "WebPush"]
    # 表单中可选的消息类型
    _FORM_MTYPES = ["Other", "Plugin", "Download", "Organize",
                    "Subscribe", "SiteMessage", "MediaServer", "Manual", "Agent"]

    # 私有属性
    _enabled = False
    _default_channel = "Wechat"
    _default_mtype = "Other"

    def init_plugin(self, config: dict = None):
        """初始化插件配置"""
        if config is None:
            config = self.get_config() or {}
        self._enabled = config.get("enabled", False)
        self._default_channel = (config.get("default_channel") or "Wechat").strip() or "Wechat"
        self._default_mtype = (config.get("default_mtype") or "Other").strip() or "Other"

    def get_state(self) -> bool:
        return self._enabled

    # ==================== 对外 API ====================

    def get_api(self) -> List[dict]:
        """注册消息接收端点（核心会自动挂载为 /api/v1/plugin/UrlNotify/send，apikey 认证）"""
        return [{
            "path": "/send",
            "endpoint": self.api_send,
            "methods": ["GET", "POST"],
            "summary": "外部消息推送：发送消息到通知渠道",
            "description": "title 必填；text/channel/mtype 可选。支持 query 参数或 JSON body 调用。"
                           "channel 可选值：Wechat(微信)、Telegram、Feishu(飞书)、Slack、Discord、QQ 等，"
                           "不传则使用插件配置的默认渠道。",
        }]

    def api_send(self, title: str = None, text: str = None, channel: str = None,
                 mtype: str = None, body: Optional[dict] = Body(default=None)) -> dict:
        """
        外部消息推送接口
        :param title: 消息标题（必填）
        :param text: 消息内容
        :param channel: 通知渠道（可选，不传用默认渠道）
        :param mtype: 消息类型（可选，不传用默认类型）
        :param body: JSON body 调用方式（POST {"title":..., "text":..., "channel":...}）
        """
        if not self._enabled:
            return {"success": False, "message": "插件未启用，请先在插件配置中开启"}
        # JSON body 参数合并（兼容 POST JSON 调用方式）
        if isinstance(body, dict):
            title = title or body.get("title")
            text = text if text is not None else body.get("text")
            channel = channel or body.get("channel")
            mtype = mtype or body.get("mtype")
        if not title:
            return {"success": False, "message": "缺少 title 参数"}
        return self._push(title=str(title), text=str(text or ""),
                          channel_arg=channel, mtype_arg=mtype)

    # ==================== 推送逻辑 ====================

    def _resolve_channel(self, name: str) -> Optional[MessageChannel]:
        """解析渠道参数为 MessageChannel 枚举"""
        if not name or str(name).strip().lower() in ("", "default"):
            return None
        return self._CHANNEL_MAP.get(str(name).strip().lower())

    def _resolve_mtype(self, name: str) -> Optional[NotificationType]:
        """解析消息类型参数为 NotificationType 枚举"""
        if not name or str(name).strip().lower() in ("", "default"):
            return None
        return self._MTYPE_MAP.get(str(name).strip().lower())

    def _push(self, title: str, text: str,
              channel_arg: Optional[str], mtype_arg: Optional[str]) -> dict:
        """将消息通过 post_message 转发到通知渠道"""
        # 渠道优先级：本次调用指定 > 插件默认配置
        channel = self._resolve_channel(channel_arg)
        if channel is None:
            channel = self._resolve_channel(self._default_channel)
        # 消息类型优先级：本次调用指定 > 插件默认配置
        mtype = self._resolve_mtype(mtype_arg)
        if mtype is None:
            mtype = self._resolve_mtype(self._default_mtype)

        channel_name = channel.value if channel else "默认路由"
        try:
            self.post_message(channel=channel, mtype=mtype, title=title, text=text)
            logger.info(f"UrlNotify: 消息已转发到 {channel_name}：{title}")
            self._save_record(title, channel_name, True, "")
            return {"success": True, "message": f"已发送到 {channel_name}", "channel": channel_name}
        except Exception as e:
            logger.error(f"UrlNotify: 消息转发失败: {e}")
            self._save_record(title, channel_name, False, str(e))
            return {"success": False, "message": f"发送失败: {e}", "channel": channel_name}

    def _save_record(self, title: str, channel: str, success: bool, error: str):
        """保存最近消息记录（供详情页展示，最多 20 条）"""
        try:
            config = self.get_config() or {}
            recent = config.get("recent") or []
            recent.insert(0, {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "title": title[:50],
                "channel": str(channel),
                "success": bool(success),
                "error": error[:100],
            })
            config["recent"] = recent[:20]
            self.update_config(config)
        except Exception as e:
            logger.warn(f"UrlNotify: 保存消息记录失败: {e}")

    # ==================== 界面 ====================

    def get_form(self) -> tuple:
        """插件配置表单"""
        channel_items = [{"title": "跟随系统通知设置", "value": "default"}]
        for c in self._FORM_CHANNELS:
            ch = MessageChannel[c]
            title = f"{ch.value}（{c}）"
            if c == "Wechat":
                title = "微信/企业微信（Wechat）"
            channel_items.append({"title": title, "value": c})
        mtype_items = [{"title": f"{NotificationType[t].value}（{t}）", "value": t}
                       for t in self._FORM_MTYPES]
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VSwitch",
                        "props": {"model": "enabled", "label": "启用插件"},
                    },
                    {
                        "component": "VSelect",
                        "props": {
                            "model": "default_channel",
                            "label": "默认通知渠道",
                            "items": channel_items,
                            "hint": "外部调用未指定 channel 参数时，消息转发到该渠道；"
                                    "需先在 设定-通知 中配置好对应渠道",
                            "persistent-hint": True,
                        },
                    },
                    {
                        "component": "VSelect",
                        "props": {
                            "model": "default_mtype",
                            "label": "默认消息类型",
                            "items": mtype_items,
                            "hint": "影响通知场景开关判断（设定-通知-消息类型开关）",
                            "persistent-hint": True,
                        },
                    },
                ]
            }
        ], {
            "enabled": False,
            "default_channel": "Wechat",
            "default_mtype": "Other",
        }

    def get_page(self) -> List[dict]:
        """详情页：调用方式说明 + 最近消息记录"""
        base = settings.MP_DOMAIN(f"{settings.API_V1_STR}/plugin/UrlNotify/send")
        token = settings.API_TOKEN

        # 最近消息记录
        config = self.get_config() or {}
        recent = config.get("recent") or []
        record_rows = []
        if recent:
            for item in recent[:10]:
                record_rows.append({
                    "component": "div",
                    "props": {"class": "d-flex justify-space-between align-center px-4 py-1"},
                    "content": [
                        {"component": "span",
                         "props": {"class": "text-truncate"},
                         "text": f'{item.get("time", "")}  {item.get("title", "")}'},
                        {"component": "span",
                         "props": {"class": "text-success" if item.get("success") else "text-error"},
                         "text": f'{item.get("channel", "")} · 已发送' if item.get("success")
                                 else f'失败：{item.get("error", "")}'},
                    ],
                })
        else:
            record_rows.append({
                "component": "div",
                "props": {"class": "px-4 py-2 text-medium-emphasis"},
                "text": "暂无消息记录，外部服务调用后此处显示最近推送",
            })

        return [
            {
                "component": "VCard",
                "props": {"variant": "tonal", "class": "mb-2"},
                "content": [
                    {
                        "component": "div",
                        "props": {"class": "px-4 pt-3 text-h6"},
                        "text": "调用方式",
                    },
                    {
                        "component": "div",
                        "props": {"class": "px-4 pb-2 text-body-2 text-medium-emphasis"},
                        "text": "GET 请求（query 参数）：",
                    },
                    {
                        "component": "div",
                        "props": {"class": "px-4 pb-2 text-body-2 font-weight-bold"},
                        "text": f"{base}?apikey={token}&title=消息标题&text=消息内容",
                    },
                    {
                        "component": "div",
                        "props": {"class": "px-4 pb-2 text-body-2 text-medium-emphasis"},
                        "text": 'POST 请求（JSON body）：body 传 {"title": "标题", "text": "内容"}，apikey 放 query 或 Header',
                    },
                    {
                        "component": "div",
                        "props": {"class": "px-4 pb-3 text-body-2 text-medium-emphasis"},
                        "text": "可选参数：channel=Telegram 指定渠道（微信/Wechat、飞书/Feishu、Telegram、Slack 等）；"
                                "mtype=Plugin 指定消息类型。不传则使用插件配置的默认值。",
                    },
                ],
            },
            {
                "component": "VCard",
                "props": {"variant": "tonal"},
                "content": [
                    {
                        "component": "div",
                        "props": {"class": "px-4 pt-3 pb-1 text-h6"},
                        "text": f"最近消息（{min(len(recent), 10)} 条）",
                    },
                    *record_rows,
                ],
            },
        ]

    def stop_service(self):
        """停止插件（无定时任务与连接资源）"""
        pass
