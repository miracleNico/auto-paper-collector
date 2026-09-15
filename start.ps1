param(
    [int]$Port = 8765,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $venvPython)) {
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        & $pyLauncher.Source -3.12 -m venv (Join-Path $projectRoot '.venv')
    } elseif ($pythonCommand) {
        & $pythonCommand.Source -m venv (Join-Path $projectRoot '.venv')
    } else {
        throw '未找到 Python。请先安装 Python 3.12。'
    }
    if ($LASTEXITCODE -ne 0) { throw '无法创建 Python 虚拟环境。' }
}

& $venvPython -c "import fastapi,httpx,playwright,pypdf,pywinauto,uvicorn" 2>$null
$needsInstall = $LASTEXITCODE -ne 0
if ($needsInstall) {
    $installArguments = @('install', '--disable-pip-version-check')
    if (Test-NetConnection -ComputerName 127.0.0.1 -Port 10808 -InformationLevel Quiet -WarningAction SilentlyContinue) {
        $installArguments += @('--proxy', 'http://127.0.0.1:10808')
    }
    $installArguments += @('-r', (Join-Path $projectRoot 'requirements.lock'))
    & $venvPython -m pip @installArguments
    if ($LASTEXITCODE -ne 0) { throw 'Python 依赖安装失败。' }
}

$arguments = @('-m', 'paper_endnote.app', '--port', "$Port")
if ($NoBrowser) { $arguments += '--no-browser' }
& $venvPython @arguments
