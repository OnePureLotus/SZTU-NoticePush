"""
清空 SZTU-NoticePush 本地 SQLite 数据。

典型场景：服务启动后立刻爬了一轮，此时还没订阅用户，库里已有文章但不会再被当作
「新文章」推送。在 Web 上完成订阅后，先停止 server.py，再清空文章库并重启，
下一轮爬取会重新入库并尝试发邮件（订阅者库默认保留）。

用法:
  python clear_database.py              # 仅清空文章库（默认）
  python clear_database.py --articles -y
  python clear_database.py --all -y     # 文章 + 订阅者全部清空
  python clear_database.py --reset-push-times -y  # 只重置推送时间，不清文章
  python clear_database.py --clear-push-outbox -y  # 清空 SMTP 失败待发队列
  python clear_database.py --clear-push-pending -y  # 清空待推送文章队列
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config import ARTICLES_DATABASE_URI, SUBSCRIBERS_DATABASE_URI  # noqa: E402


def sqlite_path(uri: str) -> Path:
    prefix = "sqlite:///"
    if not uri.startswith(prefix):
        raise ValueError(f"不支持的数据库 URI: {uri}")
    return Path(uri[len(prefix) :])


def delete_db_file(path: Path) -> bool:
    if not path.exists():
        print(f"  跳过（文件不存在）: {path}")
        return False
    try:
        path.unlink()
        print(f"  已删除: {path}")
        return True
    except PermissionError:
        print(f"  失败: 无法删除 {path}，请先停止正在运行的 server.py")
        return False


def clear_push_outbox() -> None:
    from email_subscriber.subscriberDB import EmailSubscriberManager

    manager = EmailSubscriberManager()
    n = manager.clear_push_outbox()
    print(f"  已清空 email_push_outbox，删除 {n} 条")


def clear_push_pending() -> None:
    from email_subscriber.subscriberDB import EmailSubscriberManager

    manager = EmailSubscriberManager()
    n = manager.clear_article_pending()
    print(f"  已清空 email_article_pending，删除 {n} 条")


def reset_subscriber_push_times() -> None:
    from email_subscriber.subscriberDB import EmailSubscriberDB, EmailSubscriberManager

    manager = EmailSubscriberManager()
    session = manager.get_session()
    try:
        updated = (
            session.query(EmailSubscriberDB)
            .filter(EmailSubscriberDB.last_email_sent_time.isnot(None))
            .update({EmailSubscriberDB.last_email_sent_time: None}, synchronize_session=False)
        )
        cleared_windows = (
            session.query(EmailSubscriberDB)
            .filter(EmailSubscriberDB.push_delivery_started_at.isnot(None))
            .update(
                {EmailSubscriberDB.push_delivery_started_at: None},
                synchronize_session=False,
            )
        )
        session.commit()
        print(
            f"  已重置 {updated} 个订阅者的 last_email_sent_time，"
            f"清除 {cleared_windows} 个投递窗口"
        )
    finally:
        session.close()


def confirm(prompt: str) -> bool:
    try:
        answer = input(f"{prompt} [y/N]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in ("y", "yes")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="清空 SZTU-NoticePush 本地数据库（运行前请先停止 Web 服务）"
    )
    parser.add_argument(
        "--articles",
        action="store_true",
        help="删除 articles.sqlite3（下次爬取会重新当作新文章）",
    )
    parser.add_argument(
        "--subscribers",
        action="store_true",
        help="删除 subscribers.sqlite3（订阅者、平台列表会随库删除；重启服务会重建平台）",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="等同于 --articles --subscribers",
    )
    parser.add_argument(
        "--reset-push-times",
        action="store_true",
        help="保留文章与订阅者，仅将订阅者上次推送时间清零",
    )
    parser.add_argument(
        "--clear-push-outbox",
        action="store_true",
        help="清空 SMTP 失败待发队列（email_push_outbox），不删文章与订阅者",
    )
    parser.add_argument(
        "--clear-push-pending",
        action="store_true",
        help="清空待推送文章队列（email_article_pending），不删文章与订阅者",
    )
    parser.add_argument("-y", "--yes", action="store_true", help="跳过确认")
    args = parser.parse_args()

    if args.all:
        args.articles = True
        args.subscribers = True

    if not any(
        [
            args.articles,
            args.subscribers,
            args.reset_push_times,
            args.clear_push_outbox,
            args.clear_push_pending,
        ]
    ):
        args.articles = True

    articles_path = sqlite_path(ARTICLES_DATABASE_URI)
    subscribers_path = sqlite_path(SUBSCRIBERS_DATABASE_URI)

    actions = []
    if args.articles:
        actions.append(f"删除文章库: {articles_path}")
    if args.subscribers:
        actions.append(f"删除订阅库: {subscribers_path}")
    if args.reset_push_times:
        actions.append("重置所有订阅者的推送时间")
    if args.clear_push_outbox:
        actions.append("清空 email_push_outbox 待发邮件队列")
    if args.clear_push_pending:
        actions.append("清空 email_article_pending 待推送队列")

    print("将执行以下操作:")
    for line in actions:
        print(f"  • {line}")

    if not args.yes and not confirm("确认继续?"):
        print("已取消")
        return 1

    ok = True
    if args.articles:
        ok = delete_db_file(articles_path) and ok
    if args.subscribers:
        ok = delete_db_file(subscribers_path) and ok
    if args.reset_push_times:
        if subscribers_path.exists():
            reset_subscriber_push_times()
        else:
            print("  跳过重置推送时间：订阅库不存在")
    if args.clear_push_outbox:
        if subscribers_path.exists():
            clear_push_outbox()
        else:
            print("  跳过清空 outbox：订阅库不存在")
    if args.clear_push_pending:
        if subscribers_path.exists():
            clear_push_pending()
        else:
            print("  跳过清空待推队列：订阅库不存在")

    if ok:
        print("\n完成。建议流程: 启动 server.py → 若已订阅则等待定时爬取，或重启以触发首轮爬取。")
        if args.articles and not args.subscribers:
            print(
                "提示: 仅清空文章库时订阅者会保留；重启服务后首轮爬取会重新入库并尝试推送。"
            )
        return 0

    print("\n部分操作失败，请确认服务已停止后重试。")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
