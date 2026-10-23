# -*- coding: utf-8 -*-
"""
WorkBuddy + Trae 每日自动签到插件
- WorkBuddy：通过 refreshToken 调腾讯 WorkBuddy 接口完成每日签到（+100积分/天）
- Trae：通过 refreshToken 调 Trae（api.trae.cn）接口领取每日签到积分
支持：定时签到、详情页一键签到、远程命令/API 触发、消息通知策略配置。
"""
import hashlib
import json
import platform
import secrets
import time
import uuid
from datetime import datetime
from typing import List, Dict

import requests
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.event import eventmanager, EventType
from app.log import logger
from app.plugins import _PluginBase

# Trae 客户端固定参数（逆向自官方客户端，客户端大版本升级后可能需要同步调整）
TRAE_APP_ID = "6eefa01c-1036-4c7e-9ca5-d891f63bfcd8"
TRAE_CLIENT_ID = "ono9krqynydwx5"
TRAE_IDE_VERSION = "3.3.67"
TRAE_IDE_VERSION_CODE = "20260401"


class TokenInvalidError(Exception):
    """refreshToken 已失效（被服务端拒绝），需要用户重新提取并填写"""
    def __init__(self, detail: str = ""):
        self.detail = detail
        super().__init__(detail or "refreshToken 已失效")


