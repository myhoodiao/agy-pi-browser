param(
    [string]$PythonExe = "",
    [string]$BackendDir = ""
)

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

Write-Host "=== [1/5] 检查 Python 3.10+ 运行环境 ===" -ForegroundColor Cyan

function Resolve-PythonExe {
    param([string]$ExplicitPath)
    if ($ExplicitPath -and (Test-Path $ExplicitPath)) {
        return (Resolve-Path $ExplicitPath).Path
    }
    $candidates = @()
    $localPyDir = Join-Path $env:LOCALAPPDATA "Programs\Python"
    if (Test-Path $localPyDir) {
        $candidates += Get-ChildItem -Path $localPyDir -Filter "python.exe" -Recurse -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending |
            Select-Object -ExpandProperty FullName
    }
    $cmdPy = Get-Command python -ErrorAction SilentlyContinue
    if ($cmdPy -and $cmdPy.Source -notmatch "WindowsApps") {
        $candidates += $cmdPy.Source
    }
    foreach ($cand in $candidates) {
        if (Test-Path $cand) {
            try {
                $verOut = & $cand -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
                if ($LASTEXITCODE -eq 0 -and $verOut) {
                    $parts = $verOut.Trim().Split(".")
                    if ([int]$parts[0] -ge 3 -and [int]$parts[1] -ge 10) {
                        return $cand
                    }
                }
            } catch {}
        }
    }
    throw "未找到可用的 Python 3.10+，请先安装 Python 或通过 -PythonExe 指定路径。"
}

$ResolvedPython = Resolve-PythonExe -ExplicitPath $PythonExe
$PyVer = & $ResolvedPython --version
Write-Host "  已定位 Python: $ResolvedPython ($PyVer)" -ForegroundColor Green

Write-Host "=== [2/5] 解析插件部署路径 ===" -ForegroundColor Cyan

$RepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$PluginRoot = Join-Path $env:USERPROFILE ".gemini\config\plugins\agy-pi-browser"

$TempPrefix = [System.IO.Path]::GetTempPath().TrimEnd('\')
if ($BackendDir) {
    $EffectiveRoot = (Resolve-Path $BackendDir).Path
} elseif (-not $RepoRoot.StartsWith($TempPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    $EffectiveRoot = $RepoRoot
} else {
    $EffectiveRoot = $PluginRoot
}

Write-Host "  源码目录: $RepoRoot" -ForegroundColor Green
Write-Host "  插件路径: $PluginRoot" -ForegroundColor Green
Write-Host "  引擎目录: $EffectiveRoot" -ForegroundColor Green

Write-Host "=== [3/5] 部署插件文件与生成 mcp_config.json ===" -ForegroundColor Cyan

foreach ($sub in @("assets", "prompts", "rules", "scripts", "skills\agy-pi-browser")) {
    $dir = Join-Path $PluginRoot $sub
    if (-not (Test-Path $dir)) {
        New-Item -ItemType Directory -Path $dir -Force | Out-Null
    }
}

Copy-Item -Path (Join-Path $RepoRoot "plugin\plugin.json") -Destination (Join-Path $PluginRoot "plugin.json") -Force
Copy-Item -Path (Join-Path $RepoRoot "plugin\assets\logo.svg") -Destination (Join-Path $PluginRoot "assets\logo.svg") -Force
Copy-Item -Path (Join-Path $RepoRoot "plugin\rules\AGENTS.md") -Destination (Join-Path $PluginRoot "rules\AGENTS.md") -Force
Copy-Item -Path (Join-Path $RepoRoot "plugin\skills\agy-pi-browser\SKILL.md") -Destination (Join-Path $PluginRoot "skills\agy-pi-browser\SKILL.md") -Force
Copy-Item -Path (Join-Path $RepoRoot "prompts\browser_worker.md") -Destination (Join-Path $PluginRoot "prompts\browser_worker.md") -Force
Copy-Item -Path (Join-Path $RepoRoot "scripts\pi_browser_core.py") -Destination (Join-Path $PluginRoot "scripts\pi_browser_core.py") -Force
Copy-Item -Path (Join-Path $RepoRoot "scripts\pi_browser_mcp.py") -Destination (Join-Path $PluginRoot "scripts\pi_browser_mcp.py") -Force

$McpScriptPath = Join-Path $EffectiveRoot "scripts\pi_browser_mcp.py"
$StateDirPath = Join-Path $EffectiveRoot ".state"
$Utf8NoBom = New-Object System.Text.UTF8Encoding($false)

$McpConfigObj = [ordered]@{
    mcpServers = [ordered]@{
        "pi-browser" = [ordered]@{
            command = $ResolvedPython
            args    = @("-X", "utf8", $McpScriptPath)
            env     = [ordered]@{
                AGY_PI_BROWSER_ROOT      = $EffectiveRoot
                AGY_PI_BROWSER_STATE_DIR = $StateDirPath
                PYTHONIOENCODING         = "utf-8"
            }
        }
    }
}
$McpJson = $McpConfigObj | ConvertTo-Json -Depth 6
[System.IO.File]::WriteAllText((Join-Path $PluginRoot "mcp_config.json"), $McpJson, $Utf8NoBom)
Write-Host "  已部署插件并生成 mcp_config.json" -ForegroundColor Green

Write-Host "=== [4/5] 运行离线单元测试 ===" -ForegroundColor Cyan
& $ResolvedPython -B -X utf8 -m unittest discover -s (Join-Path $RepoRoot "tests") -v
if ($LASTEXITCODE -ne 0) {
    throw "离线单元测试未通过，请检查输出日志。"
}

Write-Host "=== [5/5] 执行 MCP JSON-RPC 握手与插件严格校验 ===" -ForegroundColor Cyan
$SmokeInput = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26"}}' + "`n" + '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' + "`n"
$SmokeOut = ($SmokeInput | & $ResolvedPython -X utf8 $McpScriptPath) | Out-String
if ($LASTEXITCODE -ne 0 -or $SmokeOut -notmatch "pi_browser_run" -or $SmokeOut -notmatch "pi_browser_resume" -or $SmokeOut -notmatch "pi_browser_status" -or $SmokeOut -notmatch "pi_browser_stop") {
    throw "MCP 服务握手自检失败：$SmokeOut"
}
Write-Host "  MCP JSON-RPC 握手与 4 个工具校验通过" -ForegroundColor Green

$AgyCmd = Get-Command agy -ErrorAction SilentlyContinue
if ($AgyCmd) {
    & $AgyCmd plugin validate $PluginRoot --strict
    if ($LASTEXITCODE -ne 0) {
        throw "agy plugin validate --strict 校验未通过。"
    }
} else {
    Write-Host "  未检测到 agy 命令，跳过 agy plugin validate" -ForegroundColor Yellow
}

Write-Host ""
Write-Host "✅ agy-pi-browser 安装与自检全部通过！新开或重启 agy 会话即可使用。" -ForegroundColor Green
