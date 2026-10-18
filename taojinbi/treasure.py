"""寻宝中国直播任务：仅观看、退出并核对任务次数与步数。"""
from __future__ import annotations

import json
import random
import re
from dataclasses import asdict, dataclass, replace

from .model import Halt, Rect, RefreshRequired, Snapshot, Stopped, TaskChanged
from .runner import Result, Runner

COUNTER = re.compile(r"\((\d+)/(\d+)\)")
LIVE_ROOT = "com.taobao.taobao:id/taolive_root_view"


@dataclass(frozen=True)
class LiveTask:
    title: str
    done: int
    total: int
    reward: int
    button: Rect | None
    blocked: str = ""

    @property
    def supported(self) -> bool:
        return not self.skip_reason

    @property
    def skip_reason(self) -> str:
        if self.blocked:
            return self.blocked
        return "" if re.fullmatch(r"逛.*直播间", self.title) and self.reward > 0 else "非直播浏览任务跳过"

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.done)

    @property
    def detail(self) -> str:
        return f"观看直播，完成后 +{self.reward} 步"

    @property
    def kind(self) -> str:
        return "live"


class TreasurePage:
    def __init__(self, snapshot: Snapshot):
        self.s = snapshot

    @property
    def home(self) -> bool:
        return "直播攒红包" in self.s.texts and "赚步数" in self.s.texts

    @property
    def is_list(self) -> bool:
        return self.home and "做任务 集步数" in self.s.texts

    @property
    def live(self) -> bool:
        return any(n.get("resource-id") == LIVE_ROOT and self.s.visible(n) for n in self.s.nodes)

    @property
    def room_id(self) -> str | None:
        ids = [t for t in self.s.texts if re.fullmatch(r"ID:\d+", t)]
        return ids[0] if self.live and len(ids) == 1 else None

    @property
    def steps(self) -> int | None:
        values = [int(m[1]) for t in self.s.texts if (m := re.fullmatch(r"剩余(\d+)步数", t))]
        return values[0] if self.home and len(values) == 1 else None

    def tasks(self) -> list[LiveTask]:
        if not self.is_list:
            return []
        tasks: dict[str, LiveTask] = {}
        for counter in self.s.nodes:
            match = COUNTER.fullmatch(counter.get("text", "").strip())
            if not match:
                continue
            row = self.s.parents.get(counter)
            for _ in range(5):
                if row is None:
                    break
                labels = [n.get("text", "").strip() for n in row.iter("node") if n.get("text")]
                if sum(bool(COUNTER.fullmatch(t)) for t in labels) > 1:
                    break
                if "完成任务得" not in labels:
                    row = self.s.parents.get(row)
                    continue
                titles = [t for t in labels if not t.startswith("O1C") and not COUNTER.fullmatch(t)
                          and t not in ("完成任务得", "步", "去完成", "已完成", "已领取") and not t.isdecimal()]
                rewards = [int(t) for t in labels if t.isdecimal()]
                if len(titles) != 1 or len(rewards) != 1:
                    break
                done, total = map(int, match.groups())
                if not 0 <= done <= total or total <= 0:
                    raise Halt("寻宝任务次数无效，停止输入")
                buttons = [n for n in row.iter("node") if n.get("text") == "去完成" and self.s.visible(n)
                           and n.get("enabled", "true") == "true"]
                button = None
                if (len(buttons) == 1 and self.s.visible(counter) and self.s.visible(row)
                        and row.get("enabled", "true") == "true" and
                        (row.get("clickable") == "true" or buttons[0].get("clickable") == "true")):
                    button = Rect.parse(buttons[0].get("bounds", ""))
                task = LiveTask(titles[0], done, total, rewards[0], button)
                if task.title in tasks and tasks[task.title] != task:
                    task = replace(task, button=None, blocked="同名任务有歧义，跳过")
                tasks[task.title] = task
                break
        return list(tasks.values())

    def task(self, title: str) -> LiveTask | None:
        return next((t for t in self.tasks() if t.title == title), None)

    def has_title(self, title: str) -> bool:
        return any(n.get("text", "").strip() == title for n in self.s.nodes)

    def fingerprint(self):
        visible = tuple((n.get("text"), n.get("bounds")) for n in self.s.nodes
                        if self.s.visible(n) and COUNTER.fullmatch(n.get("text", "").strip()))
        return (tuple((t.title, t.done, t.total, t.reward) for t in self.tasks()), visible) if visible else ()

    def bubble(self) -> tuple[tuple[str, Rect], int | None, bool] | None:
        if not self.live:
            return None
        components = []
        for container in self.s.nodes:
            resource = container.get("resource-id", "")
            if not resource.startswith("alv_interactive_") or not self.s.visible(container):
                continue
            labels = [re.sub(r"\s+", "", n.get("text", "")) for n in container.iter("node") if self.s.visible(n)]
            timers = [int(m[1]) for t in labels if (m := re.fullmatch(r"浏览(\d+)秒得奖励", t))]
            completed = "完成任务获得奖励" in labels
            if len(timers) == 1 and not completed:
                if not 0 <= timers[0] <= 120:
                    raise Halt("直播倒计时超出已适配范围")
                components.append(((resource, Rect.parse(container.get("bounds", ""))), timers[0], False))
            elif completed and not timers:
                components.append(((resource, Rect.parse(container.get("bounds", ""))), None, True))
        if len(components) > 1:
            raise Halt("直播间奖励组件有歧义，停止输入")
        return components[0] if components else None

    def earn_button(self) -> Rect | None:
        if not self.home or self.is_list:
            return None
        labels = [n for n in self.s.nodes if n.get("text") == "赚步数" and self.s.visible(n)]
        if len(labels) != 1:
            return None
        target = labels[0]
        for _ in range(3):
            if target.get("clickable") == "true" and self.s.visible(target):
                return Rect.parse(target.get("bounds", ""))
            target = self.s.parents.get(target)
            if target is None:
                break
        return None


