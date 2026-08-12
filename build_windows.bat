@echo off
rem ============================================================
rem  B站抽奖助手 - Windows 打包脚本
rem  产物：dist/B站抽奖助手/B站抽奖助手.exe（双击启动，自动打开浏览器）
rem  依赖：node（前端构建）+ python（PyInstaller）
rem ============================================================
setlocal
cd /d "%~dp0\.."

echo [1/3] 构建前端（vite build）...
cd frontend
node node_modules/vite/bin/vite.js build
if errorlevel 1 ( echo 前端构建失败 & exit /b 1 )
cd ..

echo [2/3] 检查 PyInstaller...
python -m PyInstaller --version >nul 2>&1 || python -m pip install pyinstaller -q

echo [3/3] 打包 exe...
python -m PyInstaller --noconfirm --onedir --name "B站抽奖助手" --windowed ^
  --add-data "frontend/dist;dist" ^
  --hidden-import uvicorn.logging ^
  --hidden-import uvicorn.loops.auto ^
  --hidden-import uvicorn.loops.asyncio ^
  --hidden-import uvicorn.protocols.http.auto ^
  --hidden-import uvicorn.protocols.http.h11_impl ^
  --hidden-import uvicorn.protocols.websockets.auto ^
  --hidden-import uvicorn.protocols.websockets.websockets_impl ^
  --hidden-import uvicorn.lifespan.on ^
  --hidden-import multipart ^
  --collect-all app ^
  backend/run.py
if errorlevel 1 ( echo 打包失败 & exit /b 1 )

echo.
echo 完成！可执行文件：dist\B站抽奖助手\B站抽奖助手.exe
echo 双击运行后自动打开浏览器访问 http://127.0.0.1:8000
endlocal
