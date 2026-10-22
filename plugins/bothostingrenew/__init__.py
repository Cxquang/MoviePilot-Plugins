# -*- coding: utf-8 -*-
"""Bot-Hosting.net 自动续期插件

移植自 eooce/Auto-Renew-Bothosting（GitHub Actions + SeleniumBase 方案），
改为 MoviePilot 内运行的浏览器自动化实现：
1. session_token Cookie 访问账单页，解析订阅状态（到期/可续期时间）
2. 到可续期时间后，用 Playwright 启动 Chromium 模拟真人点击续期，
   在弹窗中处理 Cloudflare Turnstile 人机验证，点击「Renew for 4 days」
3. session_token 失效时，用 Discord Token 走 OAuth 流程自动换新（纯 HTTP）
4. 执行结果通过 MP 消息渠道推送，通知策略可选

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

# 浏览器参数
BROWSERS_PATH = "/config/.playwright"   # 持久化到 config 卷，重建容器不用重复下载
TURNSTILE_WAIT = 120                    # 等待 Turnstile token 上限（秒）
BROWSER_TOTAL_TIMEOUT = 330             # 整个浏览器流程上限（秒）

# 通知策略
NOTIFY_ALWAYS = "always"     # 每次执行都通知
NOTIFY_FAILURE = "failure"   # 仅失败/异常时通知
NOTIFY_OFF = "off"           # 关闭通知


class BotHostingRenew(_PluginBase):
    # 插件名称
    plugin_name = "Bot-Hosting自动续期"
    # 插件描述
    plugin_desc = "Bot-Hosting.net 免费套餐自动续期（4天一期），浏览器模拟点击 + Turnstile 验证，支持 Discord Token 自动换新登录"
    # 插件图标
    plugin_icon = "https://bot-hosting.net/favicon.ico"
    # 插件版本
    plugin_version = "1.2.0"
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
            "onlyonce": onlyonce,
        })

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
                "detail": f"续期方式：浏览器模拟点击（Playwright）\n新到期时间：{new_expiry}",
                "expiry": new_expiry,
                "login_method": login_method,
                "time": self.__now_str(),
            }
        else:
            err = renew_res.get("error") or "未知错误"
            return {
                "success": False,
                "title": "自动续期失败",
                "detail": f"失败原因：{err}\n"
                          "排查建议：\n"
                          "1. 确认容器可访问 bot-hosting.net 与 challenges.cloudflare.com（Turnstile 依赖后者）\n"
                          "2. 若服务器在墙内，请在插件配置中填写代理（http://user:pass@host:port）\n"
                          "3. 首次运行需联网下载 Chromium（约 150MB），失败可查看容器日志重试\n"
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
                # 去掉自动化特征
                context.add_init_script(
                    "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
                )
                context.add_cookies([
                    {"name": "session_token", "value": session_token, "url": BASE_URL, "path": "/"},
                    {"name": "login", "value": "true", "url": BASE_URL, "path": "/"},
                    {"name": "theme", "value": "system", "url": BASE_URL, "path": "/"},
                ])

                page = context.new_page()
                page.set_default_timeout(30000)
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
                            "diagnostics": snap}
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
            for sel in ('input[type="checkbox"]', "#checkbox"):
                try:
                    loc = frame.locator(sel)
                    if loc.count():
                        loc.first.click(timeout=3000)
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
                                        "text": "Session Token 获取：浏览器登录 bot-hosting.net → F12 开发者工具 → 应用/存储 → Cookie → 复制 session_token 的值。"
                                                "Discord Token 获取详见 eooce/Auto-Renew-Bothosting 项目说明；配置后 session 失效会自动走 OAuth 换新并回写。"
                                                "免费套餐每 4 天需续期一次；到可续期时间后，插件用 Playwright 无头 Chromium 打开账单页，"
                                                "模拟点击「Renew free plan」，通过 Turnstile 人机验证后点击「Renew for 4 days」。"
                                                "首次运行会自动下载 Chromium（约 150MB），保存在 /config/.playwright，之后不再重复下载。"
                                                "容器必须能访问 bot-hosting.net 与 challenges.cloudflare.com（Turnstile 依赖后者）；"
                                                "若服务器在国内或网络受限，请在下方填写代理。"
                                                "修改配置后可开启「立即执行一次」验证配置是否有效，结果按通知策略推送，也可在详情页查看。"
                                                "执行周期为 5 段 cron：分 时 日 月 周，如 0 8,20 * * * 表示每天 8 点和 20 点各执行一次。",
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
            "proxy": "",
            "onlyonce": False,
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
