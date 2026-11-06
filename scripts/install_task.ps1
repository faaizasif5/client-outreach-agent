# Registers the "ClientOutreachAgent" scheduled task (spec section 19a): runs on logon, on unlock,
# on wake from sleep (Power-Troubleshooter event 1), each with a 2-minute delay, plus an hourly
# fallback. main.py's once-per-day guard decides whether anything actually runs.
#
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1            # dry-run mode
#   powershell -ExecutionPolicy Bypass -File scripts\install_task.ps1 -Live      # after approval
#   .venv\Scripts\python main.py scheduler off | on | status                     # pause / resume
#   schtasks /Delete /TN ClientOutreachAgent /F                                  # uninstall
param([switch]$Live)

$ErrorActionPreference = "Stop"
$Repo = Split-Path -Parent $PSScriptRoot
$Pythonw = Join-Path $Repo ".venv\Scripts\pythonw.exe"
if (-not (Test-Path $Pythonw)) { throw "Missing $Pythonw. Create the venv first." }
$TaskArgs = if ($Live) { "main.py" } else { "main.py --dry-run" }
$User = "$env:USERDOMAIN\$env:USERNAME"
$Start = (Get-Date).ToString("yyyy-MM-ddTHH:00:00")

$Xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Client outreach agent: first open of the day</Description></RegistrationInfo>
  <Triggers>
    <LogonTrigger><Enabled>true</Enabled><UserId>$User</UserId><Delay>PT2M</Delay></LogonTrigger>
    <SessionStateChangeTrigger><Enabled>true</Enabled><StateChange>SessionUnlock</StateChange><UserId>$User</UserId><Delay>PT2M</Delay></SessionStateChangeTrigger>
    <EventTrigger>
      <Enabled>true</Enabled>
      <Subscription>&lt;QueryList&gt;&lt;Query Id="0" Path="System"&gt;&lt;Select Path="System"&gt;*[System[Provider[@Name='Microsoft-Windows-Power-Troubleshooter'] and EventID=1]]&lt;/Select&gt;&lt;/Query&gt;&lt;/QueryList&gt;</Subscription>
      <Delay>PT2M</Delay>
    </EventTrigger>
    <TimeTrigger>
      <Enabled>true</Enabled><StartBoundary>$Start</StartBoundary>
      <Repetition><Interval>PT1H</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
    </TimeTrigger>
  </Triggers>
  <Principals><Principal id="Author"><UserId>$User</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <ExecutionTimeLimit>PT1H</ExecutionTimeLimit>
    <Hidden>true</Hidden>
  </Settings>
  <Actions Context="Author">
    <Exec><Command>$Pythonw</Command><Arguments>$TaskArgs</Arguments><WorkingDirectory>$Repo</WorkingDirectory></Exec>
  </Actions>
</Task>
"@

$XmlPath = Join-Path $env:TEMP "ClientOutreachAgent.xml"
$Xml | Out-File -FilePath $XmlPath -Encoding Unicode
schtasks /Create /TN "ClientOutreachAgent" /XML $XmlPath /F
Remove-Item $XmlPath
Write-Host "Registered ClientOutreachAgent ($TaskArgs). Logs: $Repo\logs\"
