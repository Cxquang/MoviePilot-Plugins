# -*- coding: utf-8 -*-
"""MJJ 主机专用流量监控

监控 https://share.cjy.me/users/userinfo/（MJJ 主机云评测平台 V3.0）账号的
「主机专用流量」余额，低于阈值时通过 MoviePilot 消息渠道提醒充值；余额恢复后
发送恢复通知。

实现要点：
1. 账号密码登录（GET 取 CSRF Token -> POST /mjj6/），登录态 Cookie 持久化，
   失效后自动重新登录；
2. 直接解析用户信息页 HTML（站点为服务端渲染，无独立 JSON 接口）：
   ``<li>主机专用流量: <code>10.0GB</code></li>``；
3. 阈值与检查周期均可配置，默认每小时检查一次、剩余低于 2GB 时提醒；
4. 低于阈值期间每次检查都提醒，余额回到阈值以上时发送一次恢复通知。
"""
import re
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import requests
from apscheduler.triggers.cron import CronTrigger

from app.log import logger
from app.plugins import _PluginBase

BASE_URL = "https://share.cjy.me"
USERINFO_URL = f"{BASE_URL}/users/userinfo/"
LOGIN_POST_URL = f"{BASE_URL}/mjj6/"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)
CN_TZ = timezone(timedelta(hours=8))

# 页面字段与展示名称（顺序即详情页展示顺序）
TRAFFIC_FIELDS = [
    ("主机专用流量", "主机专用流量"),
    ("隧道专用流量", "隧道专用流量"),
    ("通用流量", "通用流量"),
    ("已评测", "已评测流量"),
    ("被评测", "被评测流量"),
]
# 单位 -> GB 换算系数
SIZE_UNITS = {
    "B": 1 / 1024 ** 3,
    "KB": 1 / 1024 ** 2,
    "MB": 1 / 1024,
    "GB": 1.0,
    "TB": 1024.0,
    "PB": 1024.0 ** 2,
}


