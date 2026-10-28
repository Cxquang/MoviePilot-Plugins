# -*- coding: utf-8 -*-
"""Bot-Hosting.net 自动续期插件

移植自 eooce/Auto-Renew-Bothosting（GitHub Actions + SeleniumBase 方案），
改为 MoviePilot 内运行的浏览器自动化实现：
1. 登录优先 session_token（免登录页 Turnstile），失效/缺失时账号密码登录兜底（处理登录页 Turnstile），无密码时可走 Discord OAuth 换新
2. 每天定时检查账单页，距到期不足 24 小时才执行续期
3. 用 Playwright 启动 Chromium 模拟真人点击续期，在弹窗中处理 Cloudflare Turnstile
4. 浏览器失败时触发 GitHub Actions（workflow_dispatch）兜底，触发成功后插件立即结束，结果由 Action 仓库自行推送
5. 成功/失败结果通过 MP 消息渠道推送（可到企业微信）

参考与致谢：https://github.com/eooce/Auto-Renew-Bothosting
"""
import os
import re
import sys
import time
import random
import shutil
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
BROWSER_TOTAL_TIMEOUT = 420             # 整个浏览器流程上限（秒），含弹窗就绪等待
XVFB_DISPLAY = ":99"                    # 容器内有头模式使用的虚拟显示

# 反自动化检测脚本：Cloudflare Turnstile 会检查 webdriver/headless 指纹
STEALTH_JS = r"""
// === 完整隐身脚本（对齐 SeleniumBase UC 模式） ===
(function() {
    // 1. navigator.webdriver — Cloudflare 最常用的检测点
    //    不仅覆盖属性访问，还要拦截 Object.getOwnPropertyDescriptor
    try {
        delete navigator.webdriver;
    } catch(e) {}
    try {
        Object.defineProperty(navigator, 'webdriver', {
            get: () => undefined,
            configurable: true,
        });
    } catch(e) {}

    // 2. navigator.plugins — 真实浏览器有 PDF Viewer 等插件
    try {
        Object.defineProperty(navigator, 'plugins', {
            get: () => [1, 2, 3, 4, 5],
            configurable: true,
        });
    } catch(e) {}

    // 3. navigator.languages
    try {
        Object.defineProperty(navigator, 'languages', {
            get: () => ['en-US', 'en'],
            configurable: true,
        });
    } catch(e) {}

    // 4. navigator.platform — Playwright 默认是 Linux x86_64 或 Win32
    try {
        Object.defineProperty(navigator, 'platform', {
            get: () => 'Win32',
            configurable: true,
        });
    } catch(e) {}

    // 5. navigator.hardwareConcurrency
    try {
        Object.defineProperty(navigator, 'hardwareConcurrency', {
            get: () => 8,
            configurable: true,
        });
    } catch(e) {}

    // 6. navigator.deviceMemory
    try {
        Object.defineProperty(navigator, 'deviceMemory', {
            get: () => 8,
            configurable: true,
        });
    } catch(e) {}

    // 7. navigator.maxTouchPoints — 桌面浏览器是 0
    try {
        Object.defineProperty(navigator, 'maxTouchPoints', {
            get: () => 0,
            configurable: true,
        });
    } catch(e) {}

    // 8. navigator.connection — 伪造网络信息
    try {
        Object.defineProperty(navigator, 'connection', {
            get: () => ({
                effectiveType: '4g',
                rtt: 50,
                downlink: 10,
                saveData: false,
            }),
            configurable: true,
        });
    } catch(e) {}

    // 9. window.chrome — Chrome 特性（非 Chrome 浏览器没有）
    //    不仅要有 chrome 对象，还要让 chrome.runtime 支持消息通信
    try {
        if (!window.chrome) {
            window.chrome = {
                runtime: {
                    connect: () => ({ onMessage: { addListener() {} }, disconnect() {} }),
                    sendMessage: () => {},
                    id: undefined,
                },
                loadTimes: () => ({}),
                csi: () => ({}),
                app: { isInstalled: false, InstallState: { DISABLED: 'disabled', INSTALLED: 'installed' } },
            };
        }
    } catch(e) {}

    // 10. window.outerWidth/outerHeight — 模拟真实窗口边框
    try {
        Object.defineProperty(window, 'outerWidth', { get: () => 1382 });
        Object.defineProperty(window, 'outerHeight', { get: () => 800 });
    } catch(e) {}

    // 11. navigator.permissions.query — 完整拦截
    try {
        const origQuery = window.navigator.permissions.query.bind(window.navigator.permissions);
        window.navigator.permissions.query = (parameters) => {
            if (parameters.name === 'notifications') {
                return Promise.resolve({ state: Notification.permission });
            }
            if (parameters.name === 'geolocation') {
                return Promise.resolve({ state: 'prompt' });
            }
            if (parameters.name === 'midi' || parameters.name === 'sysex') {
                return Promise.resolve({ state: 'denied' });
            }
            return origQuery(parameters);
        };
    } catch(e) {}

    // 12. Playwright 自带的 __playwright_evaluation_script__ 会暴露
    try {
        delete window.__playwright_evaluation_script__;
    } catch(e) {}

    // 13. Document.prototype.$cdp — 有些自动化框架会注入
    try {
        if (window.document && !window.document.$cdp) {
            Object.defineProperty(window.document, '$cdp', {
                get: () => undefined,
                configurable: true,
            });
        }
    } catch(e) {}
})();
"""

# 定位 Turnstile 实际可点击坐标（移植自 eooce/Auto-Renew-Bothosting 的 TS_COORDS_JS）
# 返回值 {cx, cy} 为视口坐标，可直接用于 page.mouse.click 或换算为屏幕坐标后 xdotool 点击
TURNSTILE_COORDS_JS = """
() => {
    function findWidget(root) {
        var els = root.querySelectorAll(
            'iframe[src*="challenges.cloudflare.com"], .cf-turnstile, [id*="cf-chl"], [class*="turnstile"]');
        for (var i = 0; i < els.length; i++) {
            var r = els[i].getBoundingClientRect();
            if (r.width > 50 && r.height > 20) return els[i];
        }
        var all = root.querySelectorAll('*');
        for (var j = 0; j < all.length; j++) {
            if (all[j].shadowRoot) {
                var f = findWidget(all[j].shadowRoot);
                if (f) return f;
            }
        }
        return null;
    }
    function boxFromTokenInput() {
        var el = document.querySelector('input[name="cf-turnstile-response"]');
        var cur = el ? el.parentElement : null;
        for (var k = 0; k < 8 && cur; k++) {
            var r = cur.getBoundingClientRect();
            if (r.width > 100 && r.width < 600 && r.height > 30 && r.height < 160) return cur;
            cur = cur.parentElement;
        }
        return null;
    }
    var hit = findWidget(document) || boxFromTokenInput();
    if (!hit) return null;
    var hr = hit.getBoundingClientRect();
    return {cx: Math.round(hr.x + 30), cy: Math.round(hr.y + hr.height / 2)};
}
"""

# GitHub Actions 备用方案
GITHUB_API = "https://api.github.com"
RENEW_WINDOW_HOURS = 24                 # 到期前多久内允许续期

# 续期方式
RENEW_MODE_BOTH = "plugin_then_action"  # 先插件浏览器续期，失败再转 Action
RENEW_MODE_ACTION = "action_only"       # 直接触发 GitHub Action，不走插件浏览器
RENEW_MODE_PLUGIN = "plugin_only"       # 仅插件浏览器续期，失败不转 Action

# 通知策略
NOTIFY_ALWAYS = "always"     # 每次执行都通知
NOTIFY_FAILURE = "failure"   # 仅失败/异常时通知
NOTIFY_OFF = "off"           # 关闭通知


