from __future__ import annotations

import json
import random
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .adb import Adb, ROOT
from .model import (PACKAGE, Halt, Rect, RefreshRequired, Snapshot, Stopped, Task,
                    TaskChanged, supported_title)

# A jump task grants its reward on entry, so it only needs the external app to appear.
JUMP_DWELL = 2.0
JUMP_LAUNCH_TIMEOUT = 20
BACK_ATTEMPTS = 3


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


def completed_transition(started: bool, finished: bool, before: int | None, after: int | None) -> bool:
    return started and finished and before is not None and after == before + 1


class Runner:
    def __init__(self, adb: Adb, emit: Callable[[str, object], None] | None = None,
                 directory: Path | None = None, search_query: str = "鼠标"):
        self.adb = adb
        self.control = adb.control
        self.emit = emit or (lambda kind, value: None)
        self.directory = directory or ROOT / "artifacts" / datetime.now().astimezone().strftime("run-%Y%m%d-%H%M%S-%f")
        self.directory.mkdir(parents=True, exist_ok=True)
        self.last: Snapshot | None = None
        self.steps = 0
        self.results: list[Result] = []
        self.baseline_tasks: list[Task] = []
        self.page = ""
        self.started_signal = False
        self.finished_signal = False
        search_query = search_query.strip()
        if not 1 <= len(search_query) <= 40 or any(ord(c) < 32 for c in search_query):
            raise Halt("搜索词须为1至40个可见字符")
        self.search_query = search_query
        self.searched = False
        self.skip: set[str] = set()

    def log(self, message: str, **data):
        event = {"time": datetime.now().astimezone().isoformat(timespec="seconds"),
                 "message": message, **data}
        with (self.directory / "events.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        self.emit("log", message)

    def state(self, message: str):
        self.emit("state", message)
        self.log(message)

    def observe(self, external: bool = False) -> Snapshot:
        s = self.adb.snapshot()
        self.last = s
        self.steps += 1
        (self.directory / f"{self.steps:03d}.xml").write_text(s.xml, encoding="utf-8")
        s.check(external)
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
            elif context in ("overlay", "feed", "guide", "product", "search", "main_search", "web_search"):
                if s.page_kind() != context:
                    raise Halt(f"页面已变化，预期 {context}，已停止输入")
            else:
                self.browse_only(s, title)
            args = builder(s)
            try:
                if args[0] == "set-search":
                    self.adb.set_search(s, args[1])
                else:
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
        if task and not task.remaining:
            return s, task, False
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

    @staticmethod
    def candidate(tasks: list[Task]) -> Task | None:
        pending = [t for t in tasks if t.supported and t.remaining]
        return min(pending, key=lambda t: (t.seconds or 120, t.done, t.title)) if pending else None

    def discover(self) -> Task | None:
        s = self.observe()
        self.list_only(s)
        self.emit("tasks", s.tasks())
        candidate = self.candidate([t for t in s.tasks() if t.title not in self.skip])
        if candidate:
            return candidate
        # Verify exhaustion across the whole scrollable list, not only the viewport.
        for down in (False, True):
            previous = None
            for _ in range(12):
                self.list_scroll(down)
                s = self.observe()
                self.list_only(s)
                candidate = self.candidate([t for t in s.tasks() if t.title not in self.skip])
                if candidate:
                    return candidate
                fingerprint = s.fingerprint()
                if not fingerprint:
                    raise Halt("滚动后任务区域无法识别，无法确认浏览任务是否耗尽")
                if fingerprint == previous:
                    break
                previous = fingerprint
            else:
                raise Halt("列表范围无法确认，停止自动发现")
        self.log("已检查完整列表，没有剩余可执行任务",
                 skipped=[{"title": t.title, "reason": t.skip_reason or "已完成"} for t in s.tasks()])
        return None

    def tap_task(self, title: str) -> tuple[Task, int | None]:
        def build(s):
            task = s.task(title)
            if not task or not task.remaining:
                raise TaskChanged(f"点击前任务已换名或完成：{title}")
            if not task.supported or not task.button:
                raise Halt(f"任务无法执行：{title}")
            if not task.button.valid(s.width, s.height):
                raise Halt("任务按钮不可见，需要重新扫描")
            x, y = task.button.center
            return ["tap", str(x), str(y)]
        s = self.action("list", build, title)
        task = s.task(title)
        assert task is not None
        self.baseline_tasks = s.tasks()
        return task, s.progress

    def enter(self, task: Task) -> Snapshot:
        self.searched = False
        deadline = self.control.clock() + 30
        while self.control.clock() < deadline:
            s = self.observe()
            kind = s.page_kind()
            if kind in ("search", "web_search") and not self.searched:
                self.start_search(kind)
                self.searched = True
                continue
            if kind in ("overlay", "feed", "guide", "main_search"):
                if not s.task_in_progress(task) and not s.task_completed(task):
                    raise Halt("目标页任务组件与本次浏览要求不符")
                self.page = kind
                self.started_signal = s.task_in_progress(task)
                self.finished_signal = False
                self.log("已识别浏览页面", page=kind, task=task.title)
                self.adb.screenshot(self.directory / "entered.png")
                return s
            if not s.is_list:
                # Allow loading placeholders, but never interact with unknown pages.
                self.log("等待目标页加载")
            self.control.wait(2)
        raise Halt("30秒内没有确认目标浏览页；未重复点击")

    def start_search(self, kind: str = "search"):
        self.state("提交商品搜索词，等待浏览任务组件")
        def focus(s):
            field = s.search_field()
            if field is None:
                raise Halt("未找到可编辑的淘宝搜索框")
            return ["tap", *map(str, Rect.parse(field.get("bounds", "")).center)]
        self.action(kind, focus)
        def type_query(s):
            if s.search_field(focused=True) is None:
                raise Halt("搜索框未获得焦点，未发送文字")
            return ["set-search", self.search_query]
        self.action(kind, type_query)
        def submit(s):
            field, button = s.search_field(), s.search_button()
            if field is None or button is None or field.get("text") != self.search_query:
                raise Halt("搜索框内容未确认，未提交搜索")
            return ["tap", *map(str, Rect.parse(button.get("bounds", "")).center)]
        self.action(kind, submit)

    def back(self, source: str, destination: str | tuple[str, ...], title: str) -> Snapshot:
        destinations = (destination,) if isinstance(destination, str) else destination
        self.action(source, lambda s: ["keyevent", "4"], title)
        deadline = self.control.clock() + 15
        while self.control.clock() < deadline:
            s = self.observe()
            if s.page_kind() in destinations:
                return s
            self.control.wait(2)
        raise Halt(f"返回后未识别到 {destination}，已停止；不会连续按返回键")

    def browse_product(self, task: Task):
        self.state("进入商品阅读，不执行购买或加购")
        def card(s):
            rect = s.product_card()
            if rect is None:
                raise Halt("未找到完整可见的商品卡片")
            return ["tap", *map(str, rect.center)]
        self.action("feed", card, task.title)
        deadline = self.control.clock() + 30
        product = None
        while self.control.clock() < deadline:
            s = self.observe()
            if s.page_kind() == "product":
                product = s
                break
            self.control.wait(2)
        if product is None:
            raise Halt("没有确认商品详情页，已停止输入")
        started = self.control.clock()
        while self.control.clock() - started < (task.seconds or 15) + 3:
            self.control.wait(random.uniform(3, 6))
            s = self.observe()
            if s.page_kind() != "product":
                raise Halt("商品阅读期间页面已变化")
        s = self.back("product", "feed", task.title)
        deadline = self.control.clock() + 15
        while not s.task_completed(task) and self.control.clock() < deadline:
            self.control.wait(2)
            s = self.observe()
            if s.page_kind() != "feed":
                raise Halt("返回商品列表后页面已变化")
        self.finished_signal = s.task_completed(task)

    def browse(self, task: Task):
        self.state(f"浏览：{task.title}")
        if self.page == "feed":
            self.browse_product(task)
            self.adb.screenshot(self.directory / "before-return.png")
            return
        started = self.control.clock()
        deadline = started + max((task.seconds or 15) + 20, 30)
        while self.control.clock() < deadline:
            self.control.wait(random.uniform(3, 6))
            s = self.observe()
            if s.page_kind() != self.page:
                raise Halt("浏览期间页面已变化，停止输入")
            if s.task_completed(task):
                self.finished_signal = True
                self.log("检测到完成提示，等待返回后的进度核验")
                break
            if self.page in ("overlay", "main_search"):
                continue
            def build(current):
                x = round(current.width * .40)
                y1 = round(current.height * .76)
                y2 = y1 - round(current.height * random.uniform(.25, .40))
                return ["swipe", str(x), str(y1), str(x), str(y2), str(random.randint(400, 800))]
            self.action(self.page, build, task.title)
        self.adb.screenshot(self.directory / "before-return.png")

    def return_to_list(self, title: str) -> Snapshot:
        self.state("返回并核验任务进度")
        if self.searched:
            s = self.back(self.page, ("list", "search", "web_search"), title)
            if s.is_list:
                return s
            def leave_search(s):
                button = s.search_back()
                if button is None:
                    raise Halt("搜索页返回入口不明确")
                return ["tap", *map(str, button.center)]
            self.action(s.page_kind(), leave_search, title)
            deadline = self.control.clock() + 15
            while self.control.clock() < deadline:
                s = self.observe()
                if s.is_list:
                    return s
                self.control.wait(2)
            raise Halt("退出搜索后未回到任务列表")
        return self.back(self.page, "list", title)

    def execute_one(self, title: str) -> Result:
        self.state(f"准备进入：{title}")
        before, before_total = self.tap_task(title)
        self.enter(before)
        self.browse(before)
        s = self.return_to_list(title)
        deadline = self.control.clock() + 15
        while True:
            advanced = advanced_task(before, self.baseline_tasks, s.tasks(), before_total, s.progress)
            transition = completed_transition(self.started_signal, self.finished_signal, before_total, s.progress)
            if advanced is not None or transition:
                # Counter ambiguity is recorded explicitly; never borrow a different row's count.
                count = advanced.done if advanced else None
                reason = "任务进度已确认" if advanced else "页面从浏览中转为完成，且总进度增加1；单项次数未归因"
                result = Result(title, before.done, count, True, reason)
                if advanced and advanced.title != title:
                    self.log("任务名称已轮换，次数变化唯一匹配", previous=title, current=advanced.title)
                self.results.append(result)
                self.log("任务完成", **asdict(result), global_progress=s.progress)
                self.emit("result", result)
                self.emit("tasks", s.tasks())
                self.adb.screenshot(self.directory / f"completed-{len(self.results)}.png")
                return result
            if self.control.clock() < deadline:
                self.control.wait(2)
                s = self.observe()
                self.list_only(s)
            else:
                raise Halt(f"“{title}”的进度没有确认增加，已停止；不会自动重做")

    def external_input(self, *args: str, title: str = ""):
        """Send one gesture while another app is in front; no page model is assumed."""
        while True:
            s = self.observe(external=True)
            try:
                self.adb.input(s, *args)
                self.log("发送操作", action=args[0], task=title)
                return
            except RefreshRequired:
                self.control.checkpoint()

    def jump_enter(self, title: str) -> str:
        """Wait for the external app to take the foreground; its reward is granted on entry."""
        deadline = self.control.clock() + JUMP_LAUNCH_TIMEOUT
        while self.control.clock() < deadline:
            package = self.adb.foreground()
            if package != PACKAGE:
                self.log("已跳转到外部应用", task=title, package=package)
                self.adb.screenshot(self.directory / "jumped.png")
                return package
            self.control.wait(1)
        raise Halt("点击去完成后没有确认外部应用到前台")

    def jump_back(self, title: str) -> Snapshot:
        """Resume Taobao's own task; the external app's depth never has to be counted."""
        self.state("从外部应用返回淘宝")
        self.adb.launch_taobao()
        deadline = self.control.clock() + 10
        while self.control.clock() < deadline:
            s = self.observe(external=True)
            if s.package == PACKAGE and s.is_list:
                return s
            self.control.wait(2)
        if self.adb.foreground() == PACKAGE:
            raise Halt("已回到淘宝，但没有识别到任务列表，请手动返回列表后重新开始")
        # Fallback only when the direct resume did not switch apps at all.
        for _ in range(BACK_ATTEMPTS):
            if self.adb.foreground() == PACKAGE:
                break
            self.external_input("keyevent", "4", title=title)
            self.control.wait(2)
        deadline = self.control.clock() + 10
        while self.control.clock() < deadline:
            s = self.observe(external=True)
            if s.package == PACKAGE and s.is_list:
                return s
            self.control.wait(2)
        raise Halt("从外部应用返回后没有回到任务列表")

    def execute_jump(self, title: str) -> Result:
        """Jump tasks leave no readable evidence outside Taobao; only the global counter counts."""
        self.state(f"跳转执行：{title}")
        before, before_total = self.tap_task(title)
        package = self.jump_enter(title)
        self.control.wait(JUMP_DWELL)
        s = self.jump_back(title)
        deadline = self.control.clock() + 15
        while True:
            if before_total is not None and s.progress == before_total + 1:
                result = Result(title, before.done, None, True,
                                f"跳转 {package} 后总进度增加1；单项次数未归因")
                self.results.append(result)
                self.log("任务完成", **asdict(result), global_progress=s.progress, package=package)
                self.emit("result", result)
                self.emit("tasks", s.tasks())
                self.adb.screenshot(self.directory / f"completed-{len(self.results)}.png")
                return result
            if self.control.clock() >= deadline:
                break
            self.control.wait(2)
            s = self.observe()
            self.list_only(s)
        result = Result(title, before.done, None, False, "跳转后总进度没有增加1")
        self.log("跳转任务未确认完成", **asdict(result), global_progress=s.progress, package=package)
        self.emit("jump_failed", result)
        return result

    def run(self, titles: list[str], limit: int | None = None, all_browse: bool = False):
        with self.adb.lease():
            return self._run(titles, limit, all_browse)

    def _run(self, titles: list[str], limit: int | None = None, all_browse: bool = False):
        try:
            if not titles and not all_browse:
                raise Halt("请先扫描并勾选任务")
            if any(not supported_title(title) for title in titles):
                raise Halt("包含尚未通过实测的任务，请重新扫描并选择可执行项目")
            self.log("会话开始", selected=titles, limit=limit, all_browse=all_browse)
            self.scan()
            changes = 0
            while True:
                pending = False
                if all_browse:
                    candidate = self.discover()
                    if candidate is None:
                        self.state("全部可执行任务已耗尽，会话结束")
                        return
                    current_titles = [candidate.title]
                else:
                    current_titles = [title for title in titles if title not in self.skip]
                    if not current_titles:
                        self.state("选定任务已耗尽，会话结束")
                        return
                for title in current_titles:
                    s, task, full = self.find(title)
                    if not task or not task.remaining:
                        self.log("当前选定任务已耗尽或不在完整列表中", task=title)
                        continue
                    if not task.supported:
                        raise Halt(f"该任务无法自动执行：{title}（{task.skip_reason}）")
                    pending = True
                    if task.jump:
                        if not self.execute_jump(title).success:
                            # One jump task is one shot; retrying it would only burn time.
                            self.skip.add(title)
                            self.state(f"“{title}”未确认完成，本轮跳过并继续")
                            continue
                        changes = 0
                        if limit is not None and len(self.results) >= limit:
                            self.state(f"达到本次上限：{len(self.results)} 项已完成")
                            return
                        self.state("任务间阅读间隔")
                        self.control.wait(random.uniform(5, 10))
                        continue
                    try:
                        self.execute_one(title)
                    except TaskChanged:
                        if not all_browse:
                            raise
                        changes += 1
                        if changes >= 5:
                            raise Halt("列表持续变化，尚未发送任务点击；请稍后重新扫描")
                        self.log("点击前任务已变化，重新发现浏览任务")
                        pending = False
                        continue
                    changes = 0
                    if limit is not None and len(self.results) >= limit:
                        self.state(f"达到本次上限：{len(self.results)} 项已完成")
                        return
                    self.state("任务间阅读间隔")
                    self.control.wait(random.uniform(5, 10))
                if not pending:
                    if all_browse:
                        continue
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
