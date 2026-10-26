# SZTU-NoticePush 部署与启动

## 环境要求

- Windows 10/11（或 Linux/macOS，命令略调）
- Python 3.8+

## 1. 解压

将发布包解压到任意目录，例如：

`C:\SZTU-NoticePush`

## 2. 安装依赖

**方式 A：pip（通用）**

```powershell
cd C:\SZTU-NoticePush
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**方式 B：uv（推荐，若已安装 uv）**

```powershell
cd C:\SZTU-NoticePush
uv sync
```

## 3. 配置（可选）

编辑 `config.py`：

- `OFFICAL_URL`：邮件内链接用的对外地址（如 `your-server:5000`）
- `SCHEDULED_CRAWL_HOURS`：自动爬取间隔（默认 0.5 ≈ 30 分钟）
- `PUSH_POLL_SECONDS`：推送消费者轮询间隔（默认 180 秒）
- `EMAIL_PUSH_INTERVAL_SECONDS`：同一用户多部门邮件之间的 SMTP 间隔（默认 300 秒）

首次运行会自动创建 `database/` 与 `logs/` 目录。

## 4. 启动

**推荐：Web + 自动爬虫 + 推送消费者 + outbox 重试**

```powershell
cd C:\SZTU-NoticePush
.\.venv\Scripts\Activate.ps1
python server.py --start-crawler
```

或：

```powershell
.\start_with_crawler.ps1
```

**仅 Web**

```powershell
python server.py
```

**自定义端口**

```powershell
python server.py --start-crawler --host 0.0.0.0 --port 5000
```

## 5. 访问

<http://localhost:5000>

## 6. 运维

| 说明 | 命令 / 地址 |
|------|-------------|
| 队列状态 | `GET /api/push_outbox_stats` |
| 爬虫状态 | `GET /api/crawl_status` |
| 异常日志 | `logs/abnormal.log` |
| 停止服务 | 终端 `Ctrl+C` |