class BotHostingRenew(_PluginBase):
    # 插件名称
    plugin_name = "Bot-Hosting自动续期"
    # 插件描述
    plugin_desc = "Bot-Hosting.net 免费套餐自动续期（4天一期）：可选先插件浏览器续期、失败转 Action / 直接走 Action / 仅插件续期，token 优先、账号密码兜底登录；可选 SeleniumBase uc 模式绕过 Cloudflare"
    # 插件图标
    plugin_icon = "https://bot-hosting.net/favicon.ico"
    # 插件版本
    plugin_version = "1.3.17"
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
    _renew_mode = RENEW_MODE_BOTH
    _gh_token = ""
    _gh_repo = "Cxquang/Auto-Renew-Bothosting"
    _gh_workflow = "renew.yml"
    _gh_ref = "main"
    _onlyonce = False
    _use_seleniumbase = False
    # 运行状态
    _last_result = None
    _xvfb_active = False

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
            self._renew_mode = config.get("renew_mode") or RENEW_MODE_BOTH
            self._gh_token = (config.get("gh_token") or "").strip()
            self._gh_repo = (config.get("gh_repo") or "Cxquang/Auto-Renew-Bothosting").strip()
            self._gh_workflow = (config.get("gh_workflow") or "renew.yml").strip()
            self._gh_ref = (config.get("gh_ref") or "main").strip()
            self._onlyonce = config.get("onlyonce") or False
            self._use_seleniumbase = config.get("use_seleniumbase") or False
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
            "renew_mode": self._renew_mode,
            "gh_token": self._gh_token,
            "gh_repo": self._gh_repo,
            "gh_workflow": self._gh_workflow,
            "gh_ref": self._gh_ref,
            "onlyonce": onlyonce,
            "use_seleniumbase": self._use_seleniumbase,
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
        use_password = bool(self._email and self._password)
        if use_password and session_token:
            login_method = "Session Token → 账号密码"
        elif use_password:
            login_method = "账号密码"
        else:
            login_method = "Session Token"

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
                          "容器无法访问 bot-hosting.net，请检查网络连接（浏览器与站点接口为直连，不走代理）。",
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

        # 4. 直接走 GitHub Actions（跳过插件浏览器续期）
        if self._renew_mode == RENEW_MODE_ACTION:
            if not (self._gh_token and self._gh_repo and self._gh_workflow):
                return {
                    "success": False,
                    "title": "GitHub Actions 未配置",
                    "detail": "续期方式已设为「直接走 GitHub Actions」，但未填写 Token / 仓库 / 工作流文件名。",
                    "expiry": expiry,
                    "login_method": login_method,
                    "time": self.__now_str(),
                }
            logger.info("续期方式为直接走 GitHub Actions，尝试触发")
            ok, gh_err = self.__dispatch_github_action()
            if ok:
                # 只触发不通知：结果统一由 Action 仓库推送，避免插件与 Action 各发一条
                return {
                    "success": True,
                    "silent": True,
                    "title": "已触发 GitHub Actions 续期",
                    "detail": "续期方式：直接走 GitHub Actions，结果由 Action 仓库推送通知。",
                    "expiry": expiry,
                    "login_method": login_method,
                    "time": self.__now_str(),
                }
            return {
                "success": False,
                "title": "GitHub Actions 触发失败",
                "detail": f"续期方式为直接走 Action，但触发失败：{gh_err}",
                "expiry": expiry,
                "login_method": login_method,
                "time": self.__now_str(),
            }

        # 5. 浏览器模拟续期（账号密码优先，其次 session_token）
        renew_res = self.__do_renew(session_token)
        if renew_res.get("ok"):
            browser_name = "SeleniumBase uc 模式" if self._use_seleniumbase else "Playwright"
            msg = renew_res.get("detail") or ""
            new_expiry = renew_res.get("expiry") or old_expiry

            if renew_res.get("confirmed") is None:
                # Playwright 流程未自带确认结果，交给统一核对
                ok, verify_msg, verified_expiry = self.__verify_renewal(old_expiry)
                msg = verify_msg
                new_expiry = verified_expiry or new_expiry
                confirmed = ok
            else:
                confirmed = bool(renew_res.get("confirmed"))

            # 未确认时补一次快速 HTTP 核对（不启动浏览器，避免又等几分钟）
            if not confirmed and self._session_token:
                try:
                    http_exp = self.__query_status(self._session_token).get("expiry")
                except Exception:
                    http_exp = None
                if http_exp:
                    old_dt = self.__parse_time(old_expiry) if old_expiry else None
                    new_dt = self.__parse_time(http_exp)
                    if old_dt and new_dt and new_dt > old_dt:
                        confirmed = True
                        new_expiry = http_exp
                        msg += f"\nHTTP 核对：到期时间已延后至 {http_exp}"
                    elif not old_dt and new_dt:
                        confirmed = True
                        new_expiry = http_exp
                        msg += f"\nHTTP 核对：当前到期时间 {http_exp}"
                    else:
                        new_expiry = new_expiry or http_exp

            if confirmed:
                return {
                    "success": True,
                    "title": "续期成功 +4 天",
                    "detail": f"续期方式：浏览器模拟点击（{browser_name}，{login_method}）\n{msg}",
                    "expiry": new_expiry or expiry,
                    "login_method": login_method,
                    "time": self.__now_str(),
                }

            # 已点击确认但读不到结果：转 GitHub Actions 复核，最终结果由 Action 通知
            return self.__pending_to_action(msg, new_expiry or expiry, login_method, browser_name)

        err = renew_res.get("error") or "未知错误"
        # 浏览器已明确告知「按钮不可用/冷却中」，属于正常状态，不触发 Action
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

        # 6. 浏览器失败后转 GitHub Actions（仅「先插件后 Action」方式且有 Token）
        if self._renew_mode == RENEW_MODE_BOTH and self._gh_token:
            logger.info("浏览器续期失败，尝试触发 GitHub Actions（先插件后 Action 方式）")
            ok, gh_err = self.__dispatch_github_action()
            if ok:
                # 触发成功即结束：Action 仓库自身会推送续期结果通知，插件静默，
                # 保证用户只收到 Action 发出的那一条成功/失败结论
                return {
                    "success": True,
                    "silent": True,
                    "title": "已转 GitHub Actions 续期",
                    "detail": f"浏览器续期失败：{err}\n"
                              "已触发 GitHub Actions 执行续期，结果由 Action 仓库推送通知。",
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
                      "1. 确认容器可直连 bot-hosting.net 与 challenges.cloudflare.com（Turnstile 依赖后者，浏览器不走代理）\n"
                      "2. 容器内安装 xvfb 与 xdotool 后可启用有头模式点击，过验证更稳\n"
                      "3. 续期方式选择「先插件后续 Action」或「直接走 Action」并填写 Token，浏览器失败时由 Action 兜底（Action 调用走代理）\n"
                      "4. 仍失败时请手动登录续期一次。",
            "expiry": expiry,
            "login_method": login_method,
            "time": self.__now_str(),
        }

    def __pending_to_action(self, detail: str, expiry: str, login_method: str,
                            browser_name: str) -> dict:
        """浏览器已点击续期但无法确认结果

        统一交给 GitHub Actions 跑一次脚本复核：插件只发一条「续期已提交」提示后静默结束，
        最终成功/失败由 Action 自己推送，保证用户只会收到一个最终结论。
        """
        if self._renew_mode == RENEW_MODE_BOTH and self._gh_token:
            logger.info("浏览器已提交续期但未确认到账，转 GitHub Actions 复核")
            ok, gh_err = self.__dispatch_github_action()
            if ok:
                self._notify_message(
                    "续期已提交，已转 GitHub Actions 复核",
                    f"{detail}\n"
                    f"已触发 {self._gh_repo} 的 {self._gh_workflow} 复核续期结果，插件到此结束。\n"
                    "最终成功/失败由 Action 推送通知。",
                    failure=False, expiry=expiry)
                return {
                    "success": True,
                    "silent": True,  # 已单独发出提示，避免重复推送
                    "title": "续期已提交，已转 GitHub Actions 复核",
                    "detail": "浏览器已点击续期确认，结果由 Action 推送通知。",
                    "expiry": expiry,
                    "login_method": login_method,
                    "time": self.__now_str(),
                }
            return {
                "success": False,
                "title": "续期未确认，GitHub Actions 触发失败",
                "detail": (f"{detail}\n"
                           f"尝试转 GitHub Actions 复核失败：{gh_err}\n"
                           "请打开账单页手动确认是否到账：https://bot-hosting.net/a/billings"),
                "expiry": expiry,
                "login_method": login_method,
                "time": self.__now_str(),
            }

        return {
            "success": False,
            "title": "自动续期失败（已提交但无法确认）",
            "detail": (f"续期方式：浏览器模拟点击（{browser_name}，{login_method}）\n"
                       f"{detail}\n"
                       "已点击确认按钮，但账单页未刷新出新的到期时间，无法确认到账。\n"
                       "请手动核对：https://bot-hosting.net/a/billings\n"
                       "提示：把续期方式改为「先插件后续 Action」可由 Action 自动复核并推送最终结果。"),
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
        # 回退：页面文本日期（多格式），仍失败则输出诊断片段
        if not result["expiry"]:
            result["expiry"] = self.__parse_expiry_fallback(html)
            if not result["expiry"]:
                logger.warn("[BotHosting] 账单页未解析到到期时间，输出诊断片段：")
                self.__log_expiry_diag(html)
        logger.info(f"Bot-Hosting 状态: tier={result['tier']} status={result['status']} "
                    f"到期={result['expiry']} 可续期={result['opens_at']}")
        return result

    @staticmethod
    def __extract_json_str(html: str, field: str) -> Optional[str]:
        """从 SSR HTML 的序列化数据中提取字符串/数字字段值

        页面序列化的键名可能不带引号（freeRenewalDueAt:"2026-10-28T00:07:50.507Z"），
        也可能带引号（"freeRenewalDueAt":"..."），这里兼容两种写法。
        """
        f = re.escape(field)
        m = re.search(rf'(?<![\w"\'])(["\']?){f}\1\s*:\s*["\']([^"\']+)["\']', html)
        if m:
            return m.group(2)
        # 时间戳形式（毫秒/秒）→ UTC+8 的 YYYY/MM/DD
        m = re.search(rf'(?<![\w"\'])(["\']?){f}\1\s*:\s*(\d{{10,13}})', html)
        if m:
            ts = int(m.group(2))
            if ts > 10_000_000_000:
                ts = int(ts / 1000)
            try:
                return time.strftime("%Y/%m/%d", time.gmtime(ts + 8 * 3600))
            except Exception:
                return None
        return None

    @staticmethod
    def __parse_expiry_fallback(html: str) -> Optional[str]:
        """JSON 字段缺失时，从页面文本回退解析到期时间（多种日期格式）"""
        m = re.search(r"Expires?\s*[:\-]?\s*(\d{4}[/-]\d{1,2}[/-]\d{1,2})", html, re.I)
        if m:
            return m.group(1).replace("-", "/")
        # 美式 MM/DD/YYYY（页面文本：Expires 10/28/2026）
        m = re.search(r"Expires?\s*[:\-]?\s*(\d{1,2}/\d{1,2}/\d{4})", html, re.I)
        if m:
            try:
                return datetime.strptime(m.group(1), "%m/%d/%Y").strftime("%Y/%m/%d")
            except ValueError:
                return None
        m = re.search(
            r"Expires?\s*[:\-]?\s*(\d{1,2}\s+(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{4})",
            html, re.I)
        if m:
            try:
                return time.strftime("%Y/%m/%d", time.strptime(m.group(1), "%d %b %Y"))
            except ValueError:
                return None
        return None

    @staticmethod
    def __log_expiry_diag(html: str):
        """到期时间解析失败时，记录页面候选片段用于定位结构变化"""
        try:
            for kw in ("freeRenewalDueAt", "freeRenewalOpensAt", "Expires", "Renew", "renew"):
                for m in list(re.finditer(re.escape(kw), html))[:3]:
                    s = max(0, m.start() - 100)
                    seg = html[s:m.end() + 160].replace("\n", " ").replace("\r", " ")
                    logger.info(f"[BotHosting][诊断] 关键词[{kw}]: ...{seg}...")
                    break
        except Exception:
            pass

    def __do_renew(self, session_token: str) -> dict:
        """浏览器续期入口

        MoviePilot 的 /run 接口运行在 asyncio 事件循环线程里，而浏览器
        同步 API 禁止在事件循环线程中使用，因此单独开线程执行再等结果。
        若开启 SeleniumBase 且已安装，优先使用其对 Cloudflare 更强的绕过能力。
        """
        outcome: Dict[str, Any] = {}

        def _worker():
            try:
                if self._use_seleniumbase:
                    sb_res = self.__sb_renew(session_token)
                    if sb_res.get("fallback"):
                        logger.info(f"[BotHosting] SeleniumBase 不可用（{sb_res.get('error')}），回退到 Playwright")
                    else:
                        outcome.update(sb_res)
                        return
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

    # ---------------------- SeleniumBase 模式（对齐 eooce 参考实现） ----------------------

    def __sb_renew(self, session_token: str) -> dict:
        """使用 SeleniumBase (uc=True) 执行续期，对 Cloudflare 绕过能力更强。

        需要容器内提前安装：pip install seleniumbase，并执行 seleniumbase install chromedriver
        未安装时返回特定错误，外层自动回退到 Playwright。
        """
        try:
            from seleniumbase import SB
        except ImportError:
            return {"ok": False, "fallback": True,
                    "error": "未安装 seleniumbase（pip install seleniumbase）"}

        logs: List[str] = []

        def log(msg: str):
            logger.info(f"[BotHosting][SB] {msg}")
            logs.append(msg)

        if not (self._email and self._password):
            # 只有 session_token 时无法走账密登录，交给 Playwright 注入 Cookie
            return {"ok": False, "fallback": True,
                    "error": "SeleniumBase 模式需要邮箱+密码，当前未填写"}

        ts_token_js = """
        try { if (window.turnstile && window.turnstile.getResponse) {
                var t = window.turnstile.getResponse(); if (t) return t; } } catch (e) {}
        var el = document.querySelector('input[name="cf-turnstile-response"]');
        return el ? (el.value || '') : '';
        """
        ts_exists_js = """
        return document.querySelector(
            'input[name="cf-turnstile-response"], iframe[src*="challenges.cloudflare.com"], .cf-turnstile'
        ) !== null;
        """
        ts_coords_js = """
        function findWidget(root) {
            var els = root.querySelectorAll('iframe[src*="challenges.cloudflare.com"], .cf-turnstile, [id*="cf-chl"], [class*="turnstile"]');
            for (var i = 0; i < els.length; i++) {
                var r = els[i].getBoundingClientRect();
                if (r.width > 50 && r.height > 20) return els[i];
            }
            var all = root.querySelectorAll('*');
            for (var j = 0; j < all.length; j++) {
                if (all[j].shadowRoot) {
                    var f = findWidget(all[j].shadowRoot);
                    if (f) return f;
                }
            }
            return null;
        }
        function boxFromTokenInput() {
            var el = document.querySelector('input[name="cf-turnstile-response"]');
            var cur = el ? el.parentElement : null;
            for (var k = 0; k < 8 && cur; k++) {
                var r = cur.getBoundingClientRect();
                if (r.width > 100 && r.width < 600 && r.height > 30 && r.height < 160) return cur;
                cur = cur.parentElement;
            }
            return null;
        }
        var hit = findWidget(document) || boxFromTokenInput();
        if (!hit) return null;
        var hr = hit.getBoundingClientRect();
        return {cx: Math.round(hr.x + 30), cy: Math.round(hr.y + hr.height / 2)};
        """

        def sb_js(sb, script, default=None):
            try:
                r = sb.execute_script(script)
                return default if r is None else r
            except Exception:
                return default

        def sb_token(sb):
            r = sb_js(sb, ts_token_js, "")
            return r if isinstance(r, str) else ""

        def sb_click_turnstile(sb):
            # 1) xdotool 底层点击（需要对 Xvfb 上的窗口发真实 X11 事件）
            coords = sb_js(sb, ts_coords_js, None)
            if coords and shutil.which("xdotool"):
                try:
                    cx, cy = int(coords["cx"]), int(coords["cy"])
                    win = sb_js(
                        sb,
                        "return '' + (window.screenX || 0) + '|' + (window.screenY || 0)"
                        " + '|' + window.outerHeight + '|' + window.innerHeight;",
                        "",
                    )
                    parts = str(win).split("|")
                    sx = int(parts[0] or 0) if len(parts) > 0 else 0
                    sy = int(parts[1] or 0) if len(parts) > 1 else 0
                    oh = int(parts[2] or 900) if len(parts) > 2 else 900
                    ih = int(parts[3] or 900) if len(parts) > 3 else 900
                    ax, ay = cx + sx, cy + sy + (oh - ih)
                    env = {**os.environ, "DISPLAY": XVFB_DISPLAY}
                    for _cls in ("chrome", "chromium", "Chromium", "Chrome", "google-chrome"):
                        try:
                            r = subprocess.run(
                                ["xdotool", "search", "--onlyvisible", "--class", _cls],
                                env=env, capture_output=True, text=True, timeout=3)
                            wids = [w for w in (r.stdout or "").split() if w.strip()]
                            if wids:
                                subprocess.run(["xdotool", "windowactivate", "--sync", wids[0]],
                                               env=env, capture_output=True, timeout=3)
                                time.sleep(0.2)
                                break
                        except Exception:
                            continue
                    subprocess.run(["xdotool", "mousemove", "--sync", str(ax), str(ay)],
                                   env=env, capture_output=True, timeout=3)
                    time.sleep(0.15)
                    subprocess.run(["xdotool", "click", "1"], env=env, capture_output=True, timeout=3)
                    log(f"底层点击 Turnstile ({ax}, {ay})")
                    return True
                except Exception as e:
                    log(f"底层点击失败: {e}")
            # 2) SeleniumBase 自带 CAPTCHA 点击兜底
            try:
                sb.uc_gui_click_captcha()
                log("调用 sb.uc_gui_click_captcha 兜底")
                return True
            except Exception:
                pass
            try:
                if hasattr(sb, "cdp") and hasattr(sb.cdp, "click_captcha"):
                    sb.cdp.click_captcha()
                    log("调用 sb.cdp.click_captcha 兜底")
                    return True
            except Exception as e:
                log(f"click_captcha 失败: {e}")
            return False

        def sb_wait_turnstile(sb, timeout: int = 60, first_wait: int = 8) -> bool:
            deadline = time.time() + timeout
            next_click = time.time() + first_wait
            clicks = 0
            while time.time() < deadline:
                if sb_token(sb):
                    return True
                if time.time() >= next_click:
                    clicks += 1
                    log(f"第 {clicks} 次点击 Turnstile...")
                    sb_click_turnstile(sb)
                    next_click = time.time() + 10 + random.uniform(0, 3)
                time.sleep(2)
            return bool(sb_token(sb))

        def _norm_expiry(d: str) -> str:
            """把 2026/10/29 或 10/29/2026 统一成 2026/10/29"""
            parts = d.split("/")
            if len(parts) == 3 and len(parts[2]) == 4:
                return f"{parts[2]}/{int(parts[0]):02d}/{int(parts[1]):02d}"
            return d

        def sb_extract_expiry(text: str):
            m = re.search(r"[Ee]xpires\s*[:\-]?\s*(\d{4}/\d{1,2}/\d{1,2}|\d{1,2}/\d{1,2}/\d{4})", text)
            if m:
                return _norm_expiry(m.group(1))
            m = re.search(r"(?:freeRenewalDueAt|expiry|expires)\D{0,20}(\d{4}-\d{2}-\d{2}T[\d:.]+Z)", text, re.I)
            if m:
                return m.group(1)
            return None

        def sb_save_session(sb):
            """把登录后拿到的 session_token 写回配置，后续 HTTP 查询可直接复用"""
            try:
                for c in sb.get_cookies():
                    if (c.get("name") or "") == "session_token" and c.get("value"):
                        if c["value"] != self._session_token:
                            self._session_token = c["value"]
                            self.__save_config()
                            log("session_token 已自动更新（后续 HTTP 查询可直接使用）")
                        return
            except Exception as e:
                logger.debug(f"[BotHosting][SB] 读取 cookie 失败: {e}")

        def sb_confirm_renew(sb, old_expiry_value: Optional[str]) -> tuple:
            """点击确认后反复刷新账单页，确认到期时间延后或出现续期冷却倒计时"""
            deadline = time.time() + 50
            last_expiry = None
            last_cooldown = ""
            cd_pattern = re.compile(
                r"(?:Renew in|Renewal cooldown|Available in|Next renew in)\s*([0-9][0-9dhms:\s]{0,24})",
                re.I)
            while time.time() < deadline:
                sb.sleep(4)
                try:
                    sb.refresh()
                    sb.wait_for_ready_state_complete()
                    sb.sleep(2)
                except Exception:
                    pass
                try:
                    text = sb.get_page_source() or ""
                except Exception:
                    text = ""
                if not text:
                    continue
                parsed = sb_extract_expiry(text)
                if parsed:
                    last_expiry = parsed
                cd = cd_pattern.search(text)
                if cd:
                    last_cooldown = cd.group(1).strip()
                # 冷却倒计时出现 = 续期已生效（账单页会显示下次可续期时间）
                if last_cooldown:
                    return True, last_expiry, last_cooldown
                old_dt = self.__parse_time(old_expiry_value)
                new_dt = self.__parse_time(last_expiry)
                if old_dt and new_dt and new_dt > old_dt:
                    return True, last_expiry, last_cooldown
                if old_expiry_value and last_expiry and last_expiry != old_expiry_value:
                    return True, last_expiry, last_cooldown
            return False, last_expiry, last_cooldown

        xvfb_proc = None
        try:
            env_note = ""
            if os.name != "nt" and shutil.which("Xvfb"):
                xvfb_proc, display_env = self.__start_xvfb(log)
                if display_env:
                    os.environ["DISPLAY"] = display_env["DISPLAY"]
                    self._xvfb_active = True
                    env_note = f"（Xvfb {XVFB_DISPLAY}）"
                else:
                    log("Xvfb 不可用，SeleniumBase 将走无头模式")

            headless = not self._xvfb_active
            log(f"启动 SeleniumBase uc 模式{env_note}（headless={headless}）...")
            with SB(uc=True, headless=headless, locale_code="en-US") as sb:
                log(f"访问 {BASE_URL}/login")
                sb.open(f"{BASE_URL}/login")
                sb.wait_for_ready_state_complete()
                sb.sleep(3)
                try:
                    sb.click('button[type="submit"]')
                except Exception:
                    pass
                sb.sleep(12)

                try:
                    if self._email:
                        sb.type('input[type="email"]', self._email)
                        sb.sleep(1)
                    if self._password:
                        sb.type('input[type="password"]', self._password)
                        sb.sleep(3)
                except Exception as e:
                    return {"ok": False, "error": f"SeleniumBase 填写登录表单失败: {e}", "logs": logs[-10:]}

                if sb_js(sb, ts_exists_js, False):
                    if not sb_wait_turnstile(sb):
                        log("登录页 Turnstile 未通过，仍尝试提交")
                sb.sleep(1)
                try:
                    sb.click('button[type="submit"]')
                except Exception:
                    pass
                log("提交登录")
                sb.sleep(6)
                sb.wait_for_ready_state_complete()

                cur_url = sb.get_current_url()
                log(f"登录后 URL: {cur_url}")
                if "/login" in cur_url:
                    return {"ok": False, "error": "SeleniumBase 登录后仍停留在登录页", "logs": logs[-10:]}

                sb.open(f"{BASE_URL}/a/billings")
                sb.wait_for_ready_state_complete()
                sb.sleep(3)

                try:
                    page_text = sb.get_page_source()
                except Exception:
                    page_text = ""
                current_expiry = sb_extract_expiry(page_text)
                if current_expiry:
                    log(f"当前到期日期: {current_expiry}")

                outer_selector = None
                countdown_text = None
                for selector in ('button:contains("Renew free plan")', 'button:contains("Renew")',
                                 'a:contains("Renew")', '[class*="renew"]', '[class*="Renew"]'):
                    try:
                        if sb.is_element_visible(selector):
                            text = sb.get_text(selector)
                            m = re.search(r"(?:Renew in|Renewal cooldown)\s+(\d{1,2}:\d{2}:\d{2})", text, re.I)
                            if m:
                                countdown_text = m.group(1)
                                log(f"续期冷却中: {text}")
                                break
                            elif "Renew" in text:
                                outer_selector = selector
                                log(f"续期按钮可用: {text}")
                                break
                    except Exception:
                        continue

                if countdown_text:
                    return {"ok": False, "expiry": current_expiry,
                            "error": f"无需续期（冷却中），倒计时: {countdown_text}", "logs": logs[-10:]}
                if not outer_selector:
                    return {"ok": False, "expiry": current_expiry,
                            "error": "未找到可用的续期按钮", "logs": logs[-10:]}

                sb.sleep(2)
                sb.click(outer_selector)
                log("点击外部续期按钮，等待弹窗...")
                sb.sleep(15)

                turnstile_passed = True
                if sb_js(sb, ts_exists_js, False):
                    turnstile_passed = sb_wait_turnstile(sb)
                    if not turnstile_passed:
                        sb_click_turnstile(sb)
                        sb.sleep(4)
                        turnstile_passed = sb_token(sb) != ""

                if not turnstile_passed:
                    return {"ok": False, "expiry": current_expiry,
                            "error": "弹窗 Turnstile 验证未通过（cf-turnstile-response 一直为空）",
                            "logs": logs[-10:]}

                sb.sleep(5)
                try:
                    sb.click('div[role="dialog"] button.btn-primary', timeout=8)
                    log("已点击弹窗确认按钮")
                except Exception:
                    try:
                        sb.click('#renew-dialog button.btn-primary', timeout=8)
                        log("已点击弹窗确认按钮（#renew-dialog）")
                    except Exception as e:
                        return {"ok": False, "expiry": current_expiry,
                                "error": f"点击弹窗确认按钮失败: {e}", "logs": logs[-10:]}

                sb.sleep(4)
                sb_save_session(sb)

                confirmed, final_expiry, cooldown = sb_confirm_renew(sb, current_expiry)
                if confirmed:
                    if final_expiry and current_expiry and final_expiry != current_expiry:
                        detail = f"到期时间：{current_expiry} → {final_expiry}"
                    else:
                        detail = f"当前到期时间：{final_expiry or current_expiry}"
                    if cooldown:
                        detail += f"\n续期冷却倒计时：{cooldown}"
                    log(f"续期已确认（{detail.replace(chr(10), '；')}）")
                    return {"ok": True, "confirmed": True,
                            "expiry": final_expiry or current_expiry,
                            "cooldown": cooldown, "detail": detail,
                            "logs": logs[-10:]}

                log("已点击确认按钮，但账单页刷新后仍未读到新的到期时间")
                return {"ok": True, "confirmed": False,
                        "expiry": final_expiry or current_expiry,
                        "cooldown": cooldown,
                        "detail": "已在浏览器中点击弹窗「续期」确认按钮并提交，但账单页刷新后仍未读到新的到期时间。",
                        "logs": logs[-10:]}

        except Exception as e:
            logger.error(f"[BotHosting][SB] 续期异常: {e}\n{traceback.format_exc()}")
            return {"ok": False, "error": f"SeleniumBase 续期异常: {e}", "logs": logs[-10:]}
        finally:
            self._xvfb_active = False
            self.__stop_xvfb(xvfb_proc)


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

        if self._proxy:
            log("浏览器直连访问 bot-hosting（家用网络干净时不走代理），代理仅用于 GitHub API")

        browser = None
        xvfb_proc = None
        try:
            with sync_playwright() as p:
                browser, xvfb_proc = self.__launch_chromium(p, "", log)

                # 动态 UA：从 browser.version 提取 Chromium 主版本号，
                # 拼接成真实 Chrome UA（避免 UA 和实际 Chromium 版本不匹配的指纹）
                chromium_ver = browser.version  # 形如 "128.0.0.0"
                ua = (
                    f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    f"AppleWebKit/537.36 (KHTML, like Gecko) "
                    f"Chrome/{chromium_ver} Safari/537.36"
                )
                log(f"Chromium 版本: {chromium_ver}, UA: Chrome/{chromium_ver}")

                context = browser.new_context(
                    user_agent=ua,
                    locale="en-US",
                    timezone_id="America/Los_Angeles",  # 美国时区，和 Cloudflare 常用测试节点一致
                    viewport={"width": 1366, "height": 768},  # inner size，outer 在 STEALTH_JS 里伪装成 1382x800
                    screen={"width": 1920, "height": 1080},
                    ignore_https_errors=True,
                    extra_http_headers={
                        "Accept-Language": "en-US,en;q=0.9",
                        "sec-ch-ua": f'"Not.A/Brand";v="8", "Chromium";v="{chromium_ver.split(".")[0]}", "Google Chrome";v="{chromium_ver.split(".")[0]}"',
                        "sec-ch-ua-mobile": "?0",
                        "sec-ch-ua-platform": '"Windows"',
                    },
                    permissions=["notifications"],
                    geolocation={"latitude": 34.0522, "longitude": -118.2437},  # LA
                )
                # 去掉自动化特征（Cloudflare 会检测 webdriver/headless 指纹）
                context.add_init_script(STEALTH_JS)
                page = context.new_page()
                page.set_default_timeout(30000)
                log(f"Turnstile 资源连通性: {self.__check_turnstile_net()}")

                # 登录策略：session_token 优先（免登录页 Turnstile），被重定向到
                # /login 或没有 token 时才走账号密码登录（登录页也带 Turnstile）
                if session_token:
                    log("注入 session_token Cookie...")
                    context.add_cookies([
                        {"name": "session_token", "value": session_token, "url": f"{BASE_URL}/"},
                        {"name": "login", "value": "true", "url": f"{BASE_URL}/"},
                        {"name": "theme", "value": "system", "url": f"{BASE_URL}/"},
                    ])
                if not self.__goto_billings(page, log):
                    if self._email and self._password:
                        log("token 失效或缺失，改用账号密码登录...")
                        ok_login = False
                        try:
                            ok_login = self.__login_with_password(page, log)
                        except Exception as e:
                            log(f"账号密码登录异常: {e}")
                        if ok_login:
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
                            if not self.__goto_billings(page, log):
                                return {"ok": False, "need_relogin": True,
                                        "error": "账号密码登录后仍无法进入账单页"}
                        else:
                            return {"ok": False, "need_relogin": True,
                                    "error": "账号密码登录失败（登录页 Turnstile 未通过或账号密码错误）",
                                    "logs": logs[-10:]}
                    else:
                        return {"ok": False, "need_relogin": True,
                                "error": "session_token 已失效，且未配置账号密码，无法登录",
                                "logs": logs[-10:]}

                renew_btn = self.__find_renew_button(page)
                if renew_btn is None:
                    return {"ok": False,
                            "error": "账单页未找到可用的续期按钮（可能尚未到可续期时间，或页面结构变化）"}
                try:
                    if not renew_btn.is_enabled():
                        cd = self.__read_button_cooldown(page)
                        msg = "续期按钮当前不可用（尚未到可续期时间/冷却中）"
                        if cd:
                            msg += f"，按钮提示：{cd}"
                            log(f"续期按钮冷却提示: {cd}")
                        return {"ok": False, "error": msg}
                except Exception:
                    pass
                log("找到续期按钮，点击打开弹窗...")
                renew_btn.click()

                try:
                    page.wait_for_selector(DIALOG_SELECTOR, state="visible", timeout=15000)
                except Exception:
                    return {"ok": False, "error": "点击续期后弹窗未出现",
                            "diagnostics": self.__turnstile_snapshot(page)}
                log("续期弹窗已出现，等待弹窗内容就绪（Turnstile 组件 / 按钮）...")

                def dialog_state() -> dict:
                    """弹窗内的 Turnstile 与确认按钮状态快照"""
                    try:
                        return page.evaluate("""() => {
                            const dlg = document.querySelector('#renew-dialog');
                            const btn = document.querySelector('#renew-dialog button.btn-primary');
                            const iframe = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
                            const cf = document.querySelector('.cf-turnstile, [class*="cf-turnstile"], [data-sitekey]');
                            const resp = document.querySelector('input[name="cf-turnstile-response"]');
                            return {
                                hasDialog: !!dlg,
                                hasIframe: !!iframe,
                                hasCfEl: !!cf,
                                hasRespInput: !!resp,
                                btnFound: !!btn,
                                btnDisabled: btn ? !!btn.disabled : null,
                                api: !!window.turnstile,
                                htmlLen: dlg ? dlg.outerHTML.length : 0,
                            };
                        }""")
                    except Exception as e:
                        return {"err": str(e)}

                # 关键（对齐 eooce）：弹窗打开后先 sleep(15) 再处理。Svelte 弹窗里的
                # Turnstile 是异步渲染的，弹出瞬间查 DOM 一定什么都没有；之前直接在
                # T+0 判定"无 Turnstile"就跳过，随后点到的永远是还 disabled 的按钮。
                # 现在改成轮询：组件出现 → 走验证；按钮已 enabled → 无需验证直接提交。
                confirm = page.locator(DIALOG_CONFIRM_SELECTOR).first
                need_turnstile = False
                button_ready = False
                state: Dict[str, Any] = {}
                deadline = time.time() + 35
                while time.time() < deadline:
                    state = dialog_state()
                    if state.get("hasIframe") or state.get("hasCfEl") or state.get("hasRespInput"):
                        need_turnstile = True
                        break
                    try:
                        if confirm.is_enabled():
                            button_ready = True
                            break
                    except Exception:
                        pass
                    page.wait_for_timeout(500)
                log(f"弹窗就绪诊断: {state}")

                token = ""
                if need_turnstile:
                    log("检测到 Turnstile 组件，开始处理验证...")
                    token = self.__wait_turnstile(page, log)
                    if not token:
                        snap = self.__turnstile_snapshot(page)
                        return {"ok": False,
                                "error": "Turnstile 验证未通过（cf-turnstile-response 一直为空）",
                                "diagnostics": snap,
                                "logs": logs[-10:]}
                    log(f"Turnstile 已通过（token 长度 {len(token)}）")
                elif button_ready:
                    log("弹窗内无 Turnstile 组件，按钮已可用，直接提交")
                else:
                    log("35 秒内未出现 Turnstile 且按钮仍 disabled，尝试兜底点击")

                # eooce 在处理完 Turnstile 后会先 sleep(5)，让 SPA 内部状态同步完
                # 再点确认按钮；过早点击会落在尚未 enable 的按钮上。
                page.wait_for_timeout(5000)
                try:
                    log(f"点击前按钮状态: enabled={confirm.is_enabled()}")
                except Exception:
                    pass

                api_ok = True
                api_msg = ""

                # 诊断：按钮 disabled 状态 + 可能原因（前面还有多少个 disabled 按钮、表单是否不完整）
                try:
                    btn_diag = page.evaluate("""(sel) => {
                        const btn = document.querySelector(sel);
                        if (!btn) return { found: false };
                        // 弹窗里所有 input/select 的填写情况
                        const inputs = Array.from(document.querySelectorAll('#renew-dialog input, #renew-dialog select'));
                        const empty = inputs.filter(i => !i.value && i.required);
                        return {
                            found: true,
                            disabled: btn.disabled,
                            innerText: (btn.innerText || btn.textContent || '').slice(0, 50),
                            title: btn.title || '',
                            requiredInputs: inputs.map(i => ({name: i.name || i.id, required: i.required, value: !!i.value, type: i.type || i.tagName})),
                            emptyRequired: empty.map(i => i.name || i.id),
                        };
                    }""", DIALOG_CONFIRM_SELECTOR)
                    log(f"续期按钮诊断: {btn_diag}")
                except Exception as e:
                    log(f"按钮诊断失败: {e}")

                # 按钮 click 兜底：Playwright 的 click() 会自动等待 visible + enabled
                # （等价 eooce 的 sb.click(..., timeout=8)）。只有正常点击失败（按钮一直
                # disabled）才进入兜底：先塞 cf-turnstile-response 试 enable，再 JS 强点。
                def click_confirm():
                    try:
                        confirm.click(timeout=8000)
                        log("按钮正常点击成功")
                        return "normal"
                    except Exception as e:
                        log(f"正常点击失败（{type(e).__name__}），开始 disabled 兜底")

                    # 兜底 1：弹窗里没 Turnstile 时，SPA 很可能在等 cf-turnstile-response
                    # 隐藏域有值才能 enable。手动塞一个占位值，看看 SPA 会不会自己 enable
                    try:
                        patched = page.evaluate("""() => {
                            // 1. 尝试找已有的隐藏域
                            let input = document.querySelector('input[name="cf-turnstile-response"]');
                            if (!input) {
                                // 2. 没有就造一个（有些 SPA 只放了 Turnstile sitekey，widget 被 Cloudflare 没出来）
                                input = document.createElement('input');
                                input.type = 'hidden';
                                input.name = 'cf-turnstile-response';
                                document.querySelector('#renew-dialog form')?.appendChild(input)
                                    || document.querySelector('#renew-dialog')?.appendChild(input);
                            }
                            if (!input) return {patched: false, reason: 'no-input'};
                            if (input.value) return {patched: false, reason: 'already-has-value', value: input.value.slice(0,10)};
                            // 塞一个占位值（真实 token 很长，随便给一个能让 SPA 过"非空"检查即可）
                            input.value = 'skipped_turnstile_patch_for_headless_automation';
                            input.dispatchEvent(new Event('input', {bubbles: true}));
                            input.dispatchEvent(new Event('change', {bubbles: true}));
                            return {patched: true, hasValue: !!input.value};
                        }""")
                        log(f"cf-turnstile-response 补丁: {patched}")
                    except Exception as e:
                        log(f"塞 cf-turnstile-response 异常: {e}")

                    # 补丁后再尝试正常点击（带短暂等待）
                    try:
                        confirm.click(timeout=3000)
                        log("补丁后按钮点击成功")
                        return "patched"
                    except Exception:
                        log("补丁后仍无法点击，改用 JS 强点（移除 disabled 再 click）")

                    # 兜底 2：JS 移除 disabled 属性 + 原生 DOM click()
                    # 只调一次 btn.click()：它会派发冒泡的 click 事件，不能再额外
                    # dispatchEvent，否则 SPA 监听器会被触发两次（重复提交续期）
                    try:
                        clicked = page.evaluate("""(sel) => {
                            const btn = document.querySelector(sel);
                            if (!btn) return {clicked: false, reason: 'not-found'};
                            let removed = false;
                            if (btn.disabled) {
                                btn.removeAttribute('disabled');
                                removed = true;
                            }
                            btn.click();
                            return {clicked: true, removed};
                        }""", DIALOG_CONFIRM_SELECTOR)
                        log(f"JS 强点结果: {clicked}")
                        return "js" if clicked.get("clicked") else None
                    except Exception as e:
                        log(f"JS 强点异常: {e}")
                        return None

                captured = False
                try:
                    with page.expect_response(
                            lambda r: "/api/billing/renew-free" in r.url
                                      and r.request.method == "POST",
                            timeout=60000) as resp_info:
                        ok = click_confirm()
                        if not ok:
                            raise Exception("续期按钮点击失败（三种方式都没成功）")
                        log(f"点击方式: {ok}")
                    resp = resp_info.value
                    captured = True
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
                if not captured:
                    # 没抓到接口响应 = SPA 的点击处理器大概率直接 return（按钮一直 disabled）
                    return {"ok": False,
                            "error": "点击续期按钮后未捕获到 /api/billing/renew-free 请求"
                                     "（按钮长期 disabled，请求没有真正发出）",
                            "diagnostics": self.__turnstile_snapshot(page),
                            "logs": logs[-10:]}
                return {"ok": True, "expiry": new_expiry, "logs": logs[-10:]}
        except Exception as e:
            logger.error(f"[BotHosting] 浏览器流程失败: {e}\n{traceback.format_exc()}")
            return {"ok": False, "error": f"浏览器流程失败: {e}", "logs": logs[-10:]}
        finally:
            self._xvfb_active = False
            if browser:
                try:
                    browser.close()
                except Exception:
                    pass
            if xvfb_proc:
                try:
                    xvfb_proc.kill()
                except Exception:
                    pass

    # ---------------------- 浏览器启动 / 登录辅助 ----------------------

    def __launch_chromium(self, p, proxy: str, log) -> tuple:
        """启动 Chromium：优先有头模式（Xvfb 虚拟显示，过 Cloudflare 更稳），
        无 Xvfb 或启动失败时回退无头。返回 (browser, xvfb_proc)

        启动参数对齐 SeleniumBase UC 模式的反检测配置：
        - --disable-blink-features=AutomationControlled  隐藏 webdriver 标记
        - --exclude-switches=enable-automation           移除 "Chrome is being controlled" 提示
        - --useAutomationExtension=false                 禁用自动化扩展
        - --disable-infobars                             禁用信息栏
        """
        args = [
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--exclude-switches=enable-automation",
            "--useAutomationExtension=false",
            "--disable-infobars",
            "--disable-gpu",
            "--no-first-run",
            "--no-default-browser-check",
            "--lang=en-US",
        ]
        launch_kwargs: Dict[str, Any] = {"args": args}
        if proxy:
            launch_kwargs["proxy"] = {"server": proxy}

        xvfb_proc = None
        if os.name == "nt":
            # Windows（本地调试）：没有 Xvfb，直接用真实桌面有头模式，
            # 指纹和有头 Linux 基本一致，Turnstile 才会正常出 token
            launch_kwargs["headless"] = False
            self._xvfb_active = False
            log("Windows 环境：使用有头模式（本机桌面）")
            return self.__launch_retry(p, launch_kwargs, log), None
        if shutil.which("Xvfb"):
            # 有头模式：启动虚拟显示，浏览器窗口渲染在 Xvfb 里，
            # 指纹更接近真实用户，Cloudflare 更愿意出 token
            xvfb_proc, display_env = self.__start_xvfb(log)
            if display_env:
                try:
                    launch_kwargs["env"] = display_env
                    launch_kwargs["headless"] = False
                    browser = self.__launch_retry(p, launch_kwargs, log)
                    self._xvfb_active = True
                    log(f"有头模式启动成功（Xvfb {XVFB_DISPLAY}，socket 已就绪）")
                    return browser, xvfb_proc
                except Exception as e:
                    log(f"有头模式启动失败，回退无头: {e}")
                    self.__stop_xvfb(xvfb_proc)
                    xvfb_proc = None

        self._xvfb_active = False
        launch_kwargs["headless"] = True
        browser = self.__launch_retry(p, launch_kwargs, log)
        log("无头模式启动成功（Cloudflare 可能拒发 token，请尽量保证有头模式）")
        return browser, None

    def __start_xvfb(self, log) -> tuple:
        """启动 Xvfb 虚拟显示，返回 (xvfb_proc, env)；失败返回 (None, None)"""
        xvfb_proc = None
        try:
            # 1. 清理残留的 Xvfb lock/socket 文件（上次 run 异常退出会留）
            lock = f"/tmp/.X{XVFB_DISPLAY[1:]}-lock"
            socket_dir = "/tmp/.X11-unix"
            socket_path = f"{socket_dir}/X{XVFB_DISPLAY[1:]}"
            for stale in (lock, socket_path):
                if os.path.exists(stale):
                    try:
                        os.remove(stale)
                        log(f"清理残留 Xvfb 文件: {stale}")
                    except Exception:
                        pass

            # 2. 启动 Xvfb（stderr 保留，便于诊断）
            xvfb_proc = subprocess.Popen(
                ["Xvfb", XVFB_DISPLAY, "-screen", "0", "1366x900x24",
                 "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            # 3. 等待并确认 Xvfb 真起来了（检查 socket 文件）
            display_ready = False
            for _ in range(15):
                time.sleep(0.3)
                if xvfb_proc.poll() is not None:
                    stderr_data = ""
                    try:
                        stderr_data = xvfb_proc.stderr.read().decode(errors="replace")
                    except Exception:
                        pass
                    log(f"Xvfb 启动后立即退出（exit={xvfb_proc.returncode}）: {stderr_data[:300]}")
                    xvfb_proc = None
                    break
                if os.path.exists(socket_path):
                    display_ready = True
                    break
            if not display_ready:
                log(f"Xvfb 启动超时（socket {socket_path} 未出现），回退无头")
                self.__stop_xvfb(xvfb_proc)
                return None, None
            return xvfb_proc, {**os.environ, "DISPLAY": XVFB_DISPLAY}
        except Exception as e:
            log(f"启动 Xvfb 失败: {e}")
            self.__stop_xvfb(xvfb_proc)
            return None, None

    @staticmethod
    def __stop_xvfb(xvfb_proc):
        if not xvfb_proc:
            return
        try:
            xvfb_proc.kill()
        except Exception:
            pass

    def __launch_retry(self, p, launch_kwargs: dict, log):
        """启动 Chromium，未安装时自动下载二进制"""
        try:
            return p.chromium.launch(**launch_kwargs)
        except Exception as e:
            msg = str(e)
            if "Executable doesn't exist" in msg or "playwright install" in msg:
                log("Chromium 未安装，开始下载（首次约 150MB，存放在 "
                    f"{os.environ.get('PLAYWRIGHT_BROWSERS_PATH')}）...")
                self.__install_chromium()
                return p.chromium.launch(**launch_kwargs)
            raise

    def __goto_billings(self, page, log) -> bool:
        """打开账单页并确认登录态；连接被重置时重试一次"""
        for attempt in (1, 2):
            try:
                log(f"打开账单页: {BILLINGS_URL}（第 {attempt} 次）")
                page.goto(BILLINGS_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(1500)
                log(f"当前地址: {page.url}")
            except Exception as e:
                log(f"打开账单页失败: {e}")
                if attempt == 1:
                    page.wait_for_timeout(4000)
                    continue
                return False
            if "/login" not in page.url:
                return True
            if attempt == 1:
                log("被重定向到登录页，稍候重试一次...")
                page.wait_for_timeout(3000)
        return False

    # ---------------------- 登录 / 结果校验 ----------------------

    def __login_with_password(self, page, log) -> bool:
        """账号密码登录（对齐 eooce 流程：Continue -> 填邮箱密码 -> Turnstile -> 提交）"""
        log("使用账号密码登录...")
        try:
            page.goto(f"{BASE_URL}/login", wait_until="domcontentloaded", timeout=60000)
        except Exception as e:
            log(f"登录页打开失败: {e}")
            return False
        page.wait_for_timeout(3000)
        # 点击 Continue with email
        try:
            page.locator('button[type="submit"]').first.click(timeout=10000)
        except Exception as e:
            log(f"未找到 Continue 按钮（可能已在邮箱表单）: {e}")
        # 等邮箱表单出现（最多 25 秒）
        try:
            page.wait_for_selector('input[type="email"]', timeout=25000)
        except Exception:
            log("登录页未出现邮箱输入框")
            return False
        log("填写邮箱/密码...")
        page.fill('input[type="email"]', self._email)
        page.wait_for_timeout(1000)
        page.fill('input[type="password"]', self._password)
        page.wait_for_timeout(4000)  # eooce 在填完密码后等待 3 秒再处理 Turnstile
        token = self.__wait_turnstile(page, log)
        if not token:
            log("登录页 Turnstile 未通过，仍尝试提交")
        page.locator('button[type="submit"]').first.click()
        page.wait_for_timeout(8000)
        cur = page.url or ""
        if "/login" in cur:
            log(f"登录后仍停留在登录页: {cur}")
            return False
        log(f"账号密码登录成功，当前地址: {cur}")
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
        # 核对只给 150 秒：浏览器核对失败不该拖住整轮任务（主流程最长 420 秒）
        t.join(timeout=min(BROWSER_TOTAL_TIMEOUT, 150))
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
                # UA 与 Chromium 真实版本对齐
                chromium_ver = browser.version
                ua = (
                    f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    f"AppleWebKit/537.36 (KHTML, like Gecko) "
                    f"Chrome/{chromium_ver} Safari/537.36"
                )
                context = browser.new_context(user_agent=ua, locale="en-US",
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
        url = f"{GITHUB_API}{path}"
        try:
            res = requests.request(
                method, url,
                headers={
                    "Authorization": f"Bearer {self._gh_token}",
                    "Accept": "application/vnd.github+json",
                    "X-GitHub-Api-Version": "2022-11-28",
                },
                json=payload, proxies=self.__req_proxies(), timeout=30,
            )
        except Exception as e:
            logger.error(f"[BotHosting] GitHub API 请求失败 {method} {path}: {e}")
            raise RuntimeError(f"无法连接 api.github.com：{e}") from e
        logger.info(f"[BotHosting] GitHub API {method} {path} -> HTTP {res.status_code}")
        if res.status_code >= 400:
            raise RuntimeError(f"HTTP {res.status_code}: {res.text[:200]}")
        return res.json() if res.text else {}

    def __dispatch_github_action(self) -> Tuple[bool, str]:
        """触发 workflow_dispatch，返回 (是否触发成功, 错误原因)"""
        if not (self._gh_token and self._gh_repo and self._gh_workflow):
            return False, "未配置 GitHub Token / 仓库 / 工作流文件名"

        # dispatch 偶发网络抖动，重试 2 次
        last_err = ""
        for attempt in (1, 2):
            try:
                self.__gh_api(
                    "POST",
                    f"/repos/{self._gh_repo}/actions/workflows/{self._gh_workflow}/dispatches",
                    {"ref": self._gh_ref or "main"})
                last_err = ""
                break
            except Exception as e:
                last_err = str(e)
                logger.error(f"[BotHosting] 触发 workflow_dispatch 失败（第 {attempt} 次）: {e}")
                if attempt == 1:
                    time.sleep(5)
        if last_err:
            return False, self.__map_gh_error(last_err)

        logger.info(f"[BotHosting] 已触发 GitHub Actions: {self._gh_repo}/{self._gh_workflow}")
        return True, ""

    @staticmethod
    def __map_gh_error(err: str) -> str:
        if "401" in err:
            return (f"GitHub 鉴权失败（{err}）：Token 无效或已过期，请重新生成 PAT，"
                    "细粒度需 Actions: Read and write")
        if "403" in err:
            return (f"GitHub 权限不足（{err}）：Token 缺 Actions 写权限，"
                    "或未授权该仓库（细粒度 Token 需选 Cxquang/Auto-Renew-Bothosting）")
        if "404" in err:
            return (f"GitHub 找不到工作流（{err}）：请核对仓库名和工作流文件名"
                    f"（当前 {self._gh_repo}/{self._gh_workflow}）")
        if "无法连接" in err:
            return (f"容器无法访问 api.github.com（{err}）：请检查代理配置，"
                    "容器内执行 curl -I https://api.github.com 验证")
        return f"GitHub API 调用失败：{err}"

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
        """轮询 cf-turnstile-response；为空时按近似真人的节奏点击复选框

        Cloudflare Turnstile 在 SPA 弹窗里经常不自动初始化——即使 api.js 已经在
        账单页加载过，续期弹窗的 widget 仍可能因反检测或时序问题不渲染。这里
        主动兜底：手动注入 api.js、手动 render widget，确保组件出来再点击。
        """
        # 1. 先确保 api.js 加载（window.turnstile 存在）
        def ensure_api():
            """如果 window.turnstile 不存在，手动注入 api.js script 标签并等加载"""
            try:
                exists = page.evaluate("() => !!window.turnstile")
                if exists:
                    log("window.turnstile 已可用")
                    return True
            except Exception:
                pass
            log("window.turnstile 未加载，手动注入 api.js...")
            inject_js = """(src) => new Promise(resolve => {
                if (window.turnstile) { resolve(true); return; }
                const s = document.createElement('script');
                s.src = src; s.async = true; s.defer = true;
                s.onload = () => {
                    let t = 0;
                    const tick = () => {
                        t += 100;
                        if (window.turnstile) resolve(true);
                        else if (t < 8000) setTimeout(tick, 100);
                        else resolve(false);
                    };
                    tick();
                };
                s.onerror = () => resolve(false);
                document.head.appendChild(s);
            })"""
            try:
                ok = page.evaluate(inject_js, TURNSTILE_API)
                log(f"api.js 手动注入结果: {ok}")
                return bool(ok)
            except Exception as e:
                log(f"api.js 手动注入异常: {e}")
                return False

        def ensure_widget():
            """如果弹窗里没出现 Turnstile widget（.cf-turnstile / data-sitekey），
            就手动 render。有些 SPA 虽然放了 data-sitekey 元素，但不主动调
            turnstile.render()，需要我们帮它调。"""
            try:
                widget = page.evaluate("""() => {
                    // 先找已存在的 widget iframe
                    const hasFrame = !!document.querySelector('iframe[src*="challenges.cloudflare.com"]');
                    if (hasFrame) return 'iframe';
                    // 找带 data-sitekey 的容器（SPA 可能只放了这个但没调 render）
                    const sk = document.querySelector('[data-sitekey]');
                    if (sk) return 'sitekey:' + (sk.getAttribute('data-sitekey') || '')[:20];
                    // 找 Turnstile 相关容器（有些 Svelte 组件会带这个 class）
                    const cf = document.querySelector('.cf-turnstile, [class*="cf-turnstile"], [id*="cf-turnstile"]');
                    if (cf) return 'cf-el';
                    return '';
                }""")
                if widget:
                    log(f"检测到 Turnstile 容器: {widget}")
            except Exception:
                widget = ""

            if widget and widget.startswith("sitekey:") and page.evaluate("() => !!window.turnstile"):
                log("发现 data-sitekey 但 widget 未渲染，手动调用 turnstile.render()...")
                try:
                    page.evaluate("""() => {
                        const sk = document.querySelector('[data-sitekey]');
                        if (sk && window.turnstile && window.turnstile.render) {
                            try { window.turnstile.render(sk); return true; }
                            catch (e) { try { window.turnstile.render('#' + sk.id); return true; } catch(e2) { return false; } }
                        }
                        return false;
                    }""")
                    page.wait_for_timeout(2000)
                except Exception as e:
                    log(f"手动 render 异常: {e}")

        ensure_api()
        ensure_widget()

        # 2. 新版权 Turnstile 常渲染在 closed shadow DOM 里，主文档不一定能查到 iframe；
        #    这里以 token 为准轮询（最长 30 秒），一旦出 token 立即返回，避免白等。
        widget_seen = False
        ready_deadline = time.time() + 30
        while time.time() < ready_deadline:
            token = self.__read_turnstile_token(page)
            if token:
                log(f"Turnstile 已通过（token 长度 {len(token)}）")
                return token
            if not widget_seen:
                try:
                    widget_seen = bool(page.evaluate("""() => !!(document.querySelector('input[name="cf-turnstile-response"]')
                        || document.querySelector('iframe[src*="challenges.cloudflare.com"]')
                        || document.querySelector('.cf-turnstile'))"""))
                except Exception:
                    pass
                if widget_seen:
                    log("Turnstile 组件已出现，等待 token...")
            page.wait_for_timeout(1000)
        log("30 秒内未拿到 Turnstile token（挑战可能被网络阻断或需要人工交互）")

        try:
            log(f"window.turnstile 是否可用: {page.evaluate('() => !!window.turnstile')}")
        except Exception:
            pass

        deadline = time.time() + TURNSTILE_WAIT
        attempt = 0
        next_click = time.time() + 6  # 先给组件几秒渲染时间
        while time.time() < deadline:
            token = self.__read_turnstile_token(page)
            if token:
                return token
            if attempt < 10 and time.time() >= next_click:
                attempt += 1
                coords = self.__get_turnstile_coords(page)
                if coords:
                    log(f"第 {attempt} 次点击 Turnstile（坐标 {coords['cx']},{coords['cy']}，token 仍为空）")
                    self.__click_turnstile(page)
                else:
                    log(f"第 {attempt} 次未定位到 Turnstile 可点击对象")
                # eooce 的节奏：每 10~13 秒重试一次，带随机抖动更像真人
                next_click = time.time() + random.uniform(8, 13)
            page.wait_for_timeout(1500)
        return ""

    def __check_turnstile_net(self) -> str:
        """从容器直接探测 Turnstile 脚本是否可达（直连，与浏览器一致），便于定位网络问题"""
        try:
            r = requests.get(TURNSTILE_API,
                             headers={"User-Agent": USER_AGENT},
                             timeout=15)
            return f"HTTP {r.status_code}, {len(r.content)} bytes"
        except Exception as e:
            return f"失败: {e}"

    @staticmethod
    def __read_turnstile_token(page) -> str:
        # eooce 同款：优先官方 JS 接口 getResponse()（显式渲染时 input 不一定及时写入）
        js = """() => {
            try {
                if (window.turnstile && window.turnstile.getResponse) {
                    const t = window.turnstile.getResponse();
                    if (t) return t;
                }
            } catch (e) {}
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
    def __get_turnstile_coords(page) -> Optional[Dict[str, int]]:
        """使用 eooce 同款 JS 定位 Turnstile 实际可点击坐标（含 shadow DOM 与 token-input 父容器兜底）"""
        try:
            coords = page.evaluate(TURNSTILE_COORDS_JS)
            if coords and coords.get("cx") and coords.get("cy"):
                return coords
        except Exception:
            pass
        return None

    def __click_turnstile(self, page) -> bool:
        """点击 Turnstile 复选框。

        Cloudflare 会拒绝纯程序化点击（CDP 注入的鼠标事件特征明显），eooce
        方案能过的关键是用 xdotool 发真实 X11 鼠标事件。有头模式下优先走
        xdotool 底层点击；无坐标时依次尝试 iframe checkbox、iframe 坐标兜底。
        """
        coords = self.__get_turnstile_coords(page)

        # 1. 有头模式：xdotool 真实鼠标点击（和 eooce 的「底层点击」一致）
        if self._xvfb_active and shutil.which("xdotool"):
            try:
                if not coords:
                    raise RuntimeError("未获取到 Turnstile 坐标")
                win = page.evaluate(
                    "() => ({sx: window.screenX || 0, sy: window.screenY || 0,"
                    " oh: window.outerHeight || 900, ih: window.innerHeight || 900})")
                bar = int(win.get("oh") or 900) - int(win.get("ih") or 900)
                ax = int(coords["cx"]) + int(win.get("sx") or 0)
                ay = int(coords["cy"]) + int(win.get("sy") or 0) + bar
                env = {**os.environ, "DISPLAY": XVFB_DISPLAY}
                # 先把 Chromium 窗口提为活动窗口（eooce 同款），
                # 否则 xdotool 的点击可能落在别的窗口上
                for _cls in ("chrom", "chrome", "google-chrome", "Chromium", "chromium"):
                    try:
                        r = subprocess.run(
                            ["xdotool", "search", "--onlyvisible", "--class", _cls],
                            env=env, capture_output=True, text=True, timeout=5)
                        wids = [w for w in (r.stdout or "").split() if w.strip()]
                        if wids:
                            subprocess.run(
                                ["xdotool", "windowactivate", "--sync", wids[0]],
                                env=env, capture_output=True, timeout=5)
                            time.sleep(0.2)
                            break
                    except Exception:
                        continue
                subprocess.run(["xdotool", "mousemove", "--sync", str(ax), str(ay)],
                               env=env, timeout=5, capture_output=True)
                time.sleep(0.15)
                subprocess.run(["xdotool", "click", "1"],
                               env=env, timeout=5, capture_output=True)
                return True
            except Exception as e:
                logger.debug(f"[BotHosting] xdotool 点击 Turnstile 失败: {e}")

        # 2. 有坐标时：带随机轨迹的 Playwright 鼠标点击
        if coords:
            try:
                cx, cy = int(coords["cx"]), int(coords["cy"])
                for _ in range(2):
                    page.mouse.move(cx + random.uniform(-20, 20),
                                    cy + random.uniform(-10, 10),
                                    steps=random.randint(6, 12))
                    page.wait_for_timeout(random.randint(80, 200))
                page.mouse.move(cx, cy, steps=random.randint(8, 16))
                page.wait_for_timeout(random.randint(150, 350))
                page.mouse.click(cx, cy)
                return True
            except Exception as e:
                logger.debug(f"[BotHosting] Playwright 坐标点击 Turnstile 失败: {e}")

        # 3. iframe 内 checkbox 常规点击
        try:
            frame = page.frame_locator(TURNSTILE_FRAME_SELECTOR)
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

        # 4. iframe 边界兜底：用 iframe bounding box 中心做最后的坐标点击
        try:
            el = page.query_selector(TURNSTILE_FRAME_SELECTOR)
            box = el.bounding_box() if el else None
            if box and box.get("width"):
                tx = box["x"] + box["width"] * 0.15
                ty = box["y"] + box["height"] * 0.5
                page.mouse.move(tx, ty, steps=random.randint(10, 20))
                page.wait_for_timeout(random.randint(200, 500))
                page.mouse.click(tx, ty)
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
            const hit = (function(){
                function findWidget(root) {
                    var els = root.querySelectorAll('iframe[src*="challenges.cloudflare.com"], .cf-turnstile, [id*="cf-chl"], [class*="turnstile"]');
                    for (var i = 0; i < els.length; i++) {
                        var r = els[i].getBoundingClientRect();
                        if (r.width > 50 && r.height > 20) return els[i];
                    }
                    var all = root.querySelectorAll('*');
                    for (var j = 0; j < all.length; j++) {
                        if (all[j].shadowRoot) {
                            var f = findWidget(all[j].shadowRoot);
                            if (f) return f;
                        }
                    }
                    return null;
                }
                return findWidget(document);
            })();
            var rect = hit ? hit.getBoundingClientRect() : null;
            return {
                dialog: dlg ? (dlg.innerText || '').slice(0, 160) : '',
                hasInput: !!el,
                tokenLen: el ? (el.value || '').length : -1,
                confirmDisabled: btn ? !!btn.disabled : null,
                hasWidget: !!document.querySelector('.cf-turnstile, [data-sitekey], iframe[src*="challenges.cloudflare.com"]'),
                hasApi: !!window.turnstile,
                widgetRect: rect ? {x: rect.x, y: rect.y, w: rect.width, h: rect.height} : null,
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
        """从当前页面 HTML 中提取到期时间（SSR JSON → 多格式文本回退）"""
        try:
            html = page.content()
        except Exception:
            return None
        val = self.__extract_json_str(html, "freeRenewalDueAt")
        if val:
            return val
        return self.__parse_expiry_fallback(html)

    @staticmethod
    def __read_button_cooldown(page) -> Optional[str]:
        """读取续期按钮上的倒计时/剩余时间文字（按钮不可用时展示）"""
        try:
            txt = page.evaluate("""() => {
                const btns = [...document.querySelectorAll('button, a, [role="button"]')];
                const b = btns.find(x => /renew/i.test(x.textContent || ''));
                if (b && b.textContent && b.textContent.trim()) return b.textContent.trim();
                const m = document.body.innerText.match(/renew[^\\n]{0,100}/i);
                if (m) return m[0].replace(/\\s+/g, ' ').trim();
                return '';
            }""")
            if not txt:
                return None
            return txt[:100]
        except Exception:
            return None

    @staticmethod
    def __mask_proxy(proxy: str) -> str:
        """代理地址脱敏，避免日志泄露账号密码"""
        return re.sub(r"//([^:@/]+):([^@/]+)@", r"//\1:***@", proxy)

    def __req_proxies(self) -> Optional[dict]:
        """仅 GitHub API 备用方案使用代理；浏览器与站点接口一律直连（家用网络干净）"""
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
        """按策略推送通知；silent 结果（已单独发过通知）直接跳过"""
        if result.get("silent"):
            return
        self._notify_message(result.get("title") or "执行完成",
                             result.get("detail") or "",
                             failure=not result.get("success"),
                             expiry=result.get("expiry"))

    def _notify_message(self, title: str, text: str, failure: bool = False,
                        expiry: str = ""):
        """拼接通知文案并推送（MP 消息渠道，含企业微信），并按通知策略过滤"""
        if self._notify == NOTIFY_OFF:
            return
        if self._notify == NOTIFY_FAILURE and not failure:
            return
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
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VSwitch",
                                    "props": {
                                        "model": "use_seleniumbase",
                                        "label": "使用 SeleniumBase（需 pip install seleniumbase）",
                                        "placeholder": "uc 模式对 Cloudflare 绕过更强；未安装时自动回退 Playwright",
                                    },
                                }]
                            },
                        ]
                    },
                    # 续期方式
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [{
                                    "component": "VSelect",
                                    "props": {
                                        "model": "renew_mode",
                                        "label": "续期方式",
                                        "items": [
                                            {"title": "先插件续期，失败转 Action", "value": RENEW_MODE_BOTH},
                                            {"title": "直接走 GitHub Action", "value": RENEW_MODE_ACTION},
                                            {"title": "仅插件浏览器续期", "value": RENEW_MODE_PLUGIN},
                                        ],
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
                                        "label": "代理（可选，仅 GitHub API 用）",
                                        "placeholder": "http://user:pass@host:port，留空直连；浏览器模拟与站点接口始终直连不走代理",
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
                                        "text": "登录优先使用 Session Token（免登录页人机验证），token 失效/未填时才用账户邮箱+密码登录（登录页也过 Turnstile），无密码时可走 Discord OAuth 换新。"
                                        "每天定时检查：距到期不足 24 小时才执行续期，先用 Chromium 模拟点击「Renew free plan」（容器内有 Xvfb 时用有头模式 + xdotool 真实点击，"
                                        "需镜像支持 xvfb/xdotool，建议 apk add xvfb xdotool），通过 Turnstile 后点「Renew for 4 days」。"
                                        "续期方式可选：先插件续期、失败转 Action（默认）；直接走 GitHub Action（跳过插件浏览器）；仅插件浏览器续期（失败不转 Action）。"
                                        "转 Action 时通过 GitHub API 触发，触发成功后插件立即结束（不再轮询监听），Action 执行结果由 Action 仓库自行推送企业微信。"
                                        "浏览器与站点接口一律直连（家用网络干净，代理节点反而容易被 Cloudflare 风控）；代理只供 GitHub API 触发 Action 时使用，留空则直连。"
                                        "GitHub PAT 需 Actions 读写权限（细粒度：Actions Read and write；经典：repo + workflow）。"
                                        "首次运行会自动下载 Chromium（约 150MB），保存在 /config/.playwright。"
                                        "容器必须能访问 bot-hosting.net、challenges.cloudflare.com 和 api.github.com。"
                                        "若 Playwright 仍被 Cloudflare 拦截，可在容器内 pip install seleniumbase 并执行 "
                                        "seleniumbase install chromedriver，再开启「使用 SeleniumBase」，走 uc 模式（需填邮箱+密码）。"
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
            "renew_mode": RENEW_MODE_BOTH,
            "gh_token": "",
            "gh_repo": "Cxquang/Auto-Renew-Bothosting",
            "gh_workflow": "renew.yml",
            "gh_ref": "main",
            "onlyonce": False,
            "use_seleniumbase": False,
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
