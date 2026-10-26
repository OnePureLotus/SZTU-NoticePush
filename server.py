from flask import Flask, jsonify, request
import argparse
import os
import pathlib
import threading
import time
import schedule
from datetime import datetime, date, timedelta

# 使用新的统一配置
from config import (
    ARTICLES_DATABASE_URI as DATABASE_URI,
    SCHEDULED_CRAWL_HOURS,
    ABNORMAL_LOG_FILE,
    PUSH_POLL_SECONDS,
)

# 直接引用official_document_crawler中的模块
from official_document_crawler.crawler.database import Base, Article, DatabaseManager
from official_document_crawler.main_crawler import main_crawler

# 导入邮件订阅相关模块
from email_subscriber.subscriber_manager import SubscriberService
from email_subscriber.push_retry_worker import run_push_retry_loop
from email_subscriber.push_pipeline import enqueue_from_urls
from email_subscriber.push_worker import (
    run_push_worker_loop,
    get_last_push_cycle_stats,
)
from ops_abnormal_log import log_abnormal

ROOT_PATH = pathlib.Path(__file__).parent.resolve()
STATIC_FOLDER = str(ROOT_PATH / "static")

app = Flask(__name__, static_folder=STATIC_FOLDER, static_url_path="")

# 使用DatabaseManager而非直接创建引擎和会话
db_manager = DatabaseManager()
# print(db_manager.url)

# 初始化邮件订阅服务
subscriber_service = SubscriberService()

push_retry_thread_started = False
push_worker_thread_started = False

# 自动爬虫状态（日志仍输出到运行 server.py 的同一终端）
crawl_running = threading.Event()
crawler_automation_enabled = False
crawler_automation_lock = threading.Lock()
crawl_status = {
    "automation_started_at": None,
    "last_started_at": None,
    "last_finished_at": None,
    "last_new_count": 0,
    "last_error": None,
}


def enqueue_new_articles_from_crawl(new_urls):
    """生产者：将本轮新文章写入各订阅者待推队列（发信由推送消费者线程处理）。"""
    try:
        urls = new_urls or []
        if not urls:
            return 0
        enqueued = enqueue_from_urls(subscriber_service, db_manager, urls)
        print(
            f"[{datetime.now()}] 📥 入队完成：新文章 {len(urls)} 条，"
            f"新增队列项 {enqueued}，待推总计 "
            f"{subscriber_service.db_manager.count_pending_articles()}"
        )
        return enqueued
    except Exception as e:
        log_abnormal("push", "爬取入队异常", e)
        print(f"[{datetime.now()}] ❌ 入队失败: {str(e)}")
        return 0


def send_new_articles_email_by_individual_frequency(new_urls):
    """兼容旧调用名：仅入队，不触发同步发信。"""
    enqueue_new_articles_from_crawl(new_urls or [])


def run_automated_crawler_loop():
    """
    在独立线程中运行 schedule 循环；crawl_task 内的 print/logging 仍写入启动服务的终端。
    首次点击启动后立即爬取一轮，之后按 SCHEDULED_CRAWL_HOURS 重复。
    """
    interval_min = int(SCHEDULED_CRAWL_HOURS * 60)
    print(
        f"[{datetime.now()}] 自动爬虫已启动：立即执行首轮爬取，"
        f"之后每 {SCHEDULED_CRAWL_HOURS} 小时（约 {interval_min} 分钟）执行一次"
    )
    crawl_task()

    schedule.clear()
    schedule.every(SCHEDULED_CRAWL_HOURS).hours.do(crawl_task)

    while True:
        schedule.run_pending()
        time.sleep(1)


def start_automated_crawler():
    """启动后台自动爬虫线程；若已在运行则返回 (False, message)。"""
    global crawler_automation_enabled
    with crawler_automation_lock:
        if crawler_automation_enabled:
            return (
                False,
                f"自动爬虫已在运行（每 {SCHEDULED_CRAWL_HOURS} 小时一轮）",
            )
        crawler_automation_enabled = True
        crawl_status["automation_started_at"] = datetime.now().isoformat()

    threading.Thread(target=run_automated_crawler_loop, daemon=True).start()
    return (
        True,
        (
            f"已启动自动爬虫：立即开始首轮抓取，之后每约 "
            f"{int(SCHEDULED_CRAWL_HOURS * 60)} 分钟重复；日志见运行服务的终端"
        ),
    )


@app.route("/api/start_crawl", methods=["POST"])
def api_start_crawl():
    ok, message = start_automated_crawler()
    if not ok:
        return jsonify({"success": False, "message": message}), 409
    return jsonify({"success": True, "message": message})


