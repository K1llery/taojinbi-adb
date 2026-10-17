from __future__ import annotations

import json
import random
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .adb import Adb, ROOT
from .model import Halt, RefreshRequired, Snapshot, Stopped, Task, supported_title


@dataclass(frozen=True)
class Result:
    title: str
    before: int
    after: int | None
    success: bool
    reason: str


def verify(before: Task, after: Task | None, before_total: int | None,
           after_total: int | None, fully_scanned: bool = False) -> bool:
    if after is not None:
        return after.total == before.total and after.done > before.done
    return (fully_scanned and before.total == 1 and before_total is not None and
            after_total is not None and after_total > before_total)


def advanced_task(before: Task, baseline: list[Task], current: list[Task],
                  before_total: int | None, current_total: int | None) -> Task | None:
    """A rotating caption must never turn into a stable task identifier."""
    if before_total is None or current_total != before_total + 1:
        return None
    old = Counter(t.done for t in baseline if t.group == before.group)
    new = Counter(t.done for t in current if t.group == before.group)
    if old[before.done] != 1:
        return None
    old[before.done] -= 1
    if not old[before.done]:
        del old[before.done]
    old[before.done + 1] += 1
    if old != new:
        return None
    matches = [t for t in current if t.group == before.group and t.done == before.done + 1]
    return matches[0] if len(matches) == 1 else None


