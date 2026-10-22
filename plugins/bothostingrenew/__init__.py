# -*- coding: utf-8 -*-
"""Bot-Hosting.net 自动续期插件

移植自 eooce/Auto-Renew-Bothosting（GitHub Actions + SeleniumBase 方案），
改为 MoviePilot 内运行的浏览器自动化实现：
1. 登录优先账号密码（密码不过期），未配置密码时用 session_token，失效可走 Discord OAuth 换新
2. 每天定时检查账单页，距到期不足 24 小时才执行续期
3. 用 Playwright 启动 Chromium 模拟真人点击续期，在弹窗中处理 Cloudflare Turnstile
4. 浏览器失败时触发 GitHub Actions（workflow_dispatch）兜底，轮询结果后推送
5. 结束后核对一次到期日，结果通过 MP 消息渠道推送（可到企业微信）

参考与致谢：https://github.com/eooce/Auto-Renew-Bothosting
"""
import os
import re
import sys
import time
import threading
import traceback
import subprocess
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
DISCORD_LOGIN_URL = f"{BASE_URL}/login/discord"
DISCORD_CLIENT_ID = "884382422530158623"
DISCORD_AUTHORIZE_API = "https://discord.com/api/v9/oauth2/authorize"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36")
CN_TZ = timezone(timedelta(hours=8))

# 站点 DOM（真机确认）
DIALOG_SELECTOR = "#renew-dialog"
DIALOG_CONFIRM_SELECTOR = "#renew-dialog button.btn-primary"
TURNSTILE_FRAME_SELECTOR = 'iframe[src*="challenges.cloudflare.com"]'
TURNSTILE_API = "https://challenges.cloudflare.com/turnstile/v0/api.js"

# 浏览器参数
BROWSERS_PATH = "/config/.playwright"   # 持久化到 config 卷，重建容器不用重复下载
TURNSTILE_WAIT = 120                    # 等待 Turnstile token 上限（秒）
BROWSER_TOTAL_TIMEOUT = 330             # 整个浏览器流程上限（秒）

# GitHub Actions 备用方案
GITHUB_API = "https://api.github.com"
GITHUB_POLL_INTERVAL = 60               # 轮询 Action 状态间隔（秒）
GITHUB_POLL_TIMEOUT = 1800              # 等待 Action 完成上限（秒）
RENEW_WINDOW_HOURS = 24                 # 到期前多久内允许续期

# 通知策略
NOTIFY_ALWAYS = "always"     # 每次执行都通知
NOTIFY_FAILURE = "failure"   # 仅失败/异常时通知
NOTIFY_OFF = "off"           # 关闭通知


