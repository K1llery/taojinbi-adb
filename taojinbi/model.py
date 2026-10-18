from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass

PACKAGE = "com.taobao.taobao"
TASK_RE = re.compile(r"^(.+?)\((\d+)/(\d+)\)(.*)$")
# A jump task opens another app and grants its reward on entry; it carries no browse timer.
JUMP_RE = re.compile(r"^点击去逛|^逛一逛|跳转|打开\s*(?:APP|应用)", re.I)
EXTERNAL_APP_RE = re.compile(r"支付宝|蚂蚁|芭芭|农场|领水果|京东|抖音|快手|拼多多|微信|闲鱼|饿了么|飞猪|高德|菜鸟|夸克|1688|钉钉|千问|淘特|阿里云盘|微博|百度", re.I)
DOWNLOAD_RE = re.compile(r"下载|安装")
# "看淘宝视频领红包" also says 点击去逛 but stays inside Taobao.
IN_APP_RE = re.compile(r"淘宝")
OTHER_RE = re.compile(r"邀请|好友|助力|兑换|积分|答题|考试|游戏|下单|购买|支付(?!宝)|开通|订阅|关注|收藏|加购|授权|拍照|上传")
RISK_RE = re.compile(r"滑动.{0,8}验证|拖动.{0,8}滑块|请完成.{0,8}验证|请进行.{0,8}验证|安全验证|访问过于频繁|操作.{0,4}频繁|账号存在风险|账户存在风险|行为异常|身份验证|验证码|人机验证|网络开小差|网络异常|请检查网络")


class Halt(RuntimeError):
    """Stop a run; never infer successful completion from an error."""


class Stopped(Halt):
    pass


class RefreshRequired(Exception):
    pass


