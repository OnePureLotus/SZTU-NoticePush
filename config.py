"""
项目统一配置文件

包含数据库、爬虫、邮件等相关配置
"""

import os
import re
from pathlib import Path

# 项目根目录
ROOT_DIR = Path(__file__).parent.resolve()
DATABASE_DIR = (ROOT_DIR / "database").resolve()
LOGS_DIR = (ROOT_DIR / "logs").resolve()
ABNORMAL_LOG_FILE = LOGS_DIR / "abnormal.log"

# 确保数据库目录存在
DATABASE_DIR.mkdir(parents=True, exist_ok=True)

# ========== 数据库配置 ==========
# 文章数据库
ARTICLES_DATABASE_URI = f"sqlite:///{str(DATABASE_DIR)}/articles.sqlite3"

# 订阅者数据库
SUBSCRIBERS_DATABASE_URI = f"sqlite:///{str(DATABASE_DIR)}/subscribers.sqlite3"

# ========== 爬虫配置 ==========
# 请求头配置
HEADERS = {
    "Referer": "https://nbw.sztu.edu.cn/",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/116.0.0.0 Safari/537.36",
}

# 请求负载
PAYLOAD = {}

# 基础URL
BASE_URL = "https://nbw.sztu.edu.cn"
LIST_URL_TEMPLATE = f"{BASE_URL}/list.jsp?totalpage=492&PAGENUM={{page}}&urltype=tree.TreeTempUrl&wbtreeid=1029"
INFO_URL_PREFIX = f"{BASE_URL}/info/"

# 请求重试次数
MAX_RETRIES = 4

# 请求超时时间（秒）
REQUEST_TIMEOUT = 5

# 休眠时间配置（秒）
SLEEP_INTERVAL = {
    "default": 0.5,
    "every_10": 1,
    "every_30": 2,
    "every_50": 2,
    "every_200": 5,
}

# 禁用代理设置
os.environ["NO_PROXY"] = "*"

# 订阅者发信默认 SMTP 端口（TLS）；各用户在订阅页配置自己的 SMTP 服务器与账号
SMTP_PORT = 587

# 订阅者收件邮箱格式限制（正则）。默认允许常见邮箱；仅学号邮箱可改为：
# r"^\d+@stumail\.sztu\.edu\.cn$"
SUBSCRIBER_MASK = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"

# 站点对外地址（邮件内链接），部署时改为实际 IP 或域名:端口
OFFICAL_URL = "localhost:5000"

# 网页「启动自动爬虫」后的定时间隔（小时），0.5 = 约每 30 分钟
SCHEDULED_CRAWL_HOURS = 0.5

# ========== 富文本邮件 ==========
MAX_EMAIL_ATTACHMENT_MB = 20
EMAIL_EMBED_IMAGES = True
EMAIL_DIGEST_ALWAYS_OPEN = True
EMAIL_INCLUDE_SOURCE_LINK = True
# 一轮爬取中，每个部门邮件发送完成后的等待时间（秒），降低 SMTP 限流；0 表示不等待
EMAIL_PUSH_INTERVAL_SECONDS = 300

# 推送消费者线程轮询间隔（秒）：检查待推队列与投递窗口，与 SMTP 部门间隔无关
PUSH_POLL_SECONDS = 180

# ========== 外网 SMTP 失败补发（Outbox 重试）==========
EMAIL_RETRY_POLL_SECONDS = 60
EMAIL_RETRY_BASE_DELAY_SECONDS = 120
EMAIL_RETRY_MAX_DELAY_SECONDS = 1800
# 重试队列中每处理一条后的间隔；None 表示与 EMAIL_PUSH_INTERVAL_SECONDS 相同
EMAIL_RETRY_ITEM_INTERVAL_SECONDS = None