@app.route("/api/crawl_status", methods=["GET"])
def api_crawl_status():
    return jsonify(
        {
            "success": True,
            "automation_enabled": crawler_automation_enabled,
            "automation_started_at": crawl_status["automation_started_at"],
            "interval_hours": SCHEDULED_CRAWL_HOURS,
            "running": crawl_running.is_set(),
            "last_started_at": crawl_status["last_started_at"],
            "last_finished_at": crawl_status["last_finished_at"],
            "last_new_count": crawl_status["last_new_count"],
            "last_error": crawl_status["last_error"],
            "push_poll_seconds": PUSH_POLL_SECONDS,
            "push_worker": get_last_push_cycle_stats(),
        }
    )


# 新增API - 获取所有平台
@app.route("/api/get_platforms", methods=["GET"])
def get_platforms():
    try:
        platforms = subscriber_service.get_all_platforms()
        return jsonify({"success": True, "platforms": platforms})
    except Exception as e:
        print(f"❌ 获取平台列表错误: {str(e)}")
        return jsonify({"success": False, "message": f"服务器错误: {str(e)}"}), 500


# 新增API - 获取用户已订阅平台
@app.route("/api/get_subscriber_platforms", methods=["POST"])
def get_subscriber_platforms():
    try:
        data = request.get_json()
        email = data.get("email", "").strip()

        if not email:
            return jsonify({"success": False, "message": "邮箱不能为空"}), 400

        subscription_info = subscriber_service.get_subscriber_platforms(email)
        if subscription_info is None:
            return jsonify({"success": False, "message": "未找到该邮箱的订阅信息"})

        return jsonify({"success": True, "subscription": subscription_info})

    except Exception as e:
        print(f"❌ 获取订阅平台错误: {str(e)}")
        return jsonify({"success": False, "message": f"服务器错误: {str(e)}"}), 500


# 邮箱订阅相关API
@app.route("/api/subscribe", methods=["POST"])
def subscribe():
    try:
        data = request.get_json()
        email = data.get("email", "").strip()
        all_platforms = data.get("all_platforms", True)
        platform_ids = data.get("platform_ids", [])
        send_frequency = data.get("send_frequency", 24)
        smtp_server = (data.get("smtp_server") or "").strip()
        smtp_email = (data.get("smtp_email") or "").strip()
        smtp_password = data.get("smtp_password")
        if smtp_password is None:
            smtp_password = ""

        print(
            f"📝 收到订阅请求: email={email}, all_platforms={all_platforms}, "
            f"platform_ids={platform_ids}, send_frequency={send_frequency}, "
            f"smtp_server={smtp_server}, smtp_email={smtp_email}"
        )

        if not email:
            return jsonify({"success": False, "message": "邮箱不能为空"}), 400

        if not smtp_server or not smtp_email:
            return (
                jsonify(
                    {
                        "success": False,
                        "message": "请填写 SMTP 服务器与发件邮箱",
                    }
                ),
                400,
            )

        existing_sub = subscriber_service.db_manager.get_subscriber_by_email(email)
        if existing_sub is None and not smtp_password:
            return (
                jsonify(
                    {
                        "success": False,
                        "message": "新订阅请填写发件密码/授权码",
                    }
                ),
                400,
            )

        # 验证发送频率
        try:
            send_frequency = int(send_frequency)
            if not (1 <= send_frequency <= 24):
                return (
                    jsonify(
                        {"success": False, "message": "发送频率必须在1-24小时之间"}
                    ),
                    400,
                )
        except (ValueError, TypeError):
            return jsonify({"success": False, "message": "发送频率格式无效"}), 400

        # 转换平台ID为整数
        if platform_ids and isinstance(platform_ids, list):
            try:
                platform_ids = [int(pid) for pid in platform_ids]
                print(f"转换后的平台ID: {platform_ids}")
            except (ValueError, TypeError) as e:
                print(f"❌ 平台ID转换错误: {str(e)}")
                platform_ids = []

        # 如果没有选择任何平台但选择了"选择特定平台"，返回错误
        if not all_platforms and not platform_ids:
            print("⚠️ 未选择任何平台，但设置了非全部平台订阅")
            return jsonify({"success": False, "message": "请至少选择一个平台"}), 400

        success, message, is_new_subscriber = subscriber_service.add_subscriber(
            email,
            platform_ids,
            all_platforms,
            send_frequency,
            smtp_server=smtp_server,
            smtp_email=smtp_email,
            smtp_password=smtp_password,
        )

        if not success:
            status = 400 if "SMTP" in message or "发件" in message or "授权码" in message else 200
            return jsonify({"success": False, "message": message}), status

        if success:
            # 获取平台名称列表（用于发送订阅确认邮件）
            platform_names = []
            if not all_platforms and platform_ids:
                # 查询平台名称
                platform_objects = []
                db_session = subscriber_service.db_manager.get_session()
                try:
                    from email_subscriber.subscriberDB import Platform

                    platform_objects = (
                        db_session.query(Platform)
                        .filter(Platform.id.in_(platform_ids))
                        .all()
                    )
                    platform_names = [p.name for p in platform_objects]
                except Exception as e:
                    print(f"❌ 获取平台名称失败: {str(e)}")
                finally:
                    db_session.close()

            # 只有新订阅者才发送确认邮件
            if is_new_subscriber:
                sub = subscriber_service.db_manager.get_subscriber_by_email(email)
                send_subscription_confirmation(
                    email,
                    all_platforms,
                    platform_names,
                    send_frequency,
                    smtp_server=sub.smtp_server if sub else smtp_server,
                    smtp_email=sub.smtp_email if sub else smtp_email,
                    smtp_password=sub.smtp_password if sub else smtp_password,
                )
                print(f"✅ 新用户 {email} 订阅成功")
            else:
                print(f"✅ 用户 {email} 更新订阅设置成功")

        return jsonify({"success": success, "message": message})

    except Exception as e:
        print(f"❌ 订阅处理错误: {str(e)}")
        import traceback

        traceback.print_exc()  # 打印完整堆栈信息
        return jsonify({"success": False, "message": f"服务器错误: {str(e)}"}), 500


