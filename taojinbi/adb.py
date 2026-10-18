from __future__ import annotations

import re
import hashlib
import base64
import msvcrt
from contextlib import contextmanager
import shutil
import subprocess
import threading
import time
from pathlib import Path

from .model import Halt, PACKAGE, RefreshRequired, Snapshot, Stopped

ROOT = Path(__file__).resolve().parent.parent


class Control:
    def __init__(self):
        self.stopped = threading.Event()
        self.paused = threading.Event()
        self.lock = threading.RLock()
        self.epoch = 0
        self.paused_total = 0.0
        self.paused_at: float | None = None

    def pause(self):
        with self.lock:
            if self.paused_at is None:
                self.paused_at = time.monotonic()
            self.paused.set()
            self.epoch += 1

    def resume(self):
        with self.lock:
            if self.paused_at is not None:
                self.paused_total += time.monotonic() - self.paused_at
                self.paused_at = None
            self.paused.clear()
            self.epoch += 1

    def stop(self):
        with self.lock:
            self.stopped.set()

    def clock(self) -> float:
        with self.lock:
            return (self.paused_at if self.paused_at is not None else time.monotonic()) - self.paused_total

    def checkpoint(self):
        while self.paused.is_set():
            if self.stopped.wait(.1):
                raise Stopped("已停止")
        if self.stopped.is_set():
            raise Stopped("已停止")

    def wait(self, seconds: float):
        remaining = seconds
        while remaining > 0:
            self.checkpoint()
            started = time.monotonic()
            self.stopped.wait(min(.1, remaining))
            if not self.paused.is_set():
                remaining -= time.monotonic() - started
        self.checkpoint()


def default_adb() -> str:
    local = ROOT / "tools" / "platform-tools" / "adb.exe"
    return str(local) if local.exists() else (shutil.which("adb") or "")


class Adb:
    def __init__(self, path: str, serial: str = "", control: Control | None = None):
        self.path = str(Path(path).resolve()) if path else ""
        self.serial = serial
        self.control = control or Control()
        self.size: tuple[int, int] | None = None
        self.sequence = 0
        self.reader_path = ""
        self.lease_stream = None

    @contextmanager
    def lease(self):
        if self.lease_stream is not None:
            yield
            return
        folder = ROOT / "artifacts" / "locks"
        folder.mkdir(parents=True, exist_ok=True)
        key = hashlib.sha256(self.serial.encode()).hexdigest()[:16]
        stream = (folder / f"{key}.lock").open("a+b")
        if stream.tell() == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            stream.close()
            raise Halt("该设备已有工具会话正在使用，请先停止另一窗口或命令行中的会话。") from exc
        self.lease_stream = stream
        try:
            yield
        finally:
            self.lease_stream = None
            stream.close()

    def run(self, args: list[str], timeout: float = 25, epoch: int | None = None) -> bytes:
        self.control.checkpoint()
        if not self.path or not Path(self.path).is_file():
            raise Halt("未找到 adb.exe，请在窗口中选择 Platform Tools 内的 adb.exe。")
        command = [self.path] + (["-s", self.serial] if self.serial else []) + args
        with self.control.lock:
            if self.control.stopped.is_set():
                raise Stopped("已停止")
            if epoch is not None and (self.control.paused.is_set() or epoch != self.control.epoch):
                raise RefreshRequired()
            proc = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        deadline = time.monotonic() + timeout
        try:
            while True:
                if self.control.stopped.is_set():
                    raise Stopped("已停止；不再发送操作指令")
                if time.monotonic() > deadline:
                    raise Halt(f"ADB 命令超时：{' '.join(args[:3])}")
                try:
                    out, err = proc.communicate(timeout=.15)
                    break
                except subprocess.TimeoutExpired:
                    pass
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()
        combined = (out + err).decode("utf-8", "replace")
        if "INJECT_EVENTS" in combined or "SecurityException" in combined:
            raise Halt("手机拒绝模拟输入。小米请开启 USB 调试（安全设置）并重启手机。")
        if proc.returncode or "ERROR:" in combined or "Exception occurred" in combined:
            raise Halt(combined.strip()[:700] or f"ADB 错误：{proc.returncode}")
        return out

    def devices(self) -> list[str]:
        raw = self.run(["devices", "-l"]).decode("utf-8", "replace")
        return [line.split()[0] for line in raw.splitlines() if re.match(r"^\S+\s+device(?:\s|$)", line)]

    def dimensions(self) -> tuple[int, int]:
        if self.size is None:
            raw = self.run(["shell", "wm", "size"]).decode()
            sizes = re.findall(r"(\d+)x(\d+)", raw)
            if not sizes:
                raise Halt("无法确认手机分辨率")
            self.size = tuple(map(int, sizes[-1]))
        return self.size

    def foreground(self) -> str:
        """Return the resumed package, or "" while the system has not settled yet."""
        activity = self.run(["shell", "dumpsys", "activity", "activities"]).decode("utf-8", "replace")
        match = re.search(r"(?:topResumedActivity|mResumedActivity|ResumedActivity)\s*[=:].*?\s([\w.]+)/",
                          activity)
        return match[1] if match else ""

    def launch_taobao(self):
        self.run(["shell", "am", "start", "-n", f"{PACKAGE}/com.taobao.tao.welcome.Welcome"])

    def snapshot(self) -> Snapshot:
        self.control.checkpoint()
        epoch = self.control.epoch
        if not self.reader_path:
            helper = ROOT / "tools" / "ui-dump.jar"
            if not helper.is_file():
                raise Halt("缺少 tools/ui-dump.jar，请按说明构建页面读取器。")
            digest = hashlib.sha256(helper.read_bytes()).hexdigest()[:12]
            remote = f"/data/local/tmp/taojinbi-ui-{digest}.jar"
            self.run(["push", str(helper), remote])
            self.reader_path = remote
        classpath = f"/system/framework/android.test.runner.jar:/system/framework/uiautomator.jar:{self.reader_path}"
        xml = self.run(["shell", "ANDROID_DATA=/data/local/tmp", f"CLASSPATH={classpath}",
                        "app_process", "/system/bin", "UiDump"], timeout=15).decode("utf-8", "replace")
        package = self.foreground()
        if not package:
            raise Halt("无法确认前台应用")
        return Snapshot(xml, package, *self.dimensions(), epoch=epoch)

    def input(self, snapshot: Snapshot, *args: str):
        self.run(["shell", "input", *args], epoch=snapshot.epoch)

    def set_search(self, snapshot: Snapshot, query: str):
        if not self.reader_path:
            raise Halt("页面读取器尚未初始化")
        classpath = f"/system/framework/android.test.runner.jar:/system/framework/uiautomator.jar:{self.reader_path}"
        encoded = base64.b64encode(query.encode("utf-8")).decode("ascii")
        command = "set-web-search" if snapshot.page_kind() == "web_search" else "set-search"
        output = self.run(["shell", "ANDROID_DATA=/data/local/tmp", f"CLASSPATH={classpath}",
                           "app_process", "/system/bin", "UiDump", command, encoded],
                          timeout=15, epoch=snapshot.epoch)
        if output.strip() != b"OK":
            raise Halt("搜索文字设置结果未确认")

    def screenshot(self, path: Path):
        data = self.run(["exec-out", "screencap", "-p"])
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise Halt("手机截图返回格式无效")
        path.write_bytes(data)
