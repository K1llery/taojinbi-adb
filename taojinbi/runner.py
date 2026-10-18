from __future__ import annotations

import json
import random
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable

from .adb import Adb, ROOT
from .model import (PACKAGE, Halt, JumpDetected, Rect, RefreshRequired, Snapshot, Stopped,
                    Task, TaskChanged, TaskFailed, set_installed, supported_title)

# A jump task grants its reward on entry, so it only needs the external app to appear.
JUMP_DWELL = 2.0
JUMP_LAUNCH_TIMEOUT = 20
BACK_ATTEMPTS = 3
# A task that cannot be confirmed twice is left alone for the rest of the session.
MAX_TASK_FAILURES = 2
# An unknown in-app page is accepted after this many consecutive frames; its text keeps
# moving (countdowns, prices), so identical frames can never be required.
UNKNOWN_PAGE_FRAMES = 3


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
        self.failures: dict[str, int] = {}
        self.ambiguous: set[str] = set()

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
        if s.ambiguous and s.ambiguous != self.ambiguous:
            self.ambiguous = set(s.ambiguous)
            self.log("同名任务行出现复用残留，本次不执行这些行", titles=sorted(s.ambiguous))
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
            elif context == "timed":
                if s.is_list:
                    raise Halt("页面已变化，预期站内活动页，已停止输入")
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
        set_installed(self.adb.packages())
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
        if task is None and title in s.ambiguous:
            # A stale duplicate row is still on screen; scrolling will not resolve it.
            return s, None, False
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
                raise TaskFailed("任务按钮不可见，需要重新扫描")
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
        unknown = 0
        while self.control.clock() < deadline:
            # Read leniently: the app may switch between the tap and this frame, and that
            # switch is a jump to be finished, not a reason to stop the session.
            s = self.observe(external=True)
            if s.package != PACKAGE:
                raise JumpDetected(s.package)
            kind = s.page_kind()
            if kind == "list":
                # The tap did not leave the list: never click the row a second time.
                raise TaskFailed("点击后仍停留在任务列表，本次跳过")
            if kind in ("search", "web_search") and not self.searched:
                self.start_search(kind)
                self.searched = True
                continue
            if kind in ("overlay", "feed", "guide", "main_search"):
                if not s.task_in_progress(task) and not s.task_completed(task):
                    raise TaskFailed("目标页任务组件与本次浏览要求不符")
                self.page = kind
                self.started_signal = s.task_in_progress(task)
                self.finished_signal = False
                self.log("已识别浏览页面", page=kind, task=task.title)
                self.adb.screenshot(self.directory / "entered.png")
                return s
            if s.app_store_page():
                raise TaskFailed("该任务要求下载安装 APP，本次跳过", permanent=True)
            if kind is None and s.texts:
                # Some activity pages carry no reward component. Their text keeps moving
                # (countdowns, prices), so the page is accepted after it has stayed
                # unknown for a few frames instead of requiring identical frames.
                unknown += 1
                if unknown >= UNKNOWN_PAGE_FRAMES:
                    self.page = "timed"
                    self.started_signal = self.finished_signal = False
                    self.log("页面没有奖励组件，按计时浏览处理", task=task.title, seconds=task.seconds)
                    self.adb.screenshot(self.directory / "entered.png")
                    return s
            else:
                unknown = 0
            if not s.is_list:
                # Allow loading placeholders, but never interact with unknown pages.
                self.log("等待目标页加载")
            self.control.wait(2)
        raise TaskFailed("30秒内没有确认目标浏览页；未重复点击")

    def execute_timed(self, task: Task, before_total: int | None) -> Result:
        """Stay on the activity page for its stated duration, then verify by the global counter."""
        seconds = task.seconds or 15
        self.state(f"计时浏览：{task.title}（{seconds}秒，页面无奖励组件）")
        deadline = self.control.clock() + seconds + 5
        while self.control.clock() < deadline:
            self.control.wait(random.uniform(2, 4))
            s = self.observe()
            if s.is_list:
                raise TaskFailed("计时浏览期间已回到任务列表，本次跳过")
        self.adb.screenshot(self.directory / "before-return.png")
        s = self.back("timed", "list", task.title)
        return self.verify_completion(task, before_total, s,
                                      "计时浏览后总进度增加1；页面无奖励组件，单项次数未归因")

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
        try:
            package = self.await_target()
            if package:
                return self.finish_jump(before, before_total, package)
            self.enter(before)
        except JumpDetected as exc:
            # The caption never said "jump", but the device did.
            self.log("识别到外部应用，改为跳转处理", task=title, package=exc.package)
            return self.finish_jump(before, before_total, exc.package)
        if self.page == "timed":
            return self.execute_timed(before, before_total)
        self.browse(before)
        s = self.return_to_list(title)
        return self.verify_browse(before, before_total, s)

    def record_success(self, before: Task, s: Snapshot, reason: str) -> Result:
        result = Result(before.title, before.done, None, True, reason)
        self.results.append(result)
        self.log("任务完成", **asdict(result), global_progress=s.progress)
        self.emit("result", result)
        self.emit("tasks", s.tasks())
        self.adb.screenshot(self.directory / f"completed-{len(self.results)}.png")
        return result

    def record_failure(self, before: Task, s: Snapshot | None,
                       reason: str = "返回后总进度没有增加1") -> Result:
        result = Result(before.title, before.done, None, False, reason)
        self.log("任务未确认完成", **asdict(result),
                 global_progress=s.progress if s is not None else None)
        self.emit("task_failed", result)
        return result

    def counter_capped(self, s: Snapshot, before: Task, before_total: int | None) -> bool:
        """At the daily cap the counter cannot move; a cleared row is all that is left."""
        if not s.counter_full or s.progress != before_total:
            return False
        row = s.task(before.title)
        return row is None or row.done > before.done

    def verify_completion(self, before: Task, before_total: int | None, s: Snapshot,
                          reason: str) -> Result:
        deadline = self.control.clock() + 15
        while True:
            if before_total is not None and s.progress == before_total + 1:
                return self.record_success(before, s, reason)
            if self.counter_capped(s, before, before_total):
                return self.record_success(before, s, "今日总进度已满，按任务行消失确认（弱证据）")
            if self.control.clock() >= deadline:
                return self.record_failure(before, s)
            self.control.wait(2)
            s = self.observe()
            self.list_only(s)

    def verify_browse(self, before: Task, before_total: int | None, s: Snapshot) -> Result:
        deadline = self.control.clock() + 15
        while True:
            advanced = advanced_task(before, self.baseline_tasks, s.tasks(), before_total, s.progress)
            transition = completed_transition(self.started_signal, self.finished_signal,
                                              before_total, s.progress)
            if advanced is not None or transition:
                # Counter ambiguity is recorded explicitly; never borrow a different row's count.
                reason = "任务进度已确认" if advanced else "页面从浏览中转为完成，且总进度增加1；单项次数未归因"
                if advanced and advanced.title != before.title:
                    self.log("任务名称已轮换，次数变化唯一匹配",
                             previous=before.title, current=advanced.title)
                return self.record_success(before, s, reason)
            if self.counter_capped(s, before, before_total):
                return self.record_success(before, s, "今日总进度已满，按任务行消失确认（弱证据）")
            if self.control.clock() >= deadline:
                return self.record_failure(before, s)
            self.control.wait(2)
            s = self.observe()
            self.list_only(s)

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

    def await_target(self) -> str:
        """Decide from the device, not the caption: an external package means a jump.

        The caption is only a hint. ``去惊喜好物逛一逛`` stays inside Taobao while
        ``看淘宝视频领红包`` leaves it, so the foreground package is the only reliable
        signal. A snapshot taken during an app switch is read leniently so that the
        package check cannot stop the session mid-transition.
        """
        deadline = self.control.clock() + JUMP_LAUNCH_TIMEOUT
        while self.control.clock() < deadline:
            s = self.observe(external=True)
            if s.package and s.package != PACKAGE:
                return s.package
            if not s.is_list:
                return ""
            self.control.wait(1)
        raise TaskFailed("点击去完成后没有确认目标页面，本次跳过")

    def resume_taobao(self):
        """Never relaunch while Taobao is already in front: Welcome would start a new
        activity and drop the task list back to the shop home page."""
        if self.adb.foreground() == PACKAGE:
            return
        self.state("拉起淘宝")
        self.adb.launch_taobao()

    def settle_list(self, timeout: float) -> Snapshot | None:
        deadline = self.control.clock() + timeout
        while True:
            s = self.observe(external=True)
            if s.package == PACKAGE and s.is_list:
                return s
            if self.control.clock() >= deadline:
                return None
            self.control.wait(1)

    def jump_back(self, title: str) -> Snapshot:
        """Resume Taobao's own task, then walk back to the list only inside Taobao.

        ``am start`` resumes the existing task stack, but that stack may still be showing
        the in-Taobao page that launched the external app (闲鱼 opens through a Taobao
        webview), so a bounded back press is still needed. Back is never pressed while
        another app is in front and never from the list itself, so the session cannot
        fall through to the Taobao home page.
        """
        self.state("从外部应用返回淘宝")
        deadline = self.control.clock() + 45
        presses = 0
        while self.control.clock() < deadline:
            self.resume_taobao()
            s = self.settle_list(8)
            if s is not None:
                return s
            if self.adb.foreground() != PACKAGE:
                # The resume has not landed yet; pressing back here would only close
                # the external app's own page.
                self.control.wait(2)
                continue
            if presses >= BACK_ATTEMPTS:
                raise Halt("已回到淘宝，但没有识别到任务列表，请手动返回列表后重新开始")
            presses += 1
            self.log("在淘宝内部返回一次", attempt=presses, task=title)
            self.external_input("keyevent", "4", title=title)
            self.control.wait(2)
        raise Halt("从外部应用返回后没有回到任务列表")

    def finish_jump(self, before: Task, before_total: int | None, package: str) -> Result:
        """A jump leaves no readable evidence outside Taobao; only the counter counts."""
        self.log("已跳转到外部应用", task=before.title, package=package)
        self.adb.screenshot(self.directory / "jumped.png")
        self.control.wait(JUMP_DWELL)
        s = self.jump_back(before.title)
        return self.verify_completion(before, before_total, s,
                                      f"跳转 {package} 后总进度增加1；单项次数未归因")

    def ensure_front(self):
        """An app that grabs the foreground back must not end the session.

        Observed after two jumps to 百度: the external app resumed itself during the
        reading pause. Taobao's task stack is still intact, so resuming it is enough.
        """
        if self.adb.foreground() == PACKAGE:
            return
        self.state("前台不是淘宝，先恢复任务列表")
        self.jump_back("恢复")

    def recover_list(self, title: str):
        """A failed task can leave the session on an unknown page; walk back to the list.

        Only Taobao's own stack is unwound, and never from the list itself, so this
        cannot fall through to the shop home page.
        """
        try:
            self.jump_back(title)
        except Halt as exc:
            raise Halt(f"任务失败后无法回到任务列表：{exc}") from exc

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
                self.ensure_front()
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
                        # Uninstalled or excluded apps are a skip, not a reason to stop.
                        self.log("任务不可自动执行，跳过", task=title, reason=task.skip_reason)
                        self.skip.add(title)
                        continue
                    pending = True
                    try:
                        result = self.execute_one(title)
                    except TaskChanged:
                        if not all_browse:
                            raise
                        changes += 1
                        if changes >= 5:
                            raise Halt("列表持续变化，尚未发送任务点击；请稍后重新扫描")
                        self.log("点击前任务已变化，重新发现任务")
                        pending = False
                        continue
                    except TaskFailed as exc:
                        # One unusable task must not end a session with tasks left.
                        self.log("任务未执行，跳过", task=title, reason=str(exc))
                        self.emit("task_failed", Result(title, task.done, None, False, str(exc)))
                        if exc.permanent:
                            self.skip.add(title)
                        result = None
                        self.recover_list(title)
                    changes = 0
                    if result is None or not result.success:
                        self.failures[title] = self.failures.get(title, 0) + 1
                        if self.failures[title] >= MAX_TASK_FAILURES:
                            self.skip.add(title)
                            self.state(f"“{title}”连续{MAX_TASK_FAILURES}次未确认完成，本轮跳过并继续")
                        continue
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
