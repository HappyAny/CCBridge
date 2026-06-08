Option Explicit

Dim shell
Dim fso
Dim root
Dim command

Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
StopExistingTray root

command = "cmd.exe /d /c cd /d " & Quote(root) & _
    " && where pythonw.exe >nul 2>nul" & _
    " && start """" pythonw.exe -m cc_bridge.tray" & _
    " || powershell.exe -NoProfile -WindowStyle Hidden -Command " & _
    Quote("Add-Type -AssemblyName PresentationFramework;[System.Windows.MessageBox]::Show('pythonw.exe not found. Install Python or activate the conda environment, then run this launcher again.','CC Bridge')")

shell.Run command, 0, False

Function Quote(value)
    Quote = Chr(34) & Replace(value, Chr(34), Chr(34) & Chr(34)) & Chr(34)
End Function

Sub StopExistingTray(rootPath)
    On Error Resume Next

    Dim trayPath
    Dim patterns
    Dim pattern
    Dim wmi
    Dim processes
    Dim process
    Dim commandLine

    patterns = Array( _
        "cc_bridge.tray", _
        "pythonw.exe -m cc_bridge", _
        "python.exe -m cc_bridge", _
        "codex_telegram_bridge.tray", _
        "pythonw.exe -m codex_telegram_bridge", _
        "python.exe -m codex_telegram_bridge", _
        "claudecode_telegram_bridge.tray", _
        "pythonw.exe -m claudecode_telegram_bridge", _
        "python.exe -m claudecode_telegram_bridge" _
    )
    Set wmi = GetObject("winmgmts:\\.\root\cimv2")
    Set processes = wmi.ExecQuery("SELECT ProcessId, CommandLine FROM Win32_Process WHERE Name = 'pythonw.exe' OR Name = 'python.exe'")

    For Each process In processes
        commandLine = LCase(process.CommandLine & "")
        For Each pattern In patterns
            If InStr(commandLine, LCase(pattern)) > 0 Then
                process.Terminate()
                Exit For
            End If
        Next
    Next

    On Error GoTo 0
End Sub
