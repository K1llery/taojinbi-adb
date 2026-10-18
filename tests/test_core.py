import sys
import tempfile
import threading
import time
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import patch

from taojinbi.adb import Adb, Control
from taojinbi.model import Halt, PACKAGE, Rect, RefreshRequired, Snapshot, Stopped, Task
from taojinbi.runner import Runner, advanced_task, verify


def node(parent, text="", bounds="[0,0][1200,2600]", **attrs):
    return ET.SubElement(parent, "node", {"text": text, "bounds": bounds,
                                          "package": PACKAGE, **attrs})


def page(tasks=(), progress=0, overlay=False):
    root = ET.Element("hierarchy")
    window = node(root)
    node(window, "做任务赚能量", "[400,500][800,580]")
    node(window, "当前进度", "[60,700][220,760]")
    node(window, f"{progress}/50", "[60,770][150,830]")
    if overlay:
        extra = node(window)
        node(extra, "任务已经", "[1000,2100][1180,2150]")
        node(extra, "全部完成啦", "[1000,2150][1180,2200]")
    for title, done, total, y in tasks:
        row = node(window)
        node(row, f"{title}({done}/{total})浏览15秒+400", f"[220,{y}][940,{y+100}]")
        node(row, "去完成", f"[942,{y}][1143,{y+93}]", clickable="true")
    return Snapshot(ET.tostring(root, encoding="unicode"), PACKAGE, 1200, 2670)


def immersive(completed=False, seconds=15, reward=400):
    root = ET.Element("hierarchy")
    window = node(root, **{"resource-id": "ice-container"})
    node(window, "任务已完成" if completed else f"浏览{seconds}秒", "[900,1100][1180,1160]")
    node(window, f"已得{reward}" if completed else f"得{reward}", "[900,1160][1180,1220]")
    node(window, "实付需满0.5元才可得奖励哦", "[700,1220][1180,1270]")
    return Snapshot(ET.tostring(root, encoding="unicode"), PACKAGE, 1200, 2670)