class BotHostingRenew(_PluginBase):
    # 插件名称
    plugin_name = "Bot-Hosting自动续期"
    # 插件描述
    plugin_desc = "Bot-Hosting.net 免费套餐自动续期（4天一期）：账号密码优先，浏览器模拟点击过 Turnstile，失败转 GitHub Actions 兜底并推送通知"
    # 插件图标
    plugin_icon = "https://bot-hosting.net/favicon.ico"
    # 插件版本
    plugin_version = "1.3.0"
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
    _proxy = ""
    _password = ""
    _gh_fallback = False
    _gh_token = ""
    _gh_repo = "Cxquang/Auto-Renew-Bothosting"
    _gh_workflow = "renew.yml"
    _gh_ref = "main"
    _onlyonce = False
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
            self._proxy = (config.get("proxy") or "").strip()
            self._password = (config.get("password") or "").strip()
            self._gh_fallback = config.get("gh_fallback") or False
            self._gh_token = (config.get("gh_token") or "").strip()
            self._gh_repo = (config.get("gh_repo") or "Cxquang/Auto-Renew-Bothosting").strip()
            self._gh_workflow = (config.get("gh_workflow") or "renew.yml").strip()
            self._gh_ref = (config.get("gh_ref") or "main").strip()
            self._onlyonce = config.get("onlyonce") or False
            # 立即执行一次：开启并保存后触发，执行完自动关闭
            if self._onlyonce:
                self._onlyonce = False
                self.__save_config(onlyonce=False)
                if self.get_state():
                    logger.info("Bot-Hosting 立即执行一次已触发，5 秒后台执行")
                    threading.Timer(5.0, self.run_once).start()
                else:
                    logger.warn("Bot-Hosting 立即执行一次：插件未启用或未配置 Token，跳过")

    def __save_config(self, onlyonce: bool = False):
        """回写当前配置（保持字段齐全，避免丢项）"""
        self.update_config({
            "enabled": self._enabled,
            "session_token": self._session_token,
            "discord_token": self._discord_token,
            "email": self._email,
            "cron": self._cron,
            "notify": self._notify,
            "renew_attempt": self._renew_attempt,
            "proxy": self._proxy,
            "password": self._password,
            "gh_fallback": self._gh_fallback,
            "gh_token": self._gh_token,
            "gh_repo": self._gh_repo,
            "gh_workflow": self._gh_workflow,
            "gh_ref": self._gh_ref,
            "onlyonce": onlyonce,
        })

    def get_state(self) -> bool:
        return self._enabled and bool((self._email and self._password)
                                      or self._session_token or self._discord_token)

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
        use_password = bool(self._email and self._password)
        if use_password:
            login_method = "账号密码"

        # 1. 查询账单页状态（没有 session_token 时跳过，直接走浏览器登录）
        if session_token:
            status = self.__query_status(session_token)
        else:
            status = {"need_relogin": False, "expiry": None, "opens_at": None,
                      "tier": None, "status": None}
        if status.get("need_relogin"):
            if use_password:
                # 有账号密码，交给浏览器重新登录，不走 Discord
                logger.info("session_token 失效，改用账号密码登录")
                status = {"need_relogin": False, "expiry": None, "opens_at": None,
                          "tier": None, "status": None}
            else:
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
                self.__save_config()
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

        # 网络/接口异常且未解析到任何页面数据时，直接报错，避免无意义地启动浏览器
        if status.get("error") and not status.get("expiry"):
            return {
                "success": False,
                "title": "状态查询失败",
                "detail": f"{status.get('error')}\n"
                          "容器无法访问 bot-hosting.net，请检查网络或填写代理。",
                "time": self.__now_str(),
            }

        # 2. 判断是否到了可续期时间：opens_at 优先，缺失时按「到期前 24 小时」判断
        now = datetime.now(CN_TZ)
        if opens_at:
            opens_dt = self.__parse_time(opens_at)
            if opens_dt and opens_dt > now:
                hours = (opens_dt - now).total_seconds() / 3600
                return {
                    "success": True,
                    "title": "无需续期（冷却中）",
                    "detail": f"距下次可续期还有 {hours:.1f} 小时（{self.__fmt_dt(opens_dt)}）",
                    "expiry": expiry,
                    "login_method": login_method,
                    "time": self.__now_str(),
                }
        else:
            exp_dt = self.__parse_time(status.get("expiry"))
            if exp_dt:
                remain_hours = (exp_dt - now).total_seconds() / 3600
                if remain_hours > RENEW_WINDOW_HOURS:
                    return {
                        "success": True,
                        "title": "无需续期（未到窗口）",
                        "detail": f"距到期还有 {remain_hours:.1f} 小时（{self.__fmt_dt(exp_dt)}），"
                                  f"到期前 {RENEW_WINDOW_HOURS} 小时内才会执行续期",
                        "expiry": expiry,
                        "login_method": login_method,
                        "time": self.__now_str(),
                    }

        # 3. 仅监控模式
        if not self._renew_attempt:
            return {
                "success": True,
                "title": "已到可续期时间（未开启自动续期）",
                "detail": "已开启仅监控模式，请手动续期或在插件配置中开启「自动续期」",
                "expiry": expiry,
                "login_method": login_method,
                "time": self.__now_str(),
            }

        old_expiry = status.get("expiry")

        # 4. 浏览器模拟续期（账号密码优先，其次 session_token）
        renew_res = self.__do_renew(session_token)
        if renew_res.get("ok"):
            ok, msg, new_expiry = self.__verify_renewal(old_expiry)
            return {
                "success": ok,
                "title": "续期成功 +4 天" if ok else "续期已提交，但未确认到账",
                "detail": f"续期方式：浏览器模拟点击（Playwright，{login_method}）\n{msg}",
                "expiry": new_expiry or expiry,
                "login_method": login_method,
                "time": self.__now_str(),
            }

        err = renew_res.get("error") or "未知错误"
        # 浏览器已明确告知「按钮不可用/冷却中」，属于正常状态，不触发备用方案
        if "不可用" in err or "冷却" in err or "未找到可用的续期按钮" in err:
            return {
                "success": True,
                "title": "无需续期（冷却中）",
                "detail": err,
                "expiry": expiry,
                "login_method": login_method,
                "time": self.__now_str(),
            }

        diag = renew_res.get("diagnostics")
        diag_text = ""
        if isinstance(diag, dict):
            diag_text = ("\n现场诊断："
                         f"token长度={diag.get('tokenLen')}，"
                         f"响应输入框={diag.get('hasInput')}，"
                         f"Turnstile组件={diag.get('hasWidget')}，"
                         f"api.js可用={diag.get('hasApi')}，"
                         f"确认按钮禁用={diag.get('confirmDisabled')}，"
                         f"iframe={diag.get('iframes')}")
        logs_text = ""
        if renew_res.get("logs"):
            logs_text = "\n执行日志：" + "；".join(renew_res.get("logs")[-6:])

        # 5. 浏览器失败后转 GitHub Actions（若已开启备用方案）
        if self._gh_fallback and self._gh_token:
            logger.info("浏览器续期失败，尝试触发 GitHub Actions 备用方案")
            ok, run_id, gh_err = self.__dispatch_github_action()
            if ok:
                threading.Thread(target=self.__watch_github_run,
                                 args=(run_id, old_expiry), daemon=True).start()
                return {
                    "success": True,
                    "title": "已转 GitHub Actions 续期",
                    "detail": f"浏览器续期失败：{err}\n"
                              f"已触发 {self._gh_repo} 的 {self._gh_workflow}，"
                              "Action 执行完成后会再次推送通知，并自动核对到期时间。",
                    "expiry": expiry,
                    "login_method": login_method,
                    "time": self.__now_str(),
                }
            err += f"\nGitHub Actions 触发失败：{gh_err}"

        return {
            "success": False,
            "title": "自动续期失败",
            "detail": f"失败原因：{err}{diag_text}{logs_text}\n"
                      "排查建议：\n"
                      "1. 确认容器可访问 bot-hosting.net 与 challenges.cloudflare.com（Turnstile 依赖后者）\n"
                      "2. 若服务器在墙内，请在插件配置中填写代理（http://user:pass@host:port）\n"
                      "3. 开启「GitHub Actions 备用方案」并填写 Token，浏览器失败时由 Action 兜底\n"
                      "4. 仍失败时请手动登录续期一次。",
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
                proxies=self.__req_proxies(),
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
        """浏览器续期入口

        MoviePilot 的 /run 接口运行在 asyncio 事件循环线程里，而 Playwright
        同步 API 禁止在事件循环线程中使用，因此单独开线程执行再等结果。
        """
        outcome: Dict[str, Any] = {}

        def _worker():
            try:
                outcome.update(self.__browser_renew(session_token))
            except Exception as e:
                logger.error(f"[BotHosting] 浏览器续期异常: {e}\n{traceback.format_exc()}")
                outcome.update({"ok": False, "error": f"浏览器续期异常: {e}"})

        t = threading.Thread(target=_worker, name="BotHostingRenew", daemon=True)
        t.start()
        t.join(timeout=BROWSER_TOTAL_TIMEOUT)
        if t.is_alive():
            return {"ok": False,
                    "error": f"浏览器续期超时（{BROWSER_TOTAL_TIMEOUT}s），"
                             "多半是容器网络无法访问 challenges.cloudflare.com，或被 Cloudflare 风控"}
        return outcome or {"ok": False, "error": "浏览器续期未返回结果"}

    def __browser_renew(self, session_token: str) -> dict:
        """Playwright 模拟真人：打开账单页 -> 点续期 -> 过 Turnstile -> 点确认"""
        self.__prepare_browser_env()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return {"ok": False,
                    "error": "未安装 playwright 库，请在容器内执行：pip install playwright"}

        logs: List[str] = []

        def log(msg: str):
            logger.info(f"[BotHosting] {msg}")
            logs.append(msg)

        launch_kwargs: Dict[str, Any] = {
            "headless": True,
            "args": [
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-gpu",
                "--no-first-run",
                "--no-default-browser-check",
                "--lang=en-US",
            ],
        }
        if self._proxy:
            launch_kwargs["proxy"] = {"server": self._proxy}
            log(f"使用代理: {self.__mask_proxy(self._proxy)}")

        browser = None
        try:
            with sync_playwright() as p:
                log("启动 Chromium...")
                try:
                    browser = p.chromium.launch(**launch_kwargs)
                except Exception as e:
                    msg = str(e)
                    if "Executable doesn't exist" in msg or "playwright install" in msg:
                        log("Chromium 未安装，开始下载（首次约 150MB，存放在 "
                            f"{os.environ.get('PLAYWRIGHT_BROWSERS_PATH')}）...")
                        self.__install_chromium()
                        browser = p.chromium.launch(**launch_kwargs)
                    else:
                        raise

                context = browser.new_context(
                    user_agent=USER_AGENT,
                    locale="en-US",
                    viewport={"width": 1366, "height": 900},
                    ignore_https_errors=True,
                )
                # 去掉自动化特征（Cloudflare 会检测 webdriver/plugins 等）
                context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                    "Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en']});"
                    "Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});"
                    "window.chrome = { runtime: {} };"
                )
                page = context.new_page()
                page.set_default_timeout(30000)
                log(f"Turnstile 资源连通性: {self.__check_turnstile_net()}")

                logged_in = False
                if self._email and self._password:
                    # 优先级 1：账号密码登录（密码不会过期，比 session_token 稳）
                    try:
                        logged_in = self.__login_with_password(page, log)
                    except Exception as e:
                        log(f"账号密码登录异常: {e}")
                if logged_in:
                    # 同步登录后最新的 session_token，供后续查询与核对使用
                    try:
                        for c in context.cookies():
                            value = c.get("value")
                            if c.get("name") == "session_token" and value \
                                    and value != self._session_token:
                                self._session_token = value
                                self.__save_config()
                                log("已同步新的 session_token")
                                break
                    except Exception:
                        pass
                else:
                    if not session_token:
                        return {"ok": False,
                                "error": "登录失败：账号密码登录未成功，且未配置可用的 session_token",
                                "logs": logs[-10:]}
                    log("回退到 session_token Cookie 注入")
                    context.add_cookies([
                        {"name": "session_token", "value": session_token, "url": f"{BASE_URL}/"},
                        {"name": "login", "value": "true", "url": f"{BASE_URL}/"},
                        {"name": "theme", "value": "system", "url": f"{BASE_URL}/"},
                    ])

                log(f"打开账单页: {BILLINGS_URL}")
                page.goto(BILLINGS_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(1500)
                log(f"当前地址: {page.url}")
                if "/login" in page.url:
                    return {"ok": False, "need_relogin": True,
                            "error": "session_token 已失效（被重定向到登录页）"}

                renew_btn = self.__find_renew_button(page)
                if renew_btn is None:
                    return {"ok": False,
                            "error": "账单页未找到可用的续期按钮（可能尚未到可续期时间，或页面结构变化）"}
                try:
                    if not renew_btn.is_enabled():
                        return {"ok": False, "error": "续期按钮当前不可用（尚未到可续期时间/冷却中）"}
                except Exception:
                    pass
                log("找到续期按钮，点击打开弹窗...")
                renew_btn.click()

                try:
                    page.wait_for_selector(DIALOG_SELECTOR, state="visible", timeout=15000)
                except Exception:
                    return {"ok": False, "error": "点击续期后弹窗未出现",
                            "diagnostics": self.__turnstile_snapshot(page)}
                log("续期弹窗已出现，开始处理 Turnstile 验证...")

                token = self.__wait_turnstile(page, log)
                if not token:
                    snap = self.__turnstile_snapshot(page)
                    return {"ok": False,
                            "error": "Turnstile 验证未通过（cf-turnstile-response 一直为空）",
                            "diagnostics": snap,
                            "logs": logs[-10:]}
                log(f"Turnstile 已通过（token 长度 {len(token)}），点击「Renew for 4 days」")

                confirm = page.locator(DIALOG_CONFIRM_SELECTOR).first
                api_ok = True
                api_msg = ""
                try:
                    with page.expect_response(
                            lambda r: "/api/billing/renew-free" in r.url
                                      and r.request.method == "POST",
                            timeout=60000) as resp_info:
                        confirm.click()
                    resp = resp_info.value
                    log(f"续期接口返回: HTTP {resp.status}")
                    try:
                        data = resp.json()
                    except Exception:
                        data = {}
                    err = data.get("error") if isinstance(data, dict) else None
                    if resp.status >= 400 or err:
                        api_ok = False
                        api_msg = str(err or f"HTTP {resp.status}")
                except Exception as e:
                    # 没抓到接口响应时用页面结果兜底
                    log(f"未捕获续期接口响应: {e}")

                if not api_ok:
                    return {"ok": False, "error": f"续期接口返回失败: {api_msg}",
                            "diagnostics": self.__turnstile_snapshot(page)}

                log("续期请求已提交，刷新账单页确认新到期时间...")
                page.wait_for_timeout(1500)
                try:
                    page.reload(wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(1500)
                except Exception as e:
                    log(f"刷新账单页失败（忽略）: {e}")
                new_expiry = self.__read_expiry_from_page(page)
                log(f"续期后到期时间: {new_expiry or '未解析到'}")
                return {"ok": True, "expiry": new_expiry, "logs": logs[-10:]}
        except Exception as e:
            logger.error(f"[BotHosting] 浏览器流程失败: {e}\n{traceback.format_exc()}")
            return {"ok": False, "error": f"浏览器流程失败: {e}", "logs": logs[-10:]}
        finally:
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass

    # ---------------------- 登录 / 结果校验 ----------------------

    def __login_with_password(self, page, log) -> bool:
        """账号密码登录（登录页带 Turnstile，验证通过后提交）"""
        log("使用账号密码登录...")
        page.goto(f"{BASE_URL}/login", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)
        try:
            page.locator('button[type="submit"]').first.click(timeout=10000)
            page.wait_for_timeout(3000)
        except Exception as e:
            log(f"未找到 Continue 按钮（可能已在邮箱表单）: {e}")
        try:
            page.wait_for_selector('input[type="email"]', timeout=20000)
        except Exception:
            log("登录页未出现邮箱输入框")
            return False
        page.fill('input[type="email"]', self._email)
        page.wait_for_timeout(500)
        page.fill('input[type="password"]', self._password)
        page.wait_for_timeout(1000)
        token = self.__wait_turnstile(page, log)
        if not token:
            log("登录页 Turnstile 未通过，仍尝试提交")
        page.locator('button[type="submit"]').first.click()
        page.wait_for_timeout(4000)
        if "/login" in page.url:
            log(f"登录后仍停留在登录页: {page.url}")
            return False
        log(f"账号密码登录成功，当前地址: {page.url}")
        return True

    def __verify_renewal(self, old_expiry: Optional[str]) -> Tuple[bool, str, Optional[str]]:
        """续期后核对到期时间：先 HTTP 查询，读不到再用浏览器兜底（只查一次）"""
        old = old_expiry if old_expiry and not str(old_expiry).startswith("（") else None
        new_expiry = self.__query_status(self._session_token).get("expiry") if self._session_token else None
        if not new_expiry:
            logger.info("[BotHosting] HTTP 未读到到期时间，改用浏览器核对")
            new_expiry = self.__browser_expiry()
        if not new_expiry:
            return False, "无法读取账单页到期时间，请手动到 bot-hosting 检查", None
        if old:
            old_dt = self.__parse_time(old)
            new_dt = self.__parse_time(new_expiry)
            if old_dt and new_dt:
                if new_dt > old_dt:
                    return True, (f"到期时间已延后：{self.__fmt_dt(old_dt)} → "
                                  f"{self.__fmt_dt(new_dt)}"), new_expiry
                return False, f"到期时间未变化（{self.__fmt_dt(new_dt)}）", new_expiry
            if str(old) == str(new_expiry):
                return False, f"到期时间未变化（{new_expiry}）", new_expiry
        return True, f"当前到期时间：{new_expiry}", new_expiry

    def __browser_expiry(self) -> Optional[str]:
        """无头浏览器读取到期时间（HTTP 被 Cloudflare 拦截时兜底）"""
        box: Dict[str, Any] = {}

        def _worker():
            try:
                box["expiry"] = self.__browser_expiry_sync()
            except Exception as e:
                logger.warn(f"[BotHosting] 浏览器核对到期时间失败: {e}")

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        t.join(timeout=BROWSER_TOTAL_TIMEOUT)
        return box.get("expiry")

    def __browser_expiry_sync(self) -> Optional[str]:
        self.__prepare_browser_env()
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return None
        launch_kwargs: Dict[str, Any] = {
            "headless": True,
            "args": ["--no-sandbox", "--disable-dev-shm-usage",
                     "--disable-blink-features=AutomationControlled", "--disable-gpu"],
        }
        if self._proxy:
            launch_kwargs["proxy"] = {"server": self._proxy}
        with sync_playwright() as p:
            try:
                browser = p.chromium.launch(**launch_kwargs)
            except Exception as e:
                if "Executable doesn't exist" in str(e) or "playwright install" in str(e):
                    self.__install_chromium()
                    browser = p.chromium.launch(**launch_kwargs)
                else:
                    raise
            try:
                context = browser.new_context(user_agent=USER_AGENT, locale="en-US",
                                              ignore_https_errors=True)
                context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                )
                page = context.new_page()
                logged_in = False
                if self._email and self._password:
                    try:
                        logged_in = self.__login_with_password(page, lambda m: None)
                    except Exception:
                        logged_in = False
                if not logged_in:
                    if not self._session_token:
                        return None
                    context.add_cookies([
                        {"name": "session_token", "value": self._session_token,
                         "url": f"{BASE_URL}/"},
                        {"name": "login", "value": "true", "url": f"{BASE_URL}/"},
                        {"name": "theme", "value": "system", "url": f"{BASE_URL}/"},
                    ])
                page.goto(BILLINGS_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(2000)
                return self.__read_expiry_from_page(page)
            finally:
                try:
                    browser.close()
                except Exception:
                    pass

    # ---------------------- GitHub Actions 兜底 ----------------------

    def __gh_api(self, method: str, path: str, payload: Optional[dict] = None) -> dict:
        """调用 GitHub REST API（直连 api.github.com，gh-proxy 不支持可靠的 POST）"""
        res = requests.request(
            method, f"{GITHUB_API}{path}",
            headers={
                "Authorization": f"Bearer {self._gh_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            json=payload, proxies=self.__req_proxies(), timeout=30,
        )
        if res.status_code >= 400:
            raise RuntimeError(f"HTTP {res.status_code}: {res.text[:200]}")
        return res.json() if res.text else {}

    def __dispatch_github_action(self) -> Tuple[bool, Optional[int], str]:
        """触发 workflow_dispatch，返回 (成功, run_id, 错误)"""
        if not (self._gh_token and self._gh_repo and self._gh_workflow):
            return False, None, "未配置 GitHub Token / 仓库 / 工作流文件名"
        try:
            self.__gh_api("POST",
                          f"/repos/{self._gh_repo}/actions/workflows/{self._gh_workflow}/dispatches",
                          {"ref": self._gh_ref or "main"})
        except Exception as e:
            return False, None, f"{e}"
        logger.info(f"[BotHosting] 已触发 GitHub Actions: {self._gh_repo}/{self._gh_workflow}")
        # dispatch 后 run 列表有几秒延迟，轮询拿到本次 run_id
        for _ in range(6):
            time.sleep(5)
            try:
                data = self.__gh_api(
                    "GET",
                    f"/repos/{self._gh_repo}/actions/workflows/{self._gh_workflow}/runs?per_page=5")
            except Exception as e:
                logger.warn(f"[BotHosting] 查询 Action run 失败: {e}")
                continue
            runs = data.get("workflow_runs") or []
            if runs:
                return True, runs[0].get("id"), ""
        return True, None, ""

    def __watch_github_run(self, run_id: Optional[int], old_expiry: Optional[str]):
        """后台等待 Action 完成 -> 推送结论 -> 核对一次到期时间 -> 推送最终结果"""
        deadline = time.time() + GITHUB_POLL_TIMEOUT
        run = None
        while time.time() < deadline:
            time.sleep(GITHUB_POLL_INTERVAL)
            try:
                if run_id:
                    run = self.__gh_api("GET", f"/repos/{self._gh_repo}/actions/runs/{run_id}")
                else:
                    data = self.__gh_api(
                        "GET",
                        f"/repos/{self._gh_repo}/actions/workflows/{self._gh_workflow}/runs?per_page=5")
                    runs = data.get("workflow_runs") or []
                    run = runs[0] if runs else None
            except Exception as e:
                logger.warn(f"[BotHosting] 轮询 Action 状态失败: {e}")
                continue
            if run and run.get("status") == "completed":
                break

        if not run or run.get("status") != "completed":
            self._notify_message(
                "GitHub Action 结果未知",
                f"{GITHUB_POLL_TIMEOUT // 60} 分钟内未拿到完成状态，请到 Actions 页面查看",
                failure=True, expiry=old_expiry or "")
            return

        conclusion = run.get("conclusion") or "unknown"
        html_url = run.get("html_url") or ""
        logger.info(f"[BotHosting] GitHub Action 结束: {conclusion} {html_url}")
        if conclusion != "success":
            self._notify_message(
                f"GitHub Action 执行结束：{conclusion}",
                f"运行地址：{html_url}\nAction 未成功，未能自动续期，请手动检查。",
                failure=True, expiry=old_expiry or "")
            return

        ok, msg, new_expiry = self.__verify_renewal(old_expiry)
        self._notify_message(
            "GitHub Action 续期成功" if ok else "GitHub Action 已结束，但未确认续期",
            f"运行地址：{html_url}\n{msg}",
            failure=not ok, expiry=new_expiry or old_expiry or "")

    # ---------------------- 浏览器辅助方法 ----------------------

    @staticmethod
    def __prepare_browser_env() -> str:
        """设置浏览器持久化目录（/moviepilot 非卷，/config 是卷）"""
        path = os.environ.get("PLAYWRIGHT_BROWSERS_PATH") or BROWSERS_PATH
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = path
        try:
            os.makedirs(path, exist_ok=True)
        except Exception:
            pass
        return path

    def __install_chromium(self):
        """运行时安装 Chromium（镜像未内置浏览器二进制）"""
        env = os.environ.copy()
        env["PLAYWRIGHT_BROWSERS_PATH"] = self.__prepare_browser_env()
        proc = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "chromium"],
            env=env, capture_output=True, text=True, timeout=600,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "")[-500:]
            raise RuntimeError(f"Chromium 下载失败（code={proc.returncode}）：{detail}")
        logger.info(f"[BotHosting] Chromium 下载完成: {env['PLAYWRIGHT_BROWSERS_PATH']}")

    def __find_renew_button(self, page):
        """按文案定位外部续期按钮"""
        selectors = [
            'button:has-text("Renew free plan")',
            'button:has-text("续期免费")',
            'button:has-text("Renew")',
            'main button.btn-primary',
            'button.btn-primary',
        ]
        for sel in selectors:
            try:
                loc = page.locator(sel)
                count = loc.count()
            except Exception:
                continue
            for i in range(count):
                btn = loc.nth(i)
                try:
                    if not btn.is_visible():
                        continue
                    text = (btn.inner_text() or "").strip()
                except Exception:
                    continue
                low = text.lower()
                if "renew" in low or "续期" in text:
                    return btn
        return None

    def __wait_turnstile(self, page, log) -> str:
        """轮询 cf-turnstile-response；为空时尝试点击复选框"""
        # 先确认 Turnstile 组件本身是否加载出来
        try:
            page.locator(TURNSTILE_FRAME_SELECTOR).first.wait_for(state="attached", timeout=20000)
            log("Turnstile 组件已加载")
        except Exception:
            log("Turnstile 组件 20 秒内未出现（challenges.cloudflare.com 可能未加载成功）")
        try:
            log(f"window.turnstile 是否可用: {page.evaluate('() => !!window.turnstile')}")
        except Exception:
            pass

        deadline = time.time() + TURNSTILE_WAIT
        last_click = 0.0
        attempt = 0
        while time.time() < deadline:
            token = self.__read_turnstile_token(page)
            if token:
                return token
            if attempt < 10 and time.time() - last_click >= 8:
                attempt += 1
                if self.__click_turnstile(page):
                    log(f"第 {attempt} 次点击 Turnstile 复选框（token 仍为空）")
                last_click = time.time()
            page.wait_for_timeout(1500)
        return ""

    def __check_turnstile_net(self) -> str:
        """从容器直接探测 Turnstile 脚本是否可达，便于定位网络/代理问题"""
        try:
            r = requests.get(TURNSTILE_API,
                             headers={"User-Agent": USER_AGENT},
                             proxies=self.__req_proxies(), timeout=15)
            return f"HTTP {r.status_code}, {len(r.content)} bytes"
        except Exception as e:
            return f"失败: {e}"

    @staticmethod
    def __read_turnstile_token(page) -> str:
        js = """() => {
            const el = document.querySelector('input[name="cf-turnstile-response"]');
            return el ? (el.value || '') : '';
        }"""
        try:
            val = page.evaluate(js) or ""
            if val:
                return val
        except Exception:
            pass
        # 兜底：有些实现把 input 放进 iframe 内部
        for frame in page.frames:
            if "challenges.cloudflare.com" in (frame.url or ""):
                try:
                    val = frame.evaluate(js) or ""
                    if val:
                        return val
                except Exception:
                    continue
        return ""

    @staticmethod
    def __click_turnstile(page) -> bool:
        """点击 Turnstile 复选框：优先点 iframe 内 checkbox，其次按坐标点"""
        try:
            frame = page.frame_locator('iframe[src*="challenges.cloudflare.com"]')
            for sel in ('input[type="checkbox"]', "#checkbox", 'label[for="checkbox"]'):
                try:
                    loc = frame.locator(sel)
                    if loc.count():
                        loc.first.click(timeout=3000)
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        for frame in page.frames:
            if "challenges.cloudflare.com" not in (frame.url or ""):
                continue
            for sel in ('input[type="checkbox"]', "#checkbox", 'label[for="checkbox"]'):
                try:
                    loc = frame.locator(sel)
                    if loc.count():
                        loc.first.click(timeout=3000)
                        return True
                except Exception:
                    continue
            # 复选框常在 iframe 左上角，坐标点击兜底
            try:
                frame.click("body", position={"x": 30, "y": 32}, timeout=3000)
                return True
            except Exception:
                continue
        try:
            el = page.query_selector('iframe[src*="challenges.cloudflare.com"]')
            box = el.bounding_box() if el else None
            if box and box.get("width"):
                page.mouse.click(box["x"] + box["width"] * 0.15,
                                 box["y"] + box["height"] * 0.5)
                return True
        except Exception:
            pass
        return False

    @staticmethod
    def __turnstile_snapshot(page) -> dict:
        """失败现场诊断信息，写进通知方便排查"""
        js = """() => {
            const dlg = document.querySelector('#renew-dialog');
            const el = document.querySelector('input[name="cf-turnstile-response"]');
            const btn = document.querySelector('#renew-dialog button.btn-primary');
            return {
                dialog: dlg ? (dlg.innerText || '').slice(0, 160) : '',
                hasInput: !!el,
                tokenLen: el ? (el.value || '').length : -1,
                confirmDisabled: btn ? !!btn.disabled : null,
                hasWidget: !!document.querySelector('.cf-turnstile, [data-sitekey], iframe[src*="challenges.cloudflare.com"]'),
                hasApi: !!window.turnstile,
                iframes: Array.from(document.querySelectorAll('iframe'))
                    .map(f => f.src).slice(0, 5),
                scripts: Array.from(document.querySelectorAll('script[src]'))
                    .map(s => s.src).filter(s => s.includes('turnstile')).slice(0, 5),
            };
        }"""
        try:
            return page.evaluate(js)
        except Exception as e:
            return {"snapshot_error": str(e)}

    def __read_expiry_from_page(self, page) -> Optional[str]:
        """从当前页面 HTML 中提取到期时间（优先 SSR JSON）"""
        try:
            html = page.content()
        except Exception:
            return None
        val = self.__extract_json_str(html, "freeRenewalDueAt")
        if val:
            return val
        m = re.search(r"Expires\s*[:\-]?\s*(\d{4}/\d{2}/\d{2})", html)
        return m.group(1) if m else None

    @staticmethod
    def __mask_proxy(proxy: str) -> str:
        """代理地址脱敏，避免日志泄露账号密码"""
        return re.sub(r"//([^:@/]+):([^@/]+)@", r"//\1:***@", proxy)

    def __req_proxies(self) -> Optional[dict]:
        if not self._proxy:
            return None
        return {"http": self._proxy, "https": self._proxy}

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
                              proxies=self.__req_proxies(),
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
                proxies=self.__req_proxies(),
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
            sess.proxies.update(self.__req_proxies() or {})
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
        """解析 ISO 时间或账单页日期（YYYY/MM/DD）"""
        if not value:
            return None
        v = value.strip()
        try:
            if v.endswith("Z"):
                v = v[:-1] + "+00:00"
            dt = datetime.fromisoformat(v)
            return dt if dt.tzinfo else dt.replace(tzinfo=CN_TZ)
        except ValueError:
            pass
        for fmt in ("%Y/%m/%d", "%m/%d/%Y", "%Y-%m-%d"):
            try:
                return datetime.strptime(v, fmt).replace(tzinfo=CN_TZ)
            except ValueError:
                continue
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
                                        "label": "Session Token（可选，账号密码优先）",
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
                    # 邮箱 / 密码
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
                                        "label": "账户邮箱（账号密码登录必填）",
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
                                        "model": "password",
                                        "label": "账户密码（优先于 Session Token）",
                                        "placeholder": "登录密码，填入后每次自动登录",
                                        "type": "password",
                                    },
                                }]
                            },
                        ]
                    },
                    # cron
                    {
                        "component": "VRow",
                        "content": [
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
                    # GitHub Actions 备用方案
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "gh_fallback",
                                        "label": "GitHub Actions 备用方案",
                                        "placeholder": "浏览器续期失败时，触发 Auto-Renew-Bothosting 的 workflow 兜底",
                                    },
                                }]
                            },
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "gh_token",
                                        "label": "GitHub PAT（Actions 读写权限）",
                                        "placeholder": "github_pat_xxx 或 ghp_xxx",
                                        "type": "password",
                                    },
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "gh_repo",
                                        "label": "GitHub 仓库",
                                        "placeholder": "owner/repo",
                                    },
                                }]
                            },
                        ]
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "gh_workflow",
                                        "label": "工作流文件名",
                                        "placeholder": "renew.yml",
                                    },
                                }]
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "gh_ref",
                                        "label": "分支",
                                        "placeholder": "main",
                                    },
                                }]
                            },
                        ]
                    },
                    # 代理（可选）
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 8},
                                "content": [{
                                    "component": "VTextField",
                                    "props": {
                                        "model": "proxy",
                                        "label": "代理（可选）",
                                        "placeholder": "http://user:pass@host:port，留空直连",
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
                    # 立即执行一次
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "onlyonce",
                                        "label": "立即执行一次",
                                        "placeholder": "开启并保存后立即执行一次（执行完自动关闭，用于验证配置）",
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
                                        "text": "登录优先使用账户邮箱+密码（密码不会过期）；未填密码时才用 Session Token，失效再走 Discord OAuth 换新。"
                                                "每天定时检查：距到期不足 24 小时才执行续期，先用 Playwright 无头 Chromium 模拟点击「Renew free plan」，"
                                                "通过 Turnstile 后点「Renew for 4 days」；失败且开启备用方案时，通过 GitHub API 触发 Action，"
                                                "并轮询结果后推送企业微信，最后核对一次到期日。"
                                                "GitHub PAT 需 Actions 读写权限（细粒度：Actions Read and write；经典：repo + workflow）。"
                                                "GitHub API 走直连 api.github.com（可复用下方代理），MP 的 gh-proxy 只用于下载，不用于 API 触发。"
                                                "首次运行会自动下载 Chromium（约 150MB），保存在 /config/.playwright。"
                                                "容器必须能访问 bot-hosting.net、challenges.cloudflare.com 和 api.github.com。"
                                                "修改配置后可开启「立即执行一次」验证，结果按通知策略推送。",
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
            "password": "",
            "cron": "0 8 * * *",
            "notify": NOTIFY_ALWAYS,
            "renew_attempt": True,
            "proxy": "",
            "gh_fallback": False,
            "gh_token": "",
            "gh_repo": "Cxquang/Auto-Renew-Bothosting",
            "gh_workflow": "renew.yml",
            "gh_ref": "main",
            "onlyonce": False,
        }

    def get_page(self) -> List[dict]:
        """详情页：状态展示 + 立即执行按钮"""
        if not self.get_state():
            return [{
                "component": "VAlert",
                "props": {"type": "warning", "variant": "tonal",
                          "text": "插件未启用或未配置登录信息，请先在配置中填写账户邮箱+密码（推荐）或 session_token"}
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
