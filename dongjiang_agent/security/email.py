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

    def send_registration_code(self, recipient: str, code: str) -> None:
        self._send_code(
            recipient,
            subject="东江信审平台注册验证码",
            heading="注册邮箱验证码",
            code=code,
        )

    def send_password_reset_code(self, recipient: str, code: str) -> None:
        self._send_code(
            recipient,
            subject="东江信审平台密码重置验证码",
            heading="密码重置验证码",
            code=code,
        )

    def send_registration_review(
        self, recipient: str, *, approved: bool, username: str
    ) -> None:
        title = "注册申请已通过" if approved else "注册申请未通过"
        body = (
            f"您好：\n\n您的账号 {username} 已通过管理员审核，现在可以登录东江信审与合同评审平台。"
            if approved
            else f"您好：\n\n您的账号 {username} 暂未通过管理员审核，如有疑问请联系管理员。"
        )
        self._send(recipient, title, body)

    def send_notification(
        self, recipient: str, *, title: str, body: str, link: str = ""
    ) -> None:
        public_url = os.getenv("DONGJIANG_PUBLIC_URL", "").strip().rstrip("/")
        target = f"{public_url}{link}" if public_url and link.startswith("/") else ""
        link_text = f"\n\n处理链接：{target}" if target else ""
        self._send(
            recipient,
            f"东江信审平台通知：{title}",
            f"您好：\n\n{body}{link_text}\n\n东江信审与合同评审平台",
        )

    def _send_code(
        self, recipient: str, *, subject: str, heading: str, code: str
    ) -> None:
        self._send(
            recipient,
            subject,
            "您好：\n\n"
            f"您的{heading}是：{code}\n\n"
            "验证码 10 分钟内有效且只能使用一次。"
            "如果不是您本人操作，请忽略此邮件。\n\n"
            "东江信审与合同评审平台",
        )

    def _send(self, recipient: str, subject: str, body: str) -> None:
        if not self.configured:
            raise RuntimeError("邮件服务尚未配置，请联系管理员。")
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.sender
        message["To"] = recipient
        message.set_content(body)
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
