"""SMTP 连接、验证与发送（按订阅者凭据）。"""

import logging
import smtplib
from email import encoders
from email.header import Header
from email.mime.base import MIMEBase
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from config import SMTP_PORT

logger = logging.getLogger(__name__)


def build_mime_message(
    subject,
    content,
    sender_email,
    receivers,
    is_html=False,
    inline_images=None,
    file_attachments=None,
):
    inline_images = inline_images or []
    file_attachments = file_attachments or []

    message = MIMEMultipart("mixed")
    message["From"] = sender_email
    message["To"] = ";".join(receivers)
    message["Subject"] = Header(subject, "utf-8")

    if is_html:
        related = MIMEMultipart("related")
        related.attach(MIMEText(content, "html", "utf-8"))
        for img in inline_images:
            mime_img = MIMEImage(img.data, _subtype=img.mime_subtype)
            mime_img.add_header("Content-ID", f"<{img.cid}>")
            mime_img.add_header("Content-Disposition", "inline", filename=img.cid)
            related.attach(mime_img)
        message.attach(related)
    else:
        message.attach(MIMEText(content, "plain", "utf-8"))

    for attachment in file_attachments:
        if "/" in attachment.mime_type:
            maintype, subtype = attachment.mime_type.split("/", 1)
        else:
            maintype, subtype = "application", "octet-stream"
        part = MIMEBase(maintype, subtype)
        part.set_payload(attachment.data)
        encoders.encode_base64(part)
        part.add_header(
            "Content-Disposition",
            "attachment",
            filename=("utf-8", "", attachment.filename),
        )
        message.attach(part)

    return message


def verify_smtp_login(smtp_server, smtp_email, smtp_password, port=None):
    """仅验证 SMTP 登录是否成功。"""
    port = port or SMTP_PORT
    if not smtp_server or not smtp_email or not smtp_password:
        return False, "SMTP 服务器、发件邮箱和密码不能为空"
    try:
        server = smtplib.SMTP(smtp_server, port, timeout=30)
        server.starttls()
        server.login(smtp_email, smtp_password)
        server.quit()
        return True, ""
    except Exception as e:
        logger.warning("SMTP 登录失败: %s", e)
        return False, str(e)


def send_email(
    smtp_server,
    smtp_email,
    smtp_password,
    receivers,
    subject,
    content,
    is_html=False,
    inline_images=None,
    file_attachments=None,
    port=None,
):
    """使用指定凭据发送邮件。成功返回 True。"""
    port = port or SMTP_PORT
    if not receivers:
        return False, "无收件人"

    try:
        message = build_mime_message(
            subject=subject,
            content=content,
            sender_email=smtp_email,
            receivers=receivers,
            is_html=is_html,
            inline_images=inline_images,
            file_attachments=file_attachments,
        )
        server = smtplib.SMTP(smtp_server, port, timeout=60)
        server.starttls()
        server.login(smtp_email, smtp_password)
        server.sendmail(smtp_email, receivers, message.as_string())
        server.quit()
        return True, ""
    except Exception as e:
        logger.error("发送邮件失败: %s", e)
        return False, str(e)


def send_smtp_verification_test(smtp_server, smtp_email, smtp_password, recipient_email):
    """登录并向收件邮箱发送简短测试邮件。"""
    ok, err = verify_smtp_login(smtp_server, smtp_email, smtp_password)
    if not ok:
        return False, f"SMTP 登录失败: {err}"

    html = f"""
    <html><body style="font-family:sans-serif;padding:16px;">
    <p>这是一封 <strong>SZTU-NoticePush</strong> 的 SMTP 验证邮件。</p>
    <p>收件邮箱：<code>{recipient_email}</code></p>
    <p>若您能收到本邮件，说明发件设置有效，订阅将保存。</p>
    </body></html>
    """
    ok, err = send_email(
        smtp_server,
        smtp_email,
        smtp_password,
        [recipient_email],
        subject="【SZTU-NoticePush】发件邮箱验证",
        content=html,
        is_html=True,
    )
    if not ok:
        return False, f"测试邮件发送失败: {err}"
    return True, ""
