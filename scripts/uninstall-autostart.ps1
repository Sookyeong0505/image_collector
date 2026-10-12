#Requires -RunAsAdministrator
<#
.SYNOPSIS
    install-autostart.ps1 이 등록한 자동 시작 작업을 멈추고 제거한다.
    수집한 이미지, DB, 로그는 그대로 둔다.

.EXAMPLE
    관리자 PowerShell 에서:
    powershell -ExecutionPolicy Bypass -File scripts\uninstall-autostart.ps1
#>
$ErrorActionPreference = 'Stop'
$TaskName = 'ImageCollector'

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "작업 '$TaskName' 이 등록되어 있지 않습니다."
    return
}

# 먼저 멈춘다. 돌던 수집 작업은 다음에 서버를 켤 때 멈춘 자리부터 이어진다.
Stop-ScheduledTask -TaskName $TaskName
Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
Write-Host "작업 '$TaskName' 을 제거했습니다. 서버가 필요하면 'python -m app serve' 로 직접 실행하세요."
