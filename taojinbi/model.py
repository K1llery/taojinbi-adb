from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

PACKAGE = "com.taobao.taobao"
SUPPORTED = ("好物沉浸看",)
TASK_RE = re.compile(r"^(.+?)\((\d+)/(\d+)\)(.*)$")
RISK_RE = re.compile(r"滑动.{0,8}验证|拖动.{0,8}滑块|请完成.{0,8}验证|请进行.{0,8}验证|安全验证|访问过于频繁|操作.{0,4}频繁|账号存在风险|账户存在风险|行为异常|身份验证|验证码|人机验证|网络开小差|网络异常|请检查网络")


class Halt(RuntimeError):
    """Stop a run; never infer successful completion from an error."""


class Stopped(Halt):
    pass


class RefreshRequired(Exception):
    pass


@dataclass(frozen=True)
class Rect:
    left: int
    top: int
    right: int
    bottom: int

    @classmethod
    def parse(cls, value: str) -> Rect:
        numbers = re.findall(r"-?\d+", value)
        return cls(*map(int, numbers)) if len(numbers) == 4 else cls(0, 0, 0, 0)

    def valid(self, width: int, height: int) -> bool:
        return (0 <= self.left < self.right <= width and
                0 <= self.top < self.bottom <= height and
                self.right - self.left >= 20 and self.bottom - self.top >= 20)

    @property
    def center(self) -> tuple[int, int]:
        return ((self.left + self.right) // 2, (self.top + self.bottom) // 2)


@dataclass(frozen=True)
class Task:
    title: str
    done: int
    total: int
    seconds: int | None
    detail: str
    button: Rect | None

    @property
    def supported(self) -> bool:
        return supported_title(self.title) and self.seconds is not None and self.seconds > 0

    @property
    def group(self) -> tuple[int, int | None, str]:
        reward = re.search(r"\+(\d+)", self.detail)
        return self.total, self.seconds, reward[1] if reward else ""

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.done)


@dataclass
class Snapshot:
    xml: str
    package: str
    width: int
    height: int
    epoch: int = 0

    def __post_init__(self):
        try:
            self.root = ET.fromstring(self.xml)
        except ET.ParseError as exc:
            raise Halt(f"控件树解析失败：{exc}") from exc
        self.nodes = list(self.root.iter("node"))
        self.parents = {child: parent for parent in self.root.iter() for child in parent}

    @property
    def texts(self) -> list[str]:
        return [n.get(key, "").strip() for n in self.nodes for key in ("text", "content-desc")
                if n.get(key, "").strip() and self.visible(n)]

    def visible(self, node: ET.Element) -> bool:
        return (node.get("visible-to-user", "true") != "false" and
                Rect.parse(node.get("bounds", "")).valid(self.width, self.height))

    @property
    def is_list(self) -> bool:
        return "做任务赚能量" in self.texts and "当前进度" in self.texts

    @property
    def progress(self) -> int | None:
        # The global progress is a separate text node. Task counters are embedded in titles.
        values = [int(m.group(1)) for t in self.texts if (m := re.fullmatch(r"(\d+)/50", t))]
        return values[0] if len(values) == 1 else None

    def check(self):
        if self.package != PACKAGE:
            raise Halt(f"前台应用已变化：{self.package or '无法确认'}。请回到淘宝后重新开始。")
        for node in self.nodes:
            if not self.visible(node):
                continue
            if node.get("package", "") not in ("", PACKAGE):
                # Exclude status/navigation bars but stop on external dialogs/windows.
                rect = Rect.parse(node.get("bounds", ""))
                if rect.bottom > self.height * .10 and rect.top < self.height * .90:
                    raise Halt("检测到其他应用或系统弹窗，请人工处理。")
        for text in self.texts:
            if RISK_RE.search(text):
                raise Halt(f"页面出现需要人工处理的提示：{text[:100]}")

    def tasks(self) -> list[Task]:
        if not self.is_list:
            return []
        result: dict[str, Task] = {}
        for node in self.nodes:
            text = node.get("text", "").strip()
            match = TASK_RE.fullmatch(text)
            if not match:
                continue
            title, done, total, detail = match.groups()
            ancestor = self.parents.get(node)
            button = None
            for _ in range(5):
                if ancestor is None:
                    break
                buttons = [n for n in ancestor.iter("node")
                           if n.get("text") in ("去完成", "去兑换") and n.get("clickable") == "true"]
                labels = [n for n in ancestor.iter("node") if TASK_RE.fullmatch(n.get("text", "").strip())]
                if len(labels) > 1:
                    break
                if len(buttons) == 1:
                    if (buttons[0].get("text") == "去完成" and self.visible(node) and
                            self.visible(buttons[0]) and buttons[0].get("enabled", "true") == "true"):
                        button = Rect.parse(buttons[0].get("bounds", ""))
                    break
                ancestor = self.parents.get(ancestor)
            duration = re.search(r"浏览\s*(\d+)\s*(?:秒|s)", detail, re.I)
            task = Task(title, int(done), int(total), int(duration[1]) if duration else None, detail, button)
            if title in result and result[title] != task:
                raise Halt(f"任务名称存在歧义：{title}")
            result[title] = task
        return list(result.values())

    def task(self, title: str) -> Task | None:
        return next((t for t in self.tasks() if t.title == title), None)

    def fingerprint(self):
        return tuple((t.title, t.done, t.button) for t in self.tasks()
                     if t.button and t.button.valid(self.width, self.height))

    def browse_page(self, title: str) -> bool:
        if self.is_list:
            return False
        text = re.sub(r"\s+", "", "".join(self.texts))
        if title == "查看双11活动攻略":
            return "浏览15" in text and ("已得1000" in text or "得1000" in text)
        # Markers for repeatable browse pages are verified against live samples.
        return (supported_title(title) and
                any(marker in text for marker in BROWSE_MARKERS["好物沉浸看"]))

    def completed_badge(self) -> bool:
        return any(re.search(r"已得\s*\d+|任务已完成|浏览完成|任务完成", t) for t in self.texts)


# Target-page title observed on the live device; the other adapter stays disabled until sampled.
BROWSE_MARKERS: dict[str, tuple[str, ...]] = {
    "好物随心逛": (),
    "好物沉浸看": ("实付需满0.5元才可得奖励哦",),
}


def supported_title(title: str) -> bool:
    return title in SUPPORTED