class ParsingTests(unittest.TestCase):
    def test_reordered_tasks_keep_their_own_buttons(self):
        first = page([("好物沉浸看", 0, 5, 900), ("淘金币省钱等级考试", 0, 1, 1200)])
        later = page([("淘金币省钱等级考试", 0, 1, 900), ("好物沉浸看", 1, 5, 1200)], 1)
        self.assertEqual(first.task("好物沉浸看").button.center, (1042, 946))
        self.assertEqual(later.task("好物沉浸看").button.center, (1042, 1246))
        self.assertEqual(later.task("好物沉浸看").done, 1)

    def test_decorative_finished_overlay_is_not_completion(self):
        s = page([("好物沉浸看", 0, 5, 900)], overlay=True)
        self.assertEqual(s.progress, 0)
        self.assertFalse(s.completed_badge())

    def test_offscreen_button_never_valid(self):
        s = page([("好物沉浸看", 0, 5, 2700)])
        self.assertIsNone(s.task("好物沉浸看").button)
        self.assertFalse(Rect.parse("[0,0][0,0]").valid(1200, 2670))
        self.assertFalse(Rect.parse("[200,2664][900,2622]").valid(1200, 2670))

    def test_jump_tasks_are_executable_while_spending_and_download_are_not(self):
        for title in ("逛一逛支付宝金豆夺宝", "去芭芭农场领水果"):
            self.assertTrue(page([(title, 0, 1, 900)]).task(title).jump)
        for title in ("天猫积分换能量", "邀请好友", "下单奖励", "下载安装领能量"):
            self.assertFalse(page([(title, 0, 1, 900)]).task(title).supported)

    def test_browse_support_follows_requirements_after_caption_changes(self):
        for title in ("好物随心逛", "搜一搜你心仪的宝贝", "看看#屏幕挂灯", "拍立淘逛感兴趣的宝贝", "去签到领红包", "全新活动名"):
            self.assertTrue(page([(title, 0, 1, 900)]).task(title).supported)

    def test_no_button_cannot_borrow_neighbor(self):
        s = page([("好物沉浸看", 0, 5, 900), ("其他任务", 0, 1, 1200)])
        row = s.parents[next(n for n in s.nodes if n.get("text", "").startswith("好物沉浸看("))]
        row.remove(next(n for n in row if n.get("text") == "去完成"))
        refreshed = Snapshot(ET.tostring(s.root, encoding="unicode"), PACKAGE, 1200, 2670)
        self.assertIsNone(refreshed.task("好物沉浸看").button)

    def test_hidden_or_disabled_in_bounds_button_never_clickable(self):
        for attribute, value in (("visible-to-user", "false"), ("enabled", "false")):
            s = page([("好物沉浸看", 0, 5, 900)])
            button = next(n for n in s.nodes if n.get("text") == "去完成")
            button.set(attribute, value)
            refreshed = Snapshot(ET.tostring(s.root, encoding="unicode"), PACKAGE, 1200, 2670)
            self.assertIsNone(refreshed.task("好物沉浸看").button)

    def test_hidden_task_caption_cannot_use_visible_button(self):
        s = page([("好物沉浸看", 0, 5, 900)])
        caption = next(n for n in s.nodes if n.get("text", "").startswith("好物沉浸看("))
        caption.set("visible-to-user", "false")
        refreshed = Snapshot(ET.tostring(s.root, encoding="unicode"), PACKAGE, 1200, 2670)
        self.assertIsNone(refreshed.task("好物沉浸看").button)

    def test_duplicate_rows_are_dropped_instead_of_stopping_the_session(self):
        s = page([("好物沉浸看", 0, 5, 900), ("好物沉浸看", 0, 5, 1200)])
        self.assertEqual(s.tasks(), [])
        self.assertEqual(s.ambiguous, {"好物沉浸看"})

    def test_stale_caption_loses_to_the_counter_another_row_owns(self):
        s = page([("搜一搜你心仪的宝贝", 3, 10, 900), ("看看#隔离霜", 3, 10, 1200),
                  ("搜一搜你心仪的宝贝", 4, 5, 1500)])
        task = s.task("搜一搜你心仪的宝贝")
        self.assertEqual((task.done, task.total), (4, 5))
        self.assertEqual(s.ambiguous, set())

    def test_risk_text_and_description_halt(self):
        for attr in ("text", "content-desc"):
            s = page()
            node(s.root, **{attr: "请完成安全验证"})
            s = Snapshot(ET.tostring(s.root, encoding="unicode"), PACKAGE, 1200, 2670)
            with self.assertRaises(Halt):
                s.check()

    def test_hidden_risk_text_does_not_halt(self):
        s = page()
        node(s.root, "安全验证", "[0,0][0,0]")
        Snapshot(ET.tostring(s.root, encoding="unicode"), PACKAGE, 1200, 2670).check()

    def test_foreground_change_halts(self):
        s = page()
        s.package = "com.android.settings"
        with self.assertRaises(Halt):
            s.check()

    def test_system_dialog_halts(self):
        s = page()
        node(s.root, "允许", package="com.android.permissioncontroller")
        s = Snapshot(ET.tostring(s.root, encoding="unicode"), PACKAGE, 1200, 2670)
        with self.assertRaises(Halt):
            s.check()

    def test_page_markers_survive_completed_widget(self):
        self.assertTrue(immersive().browse_page("好物沉浸看"))
        self.assertTrue(immersive(True).browse_page("好物沉浸看"))
        self.assertTrue(immersive(True).completed_badge())
        self.assertFalse(page().browse_page("好物沉浸看"))
        self.assertTrue(immersive().browse_page("好物随心逛"))
        self.assertFalse(immersive().browse_page("逛一逛支付宝农场"))

    def test_bad_xml_is_explicit_failure(self):
        with self.assertRaises(Halt):
            Snapshot("not xml", PACKAGE, 1200, 2670)


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.before = Task("好物沉浸看", 0, 5, 15, "", None)

    def test_counter_increase_confirms_completion(self):
        after = Task("好物沉浸看", 1, 5, 15, "", None)
        self.assertTrue(verify(self.before, after, 1, 2))

    def test_timer_or_global_reward_alone_never_counts(self):
        self.assertFalse(verify(self.before, self.before, 1, 2))
        self.assertFalse(verify(self.before, None, 1, 2, True))

    def test_disappearing_one_shot_needs_full_scan_and_global_increase(self):
        once = Task("查看双11活动攻略", 0, 1, 15, "", None)
        self.assertFalse(verify(once, None, 0, 1))
        self.assertFalse(verify(once, None, 0, 0, True))
        self.assertFalse(verify(once, None, None, 1, True))
        self.assertTrue(verify(once, None, 0, 1, True))

    def test_replaced_counter_does_not_count(self):
        self.assertFalse(verify(self.before, Task("好物沉浸看", 1, 10, 15, "", None), 1, 2))

    def test_rotated_caption_tracks_only_unique_counter_change(self):
        old = page([("好物沉浸看", 2, 10, 900), ("看看#旧商品", 0, 10, 1200)], 4)
        new = page([("好物沉浸看", 0, 10, 900), ("看看#运动相机", 3, 10, 1200)], 5)
        advanced = advanced_task(old.task("好物沉浸看"), old.tasks(), new.tasks(), old.progress, new.progress)
        self.assertEqual(advanced.title, "看看#运动相机")
        self.assertEqual(advanced.done, 3)

    def test_two_possible_rotated_rows_halt(self):
        old = page([("好物沉浸看", 2, 10, 900), ("看看#旧商品", 3, 10, 1200)], 4)
        new = page([("看看#A", 3, 10, 900), ("看看#B", 3, 10, 1200)], 5)
        self.assertIsNone(advanced_task(old.task("好物沉浸看"), old.tasks(), new.tasks(), 4, 5))

    def test_unrelated_counter_change_does_not_complete_target(self):
        old = page([("好物沉浸看", 2, 10, 900), ("看看#其他", 0, 10, 1200)], 4)
        new = page([("好物沉浸看", 2, 10, 900), ("看看#其他", 1, 10, 1200)], 5)
        self.assertIsNone(advanced_task(old.task("好物沉浸看"), old.tasks(), new.tasks(), 4, 5))

    def test_caption_rotation_requires_global_confirmation(self):
        old = page([("好物沉浸看", 0, 10, 900)], 4)
        new = page([("看看#其他", 1, 10, 900)], 4)
        self.assertIsNone(advanced_task(old.task("好物沉浸看"), old.tasks(), new.tasks(), 4, 4))

    def test_identical_starting_counts_cannot_be_attributed(self):
        old = page([("好物沉浸看", 0, 10, 900), ("其他流程", 0, 10, 1200)], 4)
        new = page([("好物沉浸看", 0, 10, 900), ("其他流程", 1, 10, 1200)], 5)
        self.assertIsNone(advanced_task(old.task("好物沉浸看"), old.tasks(), new.tasks(), 4, 5))

    def test_live_three_run_counter_distributions_are_unambiguous(self):
        # Sanitized counters from the recorded successful 3-run session.
        sample = [([0, 3, 1], [0, 4, 1], 3), ([0, 4, 1], [1, 4, 1], 0),
                  ([1, 4, 1], [2, 4, 1], 1)]
        # Groups with a different total must never introduce an ambiguity.
        for previous, current, target in sample:
            titles = ["好物沉浸看", "其他浏览", "其他组"]
            old = [Task(titles[i], n, 10 if i < 2 else 5, 15, '浏览15秒+400', None) for i, n in enumerate(previous)]
            new = [Task(titles[i], n, 10 if i < 2 else 5, 15, '浏览15秒+400', None) for i, n in enumerate(current)]
            before = next(t for t in old if t.done == target and t.total == 10)
            self.assertIsNotNone(advanced_task(before, old, new, 5, 6))


