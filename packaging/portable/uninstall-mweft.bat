@echo off
REM ===========================================================================
REM  MWeft portable uninstaller (Windows)
REM  Removes the local runtime + ALL local memory data. Read the steps below.
REM  Open this file in Notepad to inspect it (transparent - unsigned).
REM ===========================================================================
setlocal
chcp 65001 >nul
set "HERE=%~dp0"

echo ============================================================
echo  MemoryWeft - uninstall
echo ============================================================
echo This removes MemoryWeft's runtime and ALL local memory data.
echo.
echo Recommended order:
echo   1. FIRST, in the Manager (Settings -^> each AI -^> Remove),
echo      remove the mweft MCP server from your AI clients, then
echo      RESTART any global AIs (Claude Desktop, etc.).
echo      A script cannot safely edit each AI's config - do it there.
echo   2. This script then deletes:
echo        - "%USERPROFILE%\.mweft"   (project list / global config)
echo        - "%HERE%data"             (your SQLite memory DB)
echo        - "%HERE%runtime"          (the bundled Python venv)
echo   3. After it finishes, delete this folder itself:
echo        "%HERE%"
echo.
echo Postgres users: your memory DB lives on the server - delete it separately.
echo.
set /p "ANS=Type DELETE (all caps) to proceed, or anything else to cancel: "
if /i not "%ANS%"=="DELETE" (
  echo Cancelled - nothing was removed.
  pause
  exit /b 1
)

echo.
if exist "%USERPROFILE%\.mweft" ( rmdir /s /q "%USERPROFILE%\.mweft" && echo removed: %USERPROFILE%\.mweft )
if exist "%HERE%data"    ( rmdir /s /q "%HERE%data"    && echo removed: %HERE%data )
if exist "%HERE%runtime" ( rmdir /s /q "%HERE%runtime" && echo removed: %HERE%runtime )
del /q "%HERE%.mweft-ready" 2>nul

echo.
echo Done. The memory data and runtime are gone.
echo Now close this window and delete the folder:  "%HERE%"
echo (the rest is just the launcher and bundled binaries.)
pause
endlocal
