"""Local option-length rules for the observed 淘金币 single-choice quiz."""
from __future__ import annotations

import re
from dataclasses import dataclass

from .model import Halt, Rect, RefreshRequired, Snapshot, Task

QUIZ_TITLE = "淘金币省钱等级考试"


@dataclass(frozen=True)
class Option:
    letter: str
    text: str
    button: Rect


def choose_option(options: list[Option]) -> Option:
    """Pick the isolated short/long answer; ties default to the first longest."""
    if not 2 <= len(options) <= 4 or len({o.letter for o in options}) != len(options):
        raise Halt("答题选项数量不符合已适配的2至4个不同选项")
    lengths = {o.letter: len(re.sub(r"\s+", "", o.text)) for o in options}
    if len(options) == 2:
        return max(options, key=lambda o: lengths[o.letter])
    ordered = sorted(lengths.values())
    short = ordered[1] - ordered[0]
    long = ordered[-1] - ordered[-2]
    target = min(lengths.values()) if short > long else max(lengths.values())
    return next(o for o in options if lengths[o.letter] == target)


class QuizPage:
    def __init__(self, s: Snapshot):
        self.s = s
        panels = [n for n in s.nodes if n.get("class") == "android.webkit.WebView" and
                  n.get("text") == "淘金币趣味答题" and s.visible(n)]
        if len(panels) != 1:
            raise Halt("未识别到唯一淘金币答题页面")
        all_nodes = list(panels[0].iter("node"))
        self.nodes = [n for n in all_nodes if s.visible(n)]
        labels = [n.get("text", "").strip() for n in self.nodes]
        questions = [t for t in labels if re.fullmatch(r"\d+\.\s*.+单选题", t, re.S)]
        if len(questions) != 1 or "淘金币答题" not in labels:
            raise Halt("答题题目有歧义或超出已适配的单选题范围")
        self.question = questions[0]
        self.options = []
        markers = {letter: [n for n in all_nodes if n.get("text", "").strip() == letter] for letter in "ABCDE"}
        available = [letter for letter, matches in markers.items() if matches]
        if not 2 <= len(available) <= 4 or available != list("ABCD"[:len(available)]):
            raise Halt("答题选项不完整，预期连续的A至B/C/D选项")
        for letter in available:
            letters = markers[letter]
            if len(letters) != 1:
                raise Halt(f"答题选项{letter}重复出现，无法唯一定位")
            if not s.visible(letters[0]):
                raise Halt(f"答题选项{letter}未完整显示，停止输入")
            row = s.parents.get(letters[0])
            if row is None or row.get("clickable") != "true" or row.get("enabled", "true") != "true":
                raise Halt("答题选项不可点击")
            values = [n.get("text", "").strip() for n in row.iter("node") if s.visible(n) and
                      n.get("text", "").strip() and n.get("text", "").strip() != letter and
                      n.get("class") not in ("android.widget.Image", "android.widget.ImageView")]
            if len(values) != 1 or not s.visible(row):
                raise Halt("答题选项内容有歧义")
            self.options.append(Option(letter, values[0], Rect.parse(row.get("bounds", ""))))
        self.signature = (self.question, tuple((o.letter, o.text) for o in self.options))
        correct = [m[1] for t in labels if (m := re.fullmatch(r"正确答案：([ABCD])", t))]
        chosen = [m[1] for t in labels if (m := re.fullmatch(r"你的答案：([ABCD])", t))]
        if len(correct) > 1 or len(chosen) > 1:
            raise Halt("答题反馈有歧义")
        self.feedback = (correct[0], chosen[0]) if len(correct) == len(chosen) == 1 else None
        self.submit = self.button("我选好了")
        self.finish = self.button("已成功领取奖励 回到主页") if self.feedback else None

    def button(self, text: str) -> Rect | None:
        matches = [n for n in self.nodes if n.get("text") == text and
                   n.get("class") == "android.widget.Button" and n.get("clickable") == "true" and
                   n.get("enabled", "true") == "true"]
        if len(matches) > 1:
            raise Halt("答题按钮有歧义")
        return Rect.parse(matches[0].get("bounds", "")) if matches else None


def answer(runner, page: QuizPage):
    selected = choose_option(page.options)
    while True:
        s = runner.observe()
        current = QuizPage(s)
        if current.signature != page.signature or current.feedback:
            raise Halt("选择前题目或答题状态已变化，未提交")
        selected = next(o for o in current.options if o.letter == selected.letter)
        try:
            runner.adb.input(s, "tap", *map(str, selected.button.center))
            fresh = runner.observe()
            current = QuizPage(fresh)
            if current.signature != page.signature or current.feedback:
                raise Halt("提交前题目或答题状态已变化，未提交")
            if fresh.epoch != s.epoch:
                continue  # A pause may have changed the selected answer; select it again.
            if current.submit is None:
                raise Halt("选择后提交按钮仍不可用，未重复提交")
            runner.adb.input(fresh, "tap", *map(str, current.submit.center))
            runner.log("按选项长短规则提交答案", question=page.question, option=selected.letter,
                       length=len(re.sub(r"\s+", "", selected.text)))
            return
        except RefreshRequired:
            runner.control.checkpoint()


def run_quiz(runner, task: Task, before_total: int | None):
    runner.state("按选项长短规则答题")
    deadline = runner.control.clock() + 30
    submitted = False
    expected = None
    seen_page = False
    while runner.control.clock() < deadline:
        s = runner.observe()
        if not seen_page and not ("淘金币趣味答题" in s.texts and "淘金币答题" in s.texts and
                                  any("单选题" in t or "多选题" in t for t in s.texts)):
            runner.control.wait(.5)
            continue
        page = QuizPage(s)
        seen_page = True
        if expected is None:
            expected = page.signature
        elif page.signature != expected:
            raise Halt("本轮答题题目或选项已变化，停止输入")
        if page.finish is not None:
            correct, chosen = page.feedback
            runner.log("已取得答题结果与领取反馈", correct=correct, chosen=chosen, answer_correct=correct == chosen)
            runner.adb.screenshot(runner.directory / "quiz-result.png")
            try:
                runner.adb.input(s, "tap", *map(str, page.finish.center))
            except RefreshRequired:
                runner.control.checkpoint()
                continue
            s = runner.jump_back(task.title)
            if s.counter_full and runner.baseline_energy is not None:
                s = runner.refresh_coin_list(task.title)
            row = s.task(task.title)
            delta = s.coin_energy - runner.baseline_energy if (
                s.coin_energy is not None and runner.baseline_energy is not None and s.epoch == runner.energy_epoch) else None
            if ((before_total is not None and s.progress == before_total + 1) or
                    (row is not None and row.total == task.total and row.done == task.done + 1) or
                    (delta is not None and task.reward and 0 < delta <= int(task.reward))):
                return runner.record_success(task, s,
                    f"答题页确认奖励已领取，返回后奖励已核验；答案{'正确' if correct == chosen else '错误'}" +
                    (f"，实际增加{delta}能量" if delta is not None else ""),
                    row.done if row is not None and row.done == task.done + 1 else None)
            return runner.record_failure(task, s, "答题页已结束，但返回后奖励未确认，不会重复答题")
        if page.feedback is None and not submitted:
            answer(runner, page)
            submitted = True
        runner.control.wait(.5)
    raise Halt("答题页面30秒内未出现可确认的结束状态，未重复提交")
