from sqlalchemy import (
    create_engine,
    Column,
    Integer,
    String,
    Table,
    ForeignKey,
    Boolean,
    DateTime,
    text,  # 添加 text 导入
)
from sqlalchemy.orm import sessionmaker, declarative_base, relationship
from sqlalchemy.exc import IntegrityError
import hashlib
import json
import logging
import pathlib
from datetime import datetime, timedelta
from .config import DB_DIR, DB_URL
from config import (
    EMAIL_RETRY_BASE_DELAY_SECONDS,
    EMAIL_RETRY_MAX_DELAY_SECONDS,
)


def canonical_article_urls_json(article_urls):
    """排序后的 URL 列表 JSON，用于 outbox 去重。"""
    urls = sorted(set(u for u in article_urls if u))
    return json.dumps(urls, ensure_ascii=False, separators=(",", ":"))


def article_urls_hash(article_urls_json: str) -> str:
    return hashlib.sha256(article_urls_json.encode("utf-8")).hexdigest()


def compute_outbox_next_retry_at(attempts: int, from_time=None):
    """attempts 为已失败次数（>=1），返回下次重试时刻。"""
    if from_time is None:
        from_time = datetime.now()
    exp = min(max(attempts, 1), 8)
    delay = min(
        EMAIL_RETRY_BASE_DELAY_SECONDS * (2 ** (exp - 1)),
        EMAIL_RETRY_MAX_DELAY_SECONDS,
    )
    return from_time + timedelta(seconds=delay)

# 创建 Base 类
Base = declarative_base()

# 订阅者-平台关联表（多对多关系）
subscriber_platform = Table(
    "subscriber_platform",
    Base.metadata,
    Column(
        "subscriber_id", Integer, ForeignKey("email_subscribers.id"), primary_key=True
    ),
    Column("platform_id", Integer, ForeignKey("platforms.id"), primary_key=True),
)


class Platform(Base):
    """平台数据模型"""

    __tablename__ = "platforms"

    id = Column(Integer, primary_key=True)
    name = Column(String, unique=True)  # 平台名称唯一

    def __repr__(self):
        return f"Platform(id={self.id}, name='{self.name}')"


class EmailSubscriberDB(Base):
    """邮箱订阅者数据模型"""

    __tablename__ = "email_subscribers"

    id = Column(Integer, primary_key=True)
    email = Column(String, unique=True)  # 收件（目的）邮箱，唯一
    all_platforms = Column(Boolean, default=True)  # 是否订阅所有平台
    send_frequency = Column(Integer, default=24)  # 发送频率（小时），默认24小时
    last_email_sent_time = Column(DateTime, nullable=True)  # 投递窗口关闭时更新
    push_delivery_started_at = Column(
        DateTime, nullable=True
    )  # 当前投递窗口开始；NULL 表示未在投递中
    smtp_server = Column(String, nullable=True)
    smtp_email = Column(String, nullable=True)
    smtp_password = Column(String, nullable=True)

    # 与平台的多对多关系
    platforms = relationship(
        "Platform", secondary=subscriber_platform, backref="subscribers"
    )

    def __repr__(self):
        return f"EmailSubscriber(id={self.id}, email='{self.email}', all_platforms={self.all_platforms}, send_frequency={self.send_frequency}, last_sent={self.last_email_sent_time})"


class EmailArticlePending(Base):
    """待推送文章（爬取入库后入队，按订阅者频率发送，成功后再出队）。"""

    __tablename__ = "email_article_pending"

    id = Column(Integer, primary_key=True)
    subscriber_id = Column(Integer, ForeignKey("email_subscribers.id"), nullable=False)
    article_url = Column(String, nullable=False)
    platform = Column(String, nullable=False)
    created_at = Column(DateTime, default=datetime.now, nullable=False)

    subscriber = relationship("EmailSubscriberDB", backref="pending_articles")

    def __repr__(self):
        return (
            f"EmailArticlePending(subscriber_id={self.subscriber_id}, "
            f"url='{self.article_url}', platform='{self.platform}')"
        )


