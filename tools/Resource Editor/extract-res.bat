@ECHO OFF
SETLOCAL
set file=input\*.res
FOR %%i IN ("%file%") DO (
set filename=%%~ni%%~xi
)
echo ExtractResource("%filename%"); >Extract_Resource.qsc
gconv\gconv.exe Extract_Resource.qsc -InputPath=input/ -OutputPath=output/
del Extract_Resource.qsc
pause