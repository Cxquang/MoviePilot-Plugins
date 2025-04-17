from typing import Any, List, Dict, Tuple

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
    plugin_version = "1.01"
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

    def init_plugin(self, config: dict = None):
        if config:
            self._enabled = config.get("enabled")
            self._url = config.get("url")
            self._msgtypes = config.get("msgtypes") or []
            self._topic = config.get("topic")  # 新增topic字段

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
                                            'text': ''
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
            'msgtypes': []
        }

    def get_page(self) -> List[dict]:
        pass

    @eventmanager.register(EventType.NoticeMessage)
    def send(self, event: Event):
        """
        消息发送事件
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

        # try:
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
            # 构建url
            sc_url = self._url
            res = RequestUtils(content_type="application/json").post_res(sc_url, json=event_info)
        #     if res:
        #         ret_json = res.json()
        #         logger.info(ret_json)
        #         code = ret_json.get('meta').get('status')
        #         msg = ret_json.get('meta').get('msg')
        #         if code == 200:
        #             logger.info("export消息发送成功")
        #         else:
        #             logger.warn(f"export消息发送，接口返回失败，错误码：{code}，错误原因：{msg}")
        #     elif res is not None:
        #         logger.warn(f"export消息发送失败，错误码：{res.status_code}，错误原因：{res.reason}")
        #     else:
        #         logger.warn("export消息发送失败，未获取到返回信息")
        # except Exception as msg_e:
        #     logger.error(f"export消息发送异常，{str(msg_e)}")

    def stop_service(self):
        """
        退出插件
        """
        pass
