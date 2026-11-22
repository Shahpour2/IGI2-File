@echo on
gconv compile_scripts.qsc
cd input
move *.qvm ..\..\DConv\output
del *.qsc
cd ..\..\DConv
dconv.exe qvm convert output ..\..\Compile\output
cd output
del *.qvm
cd ..\..\..\Compile
del /q input\* 
for /d %%x in (input\*) do @rd /s /q "%%x"
cd output
for %%f in (*.qsc) do ren "%%f" "%%~nf.qvm"