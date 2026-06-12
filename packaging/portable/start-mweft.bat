@echo off
REM ===========================================================================
REM  MWeft portable launcher (Windows) - docs/mweft_public/06_portable_launcher.md
REM  Unzip then double-click this file. First run sets up once, then starts fast.
REM  Open this file in Notepad to inspect it (transparent - unsigned).
REM ===========================================================================
setlocal enabledelayedexpansion
chcp 65001 >nul

set "HERE=%~dp0"
REM Package: mweft[embed-onnx,manager,postgres]. Entry = the native Manager desktop app (mweft-app).
set "PKG=mweft[embed-onnx,manager,postgres]"
set "MOD=k2g.desktop"

set "UV=%HERE%bin\uv.exe"
set "VENV=%HERE%runtime\venv"
set "PYEXE=%VENV%\Scripts\python.exe"
set "READY=%HERE%.mweft-ready"

REM --- Self-contained locations (no system intrusion) ---
set "UV_PYTHON_INSTALL_DIR=%HERE%runtime\python"
set "UV_CACHE_DIR=%HERE%runtime\cache"
REM Use a uv-managed standalone Python ONLY. Never adopt a Python already on the
REM host (e.g. a conda base env): an inherited interpreter leaks its own DLL
REM search dirs - an older vcruntime/msvcp wins and onnxruntime's native module
REM then fails to load (WinError 1114) - and the venv is not self-contained on
REM machines that lack that interpreter. only-managed makes uv download a
REM python-build-standalone into UV_PYTHON_INSTALL_DIR (needs network on first
REM setup).
set "UV_PYTHON_PREFERENCE=only-managed"

REM --- Runtime settings ---
set "K2G_DOTENV_FILE=off"
set "K2G_USE_HUB=auto"
set "EMBEDDING_PROVIDER=onnx"
set "EMBEDDING_ONNX_PATH=%HERE%models\bge-m3-onnx"
set "EMBEDDING_DIM=1024"
REM Bundled MSVC runtime for onnxruntime's native module — lets it load on a
REM clean Windows box without the VC++ Redistributable installed.
set "MWEFT_VC_RUNTIME_DIR=%HERE%vc_runtime"
REM Bundled first-run sample DB — a fresh install seeds explorable demo data
REM (domain sample_work) + a one-time intro instead of a blank create-DB prompt.
set "MWEFT_SAMPLE_DIR=%HERE%asset\mweft_sample"
REM Lazy init: MCP servers the Manager registers defer heavy init to the first
REM tool call, so the AI client doesn't hit a startup timeout on first connect.
set "K2G_MCP_LAZY_INIT=true"

REM --- Upgrade detection: rebuild runtime on VERSION mismatch (data is kept) ---
set "STAMP=%HERE%runtime\.version"
if exist "%HERE%VERSION" if exist "%STAMP%" (
  fc /b "%HERE%VERSION" "%STAMP%" >nul 2>&1 || (
    echo [MWeft] New version detected - resetting runtime...
    rmdir /s /q "%VENV%" 2>nul
    del /q "%READY%" 2>nul
  )
)

REM --- Skip when already set up ---
if exist "%READY%" if exist "%PYEXE%" goto launch

echo [MWeft] First-time setup... (once only, 1-5 min depending on network/specs)
REM Pin to 3.11 to match the cp311 wheels bundled by the offline build
REM (release.yml uses Python 3.11). Keep both sides in lockstep when bumping.
"%UV%" venv "%VENV%" --python 3.11 --python-preference only-managed
if errorlevel 1 goto err
if exist "%HERE%wheels" (
  REM full-offline: use bundled wheels only (zero network)
  "%UV%" pip install --python "%PYEXE%" --no-index --find-links "%HERE%wheels" %PKG%
) else (
  REM online: install from PyPI
  "%UV%" pip install --python "%PYEXE%" %PKG%
)
if errorlevel 1 goto err
REM Belt: onnxruntime.dll's own directory is ALWAYS searched for its native
REM dependencies, even under altered DLL search (add_dll_directory). Place the
REM bundled MSVC runtime right next to it so no host copy (e.g. an older conda
REM vcruntime/msvcp) can win and break the native load (WinError 1114). This is
REM Microsoft's sanctioned app-local deployment and is host-independent.
set "ORT_CAPI=%VENV%\Lib\site-packages\onnxruntime\capi"
if exist "%ORT_CAPI%" copy /y "%HERE%vc_runtime\*.dll" "%ORT_CAPI%\" >nul 2>&1
if exist "%HERE%VERSION" copy /y "%HERE%VERSION" "%STAMP%" >nul
echo ready> "%READY%"

:launch
if not exist "%HERE%data\project" mkdir "%HERE%data\project"
echo [MWeft] Starting the Manager app - a window will open shortly.
echo [MWeft] (The native window needs the WebView2 runtime; preinstalled on Windows 11.)
echo [MWeft] To quit, close the app window.
"%PYEXE%" -m %MOD% --project-dir-default "%HERE%data\project"
goto end

:err
echo.
echo [MWeft] Setup failed. Delete the runtime\cache folder and retry, or see README.txt.
pause
:end
endlocal
