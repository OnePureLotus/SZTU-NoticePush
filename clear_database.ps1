# 清空 SZTU-NoticePush 数据库（默认仅文章库，保留订阅者）
# 使用前请先停止 python server.py
$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

$argsList = @()
if ($args.Count -gt 0) {
    $argsList = $args
} else {
    $argsList = @("--articles", "-y")
}

& python (Join-Path $here "clear_database.py") @argsList
exit $LASTEXITCODE
