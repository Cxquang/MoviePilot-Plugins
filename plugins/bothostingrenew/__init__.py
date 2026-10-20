# -*- coding: utf-8 -*-
"""Bot-Hosting.net 自动续期插件

移植自 eooce/Auto-Renew-Bothosting（GitHub Actions + SeleniumBase 方案），
改为 MoviePilot 内运行的纯 HTTP 实现：
1. session_token Cookie 访问账单页，解析订阅状态（到期/可续期时间）
2. 到期前自动尝试 POST /api/billing/renew-free 续期（无需浏览器）
3. session_token 失效时，用 Discord Token 走 OAuth 流程自动换新（纯 HTTP）
4. 执行结果通过 MP 消息渠道推送，通知策略可选

参考与致谢：https://github.com/eooce/Auto-Renew-Bothosting
"""
import re
import traceback
from datetime import datetime, timezone, timedelta
from typing import Any, List, Dict, Tuple, Optional

import requests

from apscheduler.triggers.cron import CronTrigger
from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType

# 站点常量（来自前端逆向：PUBLIC_API_URL / PUBLIC_DISCORD_CLIENT_ID）
BASE_URL = "https://bot-hosting.net"
BILLINGS_URL = f"{BASE_URL}/a/billings"
RENEW_API = f"{BASE_URL}/api/billing/renew-free"
DISCORD_LOGIN_URL = f"{BASE_URL}/login/discord"
DISCORD_CLIENT_ID = "884382422530158623"
DISCORD_AUTHORIZE_API = "https://discord.com/api/v9/oauth2/authorize"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36")
CN_TZ = timezone(timedelta(hours=8))

# 通知策略
NOTIFY_ALWAYS = "always"     # 每次执行都通知
NOTIFY_FAILURE = "failure"   # 仅失败/异常时通知
NOTIFY_OFF = "off"           # 关闭通知


