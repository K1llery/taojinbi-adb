import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock

from taojinbi import model
from taojinbi.adb import Control
from taojinbi.gui import App
from taojinbi.model import Halt, PACKAGE, RefreshRequired, Snapshot, Task
from taojinbi.runner import Result, Runner
from tests.test_core import node, page


def generic(completed=False, shifted=False):
    root = ET.Element("hierarchy")
    window = node(root)
    node(window, "淘宝活动")
    panel = node(window, bounds="[120,1800][1080,2160]" if shifted else "[120,1900][1080,2260]")
    node(panel, "任务已完成" if completed else "浏览15秒", "[700,1950][900,2010]")
    node(panel, "已得400" if completed else "得400", "[700,2010][900,2070]")
    return Snapshot(ET.tostring(root, encoding="unicode"), PACKAGE, 1200, 2670)


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.adb = Mock(control=Control())
        self.runner = Runner(self.adb, directory=Path(self.folder.name))
        now = [0.0]
        self.runner.control.clock = lambda: now[0]
        self.runner.control.wait = Mock(side_effect=lambda s: now.__setitem__(0, now[0] + s))
        self.before = Task("原任务", 2, 5, 15, "浏览15秒+400", None)
        self.runner.baseline_tasks = [self.before]

    def test_capped_counter_requires_attributable_single_count_increase(self):
        s = page([("改名任务", 3, 5, 900)], 50)
        result = self.runner.verify_completion(self.before, 50, s, "jump")
        self.assertTrue(result.success)
        self.assertEqual(result.after, 3)

    def test_capped_counter_rejects_rotation_offscreen_and_duplicate_rows(self):
        for s in (page([("改名任务", 2, 5, 900)], 50), page([], 50),
                  page([("原任务", 2, 5, 900), ("原任务", 2, 5, 1200)], 50)):
            self.adb.snapshot.return_value = s
            self.assertFalse(self.runner.verify_completion(self.before, 50, s, "jump").success)
        self.assertEqual(self.runner.results, [])

    def test_capped_browse_can_use_a_started_and_finished_reward_component(self):
        self.runner.started_signal = self.runner.finished_signal = True
        s = page([("改名任务", 2, 5, 900)], 50)
        self.assertTrue(self.runner.verify_browse(self.before, 50, s).success)
        self.assertIsNone(self.runner.results[-1].after)

    def test_jump_does_not_borrow_the_previous_browse_component(self):
        self.runner.started_signal = self.runner.finished_signal = True
        s = page([], 50)
        self.adb.snapshot.return_value = s
        self.assertFalse(self.runner.verify_completion(self.before, 50, s, "jump").success)

    def test_generic_component_survives_timer_replacement_at_the_same_anchor(self):
        self.adb.snapshot.side_effect = [generic(), generic(True)]
        self.runner.enter(self.before)
        self.runner.browse(self.before)
        self.assertTrue(self.runner.started_signal)
        self.assertTrue(self.runner.finished_signal)
        self.adb.input.assert_not_called()

    def test_generic_completion_requires_a_previously_observed_anchor(self):
        completed = generic(True)
        self.assertIsNone(completed.page_kind())
        completed.reward_anchor = generic().generic_component()[0]
        self.assertTrue(completed.task_completed(self.before))
        shifted = generic(True, shifted=True)
        shifted.reward_anchor = completed.reward_anchor
        self.assertFalse(shifted.task_completed(self.before))

    def test_generic_product_caption_cannot_create_a_reward_component(self):
        s = generic(True)
        panel = next(n for n in s.nodes if n.get('bounds') == '[120,1900][1080,2260]')
        panel.clear()
        panel.set('bounds', '[120,1900][1080,2260]')
        panel.set('text', '浏览15秒得400任务已完成已得400')
        s = Snapshot(ET.tostring(s.root, encoding="unicode"), PACKAGE, 1200, 2670)
        self.assertEqual(s.generic_panel(), '')

    def test_back_does_not_leave_the_list_or_touch_an_external_app(self):
        external = generic()
        external.package = "com.example.other"
        for s in (page(), external):
            self.adb.snapshot.return_value = s
            self.assertIs(self.runner.back_inside_taobao("test"), s)
        self.adb.input.assert_not_called()

    def test_back_rechecks_after_pause_and_resume(self):
        self.adb.snapshot.side_effect = [generic(), page()]
        self.adb.input.side_effect = RefreshRequired()
        self.assertTrue(self.runner.back_inside_taobao("test").is_list)
        self.assertEqual(self.adb.input.call_count, 1)

    def test_back_rejects_a_system_permission_window_inside_taobao(self):
        s = generic()
        node(s.root, "允许", package="com.android.permissioncontroller")
        s = Snapshot(ET.tostring(s.root, encoding="unicode"), PACKAGE, 1200, 2670)
        self.adb.snapshot.return_value = s
        with self.assertRaises(Halt):
            self.runner.back_inside_taobao("test")
        self.adb.input.assert_not_called()

    def test_taobao_launch_rechecks_foreground_when_the_pause_epoch_changes(self):
        self.adb.foreground.side_effect = ["com.example.other", PACKAGE]
        self.adb.launch_taobao.side_effect = RefreshRequired()
        self.runner.resume_taobao()
        self.adb.launch_taobao.assert_called_once_with(epoch=0)
        self.assertEqual(self.adb.foreground.call_count, 2)

    def test_rotating_failed_names_have_a_session_wide_limit(self):
        tasks = [Task(name, 2, 5, 15, '浏览15秒+400', None) for name in ['A', 'B', 'C']]
        self.adb.lease.side_effect = lambda: nullcontext()
        self.adb.foreground.return_value = PACKAGE
        self.runner.scan = Mock()
        self.runner.discover = Mock(side_effect=tasks)
        self.runner.find = Mock(side_effect=lambda title: (None, next(t for t in tasks if t.title == title), False))
        self.runner.execute_one = Mock(side_effect=lambda title: Result(title, 2, None, False, '未确认'))
        with self.assertRaisesRegex(Halt, '连续3次'):
            self.runner.run([], all_browse=True)
        self.assertEqual(self.runner.execute_one.call_count, 3)

    def test_regular_and_lite_editions_are_distinct(self):
        model.set_installed(['com.kuaishou.nebula'])
        self.addCleanup(model.set_installed, None)
        self.assertEqual(model.missing_app('去快手极速版领奖励'), '')
        self.assertEqual(model.missing_app('去快手领奖励'), '快手')

    def test_manual_selection_does_not_move_to_an_unrelated_reordered_row(self):
        app = App.__new__(App)
        app.has_scanned = True
        old = Task('已选旧名', 2, 5, 15, '浏览15秒+400', None)
        other = Task('未选任务', 0, 5, 15, '浏览15秒+400', None)
        renamed = Task('已选新名', 3, 5, 15, '浏览15秒+400', None)
        app.tasks = {old.title: old, other.title: other}
        app.selected = {old.title}
        app.table = Mock()
        app.table.get_children.return_value = []
        app.render_tasks([other, renamed])
        self.assertNotIn(other.title, app.selected)
        self.assertNotIn(renamed.title, app.selected)

    def test_duplicate_row_is_not_chosen_by_higher_count_or_an_unrelated_counter(self):
        s = page([('重复任务', 0, 5, 900), ('重复任务', 5, 5, 1200), ('独立任务', 0, 5, 1500)])
        self.assertIsNone(s.task('重复任务'))
        self.assertEqual(s.ambiguous, {'重复任务'})
        self.assertEqual(Runner.candidate(s.tasks()).title, '独立任务')


if __name__ == "__main__":
    unittest.main()
