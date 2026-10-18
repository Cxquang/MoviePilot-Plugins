import json
from typing import Any, List, Dict, Tuple

import requests.exceptions

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
    plugin_version = "1.05"
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
        pass

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
        pass

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

        logger.info(f"exportMsg整理完成事件触发：{display_name or file_name}")

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
        self.__forward(event_info)

    @staticmethod
    def __attr(obj, name: str, default=None):
        """兼容对象/字典两种数据结构取属性"""
        if obj is None:
            return default
        if isinstance(obj, dict):
            return obj.get(name, default)
        return getattr(obj, name, default)

    def __forward(self, event_info: dict):
        """推送到外部服务（原请求与响应处理逻辑）"""
        try:
            # 构建url
            sc_url = self._url
            logger.info(sc_url)

            # 调试日志
            logger.debug(f"准备发送事件到 {self._url}，数据：{event_info}")
            res = RequestUtils(content_type="application/json").post_res(sc_url, json=event_info)

            # 检查响应是否存在
            if res is None:
                logger.error("请求失败：未收到响应")
                return
            # 记录详细日志
            logger.debug(f"响应状态码: {res.status_code}")
            logger.debug(f"响应头: {res.headers}")
            logger.debug(f"响应内容: {res.text}")

            # 检查HTTP状态码
            if res.status_code != 200:
                logger.error(f"请求失败，状态码：{res.status_code}")
                return

            # 检查响应体是否为空
            if not res.text.strip():
                logger.error("响应内容为空")
                return

            # 安全解析JSON
            try:
                ret_json = res.json()
            except json.JSONDecodeError as e:
                logger.error(f"JSON解析失败：{e}，响应内容：{res.text[:200]}...")
                return
            meta_ret = ret_json.get('meta') or {}
            code = meta_ret.get('status')
            msg = meta_ret.get('msg')
            if code == 200:
                logger.info("export消息发送成功")
            else:
                logger.warn(f"export消息发送，接口返回失败，错误码：{code}，错误原因：{msg}")
        except requests.exceptions.RequestException as msg_e:
            logger.error(f"未知错误，{str(msg_e)}", exc_info=True)

    def stop_service(self):
        """
        退出插件
        """
        pass
