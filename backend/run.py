"""Windows 打包启动器：启动后端 + 自动打开浏览器访问界面。

PyInstaller 打包入口（--windowed 无控制台窗口）。
"""
import os
import sys
import threading
import time


def _open_browser(port: int = 8000):
    time.sleep(1.5)
    try:
        import webbrowser
        webbrowser.open(f"http://127.0.0.1:{port}")
    except Exception:
        pass


def _pick_port(start: int = 8000) -> int:
    """端口探测：8000 被占用时依次尝试 8001..8019，保证双击 exe 一定能起来
    （例如 dev 后端正占着 8000 时，exe 自动用 8001，不再"点了没反应"）。"""
    import socket
    for p in range(start, start + 20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    return start


def _ensure_data_dir():
    """数据目录：exe 旁 data/（打包后不可写 sys._MEIPASS）"""
    base = os.path.dirname(os.path.abspath(sys.executable if getattr(sys, "frozen", False) else __file__))
    data_dir = os.path.join(base, "data")
    os.makedirs(data_dir, exist_ok=True)
    # 数据库路径已在 app/database.py 配置为 backend/data；打包后 data 目录在 exe 旁，
    # 通过环境变量告知 database.py 使用 exe 旁 data
    os.environ.setdefault("BILI_DATA_DIR", data_dir)
    return data_dir


def _start_tray(port: int):
    """系统托盘常驻图标：右键菜单【打开页面 / 退出程序】，知道后台在运行。

    托盘失败（无桌面环境/库缺失）静默降级，不影响主服务。
    """
    try:
        import pystray
        from PIL import Image, ImageDraw

        def _make_icon():
            img = Image.new("RGB", (64, 64), (251, 114, 153))   # 粉红底
            d = ImageDraw.Draw(img)
            d.ellipse([14, 14, 50, 50], fill="white")
            d.text((23, 25), "B", fill=(251, 114, 153))
            return img

        def _on_open(icon, item):
            import webbrowser
            webbrowser.open(f"http://127.0.0.1:{port}")

        def _on_exit(icon, item):
            icon.stop()
            os._exit(0)   # 托盘退出 = 结束主进程（uvicorn 随进程退出，无残留）

        icon = pystray.Icon(
            "lottery-assistant", _make_icon(), "抽奖助手（后台运行中）",
            menu=pystray.Menu(
                pystray.MenuItem("打开页面", _on_open, default=True),
                pystray.MenuItem("退出程序", _on_exit),
            ))
        icon.run()
    except Exception:
        pass


if __name__ == "__main__":
    data_dir = _ensure_data_dir()
    # PyInstaller --windowed（无控制台）下 sys.stdout/stderr 可能为 None（用户双击运行时
    # 即如此），uvicorn 日志 DefaultFormatter 会调用 sys.stdout.isatty() → AttributeError
    # 崩溃。打包环境**无条件**将标准输出/错误重定向到 exe 旁 data/app.log
    # （文件对象 isatty()=False，uvicorn 正常；日志落盘便于排查问题）。
    if getattr(sys, "frozen", False):
        _log_f = open(os.path.join(data_dir, "app.log"),
                      "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = _log_f
    port = _pick_port()
    print(f"B站抽奖助手启动中，访问 http://127.0.0.1:{port}", flush=True)
    threading.Thread(target=_open_browser, args=(port,), daemon=True).start()
    # 系统托盘常驻（打开页面/退出程序）——后台运行可见、可操作
    threading.Thread(target=_start_tray, args=(port,), daemon=True).start()
    import uvicorn
    from app import main   # 直接导入（PyInstaller 可静态分析依赖），不用字符串
    # 调试：记录打包环境版本（定位带参路由异常），写入 data 目录
    try:
        import fastapi, starlette
        _dbg = os.path.join(data_dir, "startup_debug.log")
        with open(_dbg, "w", encoding="utf-8") as f:
            f.write(f"fastapi={getattr(fastapi, '__version__', '?')} "
                    f"starlette={getattr(starlette, '__version__', '?')}\n")
            f.write(f"routes={len(main.app.routes)}\n")
            f.write(f"app_file={getattr(main, '__file__', '?')}\n")
            f.write(f"port={port}\n")
    except Exception:
        pass
    uvicorn.run(main.app, host="127.0.0.1", port=port, log_level="info")
