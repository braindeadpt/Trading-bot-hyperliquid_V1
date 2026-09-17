# Re-register the Hyperliquid scheduled tasks to run via run_hidden.vbs
# (no console window flash on the desktop).
$vbs  = 'C:\Users\Braindead\Documents\trading-bot-hyperliquid\scripts\ops\run_hidden.vbs'
$root = 'C:\Users\Braindead\Documents\trading-bot-hyperliquid'

$tasks = @(
    @{ Name = 'Hyperliquid-Bot-Alive';              Bat = 'bot_alive_watchdog.bat';    Sched = '/SC MINUTE /MO 15' },
    @{ Name = 'Hyperliquid-Wallet-Fills-Collector'; Bat = 'wallet_fills_collector.bat'; Sched = '/SC HOURLY' },
    @{ Name = 'Hyperliquid Research Watchdogs';     Bat = 'watchdog_supervisor.bat';    Sched = '/SC HOURLY /MO 6' },
    @{ Name = 'Hyperliquid Overnight Research';     Bat = 'overnight_nightly.bat';      Sched = '/SC DAILY /ST 05:00' }
)

foreach ($t in $tasks) {
    $tr = 'wscript.exe "' + $vbs + '" "' + $root + '\' + $t.Bat + '"'
    $cmd = 'schtasks /Create /TN "' + $t.Name + '" /TR "' + $tr + '" ' + $t.Sched + ' /F'
    Write-Output ("> " + $t.Name)
    cmd /c $cmd
}
