' Run a .bat file with no console window (scheduled tasks):
'   wscript.exe run_hidden.vbs "C:\path\to\file.bat"
' Used by the Hyperliquid-* scheduled tasks so their hourly/15-min runs
' don't flash a console on the user's desktop.
Dim shell, bat
Set shell = CreateObject("WScript.Shell")
bat = WScript.Arguments(0)
shell.Run "cmd.exe /c """ & bat & """", 0, False