@app.route("/api/unsubscribe", methods=["POST"])
def unsubscribe():
    try:
        data = request.get_json()
        email = data.get("email", "").strip()

        if not email:
            return jsonify({"success": False, "message": "邮箱不能为空"}), 400

        success, message = subscriber_service.delete_subscriber(email)
        return jsonify({"success": success, "message": message})

    except Exception as e:
        print(f"❌ 退订处理错误: {str(e)}")
        return jsonify({"success": False, "message": f"服务器错误: {str(e)}"}), 500


@app.route("/api/get_data")
def get_data():
    try:
        # 接收分页参数
        page = request.args.get("page", 1, type=int)
        per_page = request.args.get("per_page", 10, type=int)

        # 使用database.py中的会话管理
        session = db_manager.get_session()

        # 查询总数
        total_count = session.query(Article).count()

        # 分页查询文章 - 确保按日期和时间正确排序
        articles = (
            session.query(Article)
            .order_by(Article.date.desc(), Article.detail_time.desc())
            .limit(per_page)
            .offset((page - 1) * per_page)
            .all()
        )

        print(f"📊 查询到 {len(articles)} 篇文章")
        result = []

        # 转换为字典列表，保持与前端接口兼容
        for article in articles:
            item = {
                "title": article.title,
                "source": article.source,
                "detail_time": (
                    article.date + " " + article.detail_time
                    if article.date and article.detail_time
                    else ""
                ),
                "click_num": article.click_num,
                "url": article.url,
                "fujians": article.fujians,
                "content": article.content,  # 添加内容字段
            }
            result.append(item)

        session.close()

        # 返回带有分页信息的结果
        response_data = {
            "data": result,
            "pagination": {
                "total": total_count,
                "page": page,
                "per_page": per_page,
                "total_pages": (total_count + per_page - 1) // per_page,
            },
        }

        return jsonify(response_data)

    except Exception as e:
        print(f"❌ Error: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/api/get_today_data")
def get_today_data():
    try:
        # 获取今天的日期
        today = date.today().strftime("%Y-%m-%d")
        print(f"📅 当前日期: {today}, 正在查询此日期的文章")

        # 使用database.py中的会话管理
        session = db_manager.get_session()

        # 查询当天的文章（使用更精确的过滤）
        articles = (
            session.query(Article)
            .filter(Article.date == today)
            .order_by(Article.detail_time.desc())
            .all()
        )

        # 打印所有日期以便调试
        all_dates = set(article.date for article in session.query(Article).all())
        print(f"🗓️ 数据库中的所有日期: {all_dates}")

        # 如果没有找到文章，尝试查找最近的文章
        if not articles:
            print(f"⚠️ 未找到今日({today})文章，尝试查找最新文章")
            articles = (
                session.query(Article)
                .order_by(Article.date.desc(), Article.detail_time.desc())
                .limit(10)
                .all()
            )
            if articles:
                latest_date = articles[0].date
                print(f"📝 使用最新日期: {latest_date} 的文章")
                articles = (
                    session.query(Article)
                    .filter(Article.date == latest_date)
                    .order_by(Article.detail_time.desc())
                    .all()
                )

        print(f"📄 查询到 {len(articles)} 篇今日文章")
        result = []

        # 转换为字典列表
        for article in articles:
            item = {
                "title": article.title,
                "source": article.source,
                "detail_time": (
                    article.date + " " + article.detail_time
                    if article.date and article.detail_time
                    else ""
                ),
                "click_num": article.click_num,
                "url": article.url,
                "fujians": article.fujians,
                "content": article.content,
            }
            result.append(item)

        session.close()
        return jsonify({"data": result, "queryDate": today})

    except Exception as e:
        print(f"❌ Error: {str(e)}")
        return jsonify({"error": str(e)}), 500


