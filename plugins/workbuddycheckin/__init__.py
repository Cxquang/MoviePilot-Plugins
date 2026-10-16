# -*- coding: utf-8 -*-
"""
WorkBuddy 每日自动签到插件
通过 refreshToken 调腾讯 WorkBuddy 接口完成每日签到（+100积分/天），
签到结果通过 MoviePilot 内置通知通道推送到企业微信。
"""
import re
import json
import requests
from datetime import datetime, timezone, timedelta
from typing import Any, List, Dict, Optional

from app.plugins import _PluginBase
from app.log import logger
from app.core.event import eventmanager, EventType


class WorkBuddyCheckin(_PluginBase):
    """WorkBuddy 每日自动签到"""

    # 插件元数据
    plugin_name = "WorkBuddyCheckin"
    plugin_desc = "WorkBuddy 每日自动签到，自动获取 100 积分，支持远程触发和通知推送。"
    plugin_version = "1.0.0"
    plugin_author = "Cxquang"
    plugin_icon = "https://raw.githubusercontent.com/jxxghp/MoviePilot-Plugins/main/icons/autosignin.png"
    plugin_order = 100
    plugin_config_prefix = "workbuddycheckin."

    # 私有属性
    _enabled = False
    _refresh_token = ""
    _uid = ""
    _cron = "0 8 * * *"
    _notify = True
    _base_url = "https://www.workbuddy.cn"
    _session = None

    def init_plugin(self, config: dict = None):
        """初始化插件配置"""
        if not config:
            return
        self._enabled = config.get("enabled", False)
        self._refresh_token = config.get("refresh_token", "").strip()
        self._uid = config.get("uid", "").strip()
        self._cron = config.get("cron", "0 8 * * *").strip() or "0 8 * * *"
        self._notify = config.get("notify", True)
        self._session = requests.Session()

        if self._enabled and not self._refresh_token:
            logger.warn("WorkBuddyCheckin: 已启用但未配置 refreshToken，请先配置")

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[dict]:
        """注册远程命令（聊天/微信触发）"""
        return [{
            "cmd": "/wb_checkin",
            "event": EventType.PluginAction,
            "desc": "WorkBuddy签到",
            "category": "签到管理",
            "data": {"action": "workbuddy_checkin"}
        }]

    @eventmanager.register(EventType.PluginAction)
    def handle_command(self, event):
        """处理远程命令"""
        event_data = event.event_data
        if not event_data or event_data.get("action") != "workbuddy_checkin":
            return
        result = self._do_checkin()
        if self._notify:
            self._send_notify(result["title"], result["text"])

    def get_api(self) -> List[dict]:
        """注册 REST API 端点（远程触发签到）"""
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
        }]

    def api_checkin(self):
        """API: 手动触发签到"""
        result = self._do_checkin()
        if self._notify:
            self._send_notify(result["title"], result["text"])
        return {"success": True, "message": result["title"], "detail": result["text"]}

    def api_status(self):
        """API: 查询签到状态"""
        if not self._enabled or not self._refresh_token:
            return {"success": False, "message": "插件未启用或未配置 refreshToken"}
        try:
            status = self._get_checkin_status()
            return {"success": True, "data": status}
        except Exception as e:
            return {"success": False, "message": str(e)}

    def get_service(self) -> List[dict]:
        """注册定时服务"""
        if not self._enabled or not self._refresh_token:
            return []
        cron_parts = self._parse_cron(self._cron)
        if not cron_parts:
            return []
        return [{
            "id": "WorkBuddyCheckin",
            "name": "WorkBuddy 每日签到",
            "trigger": "cron",
            "func": self._scheduled_checkin,
            "kwargs": cron_parts
        }]

    def _scheduled_checkin(self):
        """定时签到任务"""
        result = self._do_checkin()
        if self._notify:
            self._send_notify(result["title"], result["text"])

    def _do_checkin(self) -> dict:
        """执行签到全流程：刷新令牌 → 签到 → 查状态"""
        if not self._refresh_token:
            return {"title": "WorkBuddy签到失败", "text": "未配置 refreshToken"}

        try:
            # 1. 刷新令牌
            auth = self._refresh_token_api()
            if not auth or not auth.get("accessToken"):
                return {"title": "WorkBuddy签到失败",
                        "text": f"令牌刷新失败，refreshToken 可能已过期，请重新获取并更新配置"}

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
            today = status.get("today_credit", "?")

            code = checkin_result.get("code", -1)
            if code == 0:
                title = "WorkBuddy签到成功"
                text = f"签到成功 +{checkin_result.get('credit', 100)} 积分\n连签 {streak} 天 | 今日+{today} | 累计 {total} 分"
            elif code == 10001:
                title = "WorkBuddy今日已签到"
                text = f"今天已签到（重复签到，正常）\n连签 {streak} 天 | 今日+{today} | 累计 {total} 分"
            else:
                title = "WorkBuddy签到失败"
                text = f"签到返回: {checkin_result}\n连签 {streak} 天 | 累计 {total} 分"

            return {"title": title, "text": text}

        except requests.exceptions.ConnectionError:
            return {"title": "WorkBuddy签到失败", "text": "网络连接失败，请检查网络"}
        except Exception as e:
            logger.error(f"WorkBuddyCheckin: 签到异常: {e}")
            return {"title": "WorkBuddy签到失败", "text": f"异常: {str(e)}"}

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
        data = resp.json()
        if resp.status_code == 200 and data.get("code") == 0:
            return data.get("data", {})
        logger.error(f"WorkBuddyCheckin: 刷新令牌失败: {resp.status_code} {data}")
        return {}

    def _claim_checkin(self, access_token: str, uid: str, domain: str) -> dict:
        """调用签到接口"""
        headers = self._build_auth_headers(access_token, uid, domain)
        resp = self._session.post(
            f"{self._base_url}/v2/billing/meter/daily-checkin",
            headers=headers, json={}, timeout=30
        )
        data = resp.json()
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
        data = resp.json()
        if resp.status_code == 200 and data.get("code") == 0:
            return data.get("data", {})
        return {}

    def _get_checkin_status(self) -> dict:
        """供 API 调用的状态查询（先刷新再查）"""
        auth = self._refresh_token_api()
        if not auth or not auth.get("accessToken"):
            raise Exception("令牌刷新失败")
        return self._get_status_with_token(
            auth["accessToken"],
            self._uid or "",
            auth.get("domain", "www.workbuddy.cn")
        )

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

    def _send_notify(self, title: str, text: str):
        """通过 MoviePilot 内置通知通道发送消息"""
        try:
            eventmanager.send_event(EventType.NoticeMessage, {
                "title": title,
                "text": text,
            })
        except Exception as e:
            logger.error(f"WorkBuddyCheckin: 发送通知失败: {e}")

    @staticmethod
    def _parse_cron(cron_str: str) -> dict:
        """解析 cron 表达式为 APScheduler kwargs"""
        # 支持 "分 时 日 月 周" 格式
        parts = cron_str.strip().split()
        if len(parts) != 5:
            return {}
        minute, hour, day, month, day_of_week = parts
        kwargs = {}
        if minute != "*":
            kwargs["minute"] = minute
        if hour != "*":
            kwargs["hour"] = hour
        if day != "*" and day_of_week == "*":
            kwargs["day"] = day
        if month != "*":
            kwargs["month"] = month
        if day_of_week != "*":
            kwargs["day_of_week"] = day_of_week
        if not kwargs:
            kwargs["hour"] = "8"
        return kwargs

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
                        "component": "VSwitch",
                        "props": {
                            "model": "notify",
                            "label": "签到后发送通知",
                        }
                    },
                ]
            }
        ], {
            "enabled": False,
            "refresh_token": "",
            "uid": "",
            "cron": "0 8 * * *",
            "notify": True,
        }

    def get_page(self) -> List[dict]:
        """详情页展示签到状态"""
        if not self._enabled:
            return [{
                "component": "div",
                "text": "插件未启用",
                "props": {"class": "text-center text-grey"}
            }]
        try:
            status = self._get_checkin_status()
        except Exception:
            return [{
                "component": "div",
                "text": "获取状态失败，请检查 refreshToken 是否有效",
                "props": {"class": "text-center text-error"}
            }]
        return [
            {
                "component": "div",
                "props": {"class": "d-flex flex-column gap-2"},
                "content": [
                    {
                        "component": "div",
                        "props": {"class": "d-flex justify-space-between"},
                        "content": [
                            {"component": "span", "text": "今日已签到"},
                            {"component": "span", "text": "✅" if status.get("today_checked_in") else "❌"},
                        ]
                    },
                    {
                        "component": "div",
                        "props": {"class": "d-flex justify-space-between"},
                        "content": [
                            {"component": "span", "text": "连签天数"},
                            {"component": "span", "text": f'{status.get("streak_days", "?")} 天'},
                        ]
                    },
                    {
                        "component": "div",
                        "props": {"class": "d-flex justify-space-between"},
                        "content": [
                            {"component": "span", "text": "累计积分"},
                            {"component": "span", "text": f'{status.get("total_credits", "?")} 分'},
                        ]
                    },
                    {
                        "component": "div",
                        "props": {"class": "d-flex justify-space-between"},
                        "content": [
                            {"component": "span", "text": "今日积分"},
                            {"component": "span", "text": f'+{status.get("today_credit", "?")} 分'},
                        ]
                    },
                ]
            }
        ]

    def stop_service(self):
        """停止插件"""
        if self._session:
            try:
                self._session.close()
            except Exception:
                pass
            self._session = None
