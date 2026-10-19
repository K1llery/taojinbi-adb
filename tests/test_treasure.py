import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock

from taojinbi.adb import Control
from taojinbi.model import Halt, PACKAGE, RefreshRequired, Snapshot, Stopped, TaskChanged
from taojinbi.treasure import LIVE_ROOT, LiveTask, TreasurePage, TreasureRunner
from tests.test_core import node, page as coin_page


TITLE = '逛一逛精选直播间'


def task_page(done=0, steps=10000, rows=None, home_only=False):
    root = ET.Element('hierarchy')
    window = node(root, '直播攒红包', **{'class': 'android.webkit.WebView'})
    node(window, f'剩余{steps}步数')
    node(window, '赚步数', '[950,300][1150,400]', clickable='true')
    if not home_only:
        node(window, '做任务 集步数')
        for i, (title, count, total, reward) in enumerate(rows or [(TITLE, done, 10, 1000)]):
            y = 600 + 200*i
            row = node(window, bounds=f'[0,{y}][1200,{y+190}]', clickable='true')
            label = node(row, bounds=f'[200,{y}][900,{y+70}]')
            node(label, title, f'[200,{y}][700,{y+70}]')
            node(label, f'({count}/{total})', f'[710,{y}][900,{y+70}]')
            node(row, '完成任务得', f'[200,{y+90}][400,{y+150}]')
            node(row, str(reward), f'[410,{y+90}][500,{y+150}]')
            node(row, '步', f'[510,{y+90}][570,{y+150}]')
            node(row, '已完成' if count == total else '去完成', f'[970,{y+50}][1120,{y+120}]')
    return Snapshot(ET.tostring(root, encoding='unicode'), PACKAGE, 1200, 2670)


def live_page(seconds=8, completed=False, moved=False, promo=False, room='12345'):
    root = ET.Element('hierarchy')
    window = node(root, **{'resource-id': LIVE_ROOT})
    node(window, f'ID:{room}')
    if promo:
        node(window, '主播新人红包')
        node(window, '领', clickable='true')
    panel = node(window, bounds='[996,1000][1173,1220]' if moved else '[996,1074][1173,1293]',
                 **{'resource-id': 'alv_interactive_2319'})
    text = '完成任务\n获得奖励' if completed else f'浏览{seconds}秒\n得奖励'
    node(panel, text, '[1011,1206][1161,1275]')
    return Snapshot(ET.tostring(root, encoding='unicode'), PACKAGE, 1200, 2670)


class TreasureParsingTests(unittest.TestCase):
    def test_split_title_counter_and_unclickable_label_keep_their_row_button(self):
        page = TreasurePage(task_page(rows=[('邀请好友集步数',0,5,300),(TITLE,1,10,1000)]))
        self.assertTrue(page.is_list)
        task = page.task(TITLE)
        self.assertEqual((task.done, task.total, task.reward), (1,10,1000))
        self.assertEqual(task.button.center, (1045,885))
        self.assertFalse(page.task('邀请好友集步数').supported)
        self.assertEqual(page.steps, 10000)

    def test_only_live_browsing_tasks_are_supported(self):
        for title in ('逛蜜蜂惊喜社直播间','逛一逛李佳琦直播间','逛一逛陈洁KiKi直播间'):
            self.assertTrue(LiveTask(title,0,3,600,None).supported)
        for title in ('邀请好友集步数','玩消消乐领金币','看短视频领金豆','去农场1分钱领水果'):
            self.assertFalse(LiveTask(title,0,1,600,None).supported)

    def test_missing_button_cannot_borrow_the_next_rows_button(self):
        s = task_page(rows=[(TITLE,0,10,1000),('逛一逛李佳琦直播间',0,3,600)])
        button = next(n for n in s.nodes if n.get('text') == '去完成')
        s.parents[button].remove(button)
        page = TreasurePage(Snapshot(ET.tostring(s.root,encoding='unicode'),PACKAGE,1200,2670))
        self.assertIsNone(page.task(TITLE).button)

    def test_hidden_or_disabled_buttons_are_not_actionable(self):
        for attr, value in [('enabled','false'),('visible-to-user','false')]:
            s = task_page()
            next(n for n in s.nodes if n.get('text') == '去完成').set(attr,value)
            page = TreasurePage(Snapshot(ET.tostring(s.root,encoding='unicode'),PACKAGE,1200,2670))
            self.assertIsNone(page.task(TITLE).button)

    def test_duplicate_names_are_not_resolved_by_larger_count(self):
        page = TreasurePage(task_page(rows=[(TITLE,0,10,1000),(TITLE,4,10,1000)]))
        self.assertFalse(page.task(TITLE).supported)
        self.assertIsNone(page.task(TITLE).button)

    def test_coin_page_is_not_a_treasure_task_list(self):
        self.assertFalse(TreasurePage(coin_page()).is_list)
        self.assertEqual(TreasurePage(coin_page()).tasks(), [])

    def test_live_timer_and_completion_share_the_component_identity(self):
        start = TreasurePage(live_page()).bubble()
        end = TreasurePage(live_page(completed=True)).bubble()
        self.assertEqual(start[0], end[0])
        self.assertEqual(start[1:], (8,False))
        self.assertEqual(end[1:], (None,True))

    def test_chat_completion_and_countdown_do_not_become_a_task_component(self):
        s = live_page()
        n = next(n for n in s.nodes if n.get('resource-id') == 'alv_interactive_2319')
        n.set('resource-id','chat_message')
        page = TreasurePage(Snapshot(ET.tostring(s.root,encoding='unicode'),PACKAGE,1200,2670))
        self.assertIsNone(page.bubble())


class TreasureRunnerTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.adb = Mock(control=Control())
        self.adb.lease.side_effect = lambda: nullcontext()
        self.runner = TreasureRunner(self.adb, directory=Path(self.folder.name))
        now = [0.0]
        self.runner.control.clock = lambda: now[0]
        self.runner.control.wait = Mock(side_effect=lambda s: now.__setitem__(0,now[0]+s))
        self.task = TreasurePage(task_page()).task(TITLE)
        self.runner.live_room = 'ID:12345'
        self.runner.live_anchor = TreasurePage(live_page()).bubble()[0]

    def test_complete_flow_requires_counter_and_steps_and_sends_no_commercial_clicks(self):
        self.adb.snapshot.side_effect = [task_page(),live_page(),live_page(completed=True,promo=True),
                                        live_page(completed=True),live_page(completed=True),task_page(1,11000)]
        result = self.runner.execute_live(self.task)
        self.assertTrue(result.success)
        self.assertEqual((result.before,result.after),(0,1))
        self.assertEqual([c.args[1:] for c in self.adb.input.call_args_list],
                         [('tap','1045','685'),('keyevent','4')])

    def test_counter_alone_or_steps_alone_cannot_confirm_success(self):
        for after in (task_page(1,10000),task_page(0,11000)):
            self.adb.snapshot.side_effect = None
            frames = iter([task_page(),live_page(completed=True)])
            self.adb.snapshot.side_effect = lambda: next(frames,after)
            self.runner.leave_live = Mock(return_value=after)
            with self.assertRaisesRegex(Halt,'次数与步数未同时确认'):
                self.runner.execute_live(self.task)
            self.assertEqual(self.runner.results, [])

    def test_missing_steps_prevents_the_first_click(self):
        s = task_page()
        next(n for n in s.nodes if n.get('text')=='剩余10000步数').set('text','步数加载中')
        self.adb.snapshot.return_value = Snapshot(ET.tostring(s.root,encoding='unicode'),PACKAGE,1200,2670)
        with self.assertRaisesRegex(Halt,'未点击'):
            self.runner.execute_live(self.task)
        self.adb.input.assert_not_called()

    def test_ambiguous_return_row_does_not_confirm_success(self):
        after = task_page(steps=11000, rows=[(TITLE,0,10,1000),(TITLE,1,10,1000)])
        frames = iter([task_page(),live_page(completed=True)])
        self.adb.snapshot.side_effect = lambda: next(frames,after)
        self.runner.leave_live = Mock(return_value=after)
        with self.assertRaisesRegex(Halt,'次数与步数未同时确认'):
            self.runner.execute_live(self.task)
        self.assertEqual(self.runner.results, [])

    def test_changed_task_prevents_the_first_click(self):
        self.adb.snapshot.return_value = task_page(done=1)
        with self.assertRaises(TaskChanged):
            self.runner.execute_live(self.task)
        self.adb.input.assert_not_called()

    def test_return_does_not_press_back_on_the_task_list(self):
        self.adb.snapshot.return_value = task_page()
        self.assertTrue(TreasurePage(self.runner.leave_live()).is_list)
        self.adb.input.assert_not_called()

    def test_return_rechecks_after_pause_before_sending_back(self):
        self.adb.snapshot.side_effect = [live_page(completed=True),task_page()]
        self.adb.input.side_effect = RefreshRequired()
        self.assertTrue(TreasurePage(self.runner.leave_live()).is_list)
        self.assertEqual(self.adb.input.call_count,1)

    def test_return_rejects_a_different_room_after_pause(self):
        self.adb.snapshot.side_effect = [live_page(completed=True),live_page(completed=True,room='99999')]
        self.adb.input.side_effect = RefreshRequired()
        with self.assertRaisesRegex(Halt,'直播间身份已变化'):
            self.runner.leave_live()
        self.assertEqual(self.adb.input.call_count,1)

    def test_changed_live_component_does_not_trigger_return(self):
        self.adb.snapshot.side_effect = [task_page(),live_page(),live_page(completed=True,moved=True)]
        with self.assertRaisesRegex(Halt,'组件身份已变化'):
            self.runner.execute_live(self.task)
        self.assertEqual(self.adb.input.call_count,1)

    def test_discovery_traverses_completed_rows_instead_of_declaring_exhaustion(self):
        a = task_page(rows=[('已完成A',1,1,600)])
        b = task_page(rows=[('已完成B',1,1,600)])
        self.runner.ensure_list = Mock(return_value=a)
        self.adb.snapshot.side_effect = [a,b,task_page()]
        self.runner.list_scroll = Mock()
        _, task = self.runner.locate(None)
        self.assertEqual(task.title,TITLE)

    def test_stop_during_run_still_writes_results(self):
        self.runner.scan = Mock(side_effect=Stopped('已停止'))
        self.runner.run([],all_browse=True)
        self.assertEqual((Path(self.folder.name)/'results.json').read_text(encoding='utf-8'),'[]')

    def test_final_replacement_requires_transition_exact_steps_and_full_absence(self):
        final = LiveTask(TITLE,9,10,1000,self.task.button)
        after = task_page(steps=11000,rows=[('逛一逛SK-II直播间',0,1,600)])
        self.adb.snapshot.return_value = after
        self.runner.list_scroll = Mock()
        result = self.runner.verify_live(final,10000,after,transition=True)
        self.assertEqual(result.after,10)
        self.assertIn('完整列表',result.reason)
        self.assertEqual(self.runner.list_scroll.call_count,2)

    def test_final_replacement_without_observed_countdown_does_not_count(self):
        final = LiveTask(TITLE,9,10,1000,self.task.button)
        after = task_page(steps=11000,rows=[('逛一逛SK-II直播间',0,1,600)])
        self.adb.snapshot.return_value = after
        self.runner.confirm_absent = Mock()
        with self.assertRaises(Halt):
            self.runner.verify_live(final,10000,after,transition=False)
        self.runner.confirm_absent.assert_not_called()

    def test_nonfinal_replacement_never_uses_absence_as_completion(self):
        after = task_page(steps=11000,rows=[('逛一逛新直播间',0,1,600)])
        self.adb.snapshot.return_value = after
        self.runner.confirm_absent = Mock()
        with self.assertRaises(Halt):
            self.runner.verify_live(self.task,10000,after,transition=True)
        self.runner.confirm_absent.assert_not_called()

    def test_absence_scan_rejects_a_matching_offscreen_or_ambiguous_caption(self):
        s = task_page()
        title = next(n for n in s.nodes if n.get('text')==TITLE)
        title.set('visible-to-user','false')
        s = Snapshot(ET.tostring(s.root,encoding='unicode'),PACKAGE,1200,2670)
        self.adb.snapshot.return_value = s
        _, absent = self.runner.confirm_absent(TITLE)
        self.assertFalse(absent)
        self.adb.input.assert_not_called()

    def test_whitespace_in_an_existing_caption_cannot_trigger_final_absence(self):
        final = LiveTask(TITLE,9,10,1000,self.task.button)
        s = task_page(steps=11000,rows=[('  '+TITLE+'  ',9,10,1000)])
        self.adb.snapshot.return_value = s
        self.runner.confirm_absent = Mock()
        with self.assertRaises(Halt):
            self.runner.verify_live(final,10000,s,transition=True)
        self.runner.confirm_absent.assert_not_called()
        self.assertEqual(self.runner.results, [])

    def test_absence_scan_normalizes_captions_like_the_row_parser(self):
        s = task_page(rows=[('  '+TITLE+'  ',9,10,1000)])
        self.adb.snapshot.return_value = s
        _, absent = self.runner.confirm_absent(TITLE)
        self.assertFalse(absent)
        self.adb.input.assert_not_called()


if __name__ == '__main__':
    unittest.main()