class MjjTrafficMonitor(_PluginBase):
    # 插件名称
    plugin_name = "MJJ主机专用流量监控"
    # 插件描述
    plugin_desc = "监控 MJJ 主机云评测平台（share.cjy.me）账号的主机专用流量余额，低于阈值时通过 MoviePilot 消息渠道提醒充值，余额恢复后发送恢复通知。"
    # 插件图标
    plugin_icon = "Speedtest.png"
    # 插件版本
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "Cxquang"
    # 作者主页
    author_url = "https://github.com/Cxquang"
    # 插件配置项前缀
    plugin_config_prefix = "mjjtrafficmonitor_"
    # 加载顺序
    plugin_order = 50
    # 用户级别
    auth_level = 1

    _enabled: bool = False
    _username: str = ""
    _password: str = ""
    _threshold_gb: float = 2.0
    _cron: str = "0 * * * *"
    _proxy: str = ""
    _notify_on_error: bool = True
    _last_result: Dict[str, Any] = {}

    # 手动检查与定时检查互斥，避免重复请求
    _lock = threading.Lock()

    def init_plugin(self, config: dict = None):
        """初始化插件配置"""
        if config is None:
            config = self.get_config() or {}
        self._enabled = bool(config.get("enabled"))
        self._username = (config.get("username") or "").strip()
        self._password = (config.get("password") or "").strip()
        try:
            threshold = config.get("threshold_gb")
            self._threshold_gb = float(threshold) if threshold not in (None, "") else 2.0
        except (TypeError, ValueError):
            self._threshold_gb = 2.0
        self._cron = (config.get("cron") or "0 * * * *").strip() or "0 * * * *"
        self._proxy = (config.get("proxy") or "").strip()
        self._notify_on_error = bool(config.get("notify_on_error", True))
        self._last_result = self.get_data("last_result") or {}

        # 立即执行一次：开启并保存后触发，执行完自动关闭
        if config.get("run_once"):
            self._save_config(run_once=False)
            if self.get_state():
                logger.info("MjjTrafficMonitor: 已触发立即检查，5 秒后后台执行")
                threading.Timer(5.0, self.check_and_notify).start()
            else:
                logger.warning("MjjTrafficMonitor: 立即检查已跳过，插件未启用或未配置账号密码")

    def _save_config(self, run_once: bool = False):
        """回写当前配置（保持字段齐全，避免丢项）"""
        self.update_config({
            "enabled": self._enabled,
            "username": self._username,
            "password": self._password,
            "threshold_gb": self._threshold_gb,
            "cron": self._cron,
            "proxy": self._proxy,
            "notify_on_error": self._notify_on_error,
            "run_once": run_once,
        })

    def get_state(self) -> bool:
        return self._enabled and bool(self._username and self._password)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """本插件不注册远程指令"""
        return []

    def get_service(self) -> List[dict]:
        """注册定时检查服务"""
        if not self.get_state() or not self._cron:
            return []
        try:
            trigger = CronTrigger.from_crontab(self._cron)
        except Exception as e:
            logger.error(f"MjjTrafficMonitor: cron 表达式不正确({self._cron}): {e}")
            return []
        return [{
            "id": "MjjTrafficMonitor",
            "name": "MJJ主机专用流量检查",
            "trigger": trigger,
            "func": self.check_and_notify,
            "kwargs": {},
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/check",
            "endpoint": self.api_check,
            "methods": ["GET"],
            "summary": "立即检查主机专用流量",
        }]

    def api_check(self, **kwargs) -> Dict[str, Any]:
        """API：立即检查（详情页按钮）"""
        result = self.check_and_notify()
        return {
            "code": 0 if result.get("success") else 1,
            "msg": result.get("message") or "检查完成",
            "data": result,
        }

    def check_and_notify(self) -> Dict[str, Any]:
        """执行一次检查并按结果通知"""
        if not self.get_state():
            return {"success": False, "message": "插件未启用或未配置账号密码"}
        if not self._lock.acquire(blocking=False):
            logger.info("MjjTrafficMonitor: 上一次检查尚未结束，跳过本次")
            return {"success": False, "message": "上一次检查仍在进行中"}
        try:
            return self._do_check()
        finally:
            self._lock.release()

    def _do_check(self) -> Dict[str, Any]:
        """登录 -> 抓取用户信息页 -> 解析流量 -> 判断并通知"""
        now = datetime.now(CN_TZ)
        logger.info("MjjTrafficMonitor: 开始检查主机专用流量")
        try:
            session, html = self._get_session()
            data = self._parse_userinfo(html)
            dedicated_gb = data.get("dedicated_gb")
            if dedicated_gb is None:
                raise RuntimeError("未能从用户信息页解析出「主机专用流量」，站点结构可能已调整")

            below = dedicated_gb < self._threshold_gb
            state = self.get_data("alert_state") or {}
            was_below = bool(state.get("below"))

            if below:
                self._notify_low(data)
            elif was_below:
                self._notify_recovered(data)

            self.save_data("alert_state", {
                "below": below,
                "updated_at": now.strftime("%Y-%m-%d %H:%M:%S"),
            })
            # 检查成功则清除历史错误，便于下次出现新错误时再次提醒
            self.save_data("last_error", "")

            result = {
                "success": True,
                "time": now.strftime("%Y-%m-%d %H:%M:%S"),
                "message": "检查完成",
                "below": below,
                "threshold_gb": self._threshold_gb,
                "dedicated_gb": round(dedicated_gb, 3),
                "fields": data.get("fields") or {},
                "service_status": data.get("service_status") or "",
                "last_used": data.get("last_used") or "",
            }
            self._last_result = result
            self.save_data("last_result", result)
            logger.info(
                f"MjjTrafficMonitor: 检查完成，主机专用流量 "
                f"{data.get('fields', {}).get('主机专用流量', '未知')}"
                f"（{dedicated_gb:.2f}GB），阈值 {self._threshold_gb:g}GB，"
                f"{'低于阈值' if below else '正常'}"
            )
            return result
        except Exception as e:
            message = str(e) or e.__class__.__name__
            logger.error(f"MjjTrafficMonitor: 检查失败 - {message}")
            self._notify_error(message)
            result = {
                "success": False,
                "time": now.strftime("%Y-%m-%d %H:%M:%S"),
                "message": message,
                "threshold_gb": self._threshold_gb,
                "fields": {},
            }
            self._last_result = result
            self.save_data("last_result", result)
            return result

    # ---------- 登录与会话 ----------

    def _get_session(self) -> Tuple[requests.Session, str]:
        """获取已登录会话，返回 (session, 用户信息页 HTML)"""
        session = requests.Session()
        session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9",
        })
        if self._proxy:
            session.proxies = {"http": self._proxy, "https": self._proxy}
        # 恢复上次保存的 Cookie（sessionid 等）
        saved = self.get_data("cookies") or {}
        if isinstance(saved, dict):
            for name, value in saved.items():
                if name and value:
                    session.cookies.set(name, value, domain="share.cjy.me", path="/")

        html = self._request(session, "GET", USERINFO_URL).text
        if not self._is_logged_in(html):
            logger.info("MjjTrafficMonitor: 会话已失效，重新登录")
            html = self._login(session)
        if not self._is_logged_in(html):
            raise RuntimeError("登录后仍无法进入用户信息页，站点可能触发了人机验证")
        self._save_cookies(session)
        return session, html

    def _login(self, session: requests.Session) -> str:
        """账号密码登录，返回登录后的用户信息页 HTML"""
        page = self._request(session, "GET", USERINFO_URL)
        token = self._extract_csrf(page.text)
        if not token:
            raise RuntimeError("登录页未找到 CSRF Token")
        resp = self._request(
            session, "POST", LOGIN_POST_URL,
            data={
                "csrfmiddlewaretoken": token,
                "username": self._username,
                "password": self._password,
                "next": "/users/userinfo/",
            },
            headers={"Referer": USERINFO_URL},
        )
        if self._is_logged_in(resp.text):
            return resp.text
        # POST 未直接返回用户信息页时，再访问一次确认登录态
        html = self._request(session, "GET", USERINFO_URL).text
        if not self._is_logged_in(html):
            raise RuntimeError("登录失败：请检查账号密码，或站点触发了人机验证")
        return html

    def _save_cookies(self, session: requests.Session):
        """持久化站点 Cookie，避免每次检查都重新登录"""
        cookies = {
            c.name: c.value
            for c in session.cookies
            if "cjy.me" in (c.domain or "") and c.name and c.value
        }
        if cookies:
            self.save_data("cookies", cookies)

    @staticmethod
    def _is_logged_in(html: str) -> bool:
        """用户信息页含流量字段且不含登录表单即视为已登录"""
        return bool(html) and "主机专用流量" in html and 'id="id_username"' not in html

    @staticmethod
    def _extract_csrf(html: str) -> Optional[str]:
        m = re.search(r'name="csrfmiddlewaretoken"\s+value="([^"]+)"', html)
        return m.group(1) if m else None

    @staticmethod
    def _request(session: requests.Session, method: str, url: str, **kwargs) -> requests.Response:
        kwargs.setdefault("timeout", 30)
        resp = session.request(method, url, **kwargs)
        if resp.status_code >= 400:
            hint = "（可能被 Cloudflare 拦截）" if resp.status_code in (403, 503) else ""
            raise RuntimeError(f"请求 {url} 失败：HTTP {resp.status_code}{hint}")
        return resp

    # ---------- 页面解析 ----------

    def _parse_userinfo(self, html: str) -> Dict[str, Any]:
        """解析用户信息页中的流量字段"""
        values: Dict[str, str] = {}
        for name, _ in TRAFFIC_FIELDS:
            values[name] = self._extract_code(html, name) or ""
        dedicated = values.get("主机专用流量") or ""
        return {
            "fields": {
                display: (values.get(name) or "未获取")
                for name, display in TRAFFIC_FIELDS
            },
            "service_status": self._extract_code(html, "服务状态") or "",
            "last_used": self._extract_code(html, "上次使用时间") or "",
            "dedicated_gb": self._to_gb(dedicated),
        }

    @staticmethod
    def _extract_code(html: str, label: str) -> Optional[str]:
        """提取 ``<li>标签: <code>值</code>`` 中的值"""
        pattern = re.escape(label) + r"\s*[:：]\s*<code>\s*([^<]*?)\s*</code>"
        m = re.search(pattern, html, re.S)
        return m.group(1).strip() if m else None

    @staticmethod
    def _to_gb(value: str) -> Optional[float]:
        """把 "10.0GB" / "0B" 等换算为 GB 数值"""
        if not value:
            return None
        m = re.match(r"^\s*([0-9]+(?:\.[0-9]+)?)\s*([KMGTP]?B)\s*$", value, re.I)
        if not m:
            return None
        return float(m.group(1)) * SIZE_UNITS.get(m.group(2).upper(), 0.0)

    # ---------- 通知 ----------

    def _notify_low(self, data: Dict[str, Any]):
        fields = data.get("fields") or {}
        text = (
            f"主机专用流量：{fields.get('主机专用流量', '未知')}"
            f"（提醒阈值 {self._threshold_gb:g}GB）\n"
            f"隧道专用流量：{fields.get('隧道专用流量', '未知')}\n"
            f"通用流量：{fields.get('通用流量', '未知')}\n"
            f"检查时间：{datetime.now(CN_TZ):%Y-%m-%d %H:%M:%S}\n\n"
            f"专用流量已低于阈值，请及时充值。\n{USERINFO_URL}"
        )
        self._send("MJJ 主机专用流量不足", text)

    def _notify_recovered(self, data: Dict[str, Any]):
        fields = data.get("fields") or {}
        text = (
            f"主机专用流量：{fields.get('主机专用流量', '未知')}"
            f"（提醒阈值 {self._threshold_gb:g}GB）\n"
            f"检查时间：{datetime.now(CN_TZ):%Y-%m-%d %H:%M:%S}\n\n"
            f"专用流量已回到阈值以上，无需充值。\n{USERINFO_URL}"
        )
        self._send("MJJ 主机专用流量已恢复", text)

    def _notify_error(self, message: str):
        """检查失败通知：相同错误只提醒一次，避免周期性刷屏"""
        if not self._notify_on_error:
            return
        if str(self.get_data("last_error") or "") == message:
            return
        self.save_data("last_error", message)
        text = (
            f"检查失败：{message}\n"
            f"时间：{datetime.now(CN_TZ):%Y-%m-%d %H:%M:%S}\n"
            f"请检查账号密码、网络或站点状态。\n{USERINFO_URL}"
        )
        self._send("MJJ 流量监控检查失败", text)

    def _send(self, title: str, text: str):
        try:
            self.post_message(title=title, text=text)
            logger.info(f"MjjTrafficMonitor: 通知已发送 -> {title}")
        except Exception as e:
            logger.error(f"MjjTrafficMonitor: 发送通知失败: {e}")

    # ---------- 界面 ----------

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        return [
            {
                "component": "VForm",
                "content": [
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VSwitch", "props": {"model": "enabled", "label": "启用插件"}}
                        ]},
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VSwitch", "props": {
                                "model": "notify_on_error",
                                "label": "检查失败时通知（相同错误只提醒一次）"
                            }}
                        ]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {"model": "username", "label": "用户名"}}
                        ]},
                        {"component": "VCol", "props": {"cols": 12, "md": 6}, "content": [
                            {"component": "VTextField", "props": {
                                "model": "password", "label": "密码", "type": "password"
                            }}
                        ]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                            {"component": "VTextField", "props": {
                                "model": "threshold_gb", "label": "剩余流量阈值（GB）",
                                "type": "number", "placeholder": "低于该值提醒，默认 2"
                            }}
                        ]},
                        {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                            {"component": "VTextField", "props": {
                                "model": "cron", "label": "检查周期（Cron）",
                                "placeholder": "默认每小时：0 * * * *"
                            }}
                        ]},
                        {"component": "VCol", "props": {"cols": 12, "md": 4}, "content": [
                            {"component": "VTextField", "props": {
                                "model": "proxy", "label": "代理地址（可选）",
                                "placeholder": "http://127.0.0.1:7890"
                            }}
                        ]},
                    ]},
                    {"component": "VRow", "content": [
                        {"component": "VCol", "props": {"cols": 12}, "content": [
                            {"component": "VSwitch", "props": {
                                "model": "run_once", "label": "立即执行一次",
                                "placeholder": "开启并保存后立即检查一次（执行完自动关闭，用于验证配置）"
                            }}
                        ]},
                    ]},
                    {"component": "VAlert", "props": {
                        "type": "info",
                        "variant": "tonal",
                        "text": "登录 share.cjy.me 后解析账号的「主机专用流量」余额：低于阈值时每次检查都会提醒，"
                                "余额回到阈值以上时发送一次恢复通知。默认每小时检查一次、剩余低于 2GB 提醒。"
                                "站点每周一会将专用流量按比例转换为通用流量，周一余额下降属于正常现象。"
                    }},
                ],
            }
        ], {
            "enabled": False,
            "username": "",
            "password": "",
            "threshold_gb": 2,
            "cron": "0 * * * *",
            "proxy": "",
            "notify_on_error": True,
            "run_once": False,
        }

    def get_page(self) -> List[dict]:
        if not self.get_state():
            return [{
                "component": "VAlert",
                "props": {
                    "type": "warning",
                    "variant": "tonal",
                    "text": "插件未启用或未配置账号密码，请先完成配置并保存。",
                },
            }]

        r = self._last_result or self.get_data("last_result") or {}
        fields = r.get("fields") or {}
        content: List[dict] = [
            {"component": "VRow", "content": [
                {"component": "VCol", "props": {"cols": 6, "md": 3}, "content": [
                    {"component": "VStat", "props": {
                        "name": "主机专用流量",
                        "value": fields.get("主机专用流量", "未获取"),
                    }}
                ]},
                {"component": "VCol", "props": {"cols": 6, "md": 3}, "content": [
                    {"component": "VStat", "props": {
                        "name": "提醒阈值",
                        "value": f"{self._threshold_gb:g}GB",
                    }}
                ]},
                {"component": "VCol", "props": {"cols": 6, "md": 3}, "content": [
                    {"component": "VStat", "props": {"name": "上次检查", "value": r.get("time") or "未执行"}}
                ]},
                {"component": "VCol", "props": {"cols": 6, "md": 3}, "content": [
                    {"component": "VStat", "props": {"name": "检查周期", "value": self._cron}}
                ]},
            ]},
        ]

        detail_names = ["隧道专用流量", "通用流量", "已评测流量", "被评测流量"]
        detail_cols = []
        for name in detail_names:
            detail_cols.append({
                "component": "VCol", "props": {"cols": 6, "md": 3},
                "content": [{"component": "VStat", "props": {
                    "name": name, "value": fields.get(name, "未获取"),
                }}],
            })
        if detail_cols:
            content.append({"component": "VRow", "content": detail_cols})

        if r.get("success") is False:
            content.append({"component": "VAlert", "props": {
                "type": "error", "variant": "tonal",
                "text": str(r.get("message") or "检查失败")[:500],
            }})
        elif r.get("below"):
            content.append({"component": "VAlert", "props": {
                "type": "warning", "variant": "tonal",
                "text": f"当前主机专用流量低于阈值 {self._threshold_gb:g}GB，请及时充值。",
            }})
        elif r:
            content.append({"component": "VAlert", "props": {
                "type": "success", "variant": "tonal", "text": "专用流量正常。",
            }})

        content.append({"component": "VRow", "content": [
            {"component": "VCol", "props": {"cols": 12}, "content": [
                {"component": "VBtn", "props": {"color": "primary", "block": True, "text": "立即检查"},
                 "events": {"click": {"api": "plugin/MjjTrafficMonitor/check", "method": "get"}}}
            ]},
        ]})

        return [{"component": "VCard", "props": {"title": "MJJ 主机专用流量"}, "content": content}]

    def stop_service(self):
        pass
