$a = New-ScheduledTaskAction -Execute 'C:\Users\Braindead\Documents\trading-bot-hyperliquid\wallet_fills_collector.bat'
$t = New-ScheduledTaskTrigger -Once -At (Get-Date) -RepetitionInterval (New-TimeSpan -Hours 1)
Register-ScheduledTask -TaskName 'Hyperliquid-Wallet-Fills-Collector' -Action $a -Trigger $t -Force | Out-Null
Start-ScheduledTask -TaskName 'Hyperliquid-Wallet-Fills-Collector'
Get-ScheduledTask -TaskName 'Hyperliquid-Wallet-Fills-Collector' | Select-Object TaskName, State
