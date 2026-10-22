import json
from datetime import datetime
from typing import Any, List, Dict, Tuple

import requests.exceptions

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import EventType, NotificationType
from app.utils.http import RequestUtils


class ExportMsg(_PluginBase):
    # 插件名称
    plugin_name = "消息通知暴露"
    # 插件描述
    plugin_desc = "消息通知暴露给需要使用的服务，可以接收消息进一步处理"
    # 插件图标
    plugin_icon = "Plugins_A.png"
    # 插件版本
    plugin_version = "1.07"
    # 插件作者
    plugin_author = "Cxquang"
    # 作者主页
    author_url = "https://github.com/Cxquang"
    # 插件配置项ID前缀
    plugin_config_prefix = "exportmsg_"
    # 加载顺序
    plugin_order = 29
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    _url = None
    _msgtypes = []
    _topic = None  # 新增topic字段
    # 整理完成事件监听开关（开启后入库推送由 TransferComplete 事件独占）
    _enable_transfer = True

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled")
            self._url = config.get("url")
            self._msgtypes = config.get("msgtypes") or []
            self._topic = config.get("topic")  # 新增topic字段
            self._enable_transfer = config.get("enable_transfer", True)

    def get_state(self) -> bool:
        return self._enabled and (True if self._url else False)

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        pass

    def get_api(self) -> List[Dict[str, Any]]:
        return [{
            "path": "/test",
            "endpoint": self.api_test,
            "methods": ["GET", "POST"],
            "auth": "apikey",
            "summary": "测试推送：模拟整理完成事件发送到接收地址",
        }]

    def api_test(self) -> Dict[str, Any]:
        """
        测试推送：构造一条模拟的整理完成事件，发送到已保存的接收地址
        仅校验接收地址，不校验「整理完成事件」开关与消息类型筛选
        """
        if not self._url:
            return {"code": 1, "msg": "请先填写「需要接收通知的地址」并保存配置"}

        # 模拟的整理完成事件数据（结构与真实 TransferComplete 一致）
        meta = {
            "name": "测试剧集",
            "year": "2024",
            "begin_season": 1,
            "end_season": 1,
            "begin_episode": 1,
            "end_episode": 1,
            "type": "电视剧",
        }
        mediainfo = {
            "title": "测试剧集",
            "original_title": "Test Show",
            "year": "2024",
            "type": "电视剧",
            "tmdb_id": "1000001",
            "douban_id": "3000001",
        }
        transferinfo = {
            "target_diritem": {"path": "/media/tv/测试剧集 (2024)/Season 01"},
            "file_list_new": ["测试剧集 S01E01.mkv"],
        }
        fileitem = {"name": "测试剧集 S01E01.mkv"}

        event_info = self.__build_transfer_event_info(meta, mediainfo, transferinfo, fileitem)
        # 标记为测试消息，便于下游服务识别
        event_info["test"] = True
        event_info["title"] = f"【测试】{event_info['title']}"

        logger.info(f"exportMsg测试推送 -> {self._url}")
        success, msg = self.__forward(event_info)

        self.save_data("last_test", {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "url": self._url,
            "success": success,
            "msg": msg,
            "title": event_info.get("title"),
            "content": event_info.get("content"),
        })
        return {
            "code": 0 if success else 1,
            "msg": msg,
            "data": event_info,
        }

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        # 编历 NotificationType 枚举，生成消息类型选项
        MsgTypeOptions = []
        for item in NotificationType:
            MsgTypeOptions.append({
                "title": item.value,
                "value": item.name
            })
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enable_transfer',
                                            'label': '整理完成事件（入库独占）',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'url',
                                            'label': '需要接收通知的地址',
                                            'placeholder': 'http://xxx.com/api/private/v1/recept',
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'span',
                                        'props': {
                                            'class': 'text-medium-emphasis text-body-2'
                                        },
                                        'text': '保存配置后，可在插件详情页点击「发送测试（模拟整理完成事件）」按钮，'
                                                '按已保存的地址推送一条示例整理完成事件，用于验证下游服务连通性',
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VSelect',
                                        'props': {
                                            'multiple': True,
                                            'chips': True,
                                            'model': 'msgtypes',
                                            'label': '消息类型',
                                            'items': MsgTypeOptions
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '开启「整理完成事件」后，入库成功推送改由 TransferComplete 事件独占触发，'
                                                    '覆盖手动实时整理、未开启通知等场景，消息不受消息类型筛选控制，'
                                                    '并附带标题/季集/年份/TMDB/目标路径等结构化信息（media 字段），适合弹幕采集等自动化处理；'
                                                    '原通知监听自动跳过入库成功消息避免重复。关闭该开关即恢复旧版行为（仅通知监听）。'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": True,
            'url': '',
            'topic': '',
            'msgtypes': [],
            "enable_transfer": True,
        }

    def get_page(self) -> List[dict]:
        """详情页：接收地址、开关状态、测试推送按钮与最近测试结果"""
        last = self.get_data("last_test") or {}
        content = [
            {
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {'cols': 12, 'md': 6},
                        'content': [
                            {
                                'component': 'VStat',
                                'props': {
                                    'name': '插件状态',
                                    'value': '已启用' if self._enabled else '未启用',
                                }
                            }
                        ]
                    },
                    {
                        'component': 'VCol',
                        'props': {'cols': 12, 'md': 6},
                        'content': [
                            {
                                'component': 'VStat',
                                'props': {
                                    'name': '整理完成事件',
                                    'value': '已开启' if self._enable_transfer else '已关闭',
                                }
                            }
                        ]
                    }
                ]
            },
            {
                'component': 'div',
                'props': {'class': 'px-4 py-2 text-body-2'},
                'text': f'接收地址：{self._url or "未配置"}',
            },
            {
                'component': 'div',
                'props': {'class': 'px-4 pb-2 text-medium-emphasis text-body-2'},
                'text': '测试消息标题带「【测试】」前缀，payload 带 test=true，其余 title/content/event/media 字段与真实整理完成消息一致',
            },
        ]

        if last:
            last_content = str(last.get('content', '')).replace('\n', ' / ')
            content.append({
                'component': 'VAlert',
                'props': {
                    'type': 'success' if last.get('success') else 'error',
                    'variant': 'tonal',
                    'text': f"{last.get('time', '')} 测试结果：{last.get('msg', '')}"
                            f"；标题：{last.get('title', '')}；内容：{last_content}",
                }
            })

        content.append({
            'component': 'VRow',
            'content': [
                {
                    'component': 'VCol',
                    'props': {'cols': 12},
                    'content': [
                        {
                            'component': 'VBtn',
                            'props': {
                                'color': 'primary',
                                'block': True,
                                'prepend-icon': 'mdi-send-check',
                                'text': '发送测试（模拟整理完成事件）',
                            },
                            'events': {
                                'click': {
                                    'api': f'plugin/ExportMsg/test?apikey={settings.API_TOKEN}',
                                    'method': 'get',
                                }
                            },
                        }
                    ]
                }
            ]
        })
        return [{
            'component': 'VCard',
            'props': {'title': '消息通知暴露'},
            'content': content,
        }]

    @eventmanager.register(EventType.NoticeMessage)
    def send(self, event: Event):
        """
        消息发送事件：转发非入库成功类通知
        """
        if not self.get_state():
            return

        if not event.event_data:
            return

        msg_body = event.event_data
        # 渠道
        channel = msg_body.get("channel")
        if channel:
            return
        # 类型
        msg_type: NotificationType = msg_body.get("type")
        # 入库成功推送由 TransferComplete 监听独占，这里跳过避免重复（仅成功类，失败类仍走通知）
        if self._enable_transfer and msg_type and msg_type.name == "Organize":
            ctype = msg_body.get("ctype")
            ctype_val = getattr(ctype, "value", ctype)
            if ctype_val == "organizeSuccess":
                return
        # 标题
        title = msg_body.get("title")
        # 文本
        text = msg_body.get("text")

        logger.info(f"这里触发了exportMsg")
        if not title and not text:
            logger.warn("标题和内容不能同时为空")
            return

        if (msg_type and self._msgtypes
                and msg_type.name not in self._msgtypes):
            logger.info(f"消息类型 {msg_type.value} 未开启消息发送")
            return

        event_info = {
            "url": self._url,
            "title": title,
            "content": text,
            "template": "txt",
            "channel": "wechat",
        }
        # 如果配置了topic，添加到请求参数
        if self._topic:
            event_info["topic"] = self._topic
        self.__forward(event_info)

    @eventmanager.register(EventType.TransferComplete)
    @eventmanager.register(EventType.AudioTransferComplete)
    def on_transfer_complete(self, event: Event):
        """
        整理完成事件：独占入库推送
        覆盖手动实时整理、目录未开启通知等通知监听覆盖不到的场景，
        并附带结构化媒体信息（标题/季集/年份/TMDB/目标路径等），适合弹幕采集等自动化处理
        """
        if not self.get_state() or not self._enable_transfer:
            return

        data = event.event_data
        if not data:
            return

        meta = data.get("meta")
        mediainfo = data.get("mediainfo")
        transferinfo = data.get("transferinfo")
        fileitem = data.get("fileitem")

        event_info = self.__build_transfer_event_info(meta, mediainfo, transferinfo, fileitem)
        logger.info(f"exportMsg整理完成事件触发：{event_info.get('title')}")
        self.__forward(event_info)

    def __build_transfer_event_info(self, meta, mediainfo, transferinfo, fileitem) -> dict:
        """根据整理完成事件的数据拼装推送内容（真实事件与测试推送共用）"""
        # ---- 提取媒体信息（兼容对象/字典两种结构） ----
        media_title = self.__attr(mediainfo, "title") or self.__attr(meta, "name") or ""
        original_title = self.__attr(mediainfo, "original_title") or ""
        year = self.__attr(mediainfo, "year") or self.__attr(meta, "year") or ""
        media_type = self.__attr(mediainfo, "type") or self.__attr(meta, "type")
        type_name = getattr(media_type, "value", media_type) if media_type else ""
        tmdb_id = self.__attr(mediainfo, "tmdb_id") or ""
        douban_id = self.__attr(mediainfo, "douban_id") or ""

        # 季集
        begin_season = self.__attr(meta, "begin_season")
        end_season = self.__attr(meta, "end_season")
        begin_episode = self.__attr(meta, "begin_episode")
        end_episode = self.__attr(meta, "end_episode")
        season_str = ""
        if begin_season not in (None, ""):
            season_str = f"S{int(begin_season):02d}"
            if end_season not in (None, "") and int(end_season) != int(begin_season):
                season_str += f"-S{int(end_season):02d}"
        episode_str = ""
        if begin_episode not in (None, ""):
            episode_str = f"E{int(begin_episode):02d}"
            if end_episode not in (None, "") and int(end_episode) != int(begin_episode):
                episode_str += f"-E{int(end_episode):02d}"

        # 目标路径与文件
        target_diritem = self.__attr(transferinfo, "target_diritem")
        target_path = self.__attr(target_diritem, "path") or self.__attr(transferinfo, "target_path") or ""
        file_name = self.__attr(fileitem, "name") or ""
        file_list_new = self.__attr(transferinfo, "file_list_new") or []

        # ---- 拼装标题与内容 ----
        name_parts = []
        if media_title:
            name_parts.append(str(media_title))
        if year:
            name_parts.append(f"({year})")
        if season_str:
            name_parts.append(season_str)
        if episode_str:
            name_parts.append(episode_str)
        display_name = " ".join(name_parts)
        title = f"{display_name} 整理入库" if display_name else "媒体整理入库"

        text_lines = []
        if original_title and str(original_title) != str(media_title):
            text_lines.append(f"原始标题：{original_title}")
        if type_name:
            text_lines.append(f"类型：{type_name}")
        if season_str or episode_str:
            text_lines.append(f"季集：{' '.join([x for x in [season_str, episode_str] if x])}")
        if tmdb_id:
            text_lines.append(f"TMDB：{tmdb_id}")
        if douban_id:
            text_lines.append(f"豆瓣：{douban_id}")
        if target_path:
            text_lines.append(f"目录：{target_path}")
        if file_name:
            text_lines.append(f"文件：{file_name}")
        text = "\n".join(text_lines)

        # ---- 推送（附结构化媒体信息，供弹幕采集等下游服务直接使用） ----
        event_info = {
            "url": self._url,
            "title": title,
            "content": text,
            "template": "txt",
            "channel": "wechat",
            "event": "transfer_complete",
            "media": {
                "title": str(media_title or ""),
                "original_title": str(original_title or ""),
                "year": str(year or ""),
                "type": str(type_name or ""),
                "tmdb_id": str(tmdb_id or ""),
                "douban_id": str(douban_id or ""),
                "season": int(begin_season) if begin_season not in (None, "") else None,
                "episode": int(begin_episode) if begin_episode not in (None, "") else None,
                "season_str": season_str,
                "episode_str": episode_str,
                "target_path": str(target_path or ""),
                "file_name": str(file_name or ""),
                "file_list": [str(f) for f in file_list_new[:20]],
            },
        }
        if self._topic:
            event_info["topic"] = self._topic
        return event_info

    @staticmethod
    def __attr(obj, name: str, default=None):
        """兼容对象/字典两种数据结构取属性"""
        if obj is None:
            return default
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)

    def __forward(self, event_info: dict) -> tuple:
        """推送到外部服务，返回 (是否成功, 结果说明)"""
        try:
            sc_url = self._url
            # 统一先按保存的 URL 发；命中 "plain HTTP on HTTPS port" 时自动把 scheme 升级为 https 重试一次
            logger.info(sc_url)
            logger.debug(f"准备发送事件到 {sc_url}，数据：{event_info}")
            res = RequestUtils(content_type="application/json").post_res(sc_url, json=event_info)

            # ---- 命中 "plain HTTP request was sent to HTTPS port" 则升级 https 重试 ----
            retry_url = None
            if res is not None and res.status_code == 400 and sc_url.lower().startswith("http://"):
                body = (res.text or "").lower()
                if "plain http" in body and "https port" in body:
                    retry_url = "https://" + sc_url[len("http://"):]
            if retry_url:
                logger.warn(f"检测到 {sc_url} 被 HTTPS 端口拒绝，自动升级为 {retry_url} 重试")
                res = RequestUtils(content_type="application/json").post_res(retry_url, json=event_info)
                if res is None:
                    return False, (f"http/https 都连不上下游：升级 https 后仍无响应，"
                                   f"请确认 {retry_url} 是否可达")
                if res.status_code != 200:
                    return False, (f"升级为 https 后依然失败（{res.status_code}），响应："
                                   f"{(res.text or '')[:200]}；请把插件配置里的接收地址直接改成 https:// 开头")
                sc_url = retry_url
                self._url = retry_url  # 顺手回写运行时地址，本次会话后续事件也走 https

            # 检查响应是否存在
            if res is None:
                logger.error("请求失败：未收到响应")
                return False, "请求失败：未收到响应（请检查地址是否可达）"
            # 记录详细日志
            logger.debug(f"响应状态码: {res.status_code}")
            logger.debug(f"响应头: {res.headers}")
            logger.debug(f"响应内容: {res.text}")

            # 检查HTTP状态码
            if res.status_code != 200:
                logger.error(f"请求失败，状态码：{res.status_code}")
                hint = ""
                body_lower = (res.text or "").lower()
                if "plain http" in body_lower and "https port" in body_lower:
                    hint = "；疑似 http 打到 HTTPS 端口，请把接收地址改成 https:// 开头"
                elif "handshake failure" in body_lower or "ssl" in body_lower and "error" in body_lower:
                    hint = "；可能是 https 证书问题，请换 http 或检查证书"
                elif res.status_code in (401, 403):
                    hint = "；下游拒绝访问，请检查鉴权"
                elif res.status_code == 404:
                    hint = "；路径不存在，请核对接收地址的接口路径"
                return False, f"请求失败，HTTP 状态码：{res.status_code}，响应：{(res.text or '')[:200]}{hint}"

            # 检查响应体是否为空
            if not res.text.strip():
                logger.error("响应内容为空")
                return False, "请求成功但响应内容为空（下游接口未返回 meta 信息）"

            # 安全解析JSON
            try:
                ret_json = res.json()
            except json.JSONDecodeError as e:
                logger.error(f"JSON解析失败：{e}，响应内容：{res.text[:200]}...")
                return False, f"响应不是合法 JSON：{(res.text or '')[:200]}"
            meta_ret = ret_json.get('meta') or {}
            code = meta_ret.get('status')
            msg = meta_ret.get('msg')
            if code == 200:
                logger.info("export消息发送成功")
                return True, "发送成功，下游返回 status=200"
            logger.warn(f"export消息发送，接口返回失败，错误码：{code}，错误原因：{msg}")
            return False, f"下游接口返回失败，错误码：{code}，原因：{msg}"
        except requests.exceptions.RequestException as msg_e:
            logger.error(f"未知错误，{str(msg_e)}", exc_info=True)
            return False, f"请求异常：{str(msg_e)}"

    def stop_service(self):
        """
        退出插件
        """
        pass
