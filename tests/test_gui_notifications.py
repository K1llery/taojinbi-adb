import queue
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from taojinbi.adb import Control
from taojinbi.gui import App
from taojinbi.model import Stopped
from taojinbi.runner import Result


class NotificationTests(unittest.TestCase):
    def setUp(self):
        self.app = App.__new__(App)
        self.app.worker = None
        self.app.messages = queue.Queue()
        self.app.control = Control()
        self.app.tasks = {}
        self.app.selected = set()
        self.app.completed = 0
        for name, value in (("limit", "0"), ("adb_path", "adb.exe"),
                            ("serial", "phone"), ("all_browse", True),
                            ("activity", "淘金币赛车")):
            setattr(self.app, name, Mock(get=Mock(return_value=value)))
        for name in ("root", "status", "counter", "busy", "add_log",
                     "render_tasks", "notify"):
            setattr(self.app, name, Mock())

    def work(self, action, mode="run"):
        def runner(adb, emit):
            return SimpleNamespace(directory=Path("artifacts"),
                                   run=lambda *a, **kw: action(emit),
                                   scan=lambda: action(emit))

        def thread(target, daemon):
            return SimpleNamespace(start=target)

        with patch("taojinbi.gui.Adb"), \
             patch("taojinbi.gui.Runner", side_effect=runner), \
             patch("taojinbi.gui.TreasureRunner", side_effect=runner), \
             patch("taojinbi.gui.threading.Thread", side_effect=thread):
            self.app.begin(mode)
        self.app.drain()

    def test_successes_only_update_count_and_batch_end_notifies_once_for_both_activities(self):
        for activity in ("淘金币赛车", "寻宝中国 · 直播"):
            with self.subTest(activity=activity):
                self.setUp()
                self.app.activity.get.return_value = activity

                def complete(emit):
                    for n in range(3):
                        emit("result", Result("任务", n, n + 1, True, "已确认"))

                self.work(complete)
                self.assertEqual(self.app.completed, 3)
                self.app.notify.assert_called_once_with("本次执行完毕", "确认完成 3 项")
                self.app.busy.assert_called_with(False)

    def test_task_result_does_not_notify_before_session_end(self):
        self.app.messages.put(("result", Result("任务", 0, 1, True, "已确认")))
        self.app.drain()
        self.assertEqual(self.app.completed, 1)
        self.app.notify.assert_not_called()

    def test_fault_after_partial_progress_notifies_failure_without_completion(self):
        def fail(emit):
            emit("result", Result("任务", 0, 1, True, "已确认"))
            raise RuntimeError("手机断开连接")

        self.work(fail)
        self.app.notify.assert_called_once_with("执行因故障停止", "手机断开连接")
        self.app.status.set.assert_called_with("已停止：手机断开连接")
        self.assertEqual(self.app.completed, 1)

    def test_recoverable_task_fault_notifies_with_reason(self):
        self.app.messages.put(("task_failed", Result("搜索", 0, None, False, "结果未确认")))
        self.app.drain()
        self.app.notify.assert_called_once_with("任务执行遇到故障", "搜索：结果未确认")

    def test_manual_stop_returned_by_runner_does_not_notify_completion(self):
        self.work(lambda emit: self.app.control.stop())
        self.app.notify.assert_not_called()

    def test_stopped_exception_does_not_notify_completion(self):
        def stop(emit):
            raise Stopped("已停止")

        self.work(stop)
        self.app.notify.assert_not_called()

    def test_scan_success_and_failure_do_not_send_execution_reminders(self):
        self.work(lambda emit: emit("tasks", []), mode="scan")
        self.app.notify.assert_not_called()
        self.app.worker = None

        def fail(emit):
            raise RuntimeError("读取失败")

        self.work(fail, mode="scan")
        self.app.notify.assert_not_called()

    def test_empty_batch_still_notifies_completion(self):
        self.work(lambda emit: None)
        self.app.notify.assert_called_once_with("本次执行完毕", "确认完成 0 项")

    def test_sound_and_toast_switches_still_work_independently(self):
        self.app.show_toast = Mock()
        self.app.notify_sound = Mock()
        self.app.notify_toast = Mock()
        with patch("taojinbi.gui.winsound.MessageBeep") as beep:
            for sound, toast in ((False, False), (True, False), (False, True), (True, True)):
                with self.subTest(sound=sound, toast=toast):
                    beep.reset_mock()
                    self.app.show_toast.reset_mock()
                    self.app.notify_sound.get.return_value = sound
                    self.app.notify_toast.get.return_value = toast
                    App.notify(self.app, "执行因故障停止", "手机断开连接")
                    self.assertEqual(beep.call_count, int(sound))
                    self.assertEqual(self.app.show_toast.call_count, int(toast))
                    if toast:
                        self.app.show_toast.assert_called_once_with("执行因故障停止", "手机断开连接")


if __name__ == "__main__":
    unittest.main()