class WorkBuddyCheckin(_PluginBase):
    """WorkBuddy 每日自动签到"""

    # 插件元数据
    plugin_name = "WorkBuddyCheckin"
    plugin_desc = "WorkBuddy 与 Trae 每日自动签到：自动领取积分，支持手动一键签到、远程触发、消息通知策略。"
    plugin_version = "1.3.2"
    plugin_author = "Cxquang"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/autosignin.png"
    plugin_order = 100
    auth_level = 1

    # 私有属性
    _enabled = False
    _refresh_token = ""
    _uid = ""
    _cron = "0 8 * * *"
    _notify_mode = "always"  # always=每次通知 / fail=仅失败通知 / off=关闭通知
    _base_url = "https://www.workbuddy.cn"
    _session = None
    # Trae 配置
    _trae_enabled = False
    _trae_access_token = ""   # 直接填入 accessToken 时优先使用，绕过 refreshToken 换 token
    _trae_refresh_token = ""
    _trae_uid = ""
    _trae_host = "https://api.trae.cn"
    _trae_device_id = ""
    _trae_machine_id = ""
    _trae_client_id = ""      # 可选：替换默认 ClientID（不同客户端版本签发的 token 可能不同）

    def init_plugin(self, config: dict = None):
        """初始化插件配置"""
        if config is None:
            config = self.get_config() or {}
        self._enabled = config.get("enabled", False)
        self._refresh_token = (config.get("refresh_token") or "").strip()
        self._uid = (config.get("uid") or "").strip()
        self._cron = (config.get("cron") or "0 8 * * *").strip() or "0 8 * * *"
        # 通知策略（兼容旧版布尔 notify 开关）
        notify_mode = config.get("notify_mode")
        if not notify_mode:
            notify_mode = "off" if config.get("notify") is False else "always"
        self._notify_mode = notify_mode if notify_mode in ("always", "fail", "off") else "always"
        # Trae 配置
        self._trae_enabled = config.get("trae_enabled", False)
        self._trae_access_token = (config.get("trae_access_token") or "").strip()
        self._trae_refresh_token = (config.get("trae_refresh_token") or "").strip()
        self._trae_uid = (config.get("trae_uid") or "").strip()
        self._trae_host = (config.get("trae_host") or "https://api.trae.cn").strip().rstrip("/")
        self._trae_device_id = (config.get("trae_device_id") or "").strip()
        self._trae_machine_id = (config.get("trae_machine_id") or "").strip()
        self._trae_client_id = (config.get("trae_client_id") or "").strip()
        self._session = requests.Session()

        if self._enabled and not self._refresh_token:
            logger.warn("WorkBuddyCheckin: 已启用但未配置 refreshToken，请先配置")
        if self._trae_enabled and (not self._trae_access_token and not self._trae_refresh_token):
            logger.warn("WorkBuddyCheckin: Trae 已启用但 accessToken 和 refreshToken 都未配置，请先配置")

        # Trae 设备指纹需长期稳定，首次使用时生成并写入配置
        if self._trae_enabled and (not self._trae_device_id or not self._trae_machine_id):
            changed = False
            if not self._trae_device_id:
                self._trae_device_id = hashlib.sha256(secrets.token_hex(32).encode()).hexdigest()[:32]
                config["trae_device_id"] = self._trae_device_id
                changed = True
            if not self._trae_machine_id:
                self._trae_machine_id = secrets.token_hex(32)
                config["trae_machine_id"] = self._trae_machine_id
                changed = True
            if changed:
                try:
                    self.update_config(config)
                except Exception as e:
                    logger.warn(f"WorkBuddyCheckin: 保存 Trae 设备指纹失败: {e}")

    def get_state(self) -> bool:
        return self._enabled or self._trae_enabled

    @staticmethod
    def get_command() -> List[dict]:
        """注册远程命令（聊天/微信触发）"""
        return [{
            "cmd": "/wb_checkin",
            "event": EventType.PluginAction,
            "desc": "WorkBuddy签到",
            "category": "签到管理",
            "data": {"action": "workbuddy_checkin"}
        }, {
            "cmd": "/trae_checkin",
            "event": EventType.PluginAction,
            "desc": "Trae签到",
            "category": "签到管理",
            "data": {"action": "trae_checkin"}
        }]

    @eventmanager.register(EventType.PluginAction)
    def handle_command(self, event):
        """处理远程命令"""
        event_data = event.event_data
        if not event_data:
            return
        action = event_data.get("action")
        if action == "workbuddy_checkin":
            result = self._do_checkin()
        elif action == "trae_checkin":
            result = self._trae_do_checkin()
        else:
            return
        self._notify_result(result)

    def get_api(self) -> List[dict]:
        """注册 REST API 端点（详情页按钮/远程调用）"""
        return [{
            "path": "/checkin",
            "endpoint": self.api_checkin,
            "methods": ["GET", "POST"],
            "auth": "apikey",
            "summary": "手动触发 WorkBuddy 签到"
        }, {
            "path": "/status",
            "endpoint": self.api_status,
            "methods": ["GET"],
            "auth": "apikey",
            "summary": "查询 WorkBuddy 签到状态"
        }, {
            "path": "/trae_checkin",
            "endpoint": self.api_trae_checkin,
            "methods": ["GET", "POST"],
            "auth": "apikey",
            "summary": "手动触发 Trae 签到"
        }, {
            "path": "/trae_status",
            "endpoint": self.api_trae_status,
            "methods": ["GET"],
            "auth": "apikey",
            "summary": "查询 Trae 签到状态"
        }]

    def api_checkin(self):
        """API: 手动触发签到（详情页按钮 / 远程调用）"""
        if not self._refresh_token:
            return {"success": False, "message": "未配置 refreshToken，请先在插件配置中填写"}
        result = self._do_checkin()
        self._notify_result(result)
        return {
            "success": result.get("status") in ("success", "already"),
            "message": result.get("title", ""),
            "text": result.get("text", ""),
        }

    def api_status(self):
        """API: 查询签到状态"""
        if not self._refresh_token:
            return {"success": False, "message": "未配置 refreshToken"}
        try:
            status = self._get_checkin_status()
            return {"success": True, "data": status}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def api_trae_checkin(self):
        """API: 手动触发 Trae 签到"""
        if not self._trae_access_token and not self._trae_refresh_token:
            return {"success": False, "message": "未配置 Trae accessToken 或 refreshToken，请先在插件配置中填写"}
        result = self._trae_do_checkin()
        self._notify_result(result)
        return {
            "success": result.get("status") in ("success", "already"),
            "message": result.get("title", ""),
            "text": result.get("text", ""),
        }

    def api_trae_status(self):
        """API: 查询 Trae 签到状态"""
        if not self._trae_access_token and not self._trae_refresh_token:
            return {"success": False, "message": "未配置 Trae accessToken 或 refreshToken"}
        try:
            token = self._trae_get_access_token()
            return {"success": True, "data": self._trae_get_status(token)}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def get_service(self) -> List[dict]:
        """注册定时服务（设定-服务 中可查看运行状态并手动启动）"""
        has_wb = self._enabled and self._refresh_token
        has_trae = self._trae_enabled and (self._trae_access_token or self._trae_refresh_token)
        if not has_wb and not has_trae:
            return []
        try:
            return [{
                "id": "WorkBuddyCheckin",
                "name": "WorkBuddy/Trae 每日签到",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self._scheduled_checkin,
                "kwargs": {}
            }]
        except Exception as e:
            logger.error(f"WorkBuddyCheckin: cron 表达式不正确({self._cron}): {e}")
            return []

    def _scheduled_checkin(self):
        """定时签到任务：按启用情况依次签到，合并为一条通知"""
        results = []
        if self._enabled and self._refresh_token:
            results.append(self._do_checkin())
        if self._trae_enabled and self._trae_refresh_token:
            results.append(self._trae_do_checkin())
        self._notify_results(results)

    # ==================== 签到核心逻辑 ====================

    def _do_checkin(self) -> dict:
        """执行签到全流程：刷新令牌 → 签到 → 查状态，并记录最近一次结果"""
        if not self._refresh_token:
            return self._finish("no_token", "WorkBuddy签到失败", "未配置 refreshToken")

        try:
            # 1. 刷新令牌（失效时抛 TokenInvalidError，走明确的重填提示）
            try:
                auth = self._refresh_token_api()
            except TokenInvalidError as e:
                return self._finish(
                    "token_invalid",
                    "WorkBuddy签到失败：refreshToken已失效",
                    " refreshToken 已被服务端拒绝，无法自动续期。\n"
                    "请重新提取 refreshToken 并到插件配置中更新！\n"
                    "常见原因：\n"
                    "1. WorkBuddy 客户端退出登录/换账号登录\n"
                    "2. 其他设备也在用同一账号签到，把 token 刷新走了——请只保留一处签到\n"
                    f"接口返回：{e.detail or '未知'}"
                )
            if not auth or not auth.get("accessToken"):
                return self._finish("fail", "WorkBuddy签到失败",
                                    "令牌刷新异常（临时故障），下次定时任务会自动重试")

            # 保存新 refreshToken（自动轮换续期）
            new_rt = auth.get("refreshToken", "")
            if new_rt and new_rt != self._refresh_token:
                self._refresh_token = new_rt
                config = self.get_config() or {}
                config["refresh_token"] = new_rt
                self.update_config(config)
                logger.info("WorkBuddyCheckin: refreshToken 已自动轮换更新")

            uid = self._uid or ""
            domain = auth.get("domain", "www.workbuddy.cn")

            # 2. 签到
            checkin_result = self._claim_checkin(auth["accessToken"], uid, domain)

            # 3. 查状态
            status = self._get_status_with_token(auth["accessToken"], uid, domain)
            streak = status.get("streak_days", "?")
            total = status.get("total_credits", "?")

            code = checkin_result.get("code", -1)
            if code == 0:
                result = self._finish(
                    "success", "WorkBuddy签到成功",
                    f"签到成功 +{checkin_result.get('credit', 100)} 积分\n连签 {streak} 天 | 累计 {total} 分"
                )
            elif code == 10001:
                result = self._finish(
                    "already", "WorkBuddy今日已签到",
                    f"今天已签到（重复签到，正常）\n连签 {streak} 天 | 累计 {total} 分"
                )
            else:
                result = self._finish(
                    "fail", "WorkBuddy签到失败",
                    f"签到返回: code={code} {checkin_result.get('msg', '')}\n连签 {streak} 天 | 累计 {total} 分"
                )
            return result

        except requests.exceptions.ConnectionError:
            return self._finish("error", "WorkBuddy签到失败", "网络连接失败，请检查网络")
        except Exception as e:
            logger.error(f"WorkBuddyCheckin: 签到异常: {e}")
            return self._finish("error", "WorkBuddy签到失败", f"异常: {str(e)}")

    def _finish(self, status: str, title: str, text: str, prefix: str = "checkin") -> dict:
        """统一结果出口：记录最近一次签到结果到配置，供详情页展示"""
        result = {"status": status, "title": title, "text": text}
        try:
            config = self.get_config() or {}
            config[f"last_{prefix}_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            config[f"last_{prefix}_result"] = title
            self.update_config(config)
        except Exception as e:
            logger.warn(f"WorkBuddyCheckin: 保存签到记录失败: {e}")
        return result

    def _refresh_token_api(self) -> dict:
        """调用刷新令牌接口"""
        headers = {
            "X-Refresh-Token": self._refresh_token,
            "X-Auth-Refresh-Source": "plugin",
            "X-Domain": "www.workbuddy.cn",
        }
        if self._uid:
            headers["X-User-Id"] = self._uid
        resp = self._session.post(
            f"{self._base_url}/v2/plugin/auth/token/refresh",
            headers=headers, json={}, timeout=30
        )
        try:
            data = resp.json()
        except ValueError:
            data = {}
        # 判定令牌是否被服务端明确拒绝（HTTP 401/403，或业务码提示令牌无效）
        if resp.status_code in (401, 403):
            raise TokenInvalidError(f"HTTP {resp.status_code} {data.get('msg', '')}".strip())
        if resp.status_code == 200 and data.get("code") == 0:
            return data.get("data", {})
        # 其他状态 + 非零业务码：大概率是令牌过期/非法（服务端不同错误路径返回不同状态码）
        if data.get("code") not in (0, None):
            raise TokenInvalidError(f"HTTP {resp.status_code} code={data.get('code')} {data.get('msg', '')}".strip())
        logger.error(f"WorkBuddyCheckin: 刷新令牌失败: {resp.status_code} {data}")
        return {}

    def _claim_checkin(self, access_token: str, uid: str, domain: str) -> dict:
        """调用签到接口"""
        headers = self._build_auth_headers(access_token, uid, domain)
        resp = self._session.post(
            f"{self._base_url}/v2/billing/meter/daily-checkin",
            headers=headers, json={}, timeout=30
        )
        try:
            data = resp.json()
        except ValueError:
            data = {}
        return {
            "code": data.get("code", -1),
            "msg": data.get("msg", ""),
            "credit": (data.get("data") or {}).get("credit", 100) if data.get("code") == 0 else 0
        }

    def _get_status_with_token(self, access_token: str, uid: str, domain: str) -> dict:
        """查询签到状态"""
        headers = self._build_auth_headers(access_token, uid, domain)
        resp = self._session.post(
            f"{self._base_url}/v2/billing/meter/checkin-activity-status",
            headers=headers, json={}, timeout=30
        )
        try:
            data = resp.json()
        except ValueError:
            return {}
        if resp.status_code == 200 and data.get("code") == 0:
            return data.get("data", {})
        return {}

    def _get_checkin_status(self) -> dict:
        """供详情页/API 使用的状态查询（先刷新再查）"""
        auth = self._refresh_token_api()
        if not auth or not auth.get("accessToken"):
            raise Exception("令牌刷新失败")
        return self._get_status_with_token(
            auth["accessToken"],
            self._uid or "",
            auth.get("domain", "www.workbuddy.cn")
        )

    # ==================== Trae 签到逻辑 ====================

    def _trae_headers(self, access_token: str) -> dict:
        """构建 Trae 客户端同款鉴权头（设备指纹长期复用，避免被识别为旁路请求）"""
        system = platform.system()
        device_type = {"Windows": "windows", "Darwin": "mac", "Linux": "linux"}.get(system, "unknown")
        os_version = f"{system} {platform.release()}"
        return {
            "Authorization": f"Cloud-IDE-JWT {access_token}",
            "X-Cloudide-Token": access_token,
            "x-uid": self._trae_uid,
            "x-app-id": TRAE_APP_ID,
            "x-device-id": self._trae_device_id,
            "x-machine-id": self._trae_machine_id,
            "x-request-id": str(uuid.uuid4()),
            "x-ide-version": TRAE_IDE_VERSION,
            "x-ide-version-code": TRAE_IDE_VERSION_CODE,
            "x-device-type": device_type,
            "x-os-version": os_version,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    def _trae_exchange_token(self) -> str:
        """用 refreshToken 换取 accessToken；refreshToken 被拒绝时抛 TokenInvalidError"""
        if not self._trae_refresh_token:
            raise TokenInvalidError("未配置 Trae refreshToken")
        if not self._trae_uid:
            raise TokenInvalidError("未配置 Trae User ID（x-uid）")
        client_id = self._trae_client_id or TRAE_CLIENT_ID
        resp = self._session.post(
            f"{self._trae_host}/cloudide/api/v3/trae/oauth/ExchangeToken",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            json={
                "ClientID": client_id,
                "RefreshToken": self._trae_refresh_token,
                "ClientSecret": "-",
                "UserID": self._trae_uid,
            },
            timeout=30,
        )
        try:
            data = resp.json()
        except ValueError:
            data = {}
        result = data.get("Result") or {}
        token = result.get("Token")
        # 若服务端下发了新 refreshToken，自动轮换保存
        new_rt = result.get("RefreshToken")
        if new_rt and new_rt != self._trae_refresh_token:
            self._trae_refresh_token = new_rt
            config = self.get_config() or {}
            config["trae_refresh_token"] = new_rt
            self.update_config(config)
            logger.info("WorkBuddyCheckin: Trae refreshToken 已自动轮换更新")
        if token:
            return token
        # 区分错误类型：ClientID 不匹配时提示用户填正确的 ClientID
        body = json.dumps(data, ensure_ascii=False) if data else resp.text
        if "not matched" in body.lower() or "refresh token is not matched" in body:
            raise TokenInvalidError(
                f"HTTP {resp.status_code} 服务端提示 refreshToken 与 ClientID 不匹配。\n"
                f"当前使用的 ClientID={client_id}，请在 Trae 客户端抓包确认实际 ClientID 后填入配置。\n"
                f"（或直接在「Access Token」处粘贴从登录态解密出来的 accessToken，绕过换 token 步骤）"
            )
        if resp.status_code in (401, 403):
            raise TokenInvalidError(f"HTTP {resp.status_code} {body[:200]}")
        detail = data.get("Message") or data.get("Error") or data.get("message") or body
        raise TokenInvalidError(f"HTTP {resp.status_code} {detail}".strip())

    def _trae_get_access_token(self) -> str:
        """优先使用用户手动配置的 accessToken，否则用 refreshToken 自动换新"""
        if self._trae_access_token:
            return self._trae_access_token
        return self._trae_exchange_token()

    def _trae_get_status(self, access_token: str) -> dict:
        """查询 Trae 今日签到状态"""
        resp = self._session.post(
            f"{self._trae_host}/trae/api/v2/ug/checkin_credits/status",
            headers=self._trae_headers(access_token), json={}, timeout=30
        )
        try:
            data = resp.json()
        except ValueError:
            data = {}
        if resp.status_code in (401, 403):
            raise TokenInvalidError(f"HTTP {resp.status_code} {data.get('message', '')}".strip())
        return data

    def _trae_claim(self, access_token: str) -> dict:
        """调用 Trae 领取签到积分接口，对 9074 限流做指数退避重试"""
        data = {}
        for attempt in range(3):
            resp = self._session.post(
                f"{self._trae_host}/trae/api/v2/ug/checkin_credits/claim",
                headers=self._trae_headers(access_token), json={}, timeout=30
            )
            try:
                data = resp.json()
            except ValueError:
                data = {}
            if resp.status_code in (401, 403):
                raise TokenInvalidError(f"HTTP {resp.status_code} {data.get('message', '')}".strip())
            # 9074 = 参与用户太多，指数退避重试
            if data.get("code") == 9074:
                wait = 2 ** attempt
                logger.warn(f"WorkBuddyCheckin: Trae 签到限流 9074，{wait}s 后第 {attempt+2} 次重试")
                time.sleep(wait)
                continue
            return data
        return data  # 重试 3 次后仍然限流，返回最后一次结果

    def _trae_do_checkin(self) -> dict:
        """执行 Trae 签到全流程：取 token（优先 accessToken）→ 查状态 → 未签则领取"""
        if not self._trae_uid:
            return self._finish("no_token", "Trae签到失败",
                                "未配置 Trae User ID（x-uid），请从客户端登录态中获取 userId 后填入",
                                prefix="trae")
        if not self._trae_access_token and not self._trae_refresh_token:
            return self._finish(
                "no_token", "Trae签到失败",
                "未配置 Trae accessToken 或 refreshToken，至少填其一", prefix="trae")
        try:
            token = self._trae_get_access_token()
            status = self._trae_get_status(token)
            code = status.get("code", -1)
            if code != 0:
                return self._finish(
                    "fail", "Trae签到失败",
                    f"状态查询返回: code={code} {status.get('message', '')}", prefix="trae")

            credits = status.get("credits", "?")
            extra = status.get("extra_credits")
            summary = f"基础 {credits} 分" + (f" | 额外 {extra} 分" if extra is not None else "")

            if status.get("checked_in"):
                return self._finish(
                    "already", "Trae今日已签到",
                    f"今天已签到（重复签到，正常）\n{summary}", prefix="trae")

            claim = self._trae_claim(token)
            ccode = claim.get("code", -1)
            if ccode == 0:
                cdata = claim.get("data") or {}
                gained = claim.get("gained_credits")
                if gained is None:
                    gained = claim.get("credits_gained")
                if gained is None:
                    gained = claim.get("reward")
                if gained is None:
                    gained = cdata.get("gained_credits")
                gain_text = f" +{gained} 积分" if gained not in (None, "") else ""
                return self._finish(
                    "success", "Trae签到成功",
                    f"签到成功{gain_text}\n{summary}", prefix="trae")

            return self._finish(
                "fail", "Trae签到失败",
                f"领取返回: code={ccode} {claim.get('message', '')}\n{summary}", prefix="trae")

        except TokenInvalidError as e:
            return self._finish(
                "token_invalid", "Trae签到失败：令牌获取失败",
                f"{e.detail or '未知错误'}\n\n"
                "常见解决办法：\n"
                "1. 优先使用 Access Token（从客户端登录态直接提取，绕过换 token 步骤）\n"
                "2. 若必须用 RefreshToken，请确认 ClientID 与签发 refreshToken 的客户端版本匹配\n"
                "3. 确保同一账号只在一个地方执行签到，避免多个脚本互相轮换刷新令牌",
                prefix="trae")
        except requests.exceptions.ConnectionError:
            return self._finish(
                "error", "Trae签到失败",
                "网络连接失败，请检查 MoviePilot 能否访问 api.trae.cn", prefix="trae")
        except Exception as e:
            logger.error(f"WorkBuddyCheckin: Trae签到异常: {e}")
            return self._finish("error", "Trae签到失败", f"异常: {str(e)}", prefix="trae")

    @staticmethod
    def _build_auth_headers(access_token: str, uid: str, domain: str) -> dict:
        """构建需要 Bearer 认证的请求头"""
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        if uid:
            headers["X-User-Id"] = uid
        if domain:
            headers["X-Domain"] = domain
        return headers

    # ==================== 通知 ====================

    def _notify_result(self, result: dict):
        """单条结果通知（手动签到/命令触发）"""
        self._notify_results([result])

    def _notify_results(self, results: List[dict]):
        """按通知策略通过 MoviePilot 消息渠道（如企业微信）推送签到结果，多服务合并为一条"""
        results = [r for r in results if r]
        if not results or self._notify_mode == "off":
            return
        fail_status = ("fail", "token_invalid", "error", "no_token")
        if self._notify_mode == "fail" and not any(
                r.get("status") in fail_status for r in results):
            return
        if len(results) == 1:
            title = results[0].get("title") or "签到结果"
            text = results[0].get("text", "")
        else:
            title = "每日签到结果"
            text = "\n\n".join(
                f"{r.get('title', '')}\n{r.get('text', '')}".strip() for r in results)
        try:
            self.post_message(title=title, text=text)
        except Exception as e:
            logger.error(f"WorkBuddyCheckin: 发送通知失败: {e}")

    # ==================== 界面 ====================

    def get_form(self) -> tuple:
        """插件配置表单"""
        return [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VSwitch",
                        "props": {
                            "model": "enabled",
                            "label": "启用插件",
                        }
                    },
                    {
                        "component": "VTextarea",
                        "props": {
                            "model": "refresh_token",
                            "label": "RefreshToken",
                            "placeholder": "粘贴 eyJ 开头的 refreshToken（约700字符）",
                            "rows": 3,
                            "hint": "从 WorkBuddy 桌面端提取；令牌每次签到自动轮换续期，无需频繁更新"
                        }
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "uid",
                            "label": "User ID（可选）",
                            "placeholder": "如 330111451029",
                            "hint": "WorkBuddy 用户 ID，留空则不发送 X-User-Id 头"
                        }
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "cron",
                            "label": "签到时间（Cron）",
                            "placeholder": "0 8 * * *（每天8点）",
                            "hint": "标准 Cron 表达式：分 时 日 月 周"
                        }
                    },
                    {
                        "component": "VSelect",
                        "props": {
                            "model": "notify_mode",
                            "label": "消息通知",
                            "items": [
                                {"title": "每次签到都通知", "value": "always"},
                                {"title": "仅失败时通知（含令牌失效）", "value": "fail"},
                                {"title": "关闭通知", "value": "off"},
                            ],
                            "hint": "通过 MoviePilot 已配置的消息渠道（如企业微信）推送",
                            "persistent-hint": True,
                        }
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "density": "compact",
                            "class": "mt-2 mb-1",
                            "text": "Trae 签到（api.trae.cn）：建议直接填 Access Token，"
                                    "绕过 refreshToken 换 token 步骤（不同客户端版本的 ClientID 可能不同）；"
                                    "与 WorkBuddy 共用上面的签到时间与通知策略"
                        }
                    },
                    {
                        "component": "VSwitch",
                        "props": {
                            "model": "trae_enabled",
                            "label": "启用 Trae 签到",
                        }
                    },
                    {
                        "component": "VTextarea",
                        "props": {
                            "model": "trae_access_token",
                            "label": "Trae Access Token（推荐）",
                            "placeholder": "粘贴 Trae 登录态中的 token（eyJ 开头的 JWT）",
                            "rows": 2,
                            "hint": "直接调用签到接口，最稳定；从客户端 storage.json 解密出来的登录态 JSON 里的 token 字段。"
                                    "有效期约 15-30 天，过期后需重新提取"
                        }
                    },
                    {
                        "component": "VTextarea",
                        "props": {
                            "model": "trae_refresh_token",
                            "label": "Trae RefreshToken（备选）",
                            "placeholder": "粘贴 Trae 登录态中的 refreshToken",
                            "rows": 2,
                            "hint": "留空则只使用 Access Token；某些客户端版本的 refreshToken 与默认 ClientID 不匹配，"
                                    "报 400 时可尝试填 ClientID 或直接只用 Access Token"
                        }
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "trae_uid",
                            "label": "Trae User ID",
                            "placeholder": "登录态中的 userId",
                            "hint": "x-uid 请求头必填（Access Token 换 token 时也要用到）"
                        }
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "trae_client_id",
                            "label": "Trae ClientID（可选）",
                            "placeholder": "留空用默认 ono9krqynydwx5",
                            "hint": "用于 refreshToken 换 accessToken，不同客户端版本可能不同；"
                                    "报 400 refresh token 与 ClientID 不匹配时，从 Trae 客户端抓包确认后填入"
                        }
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "trae_host",
                            "label": "Trae API 地址",
                            "placeholder": "https://api.trae.cn",
                            "hint": "国内版默认 https://api.trae.cn，一般无需修改"
                        }
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "trae_device_id",
                            "label": "Trae Device ID（可选）",
                            "placeholder": "留空自动生成并固定",
                            "hint": "对应客户端登录态里的 Device Id；建议填真实值，留空则首次启用时自动生成并保存"
                        }
                    },
                    {
                        "component": "VTextField",
                        "props": {
                            "model": "trae_machine_id",
                            "label": "Trae Machine ID（可选）",
                            "placeholder": "留空自动生成并固定",
                            "hint": "对应客户端 telemetry.machineId；留空则首次启用时自动生成并保存"
                        }
                    },
                    {
                        "component": "VDivider",
                        "props": {"class": "my-3"},
                    },
                    {
                        "component": "div",
                        "props": {"class": "d-flex gap-2 flex-wrap"},
                        "content": [
                            {
                                "component": "VBtn",
                                "props": {
                                    "color": "primary",
                                    "variant": "flat",
                                    "size": "small",
                                    "prepend-icon": "mdi-cash-check",
                                    "class": "mr-1",
                                },
                                "text": "WorkBuddy 立即签到",
                                "events": {
                                    "click": {
                                        "api": f"plugin/{self.__class__.__name__}/checkin?apikey={settings.API_TOKEN}",
                                        "method": "get",
                                    }
                                },
                            },
                            {
                                "component": "VBtn",
                                "props": {
                                    "color": "secondary",
                                    "variant": "tonal",
                                    "size": "small",
                                    "prepend-icon": "mdi-information-outline",
                                },
                                "text": "WorkBuddy 查询状态",
                                "events": {
                                    "click": {
                                        "api": f"plugin/{self.__class__.__name__}/status?apikey={settings.API_TOKEN}",
                                        "method": "get",
                                    }
                                },
                            },
                            {
                                "component": "VBtn",
                                "props": {
                                    "color": "primary",
                                    "variant": "flat",
                                    "size": "small",
                                    "prepend-icon": "mdi-cash-check",
                                    "class": "mr-1",
                                },
                                "text": "Trae 立即签到",
                                "events": {
                                    "click": {
                                        "api": f"plugin/{self.__class__.__name__}/trae_checkin?apikey={settings.API_TOKEN}",
                                        "method": "get",
                                    }
                                },
                            },
                            {
                                "component": "VBtn",
                                "props": {
                                    "color": "secondary",
                                    "variant": "tonal",
                                    "size": "small",
                                    "prepend-icon": "mdi-information-outline",
                                },
                                "text": "Trae 查询状态",
                                "events": {
                                    "click": {
                                        "api": f"plugin/{self.__class__.__name__}/trae_status?apikey={settings.API_TOKEN}",
                                        "method": "get",
                                    }
                                },
                            },
                        ],
                    },
                    {
                        "component": "div",
                        "props": {"class": "text-body-2 text-medium-emphasis mt-1"},
                        "text": "按钮点击后由 MoviePilot 插件后端执行，执行结果与完整请求链路请在 MoviePilot「日志中心」查看"
                    },
                ]
            }
        ], {
            "enabled": False,
            "refresh_token": "",
            "uid": "",
            "cron": "0 8 * * *",
            "notify_mode": "always",
            "trae_enabled": False,
            "trae_access_token": "",
            "trae_refresh_token": "",
            "trae_uid": "",
            "trae_host": "https://api.trae.cn",
            "trae_client_id": "",
            "trae_device_id": "",
            "trae_machine_id": "",
        }

    @staticmethod
    def _stat_row(label: str, value: str, value_class: str = "") -> dict:
        """详情页单行统计数据"""
        return {
            "component": "div",
            "props": {"class": "d-flex justify-space-between align-center px-4 py-1"},
            "content": [
                {"component": "span", "props": {"class": "text-medium-emphasis"}, "text": label},
                {"component": "span", "props": {"class": value_class}, "text": value},
            ],
        }

    def get_page(self) -> List[dict]:
        """详情页：仅展示缓存的上次签到结果（不做任何实时网络请求，避免页面卡住）"""
        config = self.get_config() or {}
        last_time = config.get("last_checkin_time", "")
        last_result = config.get("last_checkin_result", "")
        trae_last_time = config.get("last_trae_time", "")
        trae_last_result = config.get("last_trae_result", "")

        wb_rows = []
        if self._refresh_token:
            wb_rows.append(self._stat_row("已配置", "是", "text-success"))
        else:
            wb_rows.append(self._stat_row("已配置", "否", "text-warning"))
        wb_rows.append(self._stat_row("签到时间", config.get("cron", "0 8 * * *")))
        wb_rows.append(self._stat_row(
            "通知策略", {
                "always": "每次通知",
                "fail": "仅失败通知",
                "off": "关闭通知",
            }.get(config.get("notify_mode", "always"), config.get("notify_mode"))
        ))
        if last_result:
            wb_rows.append(self._stat_row("上次签到", f"{last_result}（{last_time}）"))

        trae_rows = []
        if self._trae_access_token or self._trae_refresh_token:
            trae_rows.append(self._stat_row("已配置", "是", "text-success"))
        else:
            trae_rows.append(self._stat_row("已配置", "否", "text-warning"))
        if self._trae_access_token:
            trae_rows.append(self._stat_row("令牌来源", "Access Token（直连）"))
        elif self._trae_refresh_token:
            trae_rows.append(self._stat_row("令牌来源", "RefreshToken（自动续期）"))
        if self._trae_uid:
            trae_rows.append(self._stat_row("User ID", self._trae_uid))
        if trae_last_result:
            trae_rows.append(self._stat_row("上次签到", f"{trae_last_result}（{trae_last_time}）"))

        return [
            {
                "component": "VAlert",
                "props": {
                    "type": "info", "variant": "tonal", "density": "compact",
                    "text": "手动签到与状态查询按钮已迁移到「插件设置」，"
                            "本页面仅展示缓存结果避免因网络请求阻塞。"
                }
            },
            {
                "component": "VCard",
                "props": {"variant": "tonal"},
                "content": [
                    {
                        "component": "div",
                        "props": {"class": "px-4 pt-3 pb-1"},
                        "content": [
                            {"component": "span", "props": {"class": "text-h6"}, "text": "WorkBuddy 签到"},
                        ],
                    },
                    *wb_rows,
                ]
            },
            {
                "component": "VCard",
                "props": {"variant": "tonal", "class": "mt-3"},
                "content": [
                    {
                        "component": "div",
                        "props": {"class": "px-4 pt-3 pb-1"},
                        "content": [
                            {"component": "span", "props": {"class": "text-h6"}, "text": "Trae 签到"},
                        ],
                    },
                    *trae_rows,
                ]
            },
        ]

    def stop_service(self):
        """停止插件"""
        if self._session:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None