class TaskChanged(Halt):
    """The list changed before any task input was sent."""


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
    def kind(self) -> str:
        """``browse``, ``jump``, or ``""`` when the task must be skipped."""
        label = self.title + self.detail
        if DOWNLOAD_RE.search(label) or OTHER_RE.search(label):
            return ""
        if self.jump_marked:
            return "jump"
        if self.seconds is not None and 0 < self.seconds <= 120:
            return "browse"
        return ""

    @property
    def jump_marked(self) -> bool:
        """A jump opens another app; an in-app task may reuse the same caption."""
        if EXTERNAL_APP_RE.search(self.title + self.detail):
            return True
        if IN_APP_RE.search(self.title):
            return False
        return bool(JUMP_RE.search(self.title) or JUMP_RE.search(self.detail))

    @property
    def jump(self) -> bool:
        return self.kind == "jump"

    @property
    def supported(self) -> bool:
        return bool(self.kind)

    @property
    def skip_reason(self) -> str:
        if self.kind:
            return ""
        label = self.title + self.detail
        if DOWNLOAD_RE.search(label):
            return "需要下载安装，跳过"
        if OTHER_RE.search(label):
            return "非纯浏览任务跳过"
        return "非计时浏览任务跳过"

    @property
    def reward(self) -> str:
        reward = re.search(r"\+(\d+)", self.detail)
        return reward[1] if reward else ""

    @property
    def group(self) -> tuple[int, int | None, str]:
        return self.total, self.seconds, self.reward

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
        self.ambiguous: set[str] = set()

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
        values = [int(m.group(1)) for t in self.texts if (m := re.fullmatch(r"(\d+)/\d+", t))]
        return values[0] if len(values) == 1 else None

    def check(self, external: bool = False):
        if not external:
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
        rows: list[Task] = []
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
            rows.append(Task(title, int(done), int(total),
                             int(duration[1]) if duration else None, detail, button))
        return self._resolve(rows)

    def _resolve(self, rows: list[Task]) -> list[Task]:
        """A recycled row keeps a stale caption; never let it stop the session."""
        self.ambiguous: set[str] = set()
        grouped: dict[str, list[Task]] = {}
        for task in rows:
            grouped.setdefault(task.title, []).append(task)
        result: dict[str, Task] = {}
        for title, candidates in grouped.items():
            if len(candidates) == 1:
                result[title] = candidates[0]
                continue
            # The stale row repeats a counter that another row still owns.
            shared = {(t.done, t.total) for t in rows if t.title != title}
            pool = [t for t in candidates if (t.done, t.total) not in shared] or candidates
            top = max(t.done for t in pool)
            winners = [t for t in pool if t.done == top]
            if len(winners) == 1:
                result[title] = winners[0]
            else:
                self.ambiguous.add(title)
        return list(result.values())

    def task(self, title: str) -> Task | None:
        return next((t for t in self.tasks() if t.title == title), None)

    def fingerprint(self):
        return tuple((n.get("text", "").strip(), n.get("bounds")) for n in self.nodes
                     if TASK_RE.fullmatch(n.get("text", "").strip()) and self.visible(n))

    def browse_page(self, title: str) -> bool:
        return (supported_title(title) and not jump_title(title) and
                self.page_kind() in ("overlay", "feed", "guide"))

    def header_text(self) -> str:
        header = next((n for n in self.nodes if n.get("resource-id") == "composeHeaderNav"), None)
        if header is None:
            return ""
        return re.sub(r"\s+", "", "".join(n.get("text", "") for n in header.iter("node") if self.visible(n))).replace("奖励单位", "")

    def reward_panel(self) -> str:
        panels = [n for n in self.nodes if n.get("resource-id") == "ice-container" and
                  any("实付需满0.5元才可得奖励哦" in c.get("text", "") for c in n.iter("node"))]
        if len(panels) != 1:
            return ""
        return re.sub(r"\s+", "", "".join(n.get("text", "") for n in panels[0].iter("node") if self.visible(n)))

    def generic_panel(self) -> str:
        """A floating component that states a browse timer and its reward, on any activity page."""
        for node in self.nodes:
            if not self.visible(node):
                continue
            rect = Rect.parse(node.get("bounds", ""))
            if rect.bottom - rect.top > self.height * .55 or rect.right - rect.left > self.width * .95:
                continue
            text = re.sub(r"\s+", "", "".join(n.get("text", "") for n in node.iter("node")
                                              if self.visible(n)))
            if re.search(r"浏览\d+秒", text) and re.search(r"已?得\d+", text):
                return text
        return ""

    def panel_text(self) -> str:
        return self.reward_panel() or self.generic_panel()

    def main_search_panel(self) -> str:
        panels = [n for n in self.nodes if n.get("class") == "android.webkit.WebView" and
                  n.get("text") == "五棱镜浮标 - 主搜专用"]
        if len(panels) != 1:
            return ""
        return re.sub(r"\s+", "", "".join(n.get("text", "") for n in panels[0].iter("node") if self.visible(n)))

    def page_kind(self) -> str | None:
        if self.is_list:
            return "list"
        if self.main_search_panel():
            return "main_search"
        panel = self.panel_text()
        if panel and re.search(r"浏览\d+秒|任务已完成", panel):
            return "overlay"
        if any(t in self.texts for t in ("体力-固推&自建feeds-interact", "体力-固搜-interact")) and self.header_text():
            return "feed"
        if self.search_field() is not None and self.search_button() is not None:
            return "web_search" if self.web_search_panel() is not None else "search"
        compact = re.sub(r"\s+", "", "".join(self.texts))
        if "淘金币双11" in compact and re.search(r"浏览15[s秒]|已得1000", compact):
            return "guide"
        if ("加入购物车" in self.texts and
                any(t in self.texts for t in ("领券购买", "立即购买", "宝贝"))):
            return "product"
        return None

    def search_field(self, focused: bool = False):
        panel = self.web_search_panel()
        source = panel.iter("node") if panel is not None else self.nodes
        matches = [n for n in source if (panel is not None or n.get("resource-id") == f"{PACKAGE}:id/searchEdit") and
                   n.get("class") == "android.widget.EditText" and self.visible(n) and
                   n.get("enabled", "true") == "true" and n.get("password", "false") != "true" and
                   (not focused or n.get("focused") == "true")]
        return matches[0] if len(matches) == 1 else None

    def search_button(self):
        panel = self.web_search_panel()
        source = panel.iter("node") if panel is not None else self.nodes
        matches = [n for n in source if
                   ((n.get("text") == "搜索" and n.get("class") == "android.widget.Button") if panel is not None
                    else n.get("resource-id") == f"{PACKAGE}:id/searchbtn") and
                   self.visible(n) and n.get("enabled", "true") == "true" and n.get("clickable") == "true"]
        return matches[0] if len(matches) == 1 else None

    def web_search_panel(self):
        panels = [n for n in self.nodes if n.get("class") == "android.webkit.WebView" and
                  n.get("text") == "淘宝精选" and self.visible(n) and
                  {"搜索有福利", "搜索后浏览立得奖励"}.issubset(
                      {c.get("text") for c in n.iter("node") if self.visible(c)})]
        return panels[0] if len(panels) == 1 else None

    def search_back(self) -> Rect | None:
        panel = self.web_search_panel()
        if panel is not None:
            arrows = [self.parents.get(n) for n in panel.iter("node")
                      if n.get("text") == "\ue679" and self.visible(n)]
            matches = [n for n in arrows if n is not None and n.get("clickable") == "true"]
        else:
            matches = [n for n in self.nodes if n.get("resource-id") == f"{PACKAGE}:id/btn_go_back"]
        matches = [n for n in matches if self.visible(n) and n.get("enabled", "true") == "true"]
        return Rect.parse(matches[0].get("bounds", "")) if len(matches) == 1 else None

    def task_completed(self, task: Task) -> bool:
        kind = self.page_kind()
        if kind == "overlay":
            panel = self.panel_text()
            return "任务已完成" in panel and reward_matches(task, panel, r"已得(\d+)")
        if kind == "feed":
            return reward_matches(task, self.header_text(), r"已得(\d+)")
        if kind == "guide":
            compact = re.sub(r"\s+", "", "".join(self.texts))
            return reward_matches(task, compact, r"已得(\d+)")
        if kind == "main_search":
            panel = self.main_search_panel()
            return "任务完成" in panel and reward_matches(task, panel, r"\+(\d+)")
        return False

    def task_in_progress(self, task: Task) -> bool:
        if self.page_kind() == "overlay":
            panel = self.panel_text()
            return (f"浏览{task.seconds}秒" in panel and
                    reward_matches(task, panel, rf"浏览{task.seconds}秒得(\d+)") and not self.task_completed(task))
        if self.page_kind() == "feed":
            return reward_matches(task, self.header_text(), rf"浏览{task.seconds}秒可领(\d+)")
        if self.page_kind() == "guide":
            compact = re.sub(r"\s+", "", "".join(self.texts))
            return (bool(re.search(rf"浏览{task.seconds}[s秒]", compact)) and
                    reward_matches(task, compact, rf"浏览{task.seconds}[s秒](?:可得|可领|得|\+)(\d+)") and not self.task_completed(task))
        if self.page_kind() == "main_search":
            panel = self.main_search_panel()
            return (bool(re.search(r"浏览\d+秒", panel)) and
                    reward_matches(task, panel, r"\+(\d+)") and not self.task_completed(task))
        return False

    def product_card(self) -> Rect | None:
        if self.page_kind() != "feed":
            return None
        cards = []
        for n in self.nodes:
            r = Rect.parse(n.get("bounds", ""))
            if (self.visible(n) and n.get("enabled", "true") == "true" and
                    n.get("clickable") == "true" and n.get("class") == "android.widget.Button" and
                    "¥" in n.get("text", "") and r.top > self.height * .12 and
                    r.bottom < self.height * .94 and r.right - r.left > self.width * .35 and
                    r.bottom - r.top > self.height * .20):
                cards.append(r)
        return min(cards, key=lambda r: (r.top, r.left)) if cards else None

    def completed_badge(self) -> bool:
        return any(re.search(r"已得\s*\d+|任务已完成|浏览完成|任务完成", t) for t in self.texts)


def supported_title(title: str) -> bool:
    """Jump titles are executable too; only download and non-browse tasks are refused."""
    return not DOWNLOAD_RE.search(title) and not OTHER_RE.search(title)


def jump_title(title: str) -> bool:
    if EXTERNAL_APP_RE.search(title):
        return True
    if IN_APP_RE.search(title):
        return False
    return bool(JUMP_RE.search(title))


def reward_matches(task: Task, text: str, pattern: str) -> bool:
    return bool(task.reward) and set(re.findall(pattern, text)) == {task.reward}
