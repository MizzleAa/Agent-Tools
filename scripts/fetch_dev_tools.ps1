# 폐쇄망 반입용 개발 도구 내려받기 (인터넷 PC 에서). make_offline_bundle.ps1 이 부른다 — 혼자 돌려도 된다.
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\fetch_dev_tools.ps1 -Out <폴더>
# 규격(기준\)의 V1·V2 판정 도구를 사용자 프로젝트에 설치할 수 있게 한다 (QA E11).
#   <폴더>\python     : pip wheel (pyflakes, pytest 와 그 의존성)     → pip 가 PIP_FIND_LINKS 로 본다
#   <폴더>\npm-cache  : npm 캐시 (eslint, @eslint/js, globals 와 의존성) → npm 이 npm_config_cache 로 본다
# 에이전트(agent/tools/shell.py)가 <에이전트>\packages 를 찾아 이 환경변수를 자동으로 넣는다.
# 주의: 이 파일은 'UTF-8 (BOM)' 으로 저장할 것 (Windows PowerShell 5.1 한글 깨짐 방지).

param(
    [Parameter(Mandatory = $true)][string]$Out,
    [string[]]$PythonTools = @('pyflakes', 'pytest'),
    [string[]]$NodeTools = @('eslint', '@eslint/js', 'globals')
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$root = Split-Path -Parent $PSScriptRoot
$py = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $py)) { $py = 'python' }

New-Item -ItemType Directory -Force "$Out\python", "$Out\npm-cache" | Out-Null
Write-Host ("  python: {0}" -f ($PythonTools -join ', '))
& $py -m pip download @PythonTools -d "$Out\python" --only-binary=:all: --platform win_amd64 --python-version 3.12 --implementation cp --quiet
if ($LASTEXITCODE -ne 0) { throw 'pip download (개발 도구) 실패' }

$npm = Get-Command npm -ErrorAction SilentlyContinue
if (-not $npm) {
    Write-Host '  npm 이 없어 node 개발 도구는 건너뜁니다 (node-web 프로젝트의 eslint 는 폐쇄망에서 설치할 수 없음)' -ForegroundColor Yellow
    return
}
Write-Host ("  node: {0}" -f ($NodeTools -join ', '))
$tmp = Join-Path ([IO.Path]::GetTempPath()) ("devtools_" + [Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Force $tmp | Out-Null
Push-Location $tmp
try {
    '{"name":"devtools-cache","version":"1.0.0","private":true}' | Set-Content -Encoding ascii package.json
    npm install --no-fund --no-audit --cache "$Out\npm-cache" @NodeTools | Out-Null   # 의존성까지 전부 캐시에 들어간다
    if ($LASTEXITCODE -ne 0) { throw 'npm install (개발 도구 캐시) 실패' }
} finally {
    Pop-Location
    Remove-Item $tmp -Recurse -Force -Confirm:$false -ErrorAction SilentlyContinue
}