class ControlTests(unittest.TestCase):
    def test_device_lease_blocks_competing_session_and_releases(self):
        with tempfile.TemporaryDirectory() as folder, patch("taojinbi.adb.ROOT", Path(folder)):
            first = Adb(sys.executable, "offline-test-device")
            second = Adb(sys.executable, "offline-test-device")
            with first.lease():
                with first.lease():
                    with self.assertRaises(Halt):
                        with second.lease():
                            self.fail("Competing session acquired the device")
            with second.lease():
                self.assertIsNotNone(second.lease_stream)

    def test_stop_wakes_pause_and_wait(self):
        control = Control()
        control.pause()
        errors = []
        def wait():
            try:
                control.wait(100)
            except Stopped:
                errors.append("stopped")
        thread = threading.Thread(target=wait)
        thread.start()
        control.stop()
        thread.join(timeout=1)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, ["stopped"])

    def test_paused_time_is_excluded(self):
        control = Control()
        with patch("taojinbi.adb.time.monotonic", return_value=10):
            start = control.clock()
            control.pause()
        with patch("taojinbi.adb.time.monotonic", return_value=30):
            self.assertEqual(control.clock(), start)
            control.resume()
            self.assertEqual(control.clock(), start)
        with patch("taojinbi.adb.time.monotonic", return_value=31):
            self.assertEqual(control.clock() - start, 1)

    def test_no_process_is_launched_after_stop(self):
        with tempfile.NamedTemporaryFile() as exe:
            control = Control()
            control.stop()
            adb = Adb(exe.name, "device", control)
            with patch("taojinbi.adb.subprocess.Popen") as popen:
                with self.assertRaises(Stopped):
                    adb.run(["shell", "input", "tap", "1", "1"], epoch=0)
                popen.assert_not_called()

    def test_resume_invalidates_pre_pause_coordinates(self):
        with tempfile.NamedTemporaryFile() as exe:
            control = Control()
            adb = Adb(exe.name, "device", control)
            control.pause()
            control.resume()
            with patch("taojinbi.adb.subprocess.Popen") as popen:
                with self.assertRaises(RefreshRequired):
                    adb.run(["shell", "input", "tap", "1", "1"], epoch=0)
                popen.assert_not_called()

    def test_running_process_is_killed_when_stop_arrives(self):
        control = Control()
        adb = Adb(sys.executable, control=control)
        timer = threading.Timer(.15, control.stop)
        timer.start()
        with self.assertRaises(Stopped):
            adb.run(["-c", "import time; time.sleep(10)"], timeout=2)
        timer.join()


class StateMachineTests(unittest.TestCase):
    def test_unknown_page_gets_no_input(self):
        class FakeAdb:
            control = Control()
            def snapshot(self):
                return page()
            def input(self, *args):
                raise AssertionError("must not inject input")
        with tempfile.TemporaryDirectory() as path:
            runner = Runner(FakeAdb(), directory=Path(path))
            with self.assertRaises(Halt):
                runner.action("browse", lambda s: ["keyevent", "4"], "好物沉浸看")

    def test_pause_between_observation_and_input_reobserves(self):
        class FakeAdb:
            def __init__(self):
                self.control = Control()
                self.snapshots = 0
                self.inputs = 0
            def snapshot(self):
                self.snapshots += 1
                return immersive()
            def input(self, *args):
                self.inputs += 1
                if self.inputs == 1:
                    raise RefreshRequired()
        adb = FakeAdb()
        with tempfile.TemporaryDirectory() as path:
            Runner(adb, directory=Path(path)).action("browse", lambda s: ["keyevent", "4"], "好物沉浸看")
        self.assertEqual(adb.snapshots, 2)
        self.assertEqual(adb.inputs, 2)


if __name__ == "__main__":
    unittest.main()
