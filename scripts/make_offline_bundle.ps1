# 폐쇄망 반입용 번들 만들기 — 인터넷이 되는 PC 에서 실행한다.
#   powershell -NoProfile -ExecutionPolicy Bypass -File scripts\make_offline_bundle.ps1
# 결과: dist\agent-offline\ 폴더 하나 → USB 등으로 통째로 옮긴 뒤 폐쇄망 PC 에서
#        agent\scripts\install_offline.ps1 을 실행한다.
# 주의: 이 파일은 'UTF-8 (BOM)' 으로 저장할 것 (Windows PowerShell 5.1 한글 깨짐 방지).

#   -AllModels : models 폴더의 GGUF 전부 넣기 (기본은 지금 쓰는 모델 + 임베딩 모델만 — 전부 넣으면 30GB 가 넘을 수 있다)

param(
    [string]$Out = '',
    [string]$PythonVersion = '3.12.10',
    [switch]$AllModels
)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$root = Split-Path -Parent $PSScriptRoot
if (-not $Out) { $Out = Join-Path $root 'dist\agent-offline' }
$py = Join-Path $root '.venv\Scripts\python.exe'

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

$llamaZips = Get-ChildItem (Join-Path $root 'runtime\downloads') -Filter '*.zip' -ErrorAction SilentlyContinue
if (-not $llamaZips) { throw 'runtime\downloads 에 llama.cpp zip 이 없습니다.' }
if ($AllModels) {
    $models = Get-ChildItem (Join-Path $root 'models') -Filter '*.gguf'
} else {
    $active = & $py -c "import sys; sys.path.insert(0, r'$root'); from agent.config import load_settings; print(load_settings().runtime.model_path)"
    $models = @(Get-Item (Join-Path $root $active)) + @(Get-ChildItem (Join-Path $root 'models') -Filter '*Embedding*.gguf' -ErrorAction SilentlyContinue)
}
if (-not $models) { throw 'models 에 .gguf 파일이 없습니다.' }
Write-Host ("넣을 모델: {0}" -f (($models | ForEach-Object { $_.Name }) -join ', '))

Write-Host "[1/5] 출력 폴더 준비: $Out"
if (Test-Path $Out) { Remove-Item $Out -Recurse -Force -Confirm:$false }
New-Item -ItemType Directory -Force "$Out\agent", "$Out\agent\models", "$Out\wheelhouse", "$Out\installers" | Out-Null

Write-Host '[2/5] 화면 빌드 확인 후 프로젝트 복사 (llama.cpp zip·고른 모델·web\dist 포함, .venv·data·node_modules 제외)'
Build-Web $root
if (-not (Test-Path (Join-Path $root 'web\dist\index.html'))) { throw '화면 빌드(web\dist)가 없습니다: cd web; npm ci; npm run build' }
# 'dist' 는 이름만 쓰면 web\dist 까지 빠지므로 전체 경로로 지정한다
$exclude = @('.venv', 'data', (Join-Path $root 'dist'), 'node_modules', '__pycache__', '.pytest_cache', '.playwright-mcp', (Join-Path $root 'runtime\llama.cpp'), (Join-Path $root 'models'))
robocopy $root "$Out\agent" /E /XD @exclude /XF *.pyc secrets.yaml user_secrets.yaml access.yaml /NFL /NDL /NJH /NJS /NP | Out-Null   # 토큰·사용자 계정·접속 설정은 반입하지 않는다 (반입한 PC 는 혼자 쓰기로 시작)
if ($LASTEXITCODE -ge 8) { throw "robocopy 실패 ($LASTEXITCODE)" }
foreach ($m in $models) { Copy-Item $m.FullName "$Out\agent\models\" }

Write-Host '[3/5] 파이썬 패키지 wheel 내려받기'
& $py -m pip download -r (Join-Path $root 'requirements.txt') -d "$Out\wheelhouse" `
    --only-binary=:all: --platform win_amd64 --python-version 3.12 --implementation cp --quiet
if ($LASTEXITCODE -ne 0) { throw 'pip download 실패' }
Write-Host '      + 사용자 프로젝트용 개발 도구 (규격 V1·V2 판정: pyflakes, pytest, eslint) → agent\packages'
& (Join-Path $PSScriptRoot 'fetch_dev_tools.ps1') -Out "$Out\agent\packages"

Write-Host '[4/5] 설치 파일 내려받기 (Python, VC++ 런타임)'
curl.exe -sSL -o "$Out\installers\python-$PythonVersion-amd64.exe" "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-amd64.exe"
curl.exe -sSL -o "$Out\installers\vc_redist.x64.exe" 'https://aka.ms/vs/17/release/vc_redist.x64.exe'

Write-Host '[5/5] 무결성 확인용 SHA256SUMS.txt 생성'
$sums = Get-ChildItem $Out -Recurse -File | ForEach-Object {
    $rel = $_.FullName.Substring($Out.Length + 1)
    '{0}  {1}' -f (Get-FileHash $_.FullName -Algorithm SHA256).Hash.ToLower(), $rel
}
[IO.File]::WriteAllLines((Join-Path $Out 'SHA256SUMS.txt'), $sums)

$size = (Get-ChildItem $Out -Recurse -File | Measure-Object Length -Sum).Sum / 1GB
Write-Host ("완료: {0}  ({1:N1} GB, 파일 {2}개)" -f $Out, $size, $sums.Count) -ForegroundColor Green
Write-Host '폐쇄망 PC 에서:  powershell -NoProfile -ExecutionPolicy Bypass -File agent\scripts\install_offline.ps1'