class EmailPushOutbox(Base):
    """待发 digest 队列（SMTP/外网失败时持久化，后台重试）。"""

    __tablename__ = "email_push_outbox"

    id = Column(Integer, primary_key=True)
    subscriber_id = Column(Integer, ForeignKey("email_subscribers.id"), nullable=False)
    platform = Column(String, nullable=False)
    article_urls = Column(String, nullable=False)
    urls_hash = Column(String, nullable=False)
    status = Column(String, default="pending", nullable=False)
    attempts = Column(Integer, default=0, nullable=False)
    next_retry_at = Column(DateTime, nullable=False)
    last_error = Column(String, nullable=True)
    created_at = Column(DateTime, default=datetime.now, nullable=False)

    subscriber = relationship("EmailSubscriberDB", backref="push_outbox_items")

    def __repr__(self):
        return (
            f"EmailPushOutbox(id={self.id}, subscriber_id={self.subscriber_id}, "
            f"platform='{self.platform}', attempts={self.attempts})"
        )


class EmailStats(Base):
    """邮件统计数据模型"""

    __tablename__ = "email_stats"

    id = Column(Integer, primary_key=True)
    total_emails_sent = Column(Integer, default=0)  # 总共发送的邮件数

    def __repr__(self):
        return f"EmailStats(id={self.id}, total_emails_sent={self.total_emails_sent})"


