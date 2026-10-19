import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock

from taojinbi.adb import Control
from taojinbi.model import Halt, PACKAGE, RefreshRequired, Snapshot
from taojinbi.treasure import POPUP_CLOSE_ASSET, LiveTask, TreasurePage, TreasureRunner, live_budget
from tests.test_core import node
from tests.test_treasure import TITLE, live_page, task_page


def with_packet(s=None, asset=POPUP_CLOSE_ASSET):
    s = s or live_page()
    web = node(s.root, '', **{'class': 'android.webkit.WebView'})
    for text in ('恭喜你被','限时红包','砸中了！','马上去用'):
        node(web,text)
    node(web,asset+'.png_140x10000.jpg_', '[540,1935][660,2052]',
         clickable='true', **{'class': 'android.widget.Image'})
    return Snapshot(ET.tostring(s.root,encoding='unicode'),PACKAGE,1200,2670)


class ResilienceTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.adb = Mock(control=Control())
        self.runner = TreasureRunner(self.adb,directory=Path(self.folder.name))
        self.now = 0.0
        self.runner.control.clock = lambda: self.now
        self.runner.control.wait = Mock(side_effect=self.advance)
        self.runner.live_room = 'ID:12345'
        self.runner.live_anchor = TreasurePage(live_page()).bubble()[0]

    def advance(self, seconds):
        self.now += seconds

    def test_timer_budgets_use_seven_seconds_or_twenty_percent_margin(self):
        self.assertEqual([live_budget(s) for s in (0,1,4,5,15,30)], [7,7,7,7,18,36])

    def test_unfinished_five_second_timer_does_not_wait_an_extra_thirty_seconds(self):
        frames = iter([task_page()])
        self.adb.snapshot.side_effect = lambda: next(frames,live_page(seconds=5))
        task = TreasurePage(task_page()).task(TITLE)
        with self.assertRaisesRegex(Halt,'未出现完成提示'):
            self.runner.execute_live(task)
        self.assertLessEqual(self.now,7.25)
        self.assertEqual(self.adb.input.call_count,1)

    def test_completed_alias_is_only_accepted_in_the_reward_component(self):
        s = live_page(completed=True)
        next(n for n in s.nodes if n.get('text')=='完成任务\n获得奖励').set('text','恭喜完成任务')
        s = Snapshot(ET.tostring(s.root,encoding='unicode'),PACKAGE,1200,2670)
        self.assertTrue(TreasurePage(s).bubble()[2])
        s = live_page()
        node(s.root,'恭喜完成任务')
        s = Snapshot(ET.tostring(s.root,encoding='unicode'),PACKAGE,1200,2670)
        self.assertFalse(TreasurePage(s).bubble()[2])

    def test_packet_selector_requires_the_known_modal_and_close_asset(self):
        self.assertEqual(TreasurePage(with_packet()).promotion_close().center, (600,1993))
        self.assertIsNone(TreasurePage(with_packet(asset='other_image')).promotion_close())
        s = with_packet()
        next(n for n in s.nodes if n.get('text')=='马上去用').set('text','其他商品')
        s = Snapshot(ET.tostring(s.root,encoding='unicode'),PACKAGE,1200,2670)
        self.assertIsNone(TreasurePage(s).promotion_close())

    def test_close_packet_does_not_click_use_or_native_room_exit(self):
        self.adb.snapshot.return_value = live_page()
        result = self.runner.dismiss_promotion(with_packet())
        self.assertTrue(TreasurePage(result).live)
        self.assertEqual(self.adb.input.call_args.args[1:], ('tap','600','1993'))
        self.assertEqual(self.runner.popup_closes,1)

    def test_close_packet_rechecks_room_after_pause(self):
        self.adb.input.side_effect = RefreshRequired()
        self.adb.snapshot.return_value = with_packet(live_page(room='99999'))
        with self.assertRaisesRegex(Halt,'直播间已变化'):
            self.runner.dismiss_promotion(with_packet())
        self.assertEqual(self.adb.input.call_count,1)

    def test_close_packet_does_not_repeat_a_stale_click_after_popup_disappears(self):
        self.adb.input.side_effect = RefreshRequired()
        self.adb.snapshot.return_value = live_page()
        self.runner.dismiss_promotion(with_packet())
        self.assertEqual(self.adb.input.call_count,1)

    def test_disappeared_packet_after_pause_cannot_rebind_to_a_different_room(self):
        self.adb.input.side_effect = RefreshRequired()
        self.adb.snapshot.return_value = live_page(room='99999')
        with self.assertRaisesRegex(Halt,'直播间已变化'):
            self.runner.dismiss_promotion(with_packet())
        self.assertEqual(self.runner.live_room,'ID:12345')
        self.assertEqual(self.adb.input.call_count,1)

    def test_entry_does_not_adopt_another_room_when_popup_closing_is_interrupted(self):
        self.adb.input.side_effect = [None,RefreshRequired()]
        self.adb.snapshot.side_effect = [task_page(),with_packet(),live_page(room='99999')]
        task = TreasurePage(task_page()).task(TITLE)
        with self.assertRaisesRegex(Halt,'直播间已变化'):
            self.runner.execute_live(task)
        self.assertEqual(self.runner.live_room,'ID:12345')
        self.assertEqual(self.adb.input.call_count,2)

    def test_popup_dismissal_has_a_bounded_retry_limit(self):
        self.runner.popup_closes = 3
        with self.assertRaises(Halt):
            self.runner.dismiss_promotion(with_packet())
        self.adb.input.assert_not_called()

    def test_one_row_replacement_avoids_scroll_but_still_requires_full_evidence(self):
        final = LiveTask(TITLE,9,10,1000,None)
        baseline = task_page(rows=[(TITLE,9,10,1000),('逛一逛其他直播间',0,3,600)])
        after = task_page(steps=11000,rows=[('逛一逛新直播间',0,1,600),('逛一逛其他直播间',0,3,600)])
        self.runner.live_baseline = TreasurePage(baseline).tasks()
        self.runner.confirm_absent = Mock(side_effect=AssertionError('不应滚动'))
        result = self.runner.verify_live(final,10000,after,transition=True)
        self.assertEqual(result.after,10)
        self.assertIn('仅原任务行被替换',result.reason)
        self.runner.confirm_absent.assert_not_called()

    def test_fast_replacement_rejects_other_changed_or_missing_rows(self):
        final = LiveTask(TITLE,9,10,1000,None)
        baseline = task_page(rows=[(TITLE,9,10,1000),('逛一逛其他直播间',0,3,600)])
        self.runner.live_baseline = TreasurePage(baseline).tasks()
        changed = task_page(rows=[('逛一逛新直播间',0,1,600),('逛一逛其他直播间',1,3,600)])
        missing = task_page(rows=[('逛一逛新直播间',0,1,600)])
        self.assertFalse(self.runner.replacement_verified(final,TreasurePage(changed)))
        self.assertFalse(self.runner.replacement_verified(final,TreasurePage(missing)))

    def test_shifted_recycled_viewport_is_not_a_one_row_replacement(self):
        final = LiveTask(TITLE,9,10,1000,None)
        baseline = task_page(rows=[(TITLE,9,10,1000),('逛一逛B直播间',0,3,600),('逛一逛C直播间',0,3,600)])
        shifted = task_page(rows=[('逛一逛B直播间',0,3,600),('逛一逛C直播间',0,3,600),('逛一逛D直播间',0,1,600)])
        self.runner.live_baseline = TreasurePage(baseline).tasks()
        self.assertFalse(self.runner.replacement_verified(final,TreasurePage(shifted)))

    def test_return_reuses_the_just_validated_frame_without_an_extra_dump(self):
        self.adb.snapshot.return_value = task_page()
        self.assertTrue(TreasurePage(self.runner.leave_live(initial=live_page(completed=True))).is_list)
        self.adb.snapshot.assert_called_once()
        self.assertEqual(self.adb.input.call_args.args[1:],('keyevent','4'))

    def test_reused_completion_frame_is_discarded_after_pause(self):
        self.adb.input.side_effect = RefreshRequired()
        self.adb.snapshot.return_value = live_page(completed=True,room='99999')
        with self.assertRaisesRegex(Halt,'直播间身份已变化'):
            self.runner.leave_live(initial=live_page(completed=True))
        self.assertEqual(self.adb.input.call_count,1)

    def test_fast_replacement_never_overrides_missing_completion_transition(self):
        final = LiveTask(TITLE,9,10,1000,None)
        baseline = task_page(rows=[(TITLE,9,10,1000),('逛一逛其他直播间',0,3,600)])
        after = task_page(steps=11000,rows=[('逛一逛新直播间',0,1,600),('逛一逛其他直播间',0,3,600)])
        self.runner.live_baseline = TreasurePage(baseline).tasks()
        self.adb.snapshot.return_value = after
        with self.assertRaises(Halt):
            self.runner.verify_live(final,10000,after,transition=False)
        self.assertEqual(self.runner.results,[])


if __name__ == '__main__':
    unittest.main()