class TreasureRunner(Runner):
    """Reuse the ADB lease, cancellation, evidence and pause-safe action loop."""

    live_room: str | None = None
    live_anchor: tuple[str, Rect] | None = None

    def list_only(self, s: Snapshot):
        if not TreasurePage(s).is_list:
            raise Halt("请打开寻宝中国的“做任务 集步数”任务页")

    def browse_only(self, s: Snapshot, title: str):
        page = TreasurePage(s)
        if title == "打开赚步数":
            if not page.home or page.is_list:
                raise Halt("寻宝中国主页已变化，停止输入")
        else:
            if not page.live or self.live_room is None or page.room_id != self.live_room:
                raise Halt("直播间身份已变化，停止输入")
            bubble = page.bubble()
            if bubble and bubble[0] != self.live_anchor:
                raise Halt("直播任务组件身份已变化，停止输入")

    def ensure_list(self) -> Snapshot:
        s = self.observe()
        page = TreasurePage(s)
        if page.is_list:
            return s
        if not page.home:
            self.list_only(s)
        def open_tasks(current):
            rect = TreasurePage(current).earn_button()
            if rect is None:
                raise Halt("未找到唯一的赚步数入口")
            return ["tap", *map(str, rect.center)]
        self.action("treasure_home", open_tasks, "打开赚步数")
        deadline = self.control.clock() + 15
        while self.control.clock() < deadline:
            self.control.wait(1)
            s = self.observe()
            if TreasurePage(s).is_list:
                return s
        raise Halt("赚步数任务页没有打开，未重复点击")

    def _scan(self) -> list[LiveTask]:
        self.state("正在读取寻宝中国直播任务")
        s = self.ensure_list()
        tasks = TreasurePage(s).tasks()
        if not tasks:
            raise Halt("未识别到寻宝任务行")
        self.emit("tasks", tasks)
        self.log("寻宝任务列表已读取", steps=TreasurePage(s).steps, tasks=[asdict(t) for t in tasks])
        return tasks

    def locate(self, title: str | None, selected: set[str] | None = None) -> tuple[Snapshot, LiveTask | None]:
        s = self.ensure_list()
        for down in (False, True):
            previous = None
            for attempt in range(13):
                page = TreasurePage(s)
                tasks = page.tasks()
                self.emit("tasks", tasks)
                pending = [t for t in tasks if t.supported and t.remaining and
                           (selected is None or t.title in selected) and (title is None or t.title == title)]
                if title and any(t.title == title and not t.remaining for t in tasks):
                    return s, None
                visible = [t for t in pending if t.button]
                if visible:
                    return s, min(visible, key=lambda t: (t.title, t.done))
                fingerprint = page.fingerprint()
                if not fingerprint:
                    raise Halt("寻宝列表滚动区域无法识别")
                if fingerprint == previous:
                    break
                if attempt == 12:
                    raise Halt("寻宝任务列表滚动超过上限")
                previous = fingerprint
                self.list_scroll(down)
                s = self.observe()
                self.list_only(s)
        return s, None

    def execute_live(self, selected: LiveTask) -> Result:
        self.live_room = self.live_anchor = None
        self.state(f"准备观看：{selected.title}")
        def tap(current):
            page = TreasurePage(current)
            if page.steps is None:
                raise Halt("无法核对起始步数，未点击任务")
            task = page.task(selected.title)
            if task is None or not task.remaining or task != selected:
                raise TaskChanged("寻宝任务在点击前已变化，请重新扫描")
            if not task.supported or task.button is None:
                raise Halt("当前直播任务不可执行")
            return ["tap", *map(str, task.button.center)]
        before = self.action("list", tap)
        before_steps = TreasurePage(before).steps
        assert before_steps is not None
        deadline = self.control.clock() + 30
        bubble = None
        while self.control.clock() < deadline:
            s = self.observe()
            page = TreasurePage(s)
            bubble = page.bubble()
            if bubble:
                if page.room_id is None:
                    raise Halt("无法确认唯一的直播间ID，停止输入")
                self.live_room = page.room_id
                break
            self.control.wait(2)
        if bubble is None:
            raise Halt("未识别到直播间任务倒计时，停止输入")
        anchor, seconds, completed = bubble
        self.live_anchor = anchor
        saw_timer = not completed
        self.log("已识别寻宝直播组件", task=selected.title, seconds=seconds, completed=completed)
        self.adb.screenshot(self.directory / "live-entered.png")
        deadline = self.control.clock() + (seconds or 0) + 30
        while not completed and self.control.clock() < deadline:
            self.control.wait(random.uniform(2, 4))
            page = TreasurePage(self.observe())
            if not page.live or page.room_id != self.live_room:
                raise Halt("观看期间直播间身份已变化，停止输入")
            current = page.bubble()
            if current:
                if current[0] != anchor:
                    raise Halt("直播任务组件身份已变化，停止输入")
                completed = current[2]
        if not completed:
            raise Halt("直播任务未出现完成提示，未返回或重做")
        self.adb.screenshot(self.directory / "live-completed.png")
        s = self.leave_live()
        return self.verify_live(selected, before_steps, s, transition=saw_timer and completed)

    def verify_live(self, selected: LiveTask, before_steps: int, s: Snapshot,
                    transition: bool = False) -> Result:
        self.list_only(s)
        deadline = self.control.clock() + 15
        checked_absence = False
        while True:
            page = TreasurePage(s)
            after = page.task(selected.title)
            steps = page.steps
            count = None
            reason = "任务次数增加1且步数按本次奖励增加"
            if steps == before_steps + selected.reward:
                if (after and after.supported and after.total == selected.total and after.reward == selected.reward
                        and after.done == selected.done + 1):
                    count = after.done
                elif (after is None and selected.remaining == 1 and transition and not checked_absence and
                      not page.has_title(selected.title)):
                    checked_absence = True
                    s, absent = self.confirm_absent(selected.title)
                    page = TreasurePage(s)
                    steps = page.steps
                    if absent and steps == before_steps + selected.reward:
                        count = selected.total
                        reason = "末次倒计时转为完成、步数精确增加且完整列表确认原任务消失"
            if count is not None:
                result = Result(selected.title, selected.done, count, True, reason)
                self.results.append(result)
                self.log("寻宝直播任务完成", **asdict(result), before_steps=before_steps, steps=steps,
                         reward=selected.reward)
                self.emit("result", result)
                self.emit("tasks", page.tasks())
                self.adb.screenshot(self.directory / f"completed-{len(self.results)}.png")
                return result
            if self.control.clock() >= deadline:
                raise Halt("返回后任务次数与步数未同时确认，已停止，不会重做")
            self.control.wait(2)
            s = self.observe()
            self.list_only(s)

    def confirm_absent(self, title: str) -> tuple[Snapshot, bool]:
        s = self.ensure_list()
        for down in (False, True):
            previous = None
            for attempt in range(13):
                self.list_only(s)
                # Include offscreen XML nodes and unresolved duplicate rows.
                if TreasurePage(s).has_title(title):
                    return s, False
                fingerprint = TreasurePage(s).fingerprint()
                if not fingerprint:
                    raise Halt("末次任务消失核验无法读取列表边界")
                if fingerprint == previous:
                    break
                if attempt == 12:
                    raise Halt("末次任务消失核验超过滚动上限")
                previous = fingerprint
                self.list_scroll(down)
                s = self.observe()
        return s, True

    def live_back(self) -> Snapshot:
        while True:
            s = self.observe()
            page = TreasurePage(s)
            if page.is_list or page.home:
                return s
            self.browse_only(s, "返回本次直播间")
            try:
                self.adb.input(s, "keyevent", "4")
                self.log("发送操作", action="keyevent", room=self.live_room)
                return s
            except RefreshRequired:
                self.control.checkpoint()

    def leave_live(self) -> Snapshot:
        for _ in range(3):
            s = self.observe()
            page = TreasurePage(s)
            if page.is_list:
                return s
            if page.home:
                return self.ensure_list()
            fresh = self.live_back()
            if TreasurePage(fresh).is_list:
                return fresh
            if TreasurePage(fresh).home:
                return self.ensure_list()
            deadline = self.control.clock() + 5
            while self.control.clock() < deadline:
                self.control.wait(1)
                s = self.observe()
                page = TreasurePage(s)
                if page.is_list:
                    return s
                if page.home:
                    return self.ensure_list()
        raise Halt("直播间返回超过3次仍未回到寻宝任务页，停止输入")

    def _run(self, titles: list[str], limit: int | None = None, all_browse: bool = False):
        try:
            if not all_browse and not titles:
                raise Halt("请先勾选直播任务或选择自动模式")
            self.log("寻宝中国会话开始", selected=titles, limit=limit, all_browse=all_browse)
            self.scan()
            while True:
                _, task = self.locate(None, None if all_browse else set(titles))
                if task is None:
                    self.state("寻宝中国本轮无剩余可执行直播入口；跳过项目请查看列表")
                    return
                self.execute_live(task)
                if limit is not None and len(self.results) >= limit:
                    self.state(f"达到本次上限：{len(self.results)} 次直播任务已确认")
                    return
                self.state("直播任务间阅读间隔")
                self.control.wait(random.uniform(5, 10))
        except Stopped as exc:
            self.log(str(exc))
            self.emit("state", "已停止")
        except Exception as exc:
            self.fail(exc)
            raise
        finally:
            (self.directory / "results.json").write_text(
                json.dumps([asdict(r) for r in self.results], ensure_ascii=False, indent=2), encoding="utf-8")
