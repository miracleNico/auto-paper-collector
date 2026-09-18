param(
    [int]$Port = 8765,
    [switch]$NoBrowser,
    # Local proxy port for installing dependencies; skips proxy auto-detection.
    [ValidateRange(1, 65535)]
    [int]$ProxyPort
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
$supportedPython = @('3.14', '3.13', '3.12')
$proxyPortGiven = $PSBoundParameters.ContainsKey('ProxyPort')

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

function Test-DependenciesInstalled {
    $ErrorActionPreference = 'Continue'
    & $venvPython -c "import fastapi,httpx,keyring,playwright,pypdf,pypdfium2,pytesseract,uvicorn" 2>$null
    return $LASTEXITCODE -eq 0
}

function Test-TcpPort([string]$HostName, [int]$PortNumber, [int]$TimeoutMs = 500) {
    $client = [System.Net.Sockets.TcpClient]::new()
    try {
        return $client.ConnectAsync($HostName, $PortNumber).Wait($TimeoutMs) -and $client.Connected
    } catch {
        return $false
    } finally {
        $client.Dispose()
    }
}

function Get-ProxyFromPac([string]$Pac) {
    # Takes the first "PROXY host:port" directive; DIRECT/SOCKS entries are skipped.
    if ($Pac -match 'PROXY\s+([A-Za-z0-9.\-]+):(\d{1,5})') {
        return [Uri]"http://$($Matches[1]):$($Matches[2])/"
    }
    return $null
}

function Get-SystemProxy {
    # Resolves the manual system proxy as well as PAC (AutoConfigURL) and WPAD.
    $target = [Uri]'https://pypi.org/simple/'
    try {
        $proxy = [System.Net.WebRequest]::GetSystemWebProxy().GetProxy($target)
        if ($proxy -and $proxy.Host -ne $target.Host -and $proxy.Scheme -in @('http', 'https')) {
            return $proxy
        }
    } catch {}
    # Fallback when the API cannot evaluate the PAC script: parse it ourselves.
    $settings = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction SilentlyContinue
    if ($settings -and $settings.AutoConfigURL) {
        try {
            $pacUri = [Uri]$settings.AutoConfigURL
            if ($pacUri.IsFile) {
                $pac = Get-Content -LiteralPath $pacUri.LocalPath -Raw
            } else {
                $pac = (Invoke-WebRequest -Uri $pacUri -UseBasicParsing -TimeoutSec 5).Content
                if ($pac -is [byte[]]) { $pac = [System.Text.Encoding]::UTF8.GetString($pac) }
            }
            $pacProxy = Get-ProxyFromPac $pac
            if ($pacProxy) { return $pacProxy }
        } catch {}
    }
    return $null
}

function Read-ProxyPort {
    for ($attempt = 1; $attempt -le 3; $attempt++) {
        try {
            $answer = Read-Host '未检测到系统代理或 PAC 代理。如需代理，请输入本机代理端口（直接回车则不使用代理）'
        } catch {
            return $null  # -NonInteractive session; end of redirected input reads as empty
        }
        if ([string]::IsNullOrWhiteSpace($answer)) { return $null }
        $portNumber = 0
        if ([int]::TryParse($answer.Trim(), [ref]$portNumber) -and $portNumber -ge 1 -and $portNumber -le 65535) {
            if (Test-TcpPort '127.0.0.1' $portNumber) { return "http://127.0.0.1:$portNumber" }
            Write-Warning "无法连接 127.0.0.1:$portNumber，请确认代理软件已启动。"
        } else {
            Write-Warning '请输入 1–65535 之间的端口号。'
        }
    }
    return $null
}

function Resolve-PipProxy {
    $ErrorActionPreference = 'Continue'
    if ($proxyPortGiven) {
        if (-not (Test-TcpPort '127.0.0.1' $ProxyPort)) {
            Write-Warning "无法连接 127.0.0.1:$ProxyPort，仍按 -ProxyPort 使用该代理。"
        }
        return "http://127.0.0.1:$ProxyPort"
    }
    # pip honours these itself; passing --proxy would override them.
    foreach ($name in 'HTTPS_PROXY', 'HTTP_PROXY') {
        if ([Environment]::GetEnvironmentVariable($name)) {
            Write-Host "使用环境变量 $name 中的代理安装依赖。"
            return $null
        }
    }
    if ((& $venvPython -m pip config list 2>$null) -match '\.proxy=') {
        Write-Host '使用 pip 配置中的代理安装依赖。'
        return $null
    }
    $systemProxy = Get-SystemProxy
    if ($systemProxy) {
        if (Test-TcpPort $systemProxy.Host $systemProxy.Port) {
            return "http://$($systemProxy.Host):$($systemProxy.Port)"
        }
        Write-Warning "检测到系统代理 $($systemProxy.Host):$($systemProxy.Port)，但无法连接。"
    }
    return Read-ProxyPort
}

if (-not (Test-DependenciesInstalled)) {
    $installArguments = @('install', '--disable-pip-version-check')
    $proxyUrl = Resolve-PipProxy
    if ($proxyUrl) {
        Write-Host "通过代理 $proxyUrl 安装依赖。"
        $installArguments += @('--proxy', $proxyUrl)
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
