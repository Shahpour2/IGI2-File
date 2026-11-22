@echo on
copy input\*.qsc ..\Tools\Gconv\input /Y
cd ..\Tools\GConv
dir /b /ad "input\*" | >nul findstr "^" | (compile_v7.bat)