class EmailSubscriberManager:
    """邮箱订阅者管理类"""

    def __init__(self):
        """初始化数据库连接"""
        # 确保数据库目录存在
        DB_DIR.mkdir(parents=True, exist_ok=True)

        self.url = DB_URL
        self.engine = create_engine(DB_URL)
        print(f"数据库目录已存在: {DB_DIR.exists()}")
        print(f"连接数据库: {DB_URL}")

        # 创建或更新所有表
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        # 检查并升级数据库结构
        self._upgrade_database_structure()

        # 初始化平台数据
        self._init_platforms()

        # 检查数据库兼容性并处理老用户
        self._ensure_database_compatibility()

        print("数据库初始化完成")

    def _upgrade_database_structure(self):
        """升级数据库结构，添加缺失的字段"""
        try:
            # 检查 last_email_sent_time 字段是否存在
            with self.engine.connect() as conn:
                # 获取表信息
                result = conn.execute(text("PRAGMA table_info(email_subscribers)"))
                columns = [row[1] for row in result.fetchall()]

                if "last_email_sent_time" not in columns:
                    print("检测到数据库结构需要升级：添加 last_email_sent_time 字段")
                    # 添加缺失的字段
                    conn.execute(
                        text(
                            "ALTER TABLE email_subscribers ADD COLUMN last_email_sent_time DATETIME"
                        )
                    )
                    conn.commit()
                    print("数据库结构升级完成：已添加 last_email_sent_time 字段")
                    logging.info("数据库结构升级：添加了 last_email_sent_time 字段")
                else:
                    print("数据库结构检查完成：last_email_sent_time 字段已存在")

                for col_name, col_type in (
                    ("smtp_server", "TEXT"),
                    ("smtp_email", "TEXT"),
                    ("smtp_password", "TEXT"),
                ):
                    if col_name not in columns:
                        print(f"数据库升级：添加 {col_name} 字段")
                        conn.execute(
                            text(
                                f"ALTER TABLE email_subscribers ADD COLUMN {col_name} {col_type}"
                            )
                        )
                        conn.commit()
                        columns.append(col_name)

                if "push_delivery_started_at" not in columns:
                    print("数据库升级：添加 push_delivery_started_at 字段")
                    conn.execute(
                        text(
                            "ALTER TABLE email_subscribers ADD COLUMN push_delivery_started_at DATETIME"
                        )
                    )
                    conn.commit()
                    columns.append("push_delivery_started_at")

                conn.execute(
                    text(
                        """
                        CREATE TABLE IF NOT EXISTS email_article_pending (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            subscriber_id INTEGER NOT NULL,
                            article_url TEXT NOT NULL,
                            platform TEXT NOT NULL,
                            created_at DATETIME NOT NULL,
                            FOREIGN KEY(subscriber_id) REFERENCES email_subscribers(id),
                            UNIQUE(subscriber_id, article_url)
                        )
                        """
                    )
                )
                conn.commit()

                conn.execute(
                    text(
                        """
                        CREATE TABLE IF NOT EXISTS email_push_outbox (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            subscriber_id INTEGER NOT NULL,
                            platform TEXT NOT NULL,
                            article_urls TEXT NOT NULL,
                            urls_hash TEXT NOT NULL,
                            status TEXT NOT NULL DEFAULT 'pending',
                            attempts INTEGER NOT NULL DEFAULT 0,
                            next_retry_at DATETIME NOT NULL,
                            last_error TEXT,
                            created_at DATETIME NOT NULL,
                            FOREIGN KEY(subscriber_id) REFERENCES email_subscribers(id),
                            UNIQUE(subscriber_id, platform, urls_hash)
                        )
                        """
                    )
                )
                conn.commit()

        except Exception as e:
            print(f"数据库结构升级失败: {str(e)}")
            logging.error(f"数据库结构升级失败: {str(e)}")
            # 不抛出异常，继续执行其他初始化步骤

    def _ensure_database_compatibility(self):
        """确保数据库兼容性，处理版本升级前的用户数据"""
        session = self.get_session()
        try:
            # 检查是否有last_email_sent_time字段为NULL的老用户
            old_users_count = (
                session.query(EmailSubscriberDB)
                .filter(EmailSubscriberDB.last_email_sent_time.is_(None))
                .count()
            )

            if old_users_count > 0:
                print(
                    f"发现 {old_users_count} 个版本升级前的老用户（last_email_sent_time为NULL）"
                )
                print("这些用户将在下次推送时收到邮件（作为首次推送处理）")
                logging.info(f"数据库兼容性检查完成，发现 {old_users_count} 个老用户")
            else:
                print("所有用户数据都已是新版本格式")

        except Exception as e:
            logging.error(f"数据库兼容性检查失败: {str(e)}")
            print(f"数据库兼容性检查失败: {str(e)}")
        finally:
            session.close()

    def _init_platforms(self):
        """初始化平台数据"""
        session = self.get_session()
        try:
            # 检查是否已有平台数据
            platforms_count = session.query(Platform).count()
            print(f"当前平台数量: {platforms_count}")

            if platforms_count == 0:
                # 按拼音顺序添加平台
                platforms = [
                    "安全保卫中心",
                    "采购与招投标管理中心",
                    "餐饮服务中心",
                    "城市交通与物流学院",
                    "创意设计学院",
                    "大数据与互联网学院",
                    "党委宣传部",
                    "党委组织部（党委统战部）",
                    "党政办公室",
                    "分析测试中心",
                    "工程物理学院",
                    "工会",
                    "国际合作与交流部（港澳台工作办公室）",
                    "国有资产与实验室管理开发部",
                    "后勤保障部",
                    "健康与环境工程学院",
                    "教学质量督导室",
                    "教务部",
                    "计划财务部",
                    "集成电路与光电芯片学院",
                    "科研与校企合作部",
                    "马克思主义学院(人文社科学院)",
                    "人力资源部",
                    "商学院",
                    "体育场馆管理中心",
                    "体育与艺术学院",
                    "图书馆",
                    "外国语学院",
                    "未来技术学院",
                    "网络安全和信息化工作办公室",
                    "校团委",
                    "校医院",
                    "新材料与新能源学院",
                    "学生部",
                    "学生就业指导中心",
                    "药学院",
                    "研究生院",
                    "战略规划与发展办公室",
                    "中德智能制造学院",
                ]

                for name in platforms:
                    platform = Platform(name=name)
                    session.add(platform)
                    print(f"添加平台: {name}")

                session.commit()
                logging.info(f"初始化平台数据完成，添加了 {len(platforms)} 个平台")
                print(f"成功添加 {len(platforms)} 个平台")
            else:
                print(f"平台数据已存在，跳过初始化")
        except Exception as e:
            session.rollback()
            logging.error(f"初始化平台数据失败: {str(e)}")
            print(f"初始化平台数据失败: {str(e)}")
        finally:
            session.close()

    def get_session(self):
        """获取数据库会话"""
        return self.Session()

    def get_all_platforms(self):
        """获取所有平台"""
        session = self.get_session()
        try:
            return session.query(Platform).order_by(Platform.name).all()
        finally:
            session.close()

    def get_platform_id_by_name(self, platform_name):
        """根据平台名称获取平台ID"""
        session = self.get_session()
        try:
            platform = (
                session.query(Platform).filter(Platform.name == platform_name).first()
            )
            return platform.id if platform else None
        finally:
            session.close()

    def resolve_platform_id(self, platform_name):
        """精确或模糊匹配爬虫 source 与订阅平台名称。"""
        if not platform_name:
            return None

        name = platform_name.strip()
        pid = self.get_platform_id_by_name(name)
        if pid is not None:
            return pid

        def _norm(s: str) -> str:
            return (
                s.replace("（", "(")
                .replace("）", ")")
                .replace(" ", "")
                .strip()
            )

        target = _norm(name)
        session = self.get_session()
        try:
            for platform in session.query(Platform).all():
                if _norm(platform.name) == target:
                    return platform.id
                if name in platform.name or platform.name in name:
                    return platform.id
            return None
        finally:
            session.close()

    def add_subscriber(
        self,
        email,
        platform_ids=None,
        all_platforms=True,
        send_frequency=24,
        smtp_server=None,
        smtp_email=None,
        smtp_password=None,
    ):
        """添加订阅者到数据库"""
        session = self.get_session()
        try:
            # 验证发送频率
            try:
                send_frequency = int(send_frequency)
                if not (1 <= send_frequency <= 24):
                    send_frequency = 24
            except (ValueError, TypeError):
                send_frequency = 24

            # 打印调试信息
            print(
                f"尝试添加/更新订阅者: {email}, all_platforms={all_platforms}, platform_ids={platform_ids}, send_frequency={send_frequency}"
            )

            # 检查邮箱是否已存在
            existing = (
                session.query(EmailSubscriberDB)
                .filter(EmailSubscriberDB.email == email)
                .first()
            )

            is_new_subscriber = existing is None

            if existing:
                print(f"邮箱 {email} 已存在，更新订阅设置")
                # 如果已存在，更新订阅平台和频率
                existing.all_platforms = all_platforms
                existing.send_frequency = send_frequency
                if smtp_server:
                    existing.smtp_server = smtp_server
                if smtp_email:
                    existing.smtp_email = smtp_email
                if smtp_password:
                    existing.smtp_password = smtp_password

                if all_platforms:
                    # 全平台订阅，清空特定平台
                    existing.platforms = []
                elif platform_ids and len(platform_ids) > 0:
                    # 特定平台订阅
                    platforms = (
                        session.query(Platform)
                        .filter(Platform.id.in_(platform_ids))
                        .all()
                    )
                    print(f"找到 {len(platforms)} 个匹配的平台")
                    existing.platforms = platforms
                else:
                    # 没有选择任何平台，使用默认的全部平台
                    existing.all_platforms = True

                session.commit()
                logging.info(f"更新订阅者订阅平台: {email}")
                print(f"成功更新 {email} 的订阅设置")
                return True, is_new_subscriber
            else:
                # 创建新订阅者
                print(f"创建新订阅者: {email}")
                subscriber = EmailSubscriberDB(
                    email=email,
                    all_platforms=all_platforms,
                    send_frequency=send_frequency,
                    smtp_server=smtp_server,
                    smtp_email=smtp_email,
                    smtp_password=smtp_password,
                )

                if not all_platforms and platform_ids and len(platform_ids) > 0:
                    # 如果不是全平台订阅，设置特定平台
                    platforms = (
                        session.query(Platform)
                        .filter(Platform.id.in_(platform_ids))
                        .all()
                    )
                    print(f"为新订阅者设置 {len(platforms)} 个平台")
                    subscriber.platforms = platforms

                session.add(subscriber)
                session.commit()
                logging.info(f"添加订阅者成功: {email}")
                print(f"成功添加新订阅者: {email}")
                return True, is_new_subscriber
        except IntegrityError as e:
            session.rollback()
            logging.warning(f"订阅者邮箱已存在(并发添加): {email}, 错误: {str(e)}")
            print(f"数据库完整性错误: {str(e)}")
            return False, False
        except Exception as e:
            session.rollback()
            logging.error(f"添加订阅者失败: {str(e)}")
            print(f"添加订阅者失败: {str(e)}")
            return False, False
        finally:
            session.close()

    def delete_subscriber(self, email):
        """根据邮箱删除订阅者"""
        session = self.get_session()
        try:
            subscriber = (
                session.query(EmailSubscriberDB)
                .filter(EmailSubscriberDB.email == email)
                .first()
            )
            if not subscriber:
                logging.warning(f"订阅者不存在，邮箱: {email}")
                return False

            session.delete(subscriber)
            session.commit()
            logging.info(f"删除订阅者成功，邮箱: {email}")
            return True
        except Exception as e:
            session.rollback()
            logging.error(f"删除订阅者失败: {str(e)}")
            return False
        finally:
            session.close()

    def get_all_subscribers(self):
        """获取所有订阅者"""
        session = self.get_session()
        try:
            # 使用joinedload预加载platforms关系
            from sqlalchemy.orm import joinedload

            return (
                session.query(EmailSubscriberDB)
                .options(joinedload(EmailSubscriberDB.platforms))
                .all()
            )
        finally:
            session.close()

    def get_subscriber_by_email(self, email):
        """根据邮箱获取订阅者信息"""
        session = self.get_session()
        try:
            from sqlalchemy.orm import joinedload

            return (
                session.query(EmailSubscriberDB)
                .options(joinedload(EmailSubscriberDB.platforms))
                .filter(EmailSubscriberDB.email == email)
                .first()
            )
        finally:
            session.close()

    def get_subscribers_by_frequency(self, frequency_hours):
        """根据发送频率获取订阅者"""
        session = self.get_session()
        try:
            from sqlalchemy.orm import joinedload

            return (
                session.query(EmailSubscriberDB)
                .options(joinedload(EmailSubscriberDB.platforms))
                .filter(EmailSubscriberDB.send_frequency == frequency_hours)
                .all()
            )
        finally:
            session.close()

    def get_stats(self):
        """获取统计数据"""
        session = self.get_session()
        try:
            stats = session.query(EmailStats).first()
            subscriber_count = session.query(EmailSubscriberDB).count()

            if not stats:
                stats = EmailStats(total_emails_sent=0)
                session.add(stats)
                session.commit()

            return {
                "subscriber_count": subscriber_count,
                "total_emails_sent": stats.total_emails_sent,
            }
        finally:
            session.close()

    def increment_emails_sent(self, count=1):
        """增加已发送邮件计数"""
        session = self.get_session()
        try:
            stats = session.query(EmailStats).first()
            if not stats:
                stats = EmailStats(total_emails_sent=count)
                session.add(stats)
            else:
                stats.total_emails_sent += count

            session.commit()
            return stats.total_emails_sent
        except Exception as e:
            session.rollback()
            logging.error(f"更新邮件统计失败: {str(e)}")
            return 0
        finally:
            session.close()

    def get_subscribers_due_for_email(self, current_time=None):
        """获取应该接收邮件的订阅者（基于上次发送时间和频率）"""
        if current_time is None:
            current_time = datetime.now()

        session = self.get_session()
        try:
            from sqlalchemy.orm import joinedload

            # 查询条件：
            # 1. 从未发送过邮件的用户（last_email_sent_time为NULL） - 包括版本升级前的老用户
            # 2. 距离上次发送时间已超过设定频率的用户
            subscribers = []

            # 获取所有订阅者
            all_subscribers = (
                session.query(EmailSubscriberDB)
                .options(joinedload(EmailSubscriberDB.platforms))
                .all()
            )

            for subscriber in all_subscribers:
                should_send = False

                if subscriber.last_email_sent_time is None:
                    # 从未发送过邮件（包括版本升级前的老用户）
                    should_send = True
                    logging.debug(
                        f"用户 {subscriber.email} 首次推送或版本升级前用户，将发送邮件"
                    )
                else:
                    # 计算距离上次发送的时间（小时）
                    time_diff = current_time - subscriber.last_email_sent_time
                    hours_since_last = time_diff.total_seconds() / 3600

                    # 检查是否达到发送频率
                    if hours_since_last >= subscriber.send_frequency:
                        should_send = True
                        logging.debug(
                            f"用户 {subscriber.email} 距离上次发送已过 {hours_since_last:.1f} 小时，"
                            f"设定频率 {subscriber.send_frequency} 小时，将发送邮件"
                        )
                    else:
                        logging.debug(
                            f"用户 {subscriber.email} 距离上次发送仅 {hours_since_last:.1f} 小时，"
                            f"设定频率 {subscriber.send_frequency} 小时，暂不发送"
                        )

                if should_send:
                    subscribers.append(subscriber)

            logging.info(
                f"共找到 {len(subscribers)} 个需要推送的用户（总用户数: {len(all_subscribers)}）"
            )
            return subscribers

        finally:
            session.close()

    def update_last_email_sent_time(self, subscriber_id, sent_time=None):
        """更新订阅者的最后邮件发送时间"""
        if sent_time is None:
            sent_time = datetime.now()

        session = self.get_session()
        try:
            subscriber = (
                session.query(EmailSubscriberDB)
                .filter(EmailSubscriberDB.id == subscriber_id)
                .first()
            )

            if subscriber:
                old_time = subscriber.last_email_sent_time
                subscriber.last_email_sent_time = sent_time
                session.commit()

                if old_time is None:
                    logging.info(
                        f"首次更新订阅者 {subscriber.email} 的发送时间为 {sent_time}（版本升级兼容）"
                    )
                else:
                    logging.info(
                        f"更新订阅者 {subscriber.email} 的发送时间：{old_time} -> {sent_time}"
                    )
                return True
            else:
                logging.warning(f"未找到ID为 {subscriber_id} 的订阅者")
                return False

        except Exception as e:
            session.rollback()
            logging.error(f"更新发送时间失败: {str(e)}")
            return False
        finally:
            session.close()

    def batch_update_last_email_sent_time(self, subscriber_ids, sent_time=None):
        """批量更新订阅者的最后邮件发送时间"""
        if sent_time is None:
            sent_time = datetime.now()

        session = self.get_session()
        try:
            updated_count = (
                session.query(EmailSubscriberDB)
                .filter(EmailSubscriberDB.id.in_(subscriber_ids))
                .update({"last_email_sent_time": sent_time}, synchronize_session=False)
            )

            session.commit()
            logging.info(f"批量更新了 {updated_count} 个订阅者的发送时间为 {sent_time}")
            return updated_count

        except Exception as e:
            session.rollback()
            logging.error(f"批量更新发送时间失败: {str(e)}")
            return 0
        finally:
            session.close()

    def get_subscriber_by_id(self, subscriber_id):
        session = self.get_session()
        try:
            return (
                session.query(EmailSubscriberDB)
                .filter(EmailSubscriberDB.id == subscriber_id)
                .first()
            )
        finally:
            session.close()

    def enqueue_article_pending(self, subscriber_id, article_url, platform):
        """入队；已存在则返回 False。"""
        session = self.get_session()
        try:
            exists = (
                session.query(EmailArticlePending)
                .filter(
                    EmailArticlePending.subscriber_id == subscriber_id,
                    EmailArticlePending.article_url == article_url,
                )
                .first()
            )
            if exists:
                return False
            row = EmailArticlePending(
                subscriber_id=subscriber_id,
                article_url=article_url,
                platform=(platform or "").strip(),
                created_at=datetime.now(),
            )
            session.add(row)
            session.commit()
            return True
        except IntegrityError:
            session.rollback()
            return False
        except Exception as e:
            session.rollback()
            logging.error("enqueue_article_pending 失败: %s", e)
            return False
        finally:
            session.close()

    def list_pending_for_subscriber(self, subscriber_id):
        session = self.get_session()
        try:
            rows = (
                session.query(EmailArticlePending)
                .filter(EmailArticlePending.subscriber_id == subscriber_id)
                .order_by(EmailArticlePending.created_at.asc())
                .all()
            )
            for row in rows:
                session.expunge(row)
            return rows
        finally:
            session.close()

    def remove_pending_articles(self, subscriber_id, article_urls):
        if not article_urls:
            return 0
        session = self.get_session()
        try:
            n = (
                session.query(EmailArticlePending)
                .filter(
                    EmailArticlePending.subscriber_id == subscriber_id,
                    EmailArticlePending.article_url.in_(list(article_urls)),
                )
                .delete(synchronize_session=False)
            )
            session.commit()
            return n
        except Exception as e:
            session.rollback()
            logging.error("remove_pending_articles 失败: %s", e)
            return 0
        finally:
            session.close()

    def count_pending_articles(self):
        session = self.get_session()
        try:
            return session.query(EmailArticlePending).count()
        finally:
            session.close()

    def clear_article_pending(self):
        session = self.get_session()
        try:
            n = session.query(EmailArticlePending).delete(synchronize_session=False)
            session.commit()
            return n
        except Exception as e:
            session.rollback()
            logging.error("clear_article_pending 失败: %s", e)
            return 0
        finally:
            session.close()

    def list_subscriber_ids_with_pending(self):
        session = self.get_session()
        try:
            rows = (
                session.query(EmailArticlePending.subscriber_id)
                .distinct()
                .all()
            )
            return [r[0] for r in rows]
        finally:
            session.close()

    def count_users_in_delivery_window(self):
        session = self.get_session()
        try:
            return (
                session.query(EmailSubscriberDB)
                .filter(EmailSubscriberDB.push_delivery_started_at.isnot(None))
                .count()
            )
        finally:
            session.close()

    def start_delivery_window(self, subscriber_id, started_at=None):
        if started_at is None:
            started_at = datetime.now()
        session = self.get_session()
        try:
            sub = (
                session.query(EmailSubscriberDB)
                .filter(EmailSubscriberDB.id == subscriber_id)
                .first()
            )
            if not sub:
                return False
            sub.push_delivery_started_at = started_at
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logging.error("start_delivery_window 失败: %s", e)
            return False
        finally:
            session.close()

    def close_delivery_window(self, subscriber_id, closed_at=None):
        """关闭投递窗口并更新 last_email_sent_time。"""
        if closed_at is None:
            closed_at = datetime.now()
        session = self.get_session()
        try:
            sub = (
                session.query(EmailSubscriberDB)
                .filter(EmailSubscriberDB.id == subscriber_id)
                .first()
            )
            if not sub:
                return False
            sub.push_delivery_started_at = None
            sub.last_email_sent_time = closed_at
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logging.error("close_delivery_window 失败: %s", e)
            return False
        finally:
            session.close()

    def get_subscriber_for_push(self, subscriber_id):
        session = self.get_session()
        try:
            from sqlalchemy.orm import joinedload

            sub = (
                session.query(EmailSubscriberDB)
                .options(joinedload(EmailSubscriberDB.platforms))
                .filter(EmailSubscriberDB.id == subscriber_id)
                .first()
            )
            if sub:
                session.expunge(sub)
            return sub
        finally:
            session.close()

    def has_outbox_for_urls(self, subscriber_id, platform, article_urls):
        urls_json = canonical_article_urls_json(article_urls)
        urls_hash = article_urls_hash(urls_json)
        session = self.get_session()
        try:
            return (
                session.query(EmailPushOutbox)
                .filter(
                    EmailPushOutbox.subscriber_id == subscriber_id,
                    EmailPushOutbox.platform == platform,
                    EmailPushOutbox.urls_hash == urls_hash,
                    EmailPushOutbox.status == "pending",
                )
                .first()
                is not None
            )
        finally:
            session.close()

    def upsert_push_outbox(
        self, subscriber_id, platform, article_urls, error=None, increment_attempt=True
    ):
        urls_json = canonical_article_urls_json(article_urls)
        urls_hash = article_urls_hash(urls_json)
        err_text = (error or "SMTP send failed")[:500]
        session = self.get_session()
        try:
            row = (
                session.query(EmailPushOutbox)
                .filter(
                    EmailPushOutbox.subscriber_id == subscriber_id,
                    EmailPushOutbox.platform == platform,
                    EmailPushOutbox.urls_hash == urls_hash,
                )
                .first()
            )
            now = datetime.now()
            if row:
                if increment_attempt:
                    row.attempts = (row.attempts or 0) + 1
                row.status = "pending"
                row.last_error = err_text
                row.next_retry_at = compute_outbox_next_retry_at(row.attempts, now)
                row.article_urls = urls_json
            else:
                attempts = 1 if increment_attempt else 0
                row = EmailPushOutbox(
                    subscriber_id=subscriber_id,
                    platform=platform,
                    article_urls=urls_json,
                    urls_hash=urls_hash,
                    status="pending",
                    attempts=attempts,
                    next_retry_at=compute_outbox_next_retry_at(attempts, now),
                    last_error=err_text,
                    created_at=now,
                )
                session.add(row)
            session.commit()
            return True
        except Exception as e:
            session.rollback()
            logging.error("upsert_push_outbox 失败: %s", e)
            return False
        finally:
            session.close()

    def delete_push_outbox(self, subscriber_id, platform, article_urls):
        urls_json = canonical_article_urls_json(article_urls)
        urls_hash = article_urls_hash(urls_json)
        session = self.get_session()
        try:
            deleted = (
                session.query(EmailPushOutbox)
                .filter(
                    EmailPushOutbox.subscriber_id == subscriber_id,
                    EmailPushOutbox.platform == platform,
                    EmailPushOutbox.urls_hash == urls_hash,
                )
                .delete(synchronize_session=False)
            )
            session.commit()
            return deleted
        except Exception as e:
            session.rollback()
            logging.error("delete_push_outbox 失败: %s", e)
            return 0
        finally:
            session.close()

    def list_due_outbox(self, limit=10, now=None):
        if now is None:
            now = datetime.now()
        session = self.get_session()
        try:
            rows = (
                session.query(EmailPushOutbox)
                .filter(
                    EmailPushOutbox.status == "pending",
                    EmailPushOutbox.next_retry_at <= now,
                )
                .order_by(EmailPushOutbox.created_at.asc())
                .limit(limit)
                .all()
            )
            for row in rows:
                session.expunge(row)
            return rows
        finally:
            session.close()

    def count_pending_outbox(self):
        session = self.get_session()
        try:
            return (
                session.query(EmailPushOutbox)
                .filter(EmailPushOutbox.status == "pending")
                .count()
            )
        finally:
            session.close()

    def delete_outbox_by_id(self, outbox_id):
        session = self.get_session()
        try:
            n = (
                session.query(EmailPushOutbox)
                .filter(EmailPushOutbox.id == outbox_id)
                .delete(synchronize_session=False)
            )
            session.commit()
            return n
        except Exception as e:
            session.rollback()
            logging.error("delete_outbox_by_id 失败: %s", e)
            return 0
        finally:
            session.close()

    def clear_push_outbox(self):
        session = self.get_session()
        try:
            n = session.query(EmailPushOutbox).delete(synchronize_session=False)
            session.commit()
            return n
        except Exception as e:
            session.rollback()
            logging.error("clear_push_outbox 失败: %s", e)
            return 0
        finally:
            session.close()