class BotHostingRenew(_PluginBase):
    # 插件名称
    plugin_name = "Bot-Hosting自动续期"
    # 插件描述
    plugin_desc = "Bot-Hosting.net 免费套餐自动续期（4天一期），到期自动续并推送通知，支持 Discord Token 自动换新登录"
    # 插件图标
    plugin_icon = "https://bot-hosting.net/favicon.ico"
    # 插件版本
    plugin_version = "1.0.0"
    # 插件作者
    plugin_author = "Cxquang"
    # 作者主页
    author_url = "https://github.com/Cxquang"
    # 插件配置项ID前缀
    plugin_config_prefix = "bothostingrenew_"
    # 加载顺序
    plugin_order = 30
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _session_token = ""
    _discord_token = ""
    _email = ""
    _cron = "0 8 * * *"
    _notify = NOTIFY_ALWAYS
    _renew_attempt = True
    # 运行状态
    _last_result = None

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled") or False
            self._session_token = (config.get("session_token") or "").strip()
            self._discord_token = (config.get("discord_token") or "").strip()
            self._email = (config.get("email") or "").strip()
            self._cron = (config.get("cron") or "0 8 * * *").strip()
            self._notify = config.get("notify") or NOTIFY_ALWAYS
            self._renew_attempt = config.get("renew_attempt", True)

    def get_state(self) -> bool:
        return self._enabled and bool(self._session_token or self._discord_token)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        return [{
            "cmd": "/bh_renew",
            "event": EventType.PluginAction,
            "desc": "Bot-Hosting 立即续期",
            "category": "",
            "data": {"action": "bothostingrenew"}
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/run",
            "endpoint": self.api_run,
            "methods": ["GET", "POST"],
            "summary": "立即执行 Bot-Hosting 续期",
        }, {
            "path": "/status",
            "endpoint": self.api_status,
            "methods": ["GET"],
            "summary": "查询 Bot-Hosting 续期状态",
        }]

    def api_run(self, **kwargs) -> Dict[str, Any]:
        """API：立即执行"""
        self.run_once()
        return {"code": 0, "msg": "执行完成，详见消息通知或插件详情页"}

    def api_status(self, **kwargs) -> Dict[str, Any]:
        """API：查询状态"""
        return {"code": 0, "msg": "ok", "data": self._last_result or {}}

    def get_service(self) -> List[dict]:
        """定时服务"""
        if self._enabled and self._cron:
            try:
                trigger = CronTrigger.from_crontab(self._cron)
            except Exception as e:
                logger.error(f"Bot-Hosting cron 表达式错误: {self._cron} - {e}")
                return []
            return [{
                "id": "BotHostingRenew",
                "name": "Bot-Hosting 自动续期",
                "trigger": trigger,
                "func": self.run_once,
                "kwargs": {},
            }]
        return []

    @eventmanager.register(EventType.PluginAction)
    def handle_command(self, event: Event):
        """远程命令触发"""
        if not event or not event.event_data:
            return
        if event.event_data.get("action") != "bothostingrenew":
            return
        if not self.get_state():
            self._notify_message("Bot-Hosting续期未执行", "插件未启用或未配置 Token", failure=True)
            return
        self.run_once()

    # ------------------------------------------------------------------
    # 核心流程
    # ------------------------------------------------------------------

    def run_once(self):
        """执行一次：查状态 -> 按需续期 -> 通知"""
        if not self.get_state():
            return
        logger.info("Bot-Hosting 自动续期开始执行")
        try:
            result = self.__do_renew_flow()
            self._last_result = result
            self.save_data("last_result", result)
            self.__notify_result(result)
        except Exception as e:
            logger.error(f"Bot-Hosting 续期执行异常: {e}\n{traceback.format_exc()}")
            result = {
                "success": False,
                "title": "执行异常",
                "detail": str(e),
                "time": self.__now_str(),
            }
            self._last_result = result
            self.save_data("last_result", result)
            self.__notify_result(result)
        logger.info("Bot-Hosting 自动续期执行完毕")

    def __do_renew_flow(self) -> dict:
        """完整续期流程，返回结果 dict"""
        session_token = self._session_token
        login_method = "Session Token"

        # 1. 查询账单页状态
        status = self.__query_status(session_token)
        if status.get("need_relogin"):
            # session_token 失效，尝试 Discord OAuth 换新
            if not self._discord_token:
                return {
                    "success": False,
                    "title": "登录失败",
                    "detail": "session_token 已失效，且未配置 Discord Token，无法自动恢复。\n"
                              "请重新从浏览器提取 session_token 并更新插件配置。",
                    "expiry": status.get("expiry") or "",
                    "time": self.__now_str(),
                }
            logger.info("session_token 失效，尝试 Discord OAuth 登录换新")
            new_token = self.__discord_login()
            if not new_token:
                return {
                    "success": False,
                    "title": "登录失败",
                    "detail": "session_token 已失效，Discord OAuth 自动登录也失败。\n"
                              "请检查 Discord Token 是否有效（客户端退出登录会使令牌失效）。",
                    "time": self.__now_str(),
                }
            # 保存新 token（自动续命，下次直接用）
            self._session_token = new_token
            self.update_config({
                "enabled": self._enabled,
                "session_token": new_token,
                "discord_token": self._discord_token,
                "email": self._email,
                "cron": self._cron,
                "notify": self._notify,
                "renew_attempt": self._renew_attempt,
            })
            logger.info("Discord OAuth 登录成功，session_token 已自动更新")
            login_method = "Discord Token"
            status = self.__query_status(new_token)
            if status.get("need_relogin"):
                return {
                    "success": False,
                    "title": "登录失败",
                    "detail": "Discord OAuth 登录后仍无法访问账单页，账号可能被风控或封禁。",
                    "time": self.__now_str(),
                }

        expiry = status.get("expiry") or "（未获取到）"
        opens_at = status.get("opens_at")

        # 2. 判断是否到了可续期时间
        if opens_at:
            opens_dt = self.__parse_time(opens_at)
            if opens_dt and opens_dt > datetime.now(CN_TZ):
                remain = opens_dt - datetime.now(CN_TZ)
                hours = remain.total_seconds() / 3600
                return {
                    "success": True,
                    "title": "无需续期（冷却中）",
                    "detail": f"距下次可续期还有 {hours:.1f} 小时（{self.__fmt_dt(opens_dt)}）",
                    "expiry": expiry,
                    "login_method": login_method,
                    "time": self.__now_str(),
                }

        # 3. 尝试续期
        if not self._renew_attempt:
            return {
                "success": True,
                "title": "已到可续期时间（未开启自动续期）",
                "detail": "已开启仅监控模式，请手动续期或在插件配置中开启「自动续期」",
                "expiry": expiry,
                "login_method": login_method,
                "time": self.__now_str(),
            }

        renew_res = self.__do_renew(self._session_token)
        if renew_res.get("ok"):
            # 续期成功，重新查询新到期时间
            new_status = self.__query_status(self._session_token)
            new_expiry = new_status.get("expiry") or expiry
            return {
                "success": True,
                "title": "续期成功 +4 天",
                "detail": f"续期方式：API 直连\n新到期时间：{new_expiry}",
                "expiry": new_expiry,
                "login_method": login_method,
                "time": self.__now_str(),
            }
        else:
            # 失败（大概率 Turnstile 服务端校验）
            err = renew_res.get("error") or "未知错误"
            return {
                "success": False,
                "title": "自动续期失败",
                "detail": f"接口返回：{err}\n"
                          "常见原因：服务端要求 Turnstile 人机验证（纯 API 方式无法生成验证令牌）。\n"
                          "请手动登录 bot-hosting.net 点击续期，或继续使用浏览器自动化方案。",
                "expiry": expiry,
                "login_method": login_method,
                "time": self.__now_str(),
            }

    def __query_status(self, session_token: str) -> dict:
        """访问账单页解析订阅状态

        SSR 页面数据以序列化 JSON 嵌在 HTML 中，提取 freeRenewalDueAt / freeRenewalOpensAt
        等字段；同时回退到页面文本正则（Expires YYYY/MM/DD）。
        """
        result = {"need_relogin": False, "expiry": None, "opens_at": None, "tier": None, "status": None}
        if not session_token:
            result["need_relogin"] = True
            return result
        try:
            res = requests.get(
                BILLINGS_URL,
                headers={"User-Agent": USER_AGENT, "Accept": "text/html"},
                cookies={"session_token": session_token, "login": "true", "theme": "system"},
                timeout=30, allow_redirects=False,
            )
        except Exception as e:
            logger.error(f"Bot-Hosting 账单页访问失败: {e}")
            result["error"] = f"网络异常: {e}"
            return result

        if res.status_code in (301, 302):
            # 未登录会 302 到 /login
            result["need_relogin"] = True
            return result
        if res.status_code != 200:
            result["error"] = f"HTTP {res.status_code}"
            return result

        html = res.text
        # SvelteKit 序列化数据字段（hydration JSON）
        result["expiry"] = self.__extract_json_str(html, "freeRenewalDueAt")
        result["opens_at"] = self.__extract_json_str(html, "freeRenewalOpensAt")
        result["tier"] = self.__extract_json_str(html, "accountTier")
        result["status"] = self.__extract_json_str(html, "status")
        # 回退：页面文本日期
        if not result["expiry"]:
            m = re.search(r"Expires\s*[:\-]?\s*(\d{4}/\d{2}/\d{2})", html)
            if m:
                result["expiry"] = m.group(1)
        logger.info(f"Bot-Hosting 状态: tier={result['tier']} status={result['status']} "
                    f"到期={result['expiry']} 可续期={result['opens_at']}")
        return result

    @staticmethod
    def __extract_json_str(html: str, field: str) -> Optional[str]:
        """从 SSR HTML 的序列化数据中提取字符串字段值"""
        m = re.search(rf'"{field}"\s*:\s*"([^"]+)"', html)
        return m.group(1) if m else None

    def __do_renew(self, session_token: str) -> dict:
        """调用续期 API（turnstileToken 为空尝试）"""
        try:
            res = requests.post(
                RENEW_API,
                headers={
                    "User-Agent": USER_AGENT,
                    "Content-Type": "application/json",
                    "Origin": BASE_URL,
                    "Referer": BILLINGS_URL,
                    "Accept": "application/json",
                },
                cookies={"session_token": session_token, "login": "true"},
                json={"turnstileToken": ""},
                timeout=30,
            )
        except Exception as e:
            return {"ok": False, "error": f"网络异常: {e}"}
        if res.status_code == 200:
            try:
                data = res.json()
                if data.get("error"):
                    return {"ok": False, "error": data.get("error")}
                return {"ok": True, "data": data}
            except ValueError:
                return {"ok": True, "data": res.text[:200]}
        try:
            err = res.json().get("error") or res.text[:200]
        except ValueError:
            err = res.text[:200]
        return {"ok": False, "error": f"HTTP {res.status_code}: {err}"}

    def __discord_login(self) -> Optional[str]:
        """Discord OAuth 纯 HTTP 登录，成功返回新 session_token

        流程与 eooce/Auto-Renew-Bothosting 的 discord_authorize 一致：
        1. GET /login/discord 从 302 Location 提取 state
        2. POST Discord OAuth authorize 拿回调 location
        3. GET 回调地址捕获 Set-Cookie session_token
        """
        try:
            # 1. 获取 state
            r1 = requests.get(DISCORD_LOGIN_URL, headers={"User-Agent": USER_AGENT},
                              timeout=30, allow_redirects=False)
            if r1.status_code not in (301, 302, 303, 307):
                logger.error(f"Bot-Hosting Discord 登录页异常: HTTP {r1.status_code}")
                return None
            location = r1.headers.get("Location", "")
            m = re.search(r"[?&]state=([^&]+)", location)
            if not m:
                logger.error(f"未能从跳转地址解析 state: {location}")
                return None
            state = m.group(1)

            # 2. Discord 侧授权
            import urllib.parse
            query = urllib.parse.urlencode({
                "client_id": DISCORD_CLIENT_ID,
                "response_type": "code",
                "redirect_uri": f"{BASE_URL}/login",
                "scope": "identify email guilds",
                "state": state,
            })
            r2 = requests.post(
                f"{DISCORD_AUTHORIZE_API}?{query}",
                headers={
                    "User-Agent": USER_AGENT,
                    "Authorization": self._discord_token,
                    "Content-Type": "application/json",
                    "Origin": "https://discord.com",
                    "Referer": f"{DISCORD_AUTHORIZE_API}?{query}",
                    "x-discord-locale": "zh-CN",
                },
                json={
                    "permissions": "0",
                    "authorize": True,
                    "integration_type": 0,
                    "location_context": {
                        "guild_id": "10000",
                        "channel_id": "10000",
                        "channel_type": 10000,
                    },
                },
                timeout=30,
            )
            if r2.status_code != 200:
                logger.error(f"Discord OAuth 授权失败: HTTP {r2.status_code} - {r2.text[:200]}")
                return None
            callback = r2.json().get("location", "")
            if not callback:
                logger.error(f"Discord 授权响应缺少 location: {r2.text[:200]}")
                return None

            # 3. 回调捕获 session_token
            sess = requests.Session()
            sess.headers.update({"User-Agent": USER_AGENT})
            r3 = sess.get(callback, timeout=30)
            if "/error/banned" in r3.url:
                logger.error("Bot-Hosting 账号已被封禁")
                return None
            token = sess.cookies.get("session_token")
            if not token:
                logger.error(f"回调后未捕获 session_token，最终 URL: {r3.url}")
                return None
            return token
        except Exception as e:
            logger.error(f"Discord OAuth 登录异常: {e}")
            return None

    # ------------------------------------------------------------------
    # 通知
    # ------------------------------------------------------------------

    def __notify_result(self, result: dict):
        """按策略推送通知"""
        if self._notify == NOTIFY_OFF:
            return
        if self._notify == NOTIFY_FAILURE and result.get("success"):
            return
        self._notify_message(result.get("title") or "执行完成",
                             result.get("detail") or "",
                             failure=not result.get("success"),
                             expiry=result.get("expiry"))

    def _notify_message(self, title: str, text: str, failure: bool = False, expiry: str = ""):
        """拼接通知文案并推送（MP 消息渠道，含企业微信）"""
        lines = ["Bot-Hosting 自动续期通知", ""]
        lines.append(("❌ " if failure else "✅ ") + title)
        if self._email:
            lines.append(f"👤 账户: {self.__mask_email(self._email)}")
        if expiry:
            lines.append(f"📅 到期时间: {expiry}")
        if text:
            lines.append("")
            lines.append(text)
        lines.append(f"⏱️ 执行时间: {self.__now_str()}")
        self.post_message(
            mtype=NotificationType.Plugin,
            title="Bot-Hosting 自动续期",
            text="\n".join(lines),
        )

    @staticmethod
    def __mask_email(email: str) -> str:
        """邮箱脱敏"""
        if "@" in email:
            name, domain = email.split("@", 1)
            if len(name) > 4:
                return f"{name[:2]}****{name[-2:]}@{domain}"
            return email
        return email[:2] + "****"

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    @staticmethod
    def __now_str() -> str:
        return datetime.now(CN_TZ).strftime("%Y-%m-%d %H:%M:%S")

    @staticmethod
    def __parse_time(value: str) -> Optional[datetime]:
        """解析 ISO 时间（含时区偏移或 Z 结尾）"""
        if not value:
            return None
        try:
            v = value.strip()
            if v.endswith("Z"):
                v = v[:-1] + "+00:00"
            return datetime.fromisoformat(v)
        except ValueError:
            return None

    @staticmethod
    def __fmt_dt(dt: datetime) -> str:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(CN_TZ).strftime("%Y-%m-%d %H:%M")

    # ------------------------------------------------------------------
    # 页面
    # ------------------------------------------------------------------

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """配置页面"""
        return [
            {
                "component": "VForm",
                "content": [
                    # 启用 / 自动续期
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {"model": "enabled", "label": "启用插件"},
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "renew_attempt",
                                        "label": "自动续期（关闭则仅监控）",
                                    },
                                }]
                            },
                        ]
                    },
                    # session_token / discord_token
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "session_token",
                                        "label": "Session Token（必填）",
                                        "placeholder": "浏览器 Cookie 中的 session_token",
                                    },
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "discord_token",
                                        "label": "Discord Token（可选，失效自动换新）",
                                        "placeholder": "Discord 账户令牌，session 失效时备用登录",
                                    },
                                }]
                            },
                        ]
                    },
                    # 邮箱 / cron
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "email",
                                        "label": "账户邮箱（可选，通知显示用）",
                                        "placeholder": "your@email.com",
                                    },
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "cron",
                                        "label": "执行周期（cron）",
                                        "placeholder": "0 8 * * *（默认每天 8 点）",
                                    },
                                }]
                            },
                        ]
                    },
                    # 通知策略
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VSelect",
                                    "props": {
                                        "model": "notify",
                                        "label": "通知策略",
                                        "items": [
                                            {"title": "每次执行都通知", "value": NOTIFY_ALWAYS},
                                            {"title": "仅失败时通知", "value": NOTIFY_FAILURE},
                                            {"title": "关闭通知", "value": NOTIFY_OFF},
                                        ],
                                    },
                                }]
                            },
                        ]
                    },
                    # 说明
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VAlert",
                                    "props": {
                                        "type": "info",
                                        "variant": "tonal",
                                        "text": "Session Token 获取：浏览器登录 bot-hosting.net → F12 开发者工具 → 应用/存储 → Cookie → 复制 session_token 的值。"
                                                "Discord Token 获取详见 eooce/Auto-Renew-Bothosting 项目说明；配置后 session 失效会自动走 OAuth 换新并回写。"
                                                "免费套餐每 4 天需续期一次；插件先查询账单页状态，到可续期时间后调用官方续期接口。"
                                                "若服务端强制 Turnstile 人机验证导致续期失败，将推送通知提醒手动处理。",
                                    },
                                }]
                            },
                        ]
                    },
                ]
            }
        ], {
            "enabled": False,
            "session_token": "",
            "discord_token": "",
            "email": "",
            "cron": "0 8 * * *",
            "notify": NOTIFY_ALWAYS,
            "renew_attempt": True,
        }

    def get_page(self) -> List[dict]:
        """详情页：状态展示 + 立即执行按钮"""
        if not self.get_state():
            return [{
                "component": "VAlert",
                "props": {"type": "warning", "variant": "tonal",
                          "text": "插件未启用或未配置 Token，请先在配置中填写 session_token"}
            }]
        r = self._last_result or self.get_data("last_result") or {}
        # 状态字段
        attrs = [
            {"component": "VCol", "props": {"cols": 6, "md": 3}, "content": [
                {"component": "VStat", "props": {"name": "上次结果", "value": r.get("title") or "未执行"}}
            ]},
            {"component": "VCol", "props": {"cols": 6, "md": 3}, "content": [
                {"component": "VStat", "props": {"name": "到期时间", "value": r.get("expiry") or "—"}}
            ]},
            {"component": "VCol", "props": {"cols": 6, "md": 3}, "content": [
                {"component": "VStat", "props": {"name": "上次执行", "value": r.get("time") or "—"}}
            ]},
            {"component": "VCol", "props": {"cols": 6, "md": 3}, "content": [
                {"component": "VStat", "props": {"name": "执行周期", "value": self._cron or "—"}}
            ]},
        ]
        content = [
            {"component": "VRow", "content": attrs},
        ]
        if r.get("detail"):
            content.append({
                "component": "VAlert",
                "props": {
                    "type": "error" if not r.get("success") else "success",
                    "variant": "tonal",
                    "text": str(r.get("detail"))[:500],
                }
            })
        content.append({
            "component": "VRow",
            "content": [{
                "component": "VCol",
                "props": {"cols": 12},
                "content": [{
                    "component": "VBtn",
                    "props": {
                        "color": "primary",
                        "block": True,
                        "text": "立即续期",
                    },
                    "events": {
                        "click": {
                            "api": "plugin/BotHostingRenew/run",
                            "method": "get",
                        }
                    },
                }],
            }],
        })
        return [{
            "component": "VCard",
            "props": {"title": "Bot-Hosting 续期状态"},
            "content": content,
        }]

    def stop_service(self):
        """退出插件"""
        pass
