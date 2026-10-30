"""NcmQr — 网易云登录二维码推送（Songloft ncm-daily 配套，MoviePilot v2 插件）

功能：
  1. 暴露 POST /api/v1/plugin/NcmQr/qr（apikey 鉴权）：接收 ncm-daily 插件推送的登录二维码，
     上传企微临时素材（media/upload）并发送企业微信图片消息
  2. 注册 /ncm_login 指令（企微菜单「登录网易云」按钮）：直接执行，不经过大模型对话链路
  3. 监听 UserMessage 事件：在企业微信回复「登录」关键词时，反向调 ncm-daily 的免 JWT 端点
     取最新二维码并推送

企微 API 走反代域名拼 URL（与 MP 通知模块同款）：优先取插件配置 proxy，其次 MP 内置
WECHAT_PROXY，均为空则直连官方 qyapi.weixin.qq.com。图片消息失败时自动降级为文字消息，
附带 ncm-daily 扫码页链接（/public/qr-page），手机点开即可长按识别扫码。
"""
import base64
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote

import requests
from fastapi import Request

from app.core.config import settings
from app.core.event import Event, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType


class NcmQr(_PluginBase):
    # 插件名称
    plugin_name = "网易云登录二维码推送"
    # 插件描述
    plugin_desc = "接收 Songloft「网易云日推」插件推送的登录二维码，经企微 API 反代域名上传临时素材后发送企业微信图片消息；注册 /ncm_login 指令（企微菜单「登录网易云」），不走大模型对话链路；图片失败自动降级为扫码页链接。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/Cxquang/songloft-ncm-daily/main/static/icon.svg"
    # 插件版本
    plugin_version = "0.2.1"
    # 插件作者
    plugin_author = "Cxquang"
    # 作者主页
    author_url = "https://github.com/Cxquang"
    # 插件配置项前缀
    plugin_config_prefix = "ncmqr_"
    # 加载顺序
    plugin_order = 30
    # 用户级别
    auth_level = 1

    _enabled = False
    _corpid = ""
    _corpsecret = ""
    _agentid = ""
    _touser = "@all"
    _proxy = ""
    _keyword = "登录"
    _ncm_base = ""
    _ncm_secret = ""

    _token_cache: Optional[str] = None
    _token_expire = 0.0

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = bool(config.get("enabled"))
            self._corpid = (config.get("corpid") or "").strip()
            self._corpsecret = (config.get("corpsecret") or "").strip()
            self._agentid = (config.get("agentid") or "").strip()
            self._touser = (config.get("touser") or "@all").strip()
            self._proxy = (config.get("proxy") or "").strip()
            self._keyword = (config.get("keyword") or "登录").strip()
            self._ncm_base = (config.get("ncm_base") or "").strip().rstrip("/")
            self._ncm_secret = (config.get("ncm_secret") or "").strip()
        self._token_cache = None
        self._token_expire = 0.0

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        # 注册 /ncm_login 指令：MP 以「/」开头识别为指令直接执行（CommandExcute → PluginAction），
        # 不进入大模型对话链路；category 分组进企微菜单（一级菜单组名），按钮名取 desc
        return [{
            "cmd": "/ncm_login",
            "event": EventType.PluginAction,
            "desc": "登录网易云",
            "category": "站点",
            "data": {"action": "ncm_login"}
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/qr",
            "endpoint": self.push_qr,
            "methods": ["POST"],
            "auth": "apikey",
            "summary": "接收网易云登录二维码并推送到企业微信",
        }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {"model": "keyword", "label": "触发关键词", "placeholder": "登录"}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {"model": "corpid", "label": "企业ID（corpid）"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {"model": "corpsecret", "label": "应用Secret", "type": "password"}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {"model": "agentid", "label": "应用AgentId"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {"model": "touser", "label": "发送目标用户（企微通讯录账号，多人用 | 分隔，@all=可见范围全体）", "placeholder": "@all"}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {"model": "proxy", "label": "企微 API 反代域名（如 https://wx.example.com，留空读 MP 的 WECHAT_PROXY）"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {"model": "ncm_base", "label": "Songloft 地址（需手机可访问，用于扫码回退链接）", "placeholder": "http://192.168.123.176:58091"}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {"model": "ncm_secret", "label": "ncm-daily 回调密钥", "type": "password"}}]},
                    ]},
                ],
            }
        ], {
            "enabled": False,
            "keyword": "登录",
            "corpid": "",
            "corpsecret": "",
            "agentid": "",
            "touser": "@all",
            "proxy": "",
            "ncm_base": "",
            "ncm_secret": ""
        }

    def get_page(self) -> List[dict]:
        return [
            {
                "component": "VRow",
                "content": [
                    {
                        "component": "VCol",
                        "props": {"cols": 12},
                        "content": [
                            {
                                "component": "VAlert",
                                "props": {
                                    "type": "info",
                                    "variant": "tonal",
                                    "text": "本插件配合 Songloft「网易云日推」插件（ncm-daily）使用：ncm-daily 检测到登录过期时，通过本插件把二维码推送到企业微信图片消息（企微 API 走反代域名，与 MP 通知同一出口，规避可信 IP 限制）；已注册 /ncm_login 指令，在企业微信点菜单「登录网易云」按钮或发送 /ncm_login 即可重新获取二维码，二维码发给触发者本人，且不走大模型对话链路；图片发送失败时自动降级为文字消息并附带扫码页链接，手机点开即可长按识别。"
                                }
                            }
                        ]
                    }
                ]
            }
        ]

    def stop_service(self):
        pass

    # ---------- 对外端点 ----------

    async def push_qr(self, request: Request):
        """接收 ncm-daily 推来的二维码并转发到企业微信"""
        if not self._enabled:
            return {"result": False, "message": "插件未启用"}
        try:
            body = await request.json()
        except Exception:
            return {"result": False, "message": "请求体不是合法 JSON"}
        try:
            png = self._decode_image(body.get("image"))
            self._push_qr_bytes(png, body.get("text") or "")
            logger.info("NcmQr 二维码已推送到企业微信")
            return {"result": True, "message": "已发送"}
        except Exception as e:
            logger.error(f"NcmQr 推送失败: {e}")
            return {"result": False, "message": str(e)}

    # ---------- 触发入口（关键词 / 指令共用） ----------

    def _fetch_and_push_qr(self, touser: Optional[str]) -> None:
        """从 ncm-daily 拉取最新二维码并推送给 touser（为空时用配置的默认目标）"""
        if not (self._ncm_base and self._ncm_secret):
            self._send_text(touser, "NcmQr 未配置 Songloft 地址或回调密钥")
            return
        url = f"{self._ncm_base}/api/v1/jsplugin/ncm-daily/public/qr"
        resp = self._http("GET", url, params={"secret": self._ncm_secret})
        data = resp.json()
        if not data.get("ok"):
            self._send_text(touser, f"ncm-daily 返回失败: {data.get('error') or data}")
            return
        png = self._decode_image(data.get("qrimg"))
        hint = f"请扫码续期（第 {data.get('round')}/{data.get('maxRounds')} 轮，2分钟内有效）"
        self._push_qr_bytes(png, hint, touser)

    @eventmanager.register(EventType.UserMessage)
    def on_user_message(self, event: Event):
        """关键词触发：企业微信回复「登录」时重新获取二维码（备用入口）"""
        if not self._enabled or not self._keyword:
            return
        try:
            data = getattr(event, "event_data", None) or {}
            text = str(data.get("text") or data.get("message") or "").strip()
            if text != self._keyword:
                return
            userid = data.get("userid") or data.get("user_id") or None
            self._fetch_and_push_qr(userid)
            logger.info("NcmQr 关键词触发二维码推送完成")
        except Exception as e:
            logger.error(f"NcmQr 关键词处理失败: {e}")

    @eventmanager.register(EventType.PluginAction)
    def on_plugin_action(self, event: Event):
        """指令触发：企微菜单「登录网易云」按钮或发送 /ncm_login 时执行

        MP 指令链会自动在 event_data 里附加触发者 user（企微账号），二维码推送给触发者本人
        """
        data = getattr(event, "event_data", None) or {}
        if not self._enabled or data.get("action") != "ncm_login":
            return
        try:
            self._fetch_and_push_qr(data.get("user"))
            logger.info("NcmQr 指令触发二维码推送完成")
        except Exception as e:
            logger.error(f"NcmQr 指令处理失败: {e}")

    # ---------- 内部工具 ----------

    def _api_base(self) -> str:
        """企微 API 出口：反代域名拼 URL（MP 同款），而不是 requests.proxies。

        优先级：插件配置 proxy > MP 内置 WECHAT_PROXY > 直连官方。
        """
        proxy = (self._proxy
                 or getattr(settings, "WECHAT_PROXY", None)
                 or os.getenv("WECHAT_PROXY", "")).strip()
        if proxy:
            proxy = proxy.rstrip("/")
            if not re.match(r"^https?://", proxy, re.I):
                proxy = "https://" + proxy
            return proxy
        return "https://qyapi.weixin.qq.com"

    def _http(self, method: str, url: str, **kwargs):
        kwargs.setdefault("timeout", 20)
        return requests.request(method, url, **kwargs)

    def _get_token(self) -> str:
        if self._token_cache and time.time() < self._token_expire - 300:
            return self._token_cache
        if not (self._corpid and self._corpsecret):
            raise RuntimeError("未配置企业ID或应用Secret")
        url = f"{self._api_base()}/cgi-bin/gettoken?corpid={self._corpid}&corpsecret={self._corpsecret}"
        data = self._http("GET", url).json()
        if data.get("errcode") != 0:
            raise RuntimeError(f"gettoken 失败: {data}")
        self._token_cache = data.get("access_token")
        self._token_expire = time.time() + int(data.get("expires_in") or 7200)
        return self._token_cache

    def _upload_image(self, png_bytes: bytes) -> str:
        token = self._get_token()
        url = f"{self._api_base()}/cgi-bin/media/upload?access_token={token}&type=image"
        files = {"media": ("ncm-qr.png", png_bytes, "image/png")}
        data = self._http("POST", url, files=files).json()
        if data.get("errcode") != 0:
            raise RuntimeError(f"上传素材失败: {data}")
        return data["media_id"]

    def _send_msg(self, touser: Optional[str], msg: Dict[str, Any]) -> None:
        if not self._agentid:
            raise RuntimeError("未配置应用 AgentId")
        token = self._get_token()
        payload = {"touser": touser or self._touser or "@all", "agentid": int(self._agentid)}
        payload.update(msg)
        url = f"{self._api_base()}/cgi-bin/message/send?access_token={token}"
        data = self._http("POST", url, json=payload).json()
        if data.get("errcode") != 0:
            raise RuntimeError(f"发送失败: {data}")

    def _send_text(self, touser: Optional[str], text: str) -> None:
        self._send_msg(touser, {"msgtype": "text", "text": {"content": text}})

    def _qr_page_url(self) -> str:
        """ncm-daily 扫码页链接；未配置 Songloft 地址/密钥时返回空串。"""
        if not (self._ncm_base and self._ncm_secret):
            return ""
        return (f"{self._ncm_base}/api/v1/jsplugin/ncm-daily/public/qr-page"
                f"?secret={quote(self._ncm_secret, safe='')}")

    def _push_qr_bytes(self, png_bytes: bytes, text: str, touser: Optional[str] = None) -> None:
        try:
            media_id = self._upload_image(png_bytes)
            self._send_msg(touser, {"msgtype": "image", "image": {"media_id": media_id}})
        except Exception as e:
            logger.warning(f"NcmQr 图片消息发送失败，降级为扫码链接: {e}")
            lines = [text or "网易云登录二维码"]
            link = self._qr_page_url()
            if link:
                lines.append("图片发送失败，请点开链接扫码（约2分钟内有效）：")
                lines.append(link)
            else:
                lines.append(f"图片发送失败，且未配置 Songloft 地址/回调密钥，无法生成扫码链接: {e}")
            self._send_text(touser, "\n".join(lines))
            return
        if text:
            self._send_text(touser, text)

    @staticmethod
    def _decode_image(image: str) -> bytes:
        b64 = re.sub(r"^data:image/[\w.+-]+;base64,", "", (image or "").strip())
        if not b64:
            raise ValueError("image 字段为空")
        return base64.b64decode(b64)
