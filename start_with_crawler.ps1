# 部署启动：Web + 自动爬虫（等同 python server.py --start-crawler）
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

Write-Host "[SZTU-NoticePush] 启动 Web 并自动开始爬虫"
Write-Host "访问: http://localhost:5000"
Write-Host ""

& python (Join-Path $here "server.py") --start-crawler @args
exit $LASTEXITCODE
