Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")
base = fso.GetParentFolderName(WScript.ScriptFullName)

Set env = sh.Environment("PROCESS")
env("PATH") = base & "\deps;" & env("PATH")
env("AMD_OFFLINE") = "1"
env("AMD_GUI") = "1"

pythonw = base & "\.venv\pythonw.exe"
launcher = base & "\launcher.py"

If Not fso.FileExists(pythonw) Then
    MsgBox "Cannot find .venv\pythonw.exe. Please run from AppleMusicDecrypt-GUI folder.", vbCritical, "Apple Music Decrypt"
    WScript.Quit 1
End If

q = Chr(34)
cmd = q & pythonw & q & " " & q & launcher & q
sh.Run cmd, 0, False