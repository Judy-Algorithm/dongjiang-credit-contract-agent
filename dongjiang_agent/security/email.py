"""SMTP delivery for security-sensitive account messages."""

from __future__ import annotations

import os
import smtplib
import ssl
from email.message import EmailMessage


def _enabled(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


class SecurityEmailSender:
    def __init__(self) -> None:
        self.host = os.getenv("DONGJIANG_SMTP_HOST", "").strip()
        self.use_ssl = _enabled("DONGJIANG_SMTP_USE_SSL", True)
        default_port = 465 if self.use_ssl else 587
        self.port = int(os.getenv("DONGJIANG_SMTP_PORT", str(default_port)))
        self.username = os.getenv("DONGJIANG_SMTP_USERNAME", "").strip()
        self.password = os.getenv("DONGJIANG_SMTP_PASSWORD", "")
        self.sender = (
            os.getenv("DONGJIANG_SMTP_FROM", "").strip() or self.username
        )
        self.starttls = _enabled("DONGJIANG_SMTP_STARTTLS", not self.use_ssl)

    @property
    def configured(self) -> bool:
        return bool(self.host and self.sender)

    def send_password_reset_code(self, recipient: str, code: str) -> None:
        if not self.configured:
            raise RuntimeError("邮件服务尚未配置，请联系管理员重置密码。")
        message = EmailMessage()
        message["Subject"] = "东江信审平台密码重置验证码"
        message["From"] = self.sender
        message["To"] = recipient
        message.set_content(
            "您好：\n\n"
            f"您的密码重置验证码是：{code}\n\n"
            "验证码 10 分钟内有效且只能使用一次。"
            "如果不是您本人操作，请忽略此邮件。\n\n"
            "东江信审与合同评审平台"
        )
        context = ssl.create_default_context()
        try:
            if self.use_ssl:
                with smtplib.SMTP_SSL(
                    self.host, self.port, timeout=15, context=context
                ) as client:
                    if self.username:
                        client.login(self.username, self.password)
                    client.send_message(message)
                return
            with smtplib.SMTP(self.host, self.port, timeout=15) as client:
                client.ehlo()
                if self.starttls:
                    client.starttls(context=context)
                    client.ehlo()
                if self.username:
                    client.login(self.username, self.password)
                client.send_message(message)
        except (OSError, smtplib.SMTPException) as exc:
            raise RuntimeError("验证码邮件发送失败，请稍后重试或联系管理员。") from exc
