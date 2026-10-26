"""
开发测试：从已爬取文章随机发送富 HTML 邮件。

示例:
  python send_test_email.py --to you@qq.com --smtp-server smtp.qq.com --smtp-email you@qq.com --smtp-password AUTH_CODE --mode article
  python send_test_email.py --to you@qq.com --subscriber-email you@qq.com --mode digest --limit 3
"""

import argparse
import random
import sys

from email_content import build_platform_digest
from email_subscriber.subscriber_manager import SubscriberService
from email_subscriber.smtp_client import send_email
from official_document_crawler.crawler.database import Article, DatabaseManager


def _pick_random_article(session, seed=None):
    rng = random.Random(seed)
    rows = (
        session.query(Article)
        .filter(Article.content.isnot(None), Article.content != "")
        .all()
    )
    if not rows:
        return None
    return rng.choice(rows)


def _pick_random_department_articles(session, limit, seed=None):
    rng = random.Random(seed)
    sources = (
        session.query(Article.source)
        .filter(Article.content.isnot(None), Article.content != "")
        .distinct()
        .all()
    )
    sources = [s[0] for s in sources if s[0]]
    if not sources:
        return None, []
    platform = rng.choice(sources)
    articles = (
        session.query(Article)
        .filter(Article.source == platform, Article.content.isnot(None))
        .order_by(Article.id.desc())
        .limit(limit)
        .all()
    )
    return platform, articles


def _resolve_smtp(args, service):
    if args.subscriber_email:
        sub = service.db_manager.get_subscriber_by_email(args.subscriber_email.strip())
        if not sub or not sub.smtp_server or not sub.smtp_email or not sub.smtp_password:
            print("错误: 数据库中该订阅者未配置完整 SMTP，请先通过订阅页保存")
            return None
        return sub.smtp_server, sub.smtp_email, sub.smtp_password

    if not args.smtp_server or not args.smtp_email or not args.smtp_password:
        print(
            "错误: 请提供 --smtp-server / --smtp-email / --smtp-password，"
            "或使用 --subscriber-email 从数据库读取"
        )
        return None
    return args.smtp_server.strip(), args.smtp_email.strip(), args.smtp_password


def main():
    parser = argparse.ArgumentParser(description="随机发送测试邮件（富 HTML digest）")
    parser.add_argument("--to", required=True, help="收件人邮箱")
    parser.add_argument(
        "--mode",
        choices=("article", "digest"),
        default="article",
        help="article=随机单篇；digest=随机部门聚合",
    )
    parser.add_argument("--limit", type=int, default=5, help="digest 模式每部门最多篇数")
    parser.add_argument("--seed", type=int, default=None, help="随机种子")
    parser.add_argument("--smtp-server", help="SMTP 服务器")
    parser.add_argument("--smtp-email", help="发件邮箱")
    parser.add_argument("--smtp-password", help="发件授权码")
    parser.add_argument(
        "--subscriber-email",
        help="从 subscribers 库读取该收件邮箱对应的发件 SMTP",
    )
    args = parser.parse_args()

    service = SubscriberService()
    smtp = _resolve_smtp(args, service)
    if not smtp:
        return 1
    smtp_server, smtp_email, smtp_password = smtp

    db = DatabaseManager()
    session = db.get_session()
    try:
        if args.mode == "article":
            article = _pick_random_article(session, args.seed)
            if not article:
                print("错误: 数据库中没有已解析正文的文章，请先运行爬虫")
                return 1
            platform = article.source or "未知部门"
            articles = [article]
            print(f"选中文章 id={article.id} 部门={platform} 标题={article.title}")
        else:
            platform, articles = _pick_random_department_articles(
                session, args.limit, args.seed
            )
            if not articles:
                print("错误: 无可用文章")
                return 1
            print(
                f"选中部门={platform} 文章数={len(articles)} "
                f"ids={[a.id for a in articles]}"
            )

        digest = build_platform_digest(platform, articles)
        attach_bytes = sum(len(a.data) for a in digest.file_attachments)
        print(
            f"构建完成: 主题={digest.subject} "
            f"内嵌图={len(digest.inline_images)} 附件={len(digest.file_attachments)} "
            f"附件总大小={attach_bytes} bytes"
        )

        ok, err = send_email(
            smtp_server,
            smtp_email,
            smtp_password,
            [args.to],
            digest.subject,
            digest.html,
            is_html=True,
            inline_images=digest.inline_images,
            file_attachments=digest.file_attachments,
        )
        if ok:
            print(f"已发送至 {args.to}")
            return 0
        print(f"发送失败: {err}")
        return 1
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
