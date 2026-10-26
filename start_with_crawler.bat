@echo off
chcp 65001 >nul
cd /d "%~dp0"
title SZTU-NoticePush - Web + Crawler
echo [SZTU-NoticePush] 启动 Web 并自动开始爬虫（等同订阅页「启动自动爬虫」）
echo 访问: http://localhost:5000
echo.
python server.py --start-crawler %*
if errorlevel 1 (
    echo.
    echo 启动失败。请确认已安装 Python 依赖: pip install -r requirements.txt
    pause
)
exit /b %ERRORLEVEL%
