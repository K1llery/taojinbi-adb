import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock

from taojinbi.adb import Adb, Control
from taojinbi.model import PACKAGE, Halt, Rect, RefreshRequired, Snapshot, Task, TaskFailed
from taojinbi.runner import Runner
from tests.test_core import node, page


def coin_home(list_page=False, energy=100000):
    s = page([], 50) if list_page else Snapshot('<hierarchy/>', PACKAGE, 1200, 2670)
    node(s.root, '淘金币首页')
    node(s.root, '', **{'resource-id': 'hd_assets_coin_icon'})
    node(s.root, '赚能量', '[951,1002][1203,1164]', clickable='true',
         **{'resource-id': 'energy_task_button'})
    node(s.root, f'升级中 松手开始加速升级 升级赛车拿钱 剩余 {energy} 能量',
         '[234,1002][966,1164]', **{'class': 'android.widget.Button'})
    return Snapshot(ET.tostring(s.root, encoding='unicode'), PACKAGE, 1200, 2670)


def shop_home():
    root = ET.Element('hierarchy')
    for rid in ('homepage_root_layout', 'search_bar_container'):
        node(root, '', **{'resource-id': f'{PACKAGE}:id/{rid}'})
    node(root, '首页')
    node(root, '我的淘宝')
    return Snapshot(ET.tostring(root, encoding='unicode'), PACKAGE, 1200, 2670)


def launch_dialog(target='微博', source='淘宝', button='本次允许', package='com.miui.securitycenter'):
    root = ET.Element('hierarchy')
    for text in ('启动应用', f'{source} 想要打开 {target}，是否允许？'):
        node(root, text, package=package)
    node(root, button, '[100,1900][1100,2050]', package=package, clickable='true',
         **{'class': 'android.widget.Button'})
    return Snapshot(ET.tostring(root, encoding='unicode'), package, 1200, 2670)


def jump_list(energy):
    s = coin_home(list_page=True, energy=energy)
    row = node(s.root, '', '[0,900][1200,1120]')
    node(row, '去微博看看热搜(0/1)跳转得奖励+400', '[100,900][900,1000]')
    node(row, '去完成', '[950,900][1140,1000]', clickable='true')
    node(s.root, '关闭', '[1071,513][1170,612]', clickable='true',
         **{'class': 'android.widget.Button'})
    return Snapshot(ET.tostring(s.root, encoding='unicode'), PACKAGE, 1200, 2670)


class JumpRecoveryTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.adb = Mock(control=Control())
        self.adb.foreground.return_value = PACKAGE
        self.runner = Runner(self.adb, directory=Path(folder.name))
        self.now = 0
        self.runner.control.clock = lambda: self.now
        self.runner.control.wait = Mock(side_effect=self.wait)
        self.task = Task('去微博看看热搜', 0, 1, None, '跳转得奖励+400', None)

    def wait(self, seconds):
        self.now += seconds

    def test_coin_home_button_is_clipped_to_the_visible_screen(self):
        self.assertEqual(coin_home().coin_task_button(), Rect(951, 1002, 1200, 1164))
        self.assertIsNone(coin_home(list_page=True).coin_task_button())

    def test_coin_home_button_accepts_the_observed_go_earn_label(self):
        s = coin_home()
        next(n for n in s.nodes if n.get('resource-id') == 'energy_task_button').set('text', '去赚能量')
        s = Snapshot(ET.tostring(s.root, encoding='unicode'), PACKAGE, 1200, 2670)
        self.assertEqual(s.coin_task_button(), Rect(951, 1002, 1200, 1164))

    def test_home_recovery_with_the_go_earn_label_reopens_the_list(self):
        s = coin_home()
        next(n for n in s.nodes if n.get('resource-id') == 'energy_task_button').set('text', '去赚能量')
        s = Snapshot(ET.tostring(s.root, encoding='unicode'), PACKAGE, 1200, 2670)
        self.adb.snapshot.side_effect = [s, page()]
        self.assertTrue(self.runner.jump_back('test').is_list)
        self.assertEqual(self.adb.input.call_args.args[1:], ('tap', '1075', '1083'))

    def test_return_reopens_coin_home_modal_without_pressing_back(self):
        self.adb.snapshot.side_effect = [coin_home(), page()]
        self.assertTrue(self.runner.jump_back('test').is_list)
        self.assertEqual(self.adb.input.call_args.args[1:], ('tap', '1075', '1083'))
        self.adb.launch_taobao.assert_not_called()

    def test_shop_home_recovery_opens_activity_then_modal(self):
        self.adb.snapshot.side_effect = [shop_home(), coin_home(), page()]
        self.assertTrue(self.runner.jump_back('test').is_list)
        self.adb.open_coin_home.assert_called_once()
        self.assertEqual(self.adb.input.call_count, 1)

    def test_no_back_is_sent_from_either_home_page(self):
        for s in (coin_home(), shop_home()):
            self.adb.snapshot.return_value = s
            self.assertIs(self.runner.back_inside_taobao('test'), s)
        self.adb.input.assert_not_called()

    def test_recovery_after_pause_reobserves_before_using_coordinates(self):
        self.adb.input.side_effect = [RefreshRequired(), None]
        self.adb.snapshot.side_effect = [coin_home(), coin_home(), page()]
        self.assertTrue(self.runner.jump_back('test').is_list)
        self.assertEqual(self.adb.snapshot.call_count, 3)

    def test_repeated_home_recovery_has_an_input_limit(self):
        self.adb.snapshot.return_value = coin_home()
        with self.assertRaisesRegex(Halt, '超过上限'):
            self.runner.jump_back('test')
        self.assertEqual(self.adb.input.call_count, 3)

    def test_dialog_is_not_treated_as_the_destination_app(self):
        external = Snapshot('<hierarchy/>', 'com.sina.weibo', 1200, 2670)
        self.adb.snapshot.side_effect = [launch_dialog(), external]
        self.assertEqual(self.runner.await_target(self.task), 'com.sina.weibo')
        self.assertEqual(self.adb.input.call_args.args[1:], ('tap', '600', '1975'))

    def test_other_sources_targets_and_persistent_grants_are_rejected(self):
        for s in (launch_dialog(source='其他应用'), launch_dialog(target='微信'),
                  launch_dialog(button='始终允许'), launch_dialog(package='com.android.permissioncontroller')):
            with self.subTest(xml=s.xml):
                with self.assertRaises(Halt):
                    self.runner.handle_launch_dialog(s, self.task)
        self.adb.input.assert_not_called()

    def test_launch_dialog_repetition_is_not_authorized_again(self):
        self.runner.launch_confirmations = 1
        with self.assertRaises(TaskFailed):
            self.runner.handle_launch_dialog(launch_dialog(), self.task)
        self.adb.input.assert_not_called()

    def test_launch_dialog_pause_discards_the_old_target(self):
        self.adb.input.side_effect = RefreshRequired()
        self.adb.snapshot.side_effect = [launch_dialog(), launch_dialog(target='微信')]
        with self.assertRaises(Halt):
            self.runner.await_target(self.task)
        self.assertEqual(self.adb.input.call_count, 1)

    def test_system_panel_animation_is_waited_for_without_launching_taobao(self):
        loading = Snapshot('<hierarchy/>', 'com.miui.securitycenter', 1200, 2670)
        external = Snapshot('<hierarchy/>', 'com.sina.weibo', 1200, 2670)
        self.adb.snapshot.side_effect = [loading, launch_dialog(), external]
        self.assertEqual(self.runner.await_target(self.task), 'com.sina.weibo')
        self.adb.launch_taobao.assert_not_called()

    def test_stale_background_energy_never_confirms_capped_completion(self):
        self.runner.baseline_energy = 100000
        self.runner.open_visit = True
        s = coin_home(list_page=True, energy=100400)
        result = self.runner.capped_completion(s, self.task, 50)
        self.assertIsNone(result)
        for energy, visited in ((100000, True), (100399, True), (100401, True), (100400, False)):
            self.runner.open_visit = visited
            self.assertIsNone(self.runner.capped_completion(
                coin_home(list_page=True, energy=energy), self.task, 50))

    def test_multiple_energy_labels_are_not_accepted(self):
        s = coin_home(list_page=True)
        node(s.root, '升级中 松手开始加速升级 升级赛车拿钱 剩余 100400 能量',
             '[234,1002][966,1164]', **{'class': 'android.widget.Button'})
        s = Snapshot(ET.tostring(s.root, encoding='unicode'), PACKAGE, 1200, 2670)
        self.assertIsNone(s.coin_energy)

    def test_capped_visit_does_not_close_the_list_to_refresh_energy(self):
        s = coin_home(list_page=True, energy=100000)
        node(s.root, '关闭', '[1071,513][1170,612]', clickable='true',
             **{'class': 'android.widget.Button'})
        s = Snapshot(ET.tostring(s.root, encoding='unicode'), PACKAGE, 1200, 2670)
        self.runner.baseline_energy = 100000
        self.runner.open_visit = True
        self.runner.action = Mock()
        self.adb.snapshot.return_value = s
        self.runner.refresh_coin_list = Mock(side_effect=AssertionError('不能主动刷新'))
        result = self.runner.verify_completion(self.task, 50, s, 'test')
        self.assertFalse(result.success)
        self.assertEqual(self.runner.visits, [result])
        self.runner.action.assert_not_called()
        self.runner.refresh_coin_list.assert_not_called()
        self.adb.input.assert_not_called()

    def test_actual_external_visit_is_not_retried_even_when_caption_is_browse(self):
        task = Task('浏览后跳到外部', 0, 1, 5, '浏览5秒+400', None)
        s = coin_home(list_page=True, energy=100000)
        self.adb.snapshot.return_value = s
        self.runner.open_visit = True
        result = self.runner.verify_completion(task, 50, s, 'test')
        self.assertFalse(result.success)
        self.assertEqual(self.runner.visits, [result])
        self.assertIn(task.title, self.runner.skip)
        self.adb.input.assert_not_called()

    def test_zero_reward_cannot_confirm_an_unchanged_energy_balance(self):
        self.runner.baseline_energy = 100000
        self.runner.open_visit = True
        task = Task('跳转任务', 0, 1, None, '点击去逛+0', None)
        self.assertIsNone(self.runner.capped_completion(coin_home(list_page=True), task, 50))

    def test_task_start_only_taps_the_task_without_closing_the_list(self):
        self.adb.snapshot.return_value = jump_list(100000)
        self.runner.refresh_coin_list = Mock(side_effect=AssertionError('不能主动刷新'))
        self.runner.tap_task(self.task.title)
        self.runner.refresh_coin_list.assert_not_called()
        self.adb.input.assert_called_once()
        self.assertEqual(self.adb.input.call_args.args[1:], ('tap','1045','950'))

    def test_task_start_reobserves_after_pause_without_a_home_detour(self):
        s = jump_list(100000)
        fresh = jump_list(100400)
        fresh.epoch = 1
        self.adb.snapshot.side_effect = [s, fresh]
        self.adb.input.side_effect = [RefreshRequired(), None]
        self.runner.refresh_coin_list = Mock(side_effect=AssertionError('不能主动刷新'))
        self.runner.tap_task(self.task.title)
        self.assertIs(self.adb.input.call_args.args[0], fresh)
        self.runner.refresh_coin_list.assert_not_called()

    def test_pause_invalidates_energy_completion_evidence(self):
        self.runner.baseline_energy = 100000
        self.runner.open_visit = True
        s = coin_home(list_page=True, energy=100400)
        s.epoch = 1
        self.assertIsNone(self.runner.capped_completion(s, self.task, 50))

    def test_system_ui_and_launchers_are_not_destination_apps(self):
        for package in ('com.android.systemui', 'com.miui.home', 'com.android.launcher3'):
            with self.subTest(package=package):
                self.adb.snapshot.return_value = Snapshot('<hierarchy/>', package, 1200, 2670)
                with self.assertRaises(Halt):
                    self.runner.await_target(self.task)
        self.adb.input.assert_not_called()

    def test_lite_edition_does_not_authorize_the_regular_app(self):
        task = Task('去快手极速版签到', 0, 1, None, '点击去逛+600', None)
        with self.assertRaises(Halt):
            self.runner.handle_launch_dialog(launch_dialog(target='快手'), task)
        self.adb.input.assert_not_called()
        self.assertTrue(self.runner.handle_launch_dialog(launch_dialog(target='快手极速版'), task))

    def test_only_verified_shop_home_can_open_the_fixed_coin_url(self):
        adb = Adb('adb.exe')
        adb.run = Mock()
        with self.assertRaises(Halt):
            adb.open_coin_home(page())
        adb.run.assert_not_called()
        s = shop_home()
        s.epoch = 7
        adb.open_coin_home(s)
        self.assertEqual(adb.run.call_args.kwargs, {'epoch': 7})
        self.assertIn('https://pages-fast.m.taobao.com/wow/z/tmtjb/town/home', adb.run.call_args.args[0])

    def test_resume_uses_launcher_intent_flags(self):
        adb = Adb('adb.exe')
        adb.run = Mock()
        adb.launch_taobao(epoch=4)
        args = adb.run.call_args.args[0]
        self.assertIn('android.intent.action.MAIN', args)
        self.assertIn('android.intent.category.LAUNCHER', args)
        self.assertIn('0x10200000', args)
        self.assertEqual(adb.run.call_args.kwargs, {'epoch': 4})

    def test_external_app_reclaiming_foreground_during_verification_is_recovered(self):
        external = Snapshot('<hierarchy/>', 'com.eg.android.AlipayGphone', 1200, 2670)
        self.adb.snapshot.return_value = external
        self.runner.jump_back = Mock(return_value=page())
        self.assertTrue(self.runner.returned_list('test').is_list)
        self.runner.jump_back.assert_called_once_with('test')

    def test_home_reached_after_an_in_app_back_is_recovered(self):
        self.runner.action = Mock()
        self.adb.snapshot.return_value = coin_home()
        self.runner.jump_back = Mock(return_value=page())
        self.assertTrue(self.runner.back('timed', 'list', 'test').is_list)
        self.runner.jump_back.assert_called_once_with('test')

    def test_delayed_external_launch_during_in_app_visit_uses_jump_completion(self):
        self.adb.snapshot.return_value = Snapshot('<hierarchy/>', 'com.taobao.idlefish', 1200, 2670)
        self.runner.finish_jump = Mock(return_value='result')
        self.assertEqual(self.runner.execute_timed(self.task, 50), 'result')
        self.runner.finish_jump.assert_called_once_with(self.task, 50, 'com.taobao.idlefish')

    def test_settle_does_not_accept_a_list_covered_by_a_permission_dialog(self):
        s = page()
        node(s.root, '允许', '[100,1000][1100,1800]', package='com.android.permissioncontroller')
        self.adb.snapshot.return_value = Snapshot(ET.tostring(s.root, encoding='unicode'), PACKAGE, 1200, 2670)
        with self.assertRaises(Halt):
            self.runner.settle_list(2.4)

    def test_transient_no_active_window_is_retried(self):
        adb = Adb('adb.exe')
        adb.reader_path = '/data/local/tmp/test.jar'
        adb.size = (1200, 2670)
        adb.foreground = Mock(return_value=PACKAGE)
        adb.control.wait = Mock()
        adb.run = Mock(side_effect=[Halt('java.lang.IllegalStateException: No active accessibility window'),
                                    b'<hierarchy/>'])
        self.assertEqual(adb.snapshot().package, PACKAGE)
        self.assertEqual(adb.run.call_count, 2)

    def test_no_active_window_retry_is_bounded_and_other_errors_are_not_retried(self):
        for error, attempts in (('java.lang.IllegalStateException: No active accessibility window', 3),
                                ('device offline', 1)):
            with self.subTest(error=error):
                adb = Adb('adb.exe')
                adb.reader_path = '/data/local/tmp/test.jar'
                adb.control.wait = Mock()
                adb.run = Mock(side_effect=Halt(error))
                with self.assertRaises(Halt):
                    adb.snapshot()
                self.assertEqual(adb.run.call_count, attempts)


if __name__ == '__main__':
    unittest.main()
