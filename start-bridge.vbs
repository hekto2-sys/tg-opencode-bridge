' Тихо (без окна) запускает мост Telegram <-> OpenCode
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\1C_dev\tg-opencode-bridge"
sh.Run """C:\Users\User\AppData\Local\Programs\Python\Python311\pythonw.exe"" ""C:\1C_dev\tg-opencode-bridge\bridge.py""", 0, False
