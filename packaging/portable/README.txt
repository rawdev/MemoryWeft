MWeft — Portable Install (unsigned · unzip and run)
====================================================

This folder runs MWeft Manager with "unzip -> one launcher click". It does
not install Python on your system; everything runs self-contained inside
this folder. (To remove it, just delete this folder.)

(A Korean version of this file is available as readme_kr.txt.)

--------------------------------------------------------------------
Windows
--------------------------------------------------------------------
1) (Recommended) Right-click the downloaded zip -> "Properties" -> check
   "Unblock" at the bottom -> OK. Then extract it. (This clears the
   security mark-of-the-web.)
   * If you extract with 7-Zip, this step is unnecessary.
2) Double-click  start-mweft.bat  inside the folder.
3) First run only: automatic setup (1-5 min), then the Manager app window
   opens. If "Windows protected your PC" appears -> "More info" ->
   "Run anyway" (this is the unsigned-app notice).
   * The native window needs the WebView2 runtime (preinstalled on
     Windows 11; on Windows 10 install it from
     https://developer.microsoft.com/microsoft-edge/webview2/).

--------------------------------------------------------------------
macOS
--------------------------------------------------------------------
1) Double-click the zip to extract it.
2) Right-click  start-mweft.command  inside the folder -> "Open" ->
   confirm "Open". (First run only. This approves the unsigned app in
   Gatekeeper; afterwards a normal double-click works.)
3) A Terminal window opens; after the first-run setup, the Manager app
   window opens.

--------------------------------------------------------------------
Quit / Restart
--------------------------------------------------------------------
- Quit: close the Manager app window (the console/Terminal it launched
  from closes with it).
- Restart: run the launcher again — it skips setup and starts immediately.

--------------------------------------------------------------------
Connecting your AI client (MCP)
--------------------------------------------------------------------
The Manager app is just the control panel. To let an AI client (Claude
Code/Desktop, Cursor, Gemini CLI, ...) use this memory, register the MCP
server from the Manager's settings. It writes the client config for you,
pointing at this folder's bundled runtime + ONNX model, and sets
K2G_MCP_LAZY_INIT=true so the client doesn't time out on first connect
(heavy init is deferred to the first tool call).

--------------------------------------------------------------------
Data location / Updates
--------------------------------------------------------------------
- All memories and settings live in the  data/  folder (SQLite). You can
  back it up or move it as a whole folder.
- To update: replace with the new version folder, then copy your existing
  data/  into the new folder to keep your memories. (When VERSION differs,
  the runtime is rebuilt automatically while data/ is preserved.)

--------------------------------------------------------------------
Troubleshooting
--------------------------------------------------------------------
- Setup failure / odd behavior: delete the whole  runtime/  folder and run
  the launcher again. (Do NOT touch data/ — that preserves your memories.)
- Windows: if the app window doesn't appear, install the WebView2 runtime
  (see the Windows step above) and rerun the launcher.
