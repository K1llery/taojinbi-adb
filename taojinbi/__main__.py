import argparse
import json
import sys
from dataclasses import asdict

from .adb import Adb, default_adb
from .model import Halt
from .runner import Runner


def main():
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="淘金币 ADB 浏览助手；无参数打开窗口")
    parser.add_argument("--adb", default=default_adb())
    parser.add_argument("--serial", default="")
    parser.add_argument("--scan", action="store_true", help="只读扫描，不执行点击")
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument("--task", action="append", help="执行指定站内浏览任务，可以重复指定")
    selection.add_argument("--all-browse", action="store_true", help="持续发现站内浏览与应用跳转任务，跟随名称与位置变化")
    parser.add_argument("--limit", type=int, default=1, help="命令行默认只执行1次；0表示全部")
    parser.add_argument("--query", default="鼠标", help="搜索任务的商品词，默认鼠标，最多40个字符")
    args = parser.parse_args()
    if not args.scan and not args.task and not args.all_browse:
        from .gui import main as gui_main
        gui_main()
        return 0
    if args.limit < 0 or args.limit > 50:
        parser.error("--limit 必须在0到50之间")
    adb = Adb(args.adb, args.serial)
    try:
        if not args.serial:
            devices = adb.devices()
            if len(devices) != 1:
                raise Halt("请通过 --serial 明确指定一台已授权设备")
            adb.serial = devices[0]
        runner = Runner(adb, lambda kind, value: print(value, flush=True) if kind == "log" else None,
                        search_query=args.query)
        print(f"本地记录：{runner.directory}", flush=True)
        if args.scan:
            tasks = runner.scan()
            print(json.dumps([asdict(task) for task in tasks], ensure_ascii=False, indent=2))
        else:
            runner.run(args.task or [], args.limit or None, all_browse=args.all_browse)
        return 0
    except KeyboardInterrupt:
        adb.control.stop()
        print("已停止", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"已停止：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
