"""python -m crossborder_selector.web：启动本地 Web 向导。"""
import argparse
import sys
import webbrowser

from crossborder_selector.cli import default_factory
from crossborder_selector.web.api import Api
from crossborder_selector.web.demo import demo_factory, install_demo
from crossborder_selector.web.runs import RunManager
from crossborder_selector.web.server import make_server


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="crossborder-web")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--output-dir", default="./out")
    p.add_argument("--config")
    p.add_argument("--demo", action="store_true", help="演示模式：不接触 AWS，用模拟数据走完整流程")
    p.add_argument("--no-browser", action="store_true")
    a = p.parse_args(argv)
    api = Api(factory=demo_factory if a.demo else default_factory, config_path=a.config)
    mgr = RunManager(api, a.output_dir)
    if a.demo:
        install_demo(mgr)
    server = make_server(a.host, a.port, api, mgr, demo=a.demo)
    url = f"http://{a.host}:{server.server_address[1]}"
    print(f"Web 向导：{url}" + ("（演示模式，不接触 AWS）" if a.demo else ""))
    if not a.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。运行中的 run 线程随进程结束；如有遗留实例请用 cleanup --run-id 清理。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
