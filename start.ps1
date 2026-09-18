param(
    [int]$Port = 8765,
    [switch]$NoBrowser
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
$supportedPython = @('3.14', '3.13', '3.12')

function Get-PythonVersion([string]$Executable) {
    # Probes must not throw on stderr output under Windows PowerShell 5.1.
    $ErrorActionPreference = 'Continue'
    $version = & $Executable -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
    if ($LASTEXITCODE -eq 0 -and $version) { return "$version".Trim() }
    return $null
}

function Find-SupportedPython {
    $ErrorActionPreference = 'Continue'
    # 'py -0p' only lists installed runtimes, so probing never triggers an install.
    $pyLauncher = Get-Command py -ErrorAction SilentlyContinue
    if ($pyLauncher) {
        $installed = @{}
        foreach ($line in (& $pyLauncher.Source -0p 2>$null)) {
            if ($line -match '^\s*-V:(3\.\d+)(?:-64)?\s+\*?\s*(\S.*?\.exe)\s*$' -and -not $installed.ContainsKey($Matches[1])) {
                $installed[$Matches[1]] = $Matches[2]
            }
        }
        foreach ($version in $supportedPython) {
            if ($installed.ContainsKey($version) -and (Get-PythonVersion $installed[$version]) -eq $version) {
                return $installed[$version]
            }
        }
    }
    $pythonCommand = Get-Command python -ErrorAction SilentlyContinue
    if ($pythonCommand -and ($supportedPython -contains (Get-PythonVersion $pythonCommand.Source))) {
        return $pythonCommand.Source
    }
    return $null
}

if (Test-Path -LiteralPath $venvPython) {
    $venvVersion = Get-PythonVersion $venvPython
    if ($supportedPython -notcontains $venvVersion) {
        Write-Warning "现有 .venv 使用 Python $venvVersion，不在支持范围 3.12–3.14 内。如遇问题，请删除 .venv 后重新运行本脚本。"
    }
} else {
    $basePython = Find-SupportedPython
    if (-not $basePython) { throw '未找到 Python 3.12–3.14。请先安装其中一个版本。' }
    & $basePython -m venv (Join-Path $projectRoot '.venv')
    if ($LASTEXITCODE -ne 0) { throw '无法创建 Python 虚拟环境。' }
}

& $venvPython -c "import fastapi,httpx,keyring,playwright,pypdf,pypdfium2,pytesseract,uvicorn" 2>$null
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

$listener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listener) {
    throw "端口 $Port 已被占用。请关闭已有服务，或用 .\start.ps1 -Port <其他端口>。"
}

$arguments = @('-m', 'paper_endnote.app', '--port', "$Port")
if ($NoBrowser) { $arguments += '--no-browser' }
& $venvPython @arguments
