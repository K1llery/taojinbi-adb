import json
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock

from taojinbi import model
from taojinbi.adb import Control
from taojinbi.model import PACKAGE, Rect, Task
from taojinbi.runner import Runner
from tests.test_core import page


class RunLifecycleTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.addCleanup(model.set_installed, None)
        model.set_installed(None)
        self.adb = Mock(control=Control())
        self.adb.lease.side_effect = lambda: nullcontext()
        self.adb.foreground.return_value = PACKAGE
        self.emit = Mock()
        self.runner = Runner(self.adb, self.emit, directory=Path(folder.name))
        self.runner.scan = Mock()
        self.runner.control.wait = Mock()
        self.task = Task('逛一逛支付宝农场', 0, 1, None, '点击去逛+600', Rect(900,900,1100,1000))

    def test_no_executable_tasks_end_after_one_boundary_check(self):
        self.runner.observe = Mock(return_value=page([('已完成任务', 1, 1, 900)]))
        self.runner.list_scroll = Mock()
        self.runner.run([], all_browse=True)
        self.assertEqual([c.args[0] for c in self.runner.list_scroll.call_args_list], [False, True])
        self.adb.input.assert_not_called()
        self.assertIn(('state', '全部可执行任务已耗尽，会话结束'), [c.args for c in self.emit.call_args_list])

    def test_candidate_must_have_a_current_actionable_button(self):
        unavailable = Task('浏览入口', 0, 10, 15, '浏览15秒+400', None)
        self.assertIsNone(self.runner.candidate([unavailable]))

    def test_continuously_disappearing_candidates_cannot_restart_discovery_forever(self):
        self.runner.discover = Mock(side_effect=[self.task] * 5 + [AssertionError('不应扫描第六次')])
        self.runner.find = Mock(return_value=(page(), None, True))
        self.runner.run([], all_browse=True)
        self.assertEqual(self.runner.discover.call_count, 5)
        self.adb.input.assert_not_called()

    def test_buttons_becoming_unavailable_also_stop_without_task_input(self):
        unavailable = Task(self.task.title, 0, 1, None, '点击去逛+600', None)
        self.runner.discover = Mock(side_effect=[self.task] * 5 + [AssertionError('不应扫描第六次')])
        self.runner.find = Mock(return_value=(page(), unavailable, True))
        self.runner.run([], all_browse=True)
        self.assertEqual(self.runner.find.call_count, 5)
        self.adb.input.assert_not_called()

    def test_an_unconfirmed_visit_counts_toward_limit_without_a_fault_or_repeat(self):
        self.runner.discover = Mock(side_effect=[self.task, AssertionError('不应重复访问')])
        self.runner.find = Mock(return_value=(page(), self.task, False))
        self.runner.execute_one = Mock(side_effect=lambda title: self.runner.record_visit(self.task, '当前列表奖励未确认'))
        self.runner.run([], all_browse=True, limit=1)
        self.runner.execute_one.assert_called_once()
        self.assertIn(self.task.title, self.runner.skip)
        self.assertEqual(self.runner.results, [])
        self.assertEqual(len(self.runner.visits), 1)
        self.assertFalse(any(c.args[0] == 'task_failed' for c in self.emit.call_args_list))
        visits = json.loads((self.runner.directory / 'visits.json').read_text(encoding='utf-8'))
        self.assertEqual(visits[0]['title'], self.task.title)
        self.assertFalse(visits[0]['success'])

    def test_three_unconfirmed_visits_continue_without_the_failure_budget(self):
        tasks = [Task(f'跳转任务{i}', 0, 1, None, '点击去逛+600', self.task.button) for i in range(3)]
        self.runner.discover = Mock(side_effect=tasks + [None])
        self.runner.find = Mock(side_effect=lambda title: (page(), next(t for t in tasks if t.title == title), False))
        self.runner.execute_one = Mock(side_effect=lambda title: self.runner.record_visit(
            next(t for t in tasks if t.title == title), '奖励未确认'))
        self.runner.run([], all_browse=True)
        self.assertEqual(len(self.runner.visits), 3)
        self.assertFalse(any(c.args[0] == 'task_failed' for c in self.emit.call_args_list))

    def test_uninstalled_taobao_special_edition_is_not_an_executable_task(self):
        model.set_installed([])
        task = Task('去淘宝特价版0元购', 0, 1, None, '点击去逛+400', self.task.button)
        self.assertFalse(task.supported)
        self.assertIn('淘宝特价版', task.skip_reason)
        model.set_installed(['com.taobao.mobile.wxt'])
        self.assertTrue(task.supported)


if __name__ == '__main__':
    unittest.main()
