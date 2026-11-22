copy input\*.qvm ..\Tools\DConv\input /Y
cd ..\Tools\DConv
dconv qvm decompile input ..\..\Decompile\output
del /q input\*
for /d %%x in (input\*) do @rd /s /q "%%x"

cd ..\..\Decompile
del /q input\*
for /d %%x in (input\*) do @rd /s /q "%%x"