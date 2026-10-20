"""
主程序入口
"""

import asyncio
import logging
import sys
from typing import List, Tuple, Dict
import click

from src.core.config_manager import ConfigManager
from src.core.notification_factory import NotificationFactory
from src.core.checker import BirthdayChecker, Recipient

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("birthday_reminder.log"),
    ],
)
logger = logging.getLogger(__name__)


class BirthdayReminder:
    def __init__(self, config_path: str = None):
        # 初始化配置管理器
        self.config_manager = ConfigManager(config_path)

        # 验证配置
        if not self.config_manager.validate_config():
            raise ValueError("Invalid configuration")

        # 获取配置
        self.config = self.config_manager.config

        # 初始化组件
        self._initialize_components()

    def _initialize_components(self):
        """初始化组件 - 简单直接"""
        try:
            # 创建生日检查器
            self.birthday_checker = BirthdayChecker()

            # 创建通知发送器
            notification_factory = NotificationFactory(self.config_manager.get_templates_dir())
            self.notification_senders = notification_factory.create_senders(self.config)

            logger.info("Components initialized successfully")

        except Exception as e:
            logger.error(f"Failed to initialize components: {e}")
            raise

    async def send_birthday_reminder(self, recipient: Recipient, extra_info: Dict) -> None:
        """发送生日提醒"""
        try:
            logger.info(f"Sending birthday reminder to {recipient.name}")

            for sender in self.notification_senders:
                try:
                    # 渲染内容
                    content = sender.render_content(
                        name=recipient.name,
                        template_file=recipient.template_file,
                        extra_info=extra_info,
                    )

                    # 发送通知
                    await sender.send(
                        recipient=recipient,
                        content=content,
                        days_until=extra_info["days_until"],
                        age=extra_info["age"],
                    )

                    logger.info(f"Successfully sent {type(sender).__name__} notification to {recipient.name}")

                except Exception as e:
                    logger.error(f"Failed to send {type(sender).__name__} notification to {recipient.name}: {e}")
                    # 继续尝试其他发送器，不中断整个流程

        except Exception as e:
            logger.error(f"Failed to send birthday reminder to {recipient.name}: {e}")
            raise

    def check_birthdays(self) -> List[Tuple[Recipient, bool, Dict]]:
        """检查所有人的生日"""
        try:
            logger.info(f"Checking birthdays for {len(self.config.recipients)} recipients")
            results = self.birthday_checker.check_birthdays(self.config.recipients)

            # 统计结果
            birthday_count = sum(1 for _, is_birthday, _ in results if is_birthday)
            logger.info(f"Found {birthday_count} birthdays today")

            return results

        except Exception as e:
            logger.error(f"Failed to check birthdays: {e}")
            raise

    async def run(self) -> int:
        """运行生日提醒主流程，返回**尝试发送的提醒条数**。

        返回值给调用方（网页里的每日定时器）记录日志用 ——
        "今天发了 0 条"和"今天发了 2 条"在排查时是完全不同的信息。
        """
        try:
            logger.info("Starting birthday reminder application")

            # 检查生日
            birthday_results = self.check_birthdays()

            # 收集需要发送提醒的任务
            tasks = []
            for recipient, is_birthday, extra_info in birthday_results:
                if is_birthday:
                    logger.info(f"Processing birthday for {recipient.name}")
                    tasks.append(self.send_birthday_reminder(recipient, extra_info))

            # 并发发送所有提醒
            if tasks:
                # return_exceptions=True：一个人失败不该连累其他人
                results = await asyncio.gather(*tasks, return_exceptions=True)
                for res in results:
                    if isinstance(res, Exception):
                        logger.error(f"一条提醒发送失败: {type(res).__name__}: {res}")
                logger.info(f"Successfully processed {len(tasks)} birthday reminders")
                return len(tasks)

            logger.info("No birthdays to process today")
            return 0

        except Exception as e:
            logger.error(f"Application error: {type(e).__name__}: {e}")
            raise

    def reload_config(self) -> None:
        """重新加载配置"""
        try:
            # 重新加载配置
            self.config = self.config_manager.reload()

            # 重新初始化组件
            self._initialize_components()

            logger.info("Configuration reloaded successfully")

        except Exception as e:
            logger.error(f"Failed to reload configuration: {e}")
            raise


@click.group()
def cli():
    """生日提醒系统 - 简洁版本"""
    pass


@cli.command()
@click.option('--config', '-c', help='配置文件路径', default="config.yml")
def run(config):
    """运行生日提醒主流程"""
    try:
        app = BirthdayReminder(config)
        asyncio.run(app.run())
    except Exception as e:
        logger.error(f"Application failed: {e}")
        sys.exit(1)


@cli.command()
@click.option('--config', '-c', help='配置文件路径', default="config.yml")
def preview():
    """预览生日提醒邮件内容（默认模板）"""
    try:
        from src.notification.sender_email import EmailSender
        EmailSender.preview_email(web_open=True)
        print("已生成预览文件并尝试打开浏览器。")
    except Exception as e:
        logger.error(f"Preview failed: {e}")
        sys.exit(1)


