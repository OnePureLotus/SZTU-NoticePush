# 部署启动：仅 Web，不自动爬取（可在 /subscribe 页手动启动爬虫）
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

Write-Host "[SZTU-NoticePush] 启动 Web 服务（不自动爬取）"
Write-Host "访问: http://localhost:5000"
Write-Host ""

& python (Join-Path $here "server.py") @args
exit $LASTEXITCODE
