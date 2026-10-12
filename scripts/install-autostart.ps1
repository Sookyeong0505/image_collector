#Requires -RunAsAdministrator
<#
.SYNOPSIS
    PC 가 켜지면 수집 서버가 자동으로 시작되도록 Windows 작업 스케줄러에 등록한다.

.DESCRIPTION
    등록되는 작업 'ImageCollector' 의 동작:
      - PC 가 부팅되면 1분 뒤 시작한다. 로그인하지 않아도 실행된다.
      - 서버가 비정상 종료되면 1분 뒤 다시 시작한다.
      - 실행 시간 제한이 없다 (작업 스케줄러 기본값은 3일 뒤 강제 종료).
      - 이미 실행 중이면 또 띄우지 않는다.

    Windows 암호는 저장하지 않는다 (S4U 방식). 대신 이 작업은 네트워크 드라이브에 접근할 수 없으므로
    이미지 저장 폴더(BASE_SAVE_PATH)와 프로젝트 폴더는 로컬 디스크에 있어야 한다. 인터넷 접속은 된다.

    창이 뜨지 않는 백그라운드로 실행된다. 화면은 로그인한 뒤 브라우저에서 http://127.0.0.1:8000 으로 연다.

.EXAMPLE
    관리자 PowerShell 에서:
    powershell -ExecutionPolicy Bypass -File scripts\install-autostart.ps1
#>
param(
    [int]$Port = 8000,
    # 작업을 실행할 Windows 계정. 평소 이 PC 에 로그인하는 계정이어야 한다.
    [string]$User = "$env:USERDOMAIN\$env:USERNAME"
)

$ErrorActionPreference = 'Stop'
$TaskName = 'ImageCollector'
$Root = Split-Path -Parent $PSScriptRoot
$Python = Join-Path $Root '.venv\Scripts\pythonw.exe'   # pythonw = 콘솔 창 없이 실행하는 파이썬

if (-not (Test-Path $Python)) {
    throw "가상환경이 없습니다: $Python`n먼저 'python -m venv .venv' 와 'pip install -e .' 를 실행하세요."
}
if (-not (Test-Path (Join-Path $Root '.env'))) {
    throw ".env 파일이 없습니다. .env.sample 을 복사해 API 키를 채우세요."
}

# 같은 포트를 이미 쓰는 서버가 있으면 작업이 뜨자마자 죽고 1분마다 재시작을 반복한다
$listening = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
if ($listening) {
    throw "포트 $Port 를 이미 사용 중입니다. 직접 띄운 서버(python -m app serve)가 있으면 먼저 종료하세요."
}

$action = New-ScheduledTaskAction -Execute $Python -Argument "-m app serve --port $Port" -WorkingDirectory $Root

# 부팅 직후에는 네트워크가 아직 준비되지 않았을 수 있어 1분 기다린다
$trigger = New-ScheduledTaskTrigger -AtStartup
$trigger.Delay = 'PT1M'

# S4U: 로그인하지 않아도 이 계정으로 실행하되, 암호는 저장하지 않는다. 관리자 권한으로 올리지도 않는다.
$principal = New-ScheduledTaskPrincipal -UserId $User -LogonType S4U -RunLevel Limited

$settings = New-ScheduledTaskSettingsSet `
    -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -MultipleInstances IgnoreNew `
    -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description '이미지 수집 서버 (image_collector). 부팅 시 자동 시작, 종료 시 재시작.' -Force | Out-Null

# 재부팅을 기다리지 않고 지금 바로 시작한다
Start-ScheduledTask -TaskName $TaskName
Start-Sleep -Seconds 5

$state = (Get-ScheduledTask -TaskName $TaskName).State
Write-Host ''
Write-Host "작업 '$TaskName' 등록 완료 (실행 계정: $User, 현재 상태: $state)"
Write-Host "  화면      http://127.0.0.1:$Port"
Write-Host "  상태 확인  http://127.0.0.1:$Port/api/health"
Write-Host "  로그      $(Join-Path $Root 'logs\collector.log')"
Write-Host "  잠시 멈춤  Stop-ScheduledTask -TaskName $TaskName   (다시 시작: Start-ScheduledTask)"
Write-Host "  제거      scripts\uninstall-autostart.ps1"
if ($state -ne 'Running') {
    Write-Warning '작업이 실행 중이 아닙니다. logs\collector.log 와 작업 스케줄러의 기록 탭을 확인하세요.'
}
