# 에이전트 관리 화면 실행. 관리 서버가 llama-server(모델 서버)도 함께 켜고 끈다.
# 보통은 프로젝트 폴더의 start.cmd 를 더블클릭하면 이 스크립트가 실행된다.
# 주의: Windows PowerShell 5.1 은 BOM 없는 UTF-8 을 깨뜨려 읽으므로 이 파일은 'UTF-8 (BOM)' 으로 저장할 것.

$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root '.venv\Scripts\python.exe'
[Console]::OutputEncoding = [Text.Encoding]::UTF8

if (-not (Test-Path $py)) {
    Write-Host '가상환경이 없습니다. 먼저 설치하세요:' -ForegroundColor Red
    Write-Host '  인터넷 PC : python -m venv .venv; .venv\Scripts\python -m pip install -r requirements.txt'
    Write-Host '  폐쇄망 PC : scripts\install_offline.ps1'
    Read-Host '엔터를 누르면 닫힙니다'
    exit 1
}

# 화면(web/, React) 빌드: 결과(web\dist)가 없거나 소스가 더 새로우면 다시 빌드한다 (npm 이 있는 PC 에서만).
# 폐쇄망 PC 에는 npm 이 없어도 된다 — 반입한 web\dist 를 그대로 쓴다.
function Build-Web([string]$root) {
    $web = Join-Path $root 'web'
    $index = Join-Path $web 'dist\index.html'
    $npm = Get-Command npm -ErrorAction SilentlyContinue
    $stale = -not (Test-Path $index)
    if (-not $stale) {
        $built = (Get-Item $index).LastWriteTime
        $stale = [bool](Get-ChildItem (Join-Path $web 'src'), (Join-Path $web 'index.html'), (Join-Path $web 'package.json') -Recurse -File |
            Where-Object { $_.LastWriteTime -gt $built } | Select-Object -First 1)
    }
    if (-not $stale) { return }
    if (-not $npm) {
        if (-not (Test-Path $index)) { Write-Host '화면 빌드(web\dist)가 없습니다. 인터넷 PC 에서 cd web; npm ci; npm run build 후 옮기세요.' -ForegroundColor Yellow }
        return
    }
    Write-Host '화면(web) 빌드 중…'
    Push-Location $web
    try {
        if (-not (Test-Path 'node_modules')) { npm ci --no-fund --no-audit; if ($LASTEXITCODE -ne 0) { throw 'npm ci 실패' } }
        npm run build; if ($LASTEXITCODE -ne 0) { throw '화면 빌드 실패 (npm run build)' }
    } finally { Pop-Location }
}

Set-Location $root
try { Build-Web $root } catch { Write-Host $_ -ForegroundColor Yellow }   # 빌드에 실패해도 이전 화면으로 켠다
& $py -m server --open
if ($LASTEXITCODE -ne 0) { Read-Host '오류로 종료되었습니다. 엔터를 누르면 닫힙니다' }