@app.route("/")
def index():
    return app.send_static_file("today.html")  # 将今日页面作为首页


@app.route("/archive")
def archive():
    return app.send_static_file("index.html")  # 原始表格页面作为归档


@app.route("/subscribe")
@app.route("/subscribe/")
def subscribe_page():
    return app.send_static_file("subscribe.html")


@app.route("/subscribe.html")
def subscribe_page_legacy():
    return app.send_static_file("subscribe.html")


# 新增API - 获取统计数据
@app.route("/api/get_stats", methods=["GET"])
def get_stats():
    try:
        stats = subscriber_service.get_stats()
        return jsonify({"success": True, "stats": stats})
    except Exception as e:
        print(f"❌ 获取统计数据错误: {str(e)}")
        return jsonify({"success": False, "message": f"服务器错误: {str(e)}"}), 500


@app.route("/api/push_outbox_stats", methods=["GET"])
def push_outbox_stats():
    try:
        db = subscriber_service.db_manager
        return jsonify(
            {
                "success": True,
                "outbox_pending": db.count_pending_outbox(),
                "article_queue_pending": db.count_pending_articles(),
                "users_in_delivery_window": db.count_users_in_delivery_window(),
                "push_poll_seconds": PUSH_POLL_SECONDS,
                "push_worker": get_last_push_cycle_stats(),
                "abnormal_log": str(ABNORMAL_LOG_FILE),
            }
        )
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


# 发送订阅成功确认邮件
def send_subscription_confirmation(
    email,
    all_platforms,
    platform_names=None,
    send_frequency=1,
    smtp_server=None,
    smtp_email=None,
    smtp_password=None,
):
    try:
        # 确定订阅的平台类型描述
        if all_platforms:
            platform_description = "全部平台"
        else:
            platform_description = (
                f"以下平台：{', '.join(platform_names)}"
                if platform_names
                else "您选择的平台"
            )

        # 构建HTML邮件内容
        html_content = f"""
        <html>
        <head>
            <style>
                body {{
                    font-family: 'PingFang SC', 'Helvetica Neue', Helvetica, Arial, sans-serif;
                    background-color: #f5f5f5;
                    color: #333;
                    padding: 20px;
                    max-width: 600px;
                    margin: 0 auto;
                }}
                .container {{
                    background-color: white;
                    border-radius: 10px;
                    box-shadow: 0 2px 10px rgba(0,0,0,0.1);
                    padding: 25px;
                }}
                .header {{
                    text-align: center;
                    margin-bottom: 20px;
                    padding-bottom: 15px;
                    border-bottom: 1px solid #eee;
                }}
                .content {{
                    line-height: 1.6;
                    margin-bottom: 20px;
                }}
                .highlight {{
                    background-color: #f0f7ff;
                    padding: 10px;
                    border-radius: 5px;
                    margin: 15px 0;
                }}
                .footer {{
                    text-align: center;
                    margin-top: 20px;
                    font-size: 12px;
                    color: #888;
                    padding-top: 15px;
                    border-top: 1px solid #eee;
                }}
            </style>
        </head>
        <body>
            <div class="container">
                <div class="header">
                    <h2>🎉 订阅成功 🎉</h2>
                </div>
                <div class="content">
                    <p>您好！</p>
                    <p>感谢您订阅深圳技术大学公文通更新通知。您已成功订阅来自<b>{platform_description}</b>的最新公告。</p>
                    
                    <div class="highlight">
                        <p>📧 订阅邮箱：{email}</p>
                        <p>🔄 推送频率：每{send_frequency}小时推送一次</p>
                    </div>
                    
                    <p>从现在开始，您将按照设定的频率收到所有符合您订阅要求的最新公文通通知。</p>
                    <p>如需调整订阅设置或取消订阅，请随时访问我们的 <a href="http://localhost:5000/subscribe">订阅管理页面</a>。</p>
                </div>
                <div class="footer">
                    <p>© SZTU-NoticePush</p>
                </div>
            </div>
        </body>
        </html>
        """

        # 发送确认邮件
        subscriber_service._send_batch_email(
            subject="【SZTU-NoticePush】✅ 订阅成功确认",
            content=html_content,
            receivers=[email],
            is_html=True,
            smtp_server=smtp_server,
            smtp_email=smtp_email,
            smtp_password=smtp_password,
        )[0]
        print(f"[{datetime.now()}] ✅ 订阅确认邮件已发送至: {email}")
        return True

    except Exception as e:
        print(f"[{datetime.now()}] ❌ 发送订阅确认邮件失败: {str(e)}")
        return False


