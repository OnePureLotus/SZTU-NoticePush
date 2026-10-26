"""
批量/单封运维邮件脚本（需通过命令行提供 SMTP 凭据）。

示例:
  python email_test.py --smtp-server smtp.qq.com --smtp-email you@qq.com --smtp-password CODE --single test@qq.com
"""

import argparse
import sys
import time

from email_subscriber.smtp_client import send_email
from email_subscriber.subscriberDB import EmailSubscriberManager


def generate_apology_email():
    html_content = """
    <html>
    <head>
        <style>
            body {
                font-family: 'PingFang SC', 'Helvetica Neue', Helvetica, Arial, sans-serif;
                background-color: #f5f5f5;
                color: #333;
                padding: 20px;
                max-width: 600px;
                margin: 0 auto;
            }
            .container {
                background-color: white;
                border-radius: 10px;
                box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                padding: 25px;
            }
        </style>
    </head>
    <body>
        <div class="container">
            <h2>系统维护通知</h2>
            <p>尊敬的用户：系统已更新，如需继续接收通知请重新访问订阅页面完成配置。</p>
        </div>
    </body>
    </html>
    """
    return html_content


def send_to_all_subscribers(smtp_server, smtp_email, smtp_password, confirm=False):
    db_manager = EmailSubscriberManager()
    subscribers = db_manager.get_all_subscribers()

    if not subscribers:
        print("数据库中没有订阅者")
        return

    total = len(subscribers)
    print(f"总共找到 {total} 个订阅者")

    if not confirm:
        response = input(f"确认使用指定 SMTP 发送给全部 {total} 位订阅者? (y/n): ")
        if response.lower() != "y":
            print("操作已取消")
            return

    content = generate_apology_email()
    subject = "【重要】公文通订阅系统维护通知"

    success = 0
    failure = 0

    for idx, subscriber in enumerate(subscribers, 1):
        print(f"[{idx}/{total}] 正在处理: {subscriber.email}")
        ok, err = send_email(
            smtp_server,
            smtp_email,
            smtp_password,
            [subscriber.email],
            subject,
            content,
            is_html=True,
        )
        if ok:
            success += 1
        else:
            failure += 1
            print(f"失败: {err}")

        if idx < total:
            time.sleep(1)

    print(f"成功: {success} 失败: {failure} 总计: {total}")


def main():
    parser = argparse.ArgumentParser(description="发送运维通知邮件")
    parser.add_argument("--smtp-server", required=True)
    parser.add_argument("--smtp-email", required=True)
    parser.add_argument("--smtp-password", required=True)
    parser.add_argument(
        "--force", "-f", action="store_true", help="批量发送时不二次确认"
    )
    parser.add_argument(
        "--single", "-s", type=str, help="向单个邮箱发送测试，而非全部订阅者"
    )
    args = parser.parse_args()

    content = generate_apology_email()
    subject = "【重要】公文通订阅系统维护通知"

    if args.single:
        ok, err = send_email(
            args.smtp_server,
            args.smtp_email,
            args.smtp_password,
            [args.single],
            subject,
            content,
            is_html=True,
        )
        if ok:
            print(f"测试邮件已发送至 {args.single}")
            return 0
        print(f"发送失败: {err}")
        return 1

    send_to_all_subscribers(
        args.smtp_server,
        args.smtp_email,
        args.smtp_password,
        confirm=args.force,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n用户中断操作")
        sys.exit(0)
