"""NcmQr — 网易云验证码登录转发（Songloft ncm-daily 配套，MoviePilot v2 插件）

功能：
  1. 暴露 POST /api/v1/plugin/NcmQr/notify（apikey 鉴权）：接收 ncm-daily 推送的文字通知
     （验证码已发送、登录成功、同步结果等），转发到企业微信
  2. 注册 /code 指令（不出现在企微菜单，仅文字指令）：在企业微信发送 `/code 123456`，
     插件把验证码转给 ncm-daily 的免 JWT 端点 /public/code 完成登录；发送 `/code`
     （不带参数）则让 ncm-daily 重新请求一条短信验证码

企微凭据与出口：MP v2 的企微配置存放在「消息通知渠道」的模块配置里（WECHAT_CORPID /
WECHAT_APP_SECRET / WECHAT_APP_ID / WECHAT_PROXY），不在全局环境变量中。本插件默认自动
借用该渠道配置（即 MP 入库通知那条链路，凭据与反代出口天然可用），插件表单里的字段可覆盖。
文字直发失败时，自动改经 MP 自己的消息渠道（MessageChain）发送，不依赖可信 IP。
"""
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from fastapi import Request

from app.core.config import settings
from app.core.event import Event, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType


class NcmQr(_PluginBase):
    # 插件名称
    plugin_name = "网易云验证码登录转发"
    # 插件描述
    plugin_desc = "配合 Songloft「网易云日推」插件：把登录验证码提示推送到企业微信，并在企业微信发送 /code 123456 时把验证码转发给 ncm-daily 完成登录；自动借用 MP 企业微信渠道的凭据与反代出口，直发失败自动改经 MP 消息渠道。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/Cxquang/songloft-ncm-daily/main/static/icon.svg"
    # 插件版本
    plugin_version = "0.4.0"
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
    _ncm_base = ""
    _ncm_secret = ""

    _token_cache: Optional[str] = None
    _token_expire = 0.0

    def init_plugin(self, config: dict = None):
        # MP v2 的企微渠道配置存于模块配置（数据库），借用之；表单字段非空时优先
        borrowed = self._borrow_mp_wechat_conf()
        if config:
            self._enabled = bool(config.get("enabled"))
            self._corpid = (config.get("corpid") or "").strip() \
                or str(borrowed.get("WECHAT_CORPID") or "").strip()
            self._corpsecret = (config.get("corpsecret") or "").strip() \
                or str(borrowed.get("WECHAT_APP_SECRET") or "").strip()
            self._agentid = (config.get("agentid") or "").strip() \
                or str(borrowed.get("WECHAT_APP_ID") or "").strip()
            self._touser = (config.get("touser") or "").strip()
            self._proxy = (config.get("proxy") or "").strip() \
                or str(borrowed.get("WECHAT_PROXY") or "").strip()
            self._ncm_base = (config.get("ncm_base") or "").strip().rstrip("/")
            self._ncm_secret = (config.get("ncm_secret") or "").strip()
            if not self._touser:
                admins = [a.strip() for a in
                          str(borrowed.get("WECHAT_ADMINS") or "").split(",") if a.strip()]
                self._touser = admins[0] if admins else "@all"
        self._token_cache = None
        self._token_expire = 0.0
        if self._enabled:
            src = "自动借用 MP 企微渠道" if borrowed else "插件独立配置（未借用到 MP 渠道）"
            logger.info(f"NcmQr 已就绪：凭据来源 {src}，企微出口 {self._api_base()}")

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        # /code 指令：MP 以「/」开头识别为指令直接执行（CommandExcute → PluginAction），
        # 不进入大模型对话链路。show=False 只影响企微菜单聚合，不会禁用命令本身。
        return [{
            "cmd": "/code",
            "event": EventType.PluginAction,
            "desc": "网易云验证码登录（/code 123456）",
            "category": "站点",
            "show": False,
            "data": {"action": "ncm_code"}
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/notify",
            "endpoint": self.push_text,
            "methods": ["POST"],
            "auth": "apikey",
            "summary": "接收 ncm-daily 文字通知并转发企业微信",
        }]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {"model": "corpid", "label": "企业ID corpid（留空自动借用 MP 企业微信渠道配置）"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {"model": "corpsecret", "label": "应用Secret（留空自动借用 MP 企业微信渠道配置）", "type": "password"}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {"model": "agentid", "label": "应用AgentId（留空自动借用 MP 企业微信渠道配置）"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {"model": "touser", "label": "默认发送目标（企微账号，多人|分隔；留空=MP 渠道管理员，兜底 @all）", "placeholder": "@all"}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {"model": "proxy", "label": "企微 API 反代域名（留空依次读 MP 渠道配置 / 全局 WECHAT_PROXY）"}}]},
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {"model": "ncm_base", "label": "Songloft 地址（MP 能访问的地址）", "placeholder": "http://192.168.123.176:58091"}}]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {"model": "ncm_secret", "label": "ncm-daily 回调密钥（与插件内 publicSecret 一致）", "type": "password"}}]},
                    ]},
                ],
            }
        ], {
            "enabled": False,
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
                                    "text": "本插件配合 Songloft「网易云日推」插件（ncm-daily）使用：ncm-daily 请求短信验证码后，通过本插件把「请在企业微信回复 /code 验证码」的提示推送到企业微信；在企业微信直接发送 /code 123456（或只发 /code 让 ncm-daily 重新发一条短信），插件会把验证码转发给 ncm-daily 完成登录。企微凭据与反代出口默认自动借用 MP「消息通知」里已配置的企业微信渠道（与入库通知同一条链路，可信 IP 天然放行）；文字直发失败时自动改经 MP 自己的消息渠道发送。"
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

    async def push_text(self, request: Request):
        """接收 ncm-daily 推来的文字通知并转发到企业微信"""
        if not self._enabled:
            return {"result": False, "message": "插件未启用"}
        try:
            body = await request.json()
        except Exception:
            return {"result": False, "message": "请求体不是合法 JSON"}
        title = str(body.get("title") or "网易云日推通知").strip()
        text = str(body.get("text") or "").strip()
        if not text:
            return {"result": False, "message": "text 为空"}
        ok = self._notify_text(None, text, title)
        if ok:
            logger.info(f"NcmQr 通知已推送到企业微信：{title}")
            return {"result": True, "message": "已发送"}
        return {"result": False, "message": "企业微信发送失败，详见 MP 日志"}

    # ---------- /code 指令 ----------

    @eventmanager.register(EventType.PluginAction)
    def on_plugin_action(self, event: Event):
        """指令触发：企业微信发送 `/code 123456` 时把验证码转发给 ncm-daily

        MP 指令链会自动在 event_data 里附加触发者 user（企微账号），
        结果文字直接回发给触发者本人。
        """
        data = getattr(event, "event_data", None) or {}
        if not self._enabled or data.get("action") != "ncm_code":
            return
        user = data.get("user") or data.get("userid") or None
        arg = str(data.get("arg_str") or "").strip()
        try:
            if arg:
                ok, message = self._forward_code(user, arg)
            else:
                ok, message = self._request_code(user)
            logger.info(f"NcmQr /code 处理完成：{message}")
        except Exception as e:
            logger.error(f"NcmQr /code 处理失败: {e}")
            self._notify_text(user, f"网易云登录失败：{e}")

    def _forward_code(self, touser: Optional[str], arg: str) -> Tuple[bool, str]:
        """把企微里的 /code 参数转发给 ncm-daily 完成验证码登录"""
        code = re.sub(r"\D", "", arg)
        if not code:
            msg = "请发送 /code 验证码，例如 /code 123456"
            self._notify_text(touser, msg)
            return False, msg
        if not (self._ncm_base and self._ncm_secret):
            msg = "NcmQr 未配置 Songloft 地址或回调密钥，无法转发验证码"
            self._notify_text(touser, msg)
            return False, msg
        url = f"{self._ncm_base}/api/v1/jsplugin/ncm-daily/public/code"
        resp = self._http("POST", url, params={"secret": self._ncm_secret, "code": code},
                          json={"code": code})
        data = resp.json() if resp is not None else {}
        if data.get("ok"):
            profile = (data.get("login") or {}).get("profile") or {}
            name = profile.get("nickname") or ""
            msg = "网易云登录成功" + (f"：{name}" if name else "") + "，ncm-daily 将自动补跑同步。"
            self._notify_text(touser, msg)
            return True, msg
        msg = f"网易云登录失败：{data.get('error') or data or '无响应'}"
        self._notify_text(touser, msg)
        return False, msg

    def _request_code(self, touser: Optional[str]) -> Tuple[bool, str]:
        """不带参数发送 /code：让 ncm-daily 重新请求一条短信验证码"""
        if not (self._ncm_base and self._ncm_secret):
            msg = "NcmQr 未配置 Songloft 地址或回调密钥，无法请求验证码"
            self._notify_text(touser, msg)
            return False, msg
        url = f"{self._ncm_base}/api/v1/jsplugin/ncm-daily/public/send-code"
        resp = self._http("POST", url, params={"secret": self._ncm_secret})
        data = resp.json() if resp is not None else {}
        if data.get("ok"):
            msg = data.get("message") or f"验证码已发送到 {data.get('phoneMasked') or '配置的手机号'}，请回复 /code 验证码"
            self._notify_text(touser, msg)
            return True, msg
        msg = f"发送验证码失败：{data.get('error') or data or '无响应'}"
        self._notify_text(touser, msg)
        return False, msg

    # ---------- MP 渠道借用与兜底 ----------

    @staticmethod
    def _borrow_mp_wechat_conf() -> Dict[str, str]:
        """借用 MP「消息通知」里已启用的企业微信渠道配置（凭据 + 反代出口）。

        MP v2 的企微配置不在全局环境变量（WECHAT_PROXY 是 v1 遗留键），而是存在
        渠道配置数据库里，由 WechatModule 经 ServiceConfigHelper 读取——即 MP 入库
        通知使用的那条链路，凭据与反代出口天然可用且可信 IP 已放行。
        """
        try:
            from app.helper.service import ServiceConfigHelper
            for conf in ServiceConfigHelper.get_notification_configs() or []:
                if not getattr(conf, "enabled", False):
                    continue
                if str(getattr(conf, "type", "") or "").lower() != "wechat":
                    continue
                cfg = getattr(conf, "config", None) or {}
                if cfg.get("WECHAT_CORPID") and cfg.get("WECHAT_APP_SECRET") \
                        and cfg.get("WECHAT_APP_ID"):
                    return {k: str(v) for k, v in cfg.items() if v is not None}
        except Exception as e:
            logger.debug(f"NcmQr 借用 MP 企微渠道配置失败: {e}")
        return {}

    def _send_via_mp_channel(self, touser: Optional[str], title: str, text: str) -> bool:
        """经 MP 自己的消息渠道发送（与入库通知同链路），复用 MP 已配置好的企微客户端。"""
        try:
            from app.chain.message import MessageChain
            from app.schemas import MessageChannel, Notification
            MessageChain().post_message(Notification(
                channel=MessageChannel.Wechat,
                title=title or "网易云日推通知",
                text=text,
                userid=touser or self._touser or "@all",
            ))
            logger.info("NcmQr 已经 MP 消息渠道发送")
            return True
        except Exception as e:
            logger.warning(f"NcmQr 经 MP 消息渠道发送失败: {e}")
            return False

    def _notify_text(self, touser: Optional[str], text: str,
                     title: str = "网易云日推通知") -> bool:
        """发文字消息：优先本插件凭据直发，失败（如 60020 可信 IP）改走 MP 消息渠道。"""
        try:
            self._send_text(touser, text)
            return True
        except Exception as e:
            logger.warning(f"NcmQr 文字直发失败，改走 MP 消息渠道: {e}")
            return self._send_via_mp_channel(touser, title, text)

    # ---------- 内部工具 ----------

    def _api_base(self) -> str:
        """企微 API 出口：反代域名拼 URL（MP 同款），而不是 requests.proxies。

        优先级：插件配置 proxy > MP 渠道借用（init 时已写入 _proxy）> 全局
        WECHAT_PROXY > 直连官方。
        """
        proxy = (self._proxy
                 or getattr(settings, "WECHAT_PROXY", None)
                 or "").strip()
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