@cli.command()
@click.option('--config', '-c', help='配置文件路径', default="config.yml")
def validate(config):
    """验证配置文件"""
    try:
        config_manager = ConfigManager(config)
        if config_manager.validate_config():
            print("✅ 配置文件验证通过")
        else:
            print("❌ 配置文件验证失败")
            sys.exit(1)
    except Exception as e:
        print(f"❌ 配置文件验证失败: {e}")
        sys.exit(1)


@cli.command()
@click.option('--config', '-c', help='配置文件路径', default="config.yml")
def info(config):
    """显示应用信息"""
    try:
        config_manager = ConfigManager(config)
        config = config_manager.config

        print("📋 应用信息:")
        print(f"  检查生日人数: {len(config.recipients)}")
        print(f"  通知类型: {', '.join(config.notification_types)}")
        print(f"  模板目录: {config_manager.get_templates_dir()}")

        if config.smtp_config:
            print(f"  SMTP服务器: {config.smtp_config.host}:{config.smtp_config.port}")

        if config.serverchan_config:
            print("  ServerChan: 已配置")

    except Exception as e:
        logger.error(f"Failed to get app info: {e}")
        sys.exit(1)


@cli.command()
@click.option('--config', '-c', help='配置文件路径', default="config.yml")
@click.option('--host', help='监听地址', default="127.0.0.1")
@click.option('--port', '-p', help='监听端口', default=8000, type=int)
@click.option('--reload', help='代码改动后自动重载（开发用）', is_flag=True, default=False)
@click.option('--token', help='访问令牌；不指定则每次启动随机生成', default=None)
@click.option('--no-auth', 'no_auth', help='关闭访问令牌鉴权（仅本机自用）', is_flag=True, default=False)
@click.option('--auto-send/--no-auto-send', 'auto_send',
              help='在网页进程里每天自动发送一次（默认开启）', default=True)
@click.option('--send-time', help='每天发送的时刻，24 小时制 HH:MM，默认 09:00',
              default="09:00")
def web(config, host, port, reload, token, no_auth, auto_send, send_time):
    """启动管理台（浏览器界面）

    **默认每天 09:00 自动发送一次提醒**（--auto-send）。因为容器只跑这一个
    进程，定时器不挂在它身上就没有别的地方可挂 —— 不开启的话配置再对也
    永远不会自动发出提醒。用 --no-auto-send 关掉（例如另有 cron 在跑）。
    """
    try:
        import uvicorn
        from src.web.app import create_app
    except ImportError as e:
        logger.error(f"启动管理台需要额外依赖: {e}")
        print("请先运行: uv sync")
        sys.exit(1)

    from src.web.auth import access_url, resolve_token

    # 默认总是启用鉴权：这个界面能改配置、能触发真实发信，不该对任何能连到端口的人开放。
    auth_token = None if no_auth else resolve_token(token)

    send_hour, send_minute = 9, 0
    if auto_send:
        try:
            hh, mm = (send_time or "09:00").split(":")
            send_hour, send_minute = int(hh), int(mm)
            if not (0 <= send_hour <= 23 and 0 <= send_minute <= 59):
                raise ValueError
        except (ValueError, AttributeError):
            print(f"❌ --send-time 格式不对：{send_time!r}，要写成 09:00 这样")
            sys.exit(1)

    try:
        app = create_app(
            config,
            token=auth_token,
            auto_send=auto_send,
            send_hour=send_hour,
            send_minute=send_minute,
        )
    except Exception as e:
        logger.error(f"Failed to start web UI: {e}")
        print(f"❌ 启动失败: {e}")
        sys.exit(1)

    if auth_token:
        url = access_url(host, port, auth_token)
        # 同时走 stdout 与 logger：前者给人看，后者进 birthday_reminder.log，
        # 容器里 docker logs 能看到，重启后还能翻出来。
        print("=" * 68)
        print(f"管理台已启动: {url}")
        print("上面这个地址带访问令牌，打开一次即可（浏览器会记住 30 天）。")
        print("令牌也可用 --token 或环境变量 BIRTHDAYRS_TOKEN 固定，重启后书签不失效。")
        print("=" * 68)
        logger.info("管理台已启动: %s", url)
    else:
        print(f"管理台已启动: http://{host}:{port} （鉴权已关闭）")
        logger.warning("管理台已启动且未启用鉴权，请勿对公网开放")

    if auto_send:
        print(f"每天 {send_hour:02d}:{send_minute:02d} 自动发送提醒（--no-auto-send 可关闭）。")
    else:
        print("自动发送已关闭（--auto-send 可开启）。")
    print("按 Ctrl+C 停止。")
    uvicorn.run(app, host=host, port=port, reload=reload, log_level="info")


@cli.command()
@click.option('--config', '-c', help='配置文件路径', default="config.yml")
def app(config):
    """启动桌面应用（原生窗口，无端口）"""
    try:
        from src.desktop.app import run
    except ImportError as e:
        logger.error(f"启动桌面应用需要额外依赖: {e}")
        print("请先运行: uv sync")
        sys.exit(1)

    sys.exit(run(config))


if __name__ == "__main__":
    cli()