class Runner:
    def __init__(self, adb: Adb, emit: Callable[[str, object], None] | None = None,
                 directory: Path | None = None):
        self.adb = adb
        self.control = adb.control
        self.emit = emit or (lambda kind, value: None)
        self.directory = directory or ROOT / "artifacts" / datetime.now().astimezone().strftime("run-%Y%m%d-%H%M%S-%f")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.last: Snapshot | None = None
        self.steps = 0
        self.results: list[Result] = []
        self.baseline_tasks: list[Task] = []

    def log(self, message: str, **data):
        event = {"time": datetime.now().astimezone().isoformat(timespec="seconds"),
                 "message": message, **data}
        with (self.directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        self.emit("log", message)

    def state(self, message: str):
        self.emit("state", message)
        self.log(message)

    def observe(self) -> Snapshot:
        s = self.adb.snapshot()
        self.last = s
        self.steps += 1
        (self.directory / f"{self.steps:03d}.xml").write_text(s.xml, encoding="utf-8")
        s.check()
        return s

    def list_only(self, s: Snapshot):
        if not s.is_list:
            raise Halt("当前页面不是“做任务赚能量”列表，请手动返回列表后重新开始。")

    def browse_only(self, s: Snapshot, title: str):
        if not s.browse_page(title):
            raise Halt(f"无法确认“{title}”的浏览页面，已停止输入。")

    def action(self, context: str, builder: Callable[[Snapshot], list[str]], title: str = "") -> Snapshot:
        while True:
            s = self.observe()
            if context == "list":
                self.list_only(s)
            else:
                self.browse_only(s, title)
            args = builder(s)
            try:
                self.adb.input(s, *args)
                self.log("发送操作", action=args[0], task=title)
                return s
            except RefreshRequired:
                # A pause invalidates every coordinate and page observation.
                self.control.checkpoint()

    def scan(self) -> list[Task]:
        with self.adb.lease():
            return self._scan()

    def _scan(self) -> list[Task]:
        self.state("正在读取任务列表")
        s = self.observe()
        self.list_only(s)
        tasks = s.tasks()
        self.emit("tasks", tasks)
        self.log("任务列表已读取", progress=s.progress,
                 tasks=[{"title": t.title, "done": t.done, "total": t.total} for t in tasks])
        return tasks

    def list_scroll(self, down: bool):
        def build(s):
            # Drag in the text column, away from buttons and the fixed header.
            x = round(s.width * .40)
            top, bottom = round(s.height * .44), round(s.height * .81)
            return ["swipe", str(x), str(bottom if down else top), str(x),
                    str(top if down else bottom), str(random.randint(450, 750))]
        self.action("list", build)
        self.control.wait(random.uniform(1.5, 2.5))

    def find(self, title: str) -> tuple[Snapshot, Task | None, bool]:
        """Find a visible task; absent only means absent after top-to-bottom traversal."""
        s = self.observe()
        self.list_only(s)
        task = s.task(title)
        if task and task.button and task.button.valid(s.width, s.height):
            return s, task, False
        # First establish the top of the list, then traverse to the bottom.
        for down in (False, True):
            previous = None
            for _ in range(12):
                self.list_scroll(down)
                s = self.observe()
                self.list_only(s)
                task = s.task(title)
                if task and task.button and task.button.valid(s.width, s.height):
                    return s, task, False
                fingerprint = s.fingerprint()
                if not fingerprint:
                    raise Halt("滚动后任务区域无法识别")
                if fingerprint == previous:
                    if down:
                        return s, task, True
                    break
                previous = fingerprint
            else:
                raise Halt("任务列表滚动超过上限，无法确认是否完整")
        raise Halt("无法定位任务列表边界")

    def tap_task(self, title: str) -> tuple[Task, int | None]:
        def build(s):
            task = s.task(title)
            if not task or not task.supported or not task.remaining or not task.button:
                raise Halt(f"任务已变化或无法执行：{title}")
            if not task.button.valid(s.width, s.height):
                raise Halt("任务按钮不可见，需要重新扫描")
            x, y = task.button.center
            return ["tap", str(x), str(y)]
        s = self.action("list", build, title)
        task = s.task(title)
        assert task is not None
        self.baseline_tasks = s.tasks()
        return task, s.progress

    def enter(self, title: str) -> Snapshot:
        deadline = self.control.clock() + 30
        while self.control.clock() < deadline:
            s = self.observe()
            if s.browse_page(title):
                self.adb.screenshot(self.directory / "entered.png")
                return s
            if not s.is_list:
                # Allow loading placeholders, but never interact with unknown pages.
                self.log("等待目标页加载")
            self.control.wait(2)
        raise Halt("30秒内没有确认目标浏览页；未重复点击")

    def browse(self, task: Task):
        self.state(f"浏览：{task.title}")
        started = self.control.clock()
        minimum = (task.seconds or 15) + 3
        # Task-specific deadline includes only active time, excluding explicit pauses.
        while self.control.clock() - started < minimum:
            self.control.wait(random.uniform(3, 6))
            s = self.observe()
            self.browse_only(s, task.title)
            if s.completed_badge() and self.control.clock() - started >= (task.seconds or 15):
                self.log("检测到完成提示，等待返回后的进度核验")
                break
            if self.control.clock() - started >= minimum:
                break
            if supported_title(task.title):
                # Live page auto-rotates product images; no scroll is necessary.
                continue
            def build(current):
                x = round(current.width * .40)
                y1 = round(current.height * .76)
                y2 = y1 - round(current.height * random.uniform(.25, .40))
                return ["swipe", str(x), str(y1), str(x), str(y2), str(random.randint(400, 800))]
            self.action("browse", build, task.title)
        self.adb.screenshot(self.directory / "before-return.png")

    def return_to_list(self, title: str) -> Snapshot:
        self.state("返回并核验任务进度")
        self.action("browse", lambda s: ["keyevent", "4"], title)
        deadline = self.control.clock() + 15
        while self.control.clock() < deadline:
            s = self.observe()
            if s.is_list:
                return s
            self.control.wait(2)
        raise Halt("返回后未识别到任务列表，已停止；不会连续按返回键")

    def execute_one(self, title: str) -> Result:
        self.state(f"准备进入：{title}")
        before, before_total = self.tap_task(title)
        self.enter(title)
        self.browse(before)
        s = self.return_to_list(title)
        deadline = self.control.clock() + 15
        full = False
        after = s.task(title)
        while True:
            advanced = advanced_task(before, self.baseline_tasks, s.tasks(), before_total, s.progress)
            if advanced is not None or (before.total == 1 and verify(before, after, before_total, s.progress, full)):
                confirmed = advanced or after
                result = Result(title, before.done, confirmed.done if confirmed else None, True, "任务进度已确认")
                if advanced and advanced.title != title:
                    self.log("任务名称已轮换，次数变化唯一匹配", previous=title, current=advanced.title)
                self.results.append(result)
                self.log("任务完成", **asdict(result), global_progress=s.progress)
                self.emit("result", result)
                self.emit("tasks", s.tasks())
                self.adb.screenshot(self.directory / f"completed-{len(self.results)}.png")
                return result
            if before.total == 1 and after is None and not full:
                s, after, full = self.find(title)
            elif self.control.clock() < deadline:
                self.control.wait(2)
                s = self.observe()
                self.list_only(s)
                after = s.task(title)
            else:
                raise Halt(f"“{title}”的进度没有确认增加，已停止；不会自动重做")

    def run(self, titles: list[str], limit: int | None = None):
        with self.adb.lease():
            return self._run(titles, limit)

    def _run(self, titles: list[str], limit: int | None = None):
        try:
            if not titles:
                raise Halt("请先扫描并勾选任务")
            if any(not supported_title(title) for title in titles):
                raise Halt("包含尚未通过实测的任务，请重新扫描并选择可执行项目")
            self.log("会话开始", selected=titles, limit=limit)
            self.scan()
            while True:
                pending = False
                for title in titles:
                    s, task, full = self.find(title)
                    if not task or not task.remaining:
                        self.log("当前选定任务已耗尽或不在完整列表中", task=title)
                        continue
                    if not task.supported:
                        raise Halt(f"任务尚未适配：{title}")
                    pending = True
                    self.execute_one(title)
                    if limit is not None and len(self.results) >= limit:
                        self.state(f"达到本次上限：{len(self.results)} 项已完成")
                        return
                    self.state("任务间阅读间隔")
                    self.control.wait(random.uniform(5, 10))
                if not pending:
                    self.state("选定任务已耗尽，会话结束")
                    return
        except Stopped as exc:
            self.log(str(exc))
            self.emit("state", "已停止")
        except Exception as exc:
            self.fail(exc)
            raise
        finally:
            (self.directory / "results.json").write_text(
                json.dumps([asdict(r) for r in self.results], ensure_ascii=False, indent=2), encoding="utf-8")

    def fail(self, exc: Exception):
        self.state(f"已停止：{exc}")
        # No input during error handling. Evidence capture is best-effort only.
        try:
            self.adb.screenshot(self.directory / "failure.png")
        except Exception:
            pass
