import aiosmtplib
from email.mime.text import MIMEText
from jinja2 import Environment, FileSystemLoader
from typing import Dict
import asyncio
import logging
from functools import wraps
from src.core.config import SMTPConfig
from src.core.checker import Recipient
from src.notification.notification_base import NotificationBase
import webbrowser
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

#: 默认邮件模板名。收件人未指定模板时用它。
DEFAULT_TEMPLATE = "birthday.html"


def retry_on_failure(max_retries=3, delay=1, backoff=2):
    """重试装饰器 - 支持指数退避"""

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            last_exception = None
            current_delay = delay

            for attempt in range(max_retries):
                try:
                    return await func(*args, **kwargs)
                except Exception as e:
                    last_exception = e
                    if attempt < max_retries - 1:
                        logger.warning(
                            f"Attempt {attempt + 1}/{max_retries} failed: {type(e).__name__}: {e}. "
                            f"Retrying in {current_delay} seconds..."
                        )
                        await asyncio.sleep(current_delay)
                        current_delay *= backoff  # 指数退避
                    else:
                        logger.error(
                            f"All {max_retries} attempts failed. Last error: {type(e).__name__}: {e}"
                        )

            raise last_exception

        return wrapper

    return decorator


class EmailSender(NotificationBase):
    def __init__(self, smtp_config: SMTPConfig, templates_dir: str):
        self.smtp_config = smtp_config
        # autoescape=True：模板产出 HTML，变量里的特殊字符需要转义。
        self.env = Environment(loader=FileSystemLoader(templates_dir), autoescape=True)

    def render_content(self, name: str, template_file: str, extra_info: Dict) -> str:
        try:
            template = self.env.get_template(template_file or DEFAULT_TEMPLATE)
            return template.render(name=name, **extra_info)
        except Exception as e:
            logger.error(f"Failed to render template {template_file}: {e}")
            raise

    @retry_on_failure()
    async def send(self, recipient: Recipient, content: str, days_until: int, age: int):
        subject = f"生日提醒- {recipient.name} - {age}岁 - {days_until}天后"
        try:
            # 纯文本邮件用 MIMEText 直接构造，不要再套 MIMEMultipart：
            # 单部分正文没必要包成 multipart，多一层结构只会让反垃圾更敏感。
            message = MIMEText(content, "plain", "utf-8")
            message["From"] = self.smtp_config.username
            message["To"] = recipient.email
            message["Subject"] = subject

            async with aiosmtplib.SMTP(
                hostname=self.smtp_config.host,
                port=self.smtp_config.port,
                use_tls=self.smtp_config.use_tls,
            ) as smtp:
                await smtp.login(self.smtp_config.username, self.smtp_config.password)
                await smtp.send_message(message)
                logger.info(f"Successfully sent email to {recipient.email}")
        except Exception as e:
            logger.error(
                f"Failed to send email to {recipient.email}: {type(e).__name__}: {e}"
            )
            raise

    @staticmethod
    def preview_email(template: str = "birthday.html", web_open: bool = True):
        """用示例数据渲染模板并存成 .html 供查看。"""
        recipient = Recipient(
            name="测试用户",
            email="test@example.com",
            solar_birthday="1990-01-01",
            lunar_birthday="1989-12-05",
            reminder_days=3,
            template_file=template,
        )
        check_date = datetime.now()
        extra_info = {
            "solar_match": True,
            "lunar_match": False,
            "days_until": 0,
            "age": 34,
            "zodiac": "马",
            "gz_year": "庚午",
            "gz_month": "戊寅",
            "gz_day": "甲子",
            "gz_hour": "甲子",
            "lunar_month": "正月",
            "lunar_day": "十五",
            "lunar_festival": "元宵节",
            "solar_festival": "元旦",
            "solar_term": "立春",
            "week_name": "一",
            "constellation": "摩羯",
            "year": check_date.year,
            "month": check_date.month,
            "day": check_date.day,
        }
        env = Environment(loader=FileSystemLoader("templates"), autoescape=True)
        template_obj = env.get_template(template or DEFAULT_TEMPLATE)
        content = template_obj.render(name=recipient.name, **extra_info)

        preview_dir = Path("previews")
        preview_dir.mkdir(exist_ok=True)
        preview_file = (
            preview_dir
            / f"preview_{recipient.name}_{check_date.strftime('%Y%m%d')}.html"
        )
        with open(preview_file, "w", encoding="utf-8") as f:
            f.write(content)
        if web_open:
            webbrowser.open(f"file://{preview_file.absolute()}")
        print(f"预览文件已保存到: {preview_file}")
        return str(preview_file)