# 更新爬虫任务函数，使用新的推送方法
def crawl_task():
    global crawl_status
    if crawl_running.is_set():
        msg = "上一轮爬取尚未结束，跳过本次定时任务"
        log_abnormal("crawl", msg)
        print(f"[{datetime.now()}] {msg}")
        return []

    crawl_running.set()
    crawl_status["last_started_at"] = datetime.now().isoformat()
    crawl_status["last_error"] = None
    try:
        print(f"[{datetime.now()}] 开始执行爬取任务...")
        new_urls = main_crawler(start_page=0, end_page=2, mode="all")
        count = len(new_urls) if new_urls else 0
        crawl_status["last_new_count"] = count
        print(f"[{datetime.now()}] 爬取完成，发现 {count} 条新内容")
        enqueue_new_articles_from_crawl(new_urls or [])
        return new_urls
    except Exception as e:
        crawl_status["last_error"] = str(e)
        log_abnormal("crawl", "爬取任务执行失败", e)
        print(f"[{datetime.now()}] 爬取任务执行失败: {str(e)}")
        return []
    finally:
        crawl_status["last_finished_at"] = datetime.now().isoformat()
        crawl_running.clear()


# 保留旧的函数名以保持兼容性
def send_new_articles_email_by_frequency(new_urls):
    """兼容性函数，调用新的个性化推送函数"""
    send_new_articles_email_by_individual_frequency(new_urls)


def send_new_articles_email(new_urls):
    """兼容性函数，调用新的个性化推送函数"""
    send_new_articles_email_by_individual_frequency(new_urls)


def start_push_retry_worker():
    """启动 outbox 后台重试线程（Flask debug 重载时仅在子进程启动一次）。"""
    global push_retry_thread_started
    werkzeug_main = os.environ.get("WERKZEUG_RUN_MAIN")
    if werkzeug_main == "false":
        return
    if push_retry_thread_started:
        return
    push_retry_thread_started = True
    threading.Thread(
        target=run_push_retry_loop,
        args=(subscriber_service,),
        daemon=True,
    ).start()


def start_push_worker():
    """启动推送消费者线程（与爬虫并行，轮询待推队列与投递窗口）。"""
    global push_worker_thread_started
    werkzeug_main = os.environ.get("WERKZEUG_RUN_MAIN")
    if werkzeug_main == "false":
        return
    if push_worker_thread_started:
        return
    push_worker_thread_started = True
    threading.Thread(
        target=run_push_worker_loop,
        args=(subscriber_service, db_manager),
        daemon=True,
    ).start()


def parse_server_args(argv=None):
    parser = argparse.ArgumentParser(description="SZTU-NoticePush Web 服务")
    parser.add_argument(
        "--start-crawler",
        action="store_true",
        help="服务启动后自动开始爬虫（等同订阅页「启动自动爬虫」）",
    )
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=5000, help="监听端口")
    return parser.parse_args(argv)


def main(argv=None):
    """主函数，用于 uv run 命令调用"""
    args = parse_server_args(argv)

    print("🔄 检查数据库兼容性...")

    if args.start_crawler:
        ok, message = start_automated_crawler()
        if ok:
            print(f"[CLI] {message}")
        else:
            print(f"[CLI] 未启动爬虫: {message}")
    else:
        print(
            "爬虫未随服务启动：请使用 python server.py --start-crawler，"
            "或在「邮件订阅」页点击「启动自动爬虫」"
            f"（之后每约 {int(SCHEDULED_CRAWL_HOURS * 60)} 分钟一轮，日志在本终端）"
        )

    print(
        f"邮件推送：生产者入队 + 消费者每 {PUSH_POLL_SECONDS}s 轮询；"
        f"SMTP 失败写入 outbox 自动补发"
    )
    start_push_worker()
    start_push_retry_worker()

    app.run(debug=True, host=args.host, port=args.port)


if __name__ == "__main__":
    main()
