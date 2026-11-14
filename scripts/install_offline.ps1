# 폐쇄망 PC 설치 — make_offline_bundle.ps1 로 만든 번들 안에서 실행한다. 인터넷 불필요.
#   powershell -NoProfile -ExecutionPolicy Bypass -File agent\scripts\install_offline.ps1
# 하는 일: 파일 무결성 확인 → 가상환경 + 패키지 설치(wheelhouse) → llama.cpp 압축 해제 → 환경 점검
# 주의: 이 파일은 'UTF-8 (BOM)' 으로 저장할 것 (Windows PowerShell 5.1 한글 깨짐 방지).

param([switch]$SkipVerify)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$agent = Split-Path -Parent $PSScriptRoot
$bundle = Split-Path -Parent $agent

if (-not $SkipVerify -and (Test-Path "$bundle\SHA256SUMS.txt")) {
    Write-Host '[1/4] 파일 무결성 확인 (모델 파일이 커서 1~2분 걸립니다)'
    $bad = 0
    foreach ($line in Get-Content "$bundle\SHA256SUMS.txt" -Encoding UTF8) {
        $hash, $rel = $line -split '  ', 2
        $f = Join-Path $bundle $rel
        if (-not (Test-Path $f) -or (Get-FileHash $f -Algorithm SHA256).Hash.ToLower() -ne $hash) { Write-Host "  손상/누락: $rel" -ForegroundColor Red; $bad++ }
    }
    if ($bad) { throw "파일 $bad 개가 손상되었거나 없습니다. 다시 복사하세요." }
} else { Write-Host '[1/4] 무결성 확인 건너뜀' }

Write-Host '[2/4] Python 3.12 확인 및 가상환경 설치'
$python = $null
foreach ($cand in @('py -3.12', 'python')) {
    try { $v = Invoke-Expression "$cand --version" 2>$null; if ($v -match '3\.12') { $python = $cand; break } } catch {}
}
if (-not $python) {
    Write-Host "Python 3.12 가 없습니다. 먼저 설치하세요: $bundle\installers\python-*-amd64.exe" -ForegroundColor Red
    exit 1
}
Set-Location $agent
if (-not (Test-Path '.venv')) { Invoke-Expression "$python -m venv .venv" }
& .\.venv\Scripts\python.exe -m pip install --no-index --find-links "$bundle\wheelhouse" -r requirements.txt --quiet
if ($LASTEXITCODE -ne 0) { throw 'pip install 실패' }

if (Test-Path (Join-Path $agent 'packages')) {   # 사용자 프로젝트의 pip·npm 이 번들의 개발 도구만 보게 (인터넷 색인 안 씀)
    New-Item -ItemType File -Force (Join-Path $agent 'packages\.offline') | Out-Null
    Write-Host '      개발 도구(pyflakes·pytest·eslint) 오프라인 설치 준비: packages\'
}

Write-Host '[3/4] llama.cpp 압축 해제'
$dst = Join-Path $agent 'runtime\llama.cpp'
New-Item -ItemType Directory -Force $dst | Out-Null
Get-ChildItem (Join-Path $agent 'runtime\downloads') -Filter '*.zip' | ForEach-Object { Expand-Archive $_.FullName -DestinationPath $dst -Force }

Write-Host '[4/4] 환경 점검'
& .\.venv\Scripts\python.exe scripts\check_env.py
Write-Host ''
Write-Host '설치 끝. start.cmd 를 더블클릭하면 관리 화면이 열립니다.' -ForegroundColor Green
Write-Host 'llama-server 가 VCRUNTIME140.dll 오류로 안 뜨면 installers\vc_redist.x64.exe 를 설치하세요.'
