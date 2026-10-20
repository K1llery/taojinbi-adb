import json
import tempfile
import unittest
import xml.etree.ElementTree as ET
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock

from taojinbi import model
from taojinbi.adb import Control
from taojinbi.model import PACKAGE, Halt, Rect, Snapshot, Task, TaskChanged, TaskFailed
from taojinbi.runner import Result, Runner, completed_transition
from tests.test_core import immersive, node, page


def feed(completed=False):
    root = ET.Element("hierarchy")
    window = node(root)
    node(window, "体力-固推&自建feeds-interact")
    header = node(window, **{"resource-id": "composeHeaderNav"})
    for text in (["已得", "奖励单位", "400", "下单再得", "2000"] if completed else ["浏览", "15", "秒可领", "奖励单位", "400"]):
        node(header, text, "[500,170][1100,260]")
    node(window, "商品A ¥100 金币已抵", "[30,350][580,1200]", clickable="true", **{"class": "android.widget.Button"})
    node(window, "优惠券 ¥100", "[950,2100][1180,2450]", clickable="true", **{"class": "android.widget.Button"})
    return Snapshot(ET.tostring(root, encoding="unicode"), PACKAGE, 1200, 2670)


def product():
    root = ET.Element("hierarchy")
    window = node(root)
    node(window, "宝贝")
    node(window, "加入购物车")
    node(window, "领券购买")
    return Snapshot(ET.tostring(root, encoding="unicode"), PACKAGE, 1200, 2670)


def search_page(focused=True, value="搜索", password=False):
    root = ET.Element("hierarchy")
    window = node(root)
    node(window, value, "[180,150][890,260]", clickable="true", focused=str(focused).lower(),
         password=str(password).lower(), **{"class": "android.widget.EditText", "resource-id": f"{PACKAGE}:id/searchEdit"})
    node(window, "搜索", "[990,150][1170,260]", clickable="true", **{"resource-id": f"{PACKAGE}:id/searchbtn"})
    node(window, "返回上一页", "[0,150][170,270]", clickable="true", **{"resource-id": f"{PACKAGE}:id/btn_go_back"})
    return Snapshot(ET.tostring(root, encoding="unicode"), PACKAGE, 1200, 2670)


def main_search(completed=False):
    root = ET.Element("hierarchy")
    window = node(root)
    panel = node(window, "五棱镜浮标 - 主搜专用", **{"class": "android.webkit.WebView"})
    node(panel, "任务完成" if completed else "浏览5秒")
    node(panel, "+400")
    return Snapshot(ET.tostring(root, encoding="unicode"), PACKAGE, 1200, 2670)


def web_search(focused=False, value=""):
    root = ET.Element("hierarchy")
    panel = node(root, "淘宝精选", **{"class": "android.webkit.WebView"})
    node(panel, "搜索有福利")
    node(panel, "搜索后浏览立得奖励")
    arrow = node(panel, bounds="[24,117][129,222]", clickable="true")
    node(arrow, "\ue679", "[45,141][105,201]")
    node(panel, value, "[30,273][1170,411]", focused=str(focused).lower(),
         **{"class": "android.widget.EditText"})
    node(panel, "搜索", "[942,282][1161,399]", clickable="true", **{"class": "android.widget.Button"})
    return Snapshot(ET.tostring(root, encoding="unicode"), PACKAGE, 1200, 2670)


def shop_page(timer="42"):
    """A 闪购 activity page: no reward component, and a countdown that keeps changing."""
    root = ET.Element("hierarchy")
    window = node(root)
    for text in ("北京邮电大学", "输入商家、商品名称", f"10 : 21 : {timer}", "更多红包", "奈雪的茶"):
        node(window, text)
    return Snapshot(ET.tostring(root, encoding="unicode"), PACKAGE, 1200, 2670)


def app_store(name="快手", company="北京快手科技有限公司"):
    """Taobao's own app-detail page, as opened by 去快手打卡领手机."""
    root = ET.Element("hierarchy")
    window = node(root)
    for text in (name, "刷新", "分享", company, "版本：14.9.10107.8M2026.10.16",
                 "备案号：京ICP备15023266号-141A", "产品功能", "隐私政策", "权限说明",
                 "下载32位兼容版"):
        node(window, text)
    return Snapshot(ET.tostring(root, encoding="unicode"), PACKAGE, 1200, 2670)


class SearchTests(unittest.TestCase):
    def test_web_search_is_scoped_to_the_observed_activity_markers(self):
        s = web_search()
        self.assertEqual(s.page_kind(), "web_search")
        self.assertEqual(s.search_back().center, (76,169))
        self.assertIsNone(s.search_field(focused=True))
        marker = next(n for n in s.nodes if n.get('text') == '搜索有福利')
        marker.set('text', '其他网页')
        unrecognized = Snapshot(ET.tostring(s.root, encoding="unicode"), PACKAGE, 1200, 2670)
        self.assertIsNone(unrecognized.page_kind())
        self.assertIsNone(unrecognized.search_field())

    def test_web_search_verifies_query_before_submit(self):
        adb = Mock(control=Control())
        adb.snapshot.side_effect = [web_search(),web_search(True),web_search(True,'鼠标')]
        with tempfile.TemporaryDirectory() as folder:
            Runner(adb,directory=Path(folder)).start_search('web_search')
        adb.set_search.assert_called_once()
        self.assertEqual(adb.set_search.call_args.args[0].page_kind(), 'web_search')
        self.assertEqual(adb.input.call_count,2)

    def test_web_search_return_uses_scoped_arrow(self):
        adb=Mock(control=Control())
        adb.snapshot.side_effect=[main_search(True),web_search(),web_search(),page()]
        with tempfile.TemporaryDirectory() as folder:
            runner=Runner(adb,directory=Path(folder));runner.page='main_search';runner.searched=True
            self.assertTrue(runner.return_to_list('搜索任务').is_list)
        self.assertEqual(adb.input.call_args_list[1].args[1:], ('tap','76','169'))

    def test_main_search_widget_is_scoped_and_reward_matched(self):
        task = Task('搜一搜宝贝',0,5,15,'浏览15秒+400',None)
        self.assertEqual(main_search().page_kind(),'main_search')
        self.assertTrue(main_search().task_in_progress(task))
        self.assertTrue(main_search(True).task_completed(task))
        self.assertFalse(main_search().task_completed(task))

    def test_search_focus_password_and_query_guards(self):
        self.assertIsNotNone(search_page().search_field(focused=True))
        self.assertIsNone(search_page(False).search_field(focused=True))
        self.assertIsNone(search_page(password=True).search_field())
        self.assertEqual(search_page().page_kind(),'search')

    def test_search_replaces_text_and_checks_it_before_submit(self):
        adb = Mock(control=Control())
        adb.snapshot.side_effect = [search_page(False),search_page(True),search_page(True,'鼠标')]
        with tempfile.TemporaryDirectory() as folder:
            runner=Runner(adb,directory=Path(folder))
            runner.start_search()
        adb.set_search.assert_called_once()
        self.assertEqual(adb.set_search.call_args.args[1],'鼠标')
        self.assertEqual(adb.input.call_count,2)
        self.assertTrue(all(call.args[1]=='tap' for call in adb.input.call_args_list))

    def test_unfocused_search_sends_no_text(self):
        adb=Mock(control=Control())
        adb.snapshot.side_effect=[search_page(False),search_page(False)]
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(Halt): Runner(adb,directory=Path(folder)).start_search()
        adb.set_search.assert_not_called()
        self.assertEqual(adb.input.call_count,1)

    def test_unconfirmed_query_is_not_submitted(self):
        adb=Mock(control=Control())
        adb.snapshot.side_effect=[search_page(False),search_page(True),search_page(True,'别的文字')]
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(Halt): Runner(adb,directory=Path(folder)).start_search()
        self.assertEqual(adb.input.call_count,1)

    def test_search_return_uses_known_back_button_after_result_page(self):
        adb=Mock(control=Control())
        adb.snapshot.side_effect=[main_search(True),search_page(),search_page(),page()]
        with tempfile.TemporaryDirectory() as folder:
            runner=Runner(adb,directory=Path(folder));runner.page='main_search';runner.searched=True
            self.assertTrue(runner.return_to_list('搜一搜宝贝').is_list)
        self.assertEqual(adb.input.call_count,2)
        self.assertEqual(adb.input.call_args_list[0].args[1:],('keyevent','4'))
        self.assertEqual(adb.input.call_args_list[1].args[1], 'tap')

    def test_search_return_directly_to_list_does_not_press_back_again(self):
        adb=Mock(control=Control());adb.snapshot.side_effect=[main_search(True),page()]
        with tempfile.TemporaryDirectory() as folder:
            runner=Runner(adb,directory=Path(folder));runner.page='main_search';runner.searched=True
            self.assertTrue(runner.return_to_list('搜一搜宝贝').is_list)
        self.assertEqual(adb.input.call_count,1)

    def test_query_control_characters_are_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(Halt):Runner(Mock(control=Control()),directory=Path(folder),search_query='鼠标\n危险')


class BrowsePageTests(unittest.TestCase):
    def setUp(self):
        self.task = Task("好物随心逛", 1, 5, 15, "浏览15秒+400", None)

    def test_feed_must_read_product_before_header_reward_is_confirmed(self):
        self.assertEqual(feed().page_kind(), "feed")
        self.assertTrue(feed().task_in_progress(self.task))
        self.assertFalse(feed().task_completed(self.task))
        self.assertTrue(feed(True).task_completed(self.task))
        self.assertFalse(feed(True).task_in_progress(self.task))

    def test_product_card_excludes_coupon_and_checkout_controls(self):
        self.assertEqual(feed().product_card().center, (305, 775))
        self.assertIsNone(product().product_card())

    def test_web_search_results_share_the_feed_product_flow(self):
        for completed in (False, True):
            s = feed(completed)
            next(n for n in s.nodes if n.get('text') == '体力-固推&自建feeds-interact').set('text', '体力-固搜-interact')
            s = Snapshot(ET.tostring(s.root, encoding="unicode"), PACKAGE, 1200, 2670)
            self.assertEqual(s.page_kind(), 'feed')
            self.assertTrue(s.task_completed(self.task) if completed else s.task_in_progress(self.task))
            self.assertEqual(s.product_card().center, (305,775))

    def test_detail_page_does_not_claim_task_completion(self):
        s = product()
        self.assertEqual(s.page_kind(), "product")
        self.assertFalse(s.task_completed(self.task))

    def test_signin_and_photo_search_use_their_own_duration_reward(self):
        for title, seconds, reward in (("去签到领红包", 5, 600), ("拍立淘逛感兴趣的宝贝", 5, 400)):
            task = Task(title, 0, 1, seconds, f"浏览{seconds}秒+{reward}", None)
            self.assertTrue(immersive(False, seconds, reward).task_in_progress(task))
            self.assertTrue(immersive(True, seconds, reward).task_completed(task))
            wrong = Task(title, 0, 1, seconds, "浏览5秒+999", None)
            self.assertFalse(immersive(True, seconds, reward).task_completed(wrong))

    def test_completed_product_name_outside_reward_component_is_ignored(self):
        s = immersive(False)
        node(s.root, "任务已完成 已得400")
        refreshed = Snapshot(ET.tostring(s.root, encoding="unicode"), PACKAGE, 1200, 2670)
        self.assertFalse(refreshed.task_completed(self.task))

    def test_completed_product_name_outside_feed_header_is_ignored(self):
        s = feed()
        node(s.root, "已得400下个任务")
        refreshed = Snapshot(ET.tostring(s.root, encoding="unicode"), PACKAGE, 1200, 2670)
        self.assertFalse(refreshed.task_completed(self.task))

    def test_hidden_card_never_becomes_target(self):
        s = feed()
        card = next(n for n in s.nodes if n.get("text", "").startswith("商品A"))
        card.set("visible-to-user", "false")
        self.assertIsNone(Snapshot(ET.tostring(s.root, encoding="unicode"), PACKAGE, 1200, 2670).product_card())

    def test_no_duration_means_jump_not_browse(self):
        self.assertEqual(Task("新的活动名称", 0, 1, None, "点击去逛+400", None).kind, "open")
        self.assertFalse(Task("新的活动名称", 0, 1, None, "去答题最高得+1000", None).supported)

    def test_reward_amount_is_exact_at_start_and_completion(self):
        for factory in (immersive, feed, main_search):
            for completed in (False, True):
                original = factory(completed)
                for n in original.nodes:
                    n.set("text", n.get("text", "").replace("400", "4000"))
                s = Snapshot(ET.tostring(original.root, encoding="unicode"), PACKAGE, 1200, 2670)
                self.assertFalse(s.task_in_progress(self.task), factory.__name__)
                self.assertFalse(s.task_completed(self.task), factory.__name__)

    def test_secondary_order_reward_is_not_the_browser_reward(self):
        s = immersive(False)
        panel = next(n for n in s.nodes if n.get('resource-id') == 'ice-container')
        node(panel, '下单得2000当前页下单再得0')
        s = Snapshot(ET.tostring(s.root, encoding="unicode"), PACKAGE, 1200, 2670)
        self.assertTrue(s.task_in_progress(self.task))

    def test_guide_requires_exact_reward_before_reading(self):
        task = Task("攻略", 0, 1, 15, "浏览15秒+1000", None)
        for completed in (False, True):
            root = ET.Element("hierarchy")
            window = node(root)
            node(window, "淘金币双11")
            label = node(window, "已得1000" if completed else "浏览15s可得1000")
            s = Snapshot(ET.tostring(root, encoding="unicode"), PACKAGE, 1200, 2670)
            self.assertTrue(s.task_completed(task) if completed else s.task_in_progress(task))
            label.set("text", label.get("text").replace("1000", "10000"))
            s = Snapshot(ET.tostring(root, encoding="unicode"), PACKAGE, 1200, 2670)
            self.assertFalse(s.task_completed(task))
            self.assertFalse(s.task_in_progress(task))


class CompletionEvidenceTests(unittest.TestCase):
    def test_timer_or_badge_or_global_counter_alone_never_confirms(self):
        self.assertFalse(completed_transition(False, True, 4, 5))
        self.assertFalse(completed_transition(True, False, 4, 5))
        self.assertFalse(completed_transition(True, True, 4, 4))
        self.assertFalse(completed_transition(True, True, 4, 6))
        self.assertFalse(completed_transition(True, True, None, 5))
        self.assertTrue(completed_transition(True, True, 4, 5))

    def test_ambiguous_subtask_count_stays_unattributed(self):
        before = page([("好物沉浸看", 0, 10, 900), ("另一浏览", 0, 10, 1200)], 4)
        after = page([("好物沉浸看", 0, 10, 900), ("新名字", 1, 10, 1200)], 5)
        adb = Mock(control=Control())
        with tempfile.TemporaryDirectory() as folder:
            runner = Runner(adb, directory=Path(folder))
            runner.baseline_tasks = before.tasks()
            runner.tap_task = Mock(return_value=(before.task("好物沉浸看"), 4))
            runner.await_target = Mock(return_value="")
            runner.enter = Mock(side_effect=lambda task: setattr(runner, "started_signal", True))
            runner.browse = Mock(side_effect=lambda task: setattr(runner, "finished_signal", True))
            runner.return_to_list = Mock(return_value=after)
            result = runner.execute_one("好物沉浸看")
            self.assertTrue(result.success)
            self.assertIsNone(result.after)
            self.assertIn("单项次数未归因", result.reason)


class DiscoveryTests(unittest.TestCase):
    @staticmethod
    def finished_region(title, y=900):
        s = page([(title, 1, 1, y)])
        button = next(n for n in s.nodes if n.get("text") == "去完成")
        button.set("enabled", "false")
        return Snapshot(ET.tostring(s.root, encoding="unicode"), PACKAGE, 1200, 2670)

    def test_boundary_fingerprint_includes_disabled_rows_and_their_position(self):
        first = self.finished_region("已完成A")
        second = self.finished_region("已完成B")
        moved = self.finished_region("已完成A", 1000)
        self.assertTrue(first.fingerprint())
        self.assertNotEqual(first.fingerprint(), second.fingerprint())
        self.assertNotEqual(first.fingerprint(), moved.fingerprint())

    def test_discover_crosses_finished_regions_before_finding_pending_task(self):
        a, b, c = (self.finished_region(title) for title in ("区域A", "区域B", "区域C"))
        pending = page([("新增浏览", 0, 5, 900)])
        with tempfile.TemporaryDirectory() as folder:
            runner = Runner(Mock(control=Control()), directory=Path(folder))
            runner.observe = Mock(side_effect=[a, a, b, c, pending])
            runner.list_scroll = Mock()
            self.assertEqual(runner.discover().title, "新增浏览")
            self.assertEqual(runner.observe.call_count, 5)

    def test_unrecognizable_region_cannot_be_reported_as_exhausted(self):
        with tempfile.TemporaryDirectory() as folder:
            runner = Runner(Mock(control=Control()), directory=Path(folder))
            runner.observe = Mock(return_value=page())
            runner.list_scroll = Mock()
            with self.assertRaisesRegex(Halt, "无法确认浏览任务是否耗尽"):
                runner.discover()

    def test_candidates_skip_external_and_nonbrowse_despite_fake_duration(self):
        s = page([("逛一逛支付宝农场", 0, 1, 900), ("积分兑换", 0, 3, 1200), ("看看#新商品", 0, 10, 1500)])
        self.assertEqual(Runner.candidate(s.tasks()).title, "看看#新商品")

    def test_auto_mode_discovers_new_caption_each_round(self):
        first = Task("看看#商品A", 0, 5, 15, "浏览15秒+400", Rect(900,900,1100,1000))
        second = Task("商品B的新任务名", 1, 5, 15, "浏览15秒+400", Rect(900,900,1100,1000))
        adb = Mock(control=Control())
        adb.lease.side_effect = lambda: nullcontext()
        adb.control.wait = Mock()
        adb.foreground.return_value = PACKAGE
        with tempfile.TemporaryDirectory() as folder:
            runner = Runner(adb, directory=Path(folder))
            runner.scan = Mock()
            runner.discover = Mock(side_effect=[first, second, None])
            runner.find = Mock(side_effect=lambda title: (None, first if title == first.title else second, False))
            visited = []
            def execute(title):
                visited.append(title)
                result = Result(title, 0, 1, True, "test")
                runner.results.append(result)
                return result
            runner.execute_one = Mock(side_effect=execute)
            runner.run([], all_browse=True)
            self.assertEqual(visited, [first.title, second.title])
            self.assertEqual(len(json.loads((Path(folder)/'results.json').read_text(encoding='utf-8'))), 2)

    def test_changed_caption_before_tap_is_rediscovered_without_retry_click(self):
        task = Task("好物随心逛", 0, 5, 15, "浏览15秒+400", Rect(900,900,1100,1000))
        adb = Mock(control=Control())
        adb.lease.side_effect = lambda: nullcontext()
        adb.foreground.return_value = PACKAGE
        with tempfile.TemporaryDirectory() as folder:
            runner = Runner(adb, directory=Path(folder))
            runner.scan = Mock()
            runner.discover = Mock(side_effect=[task, None])
            runner.find = Mock(return_value=(None, task, False))
            runner.execute_one = Mock(side_effect=TaskChanged('changed'))
            runner.run([], all_browse=True)
            self.assertEqual(runner.discover.call_count, 2)
            adb.input.assert_not_called()


class JumpTaskTests(unittest.TestCase):
    """A jump task grants its reward on entry; Taobao keeps the only readable evidence."""

    JUMP = Task("逛一逛支付宝农场", 0, 1, None, "点击去逛+600", Rect(0, 0, 10, 10))

    def test_caption_marks_jump_and_keeps_unsupported_tasks_out(self):
        self.assertEqual(self.JUMP.kind, "open")
        self.assertEqual(Task("逛一逛蚂蚁庄园", 0, 1, None, "逛一逛蚂蚁庄园+600", None).kind, "open")
        self.assertEqual(Task("去芝麻信用涨分攒进度", 0, 1, None, "点击去逛+600", None).kind, "open")
        for title, detail in (("天猫积分换能量", "每300天猫积分兑换+1500"),
                              ("下载XX领能量", "点击去逛+600")):
            self.assertFalse(Task(title, 0, 1, None, detail, None).supported)

    def test_return_resumes_taobao_instead_of_counting_back_keys(self):
        adb = Mock(control=Control())
        adb.foreground.return_value = "com.eg.android.AlipayGphone"
        adb.snapshot.return_value = page([("逛一逛支付宝农场", 0, 1, 900)], 47)
        with tempfile.TemporaryDirectory() as folder:
            runner = Runner(adb, directory=Path(folder))
            self.assertTrue(runner.jump_back("逛一逛支付宝农场").is_list)
        adb.launch_taobao.assert_called_once()
        adb.input.assert_not_called()

    def test_back_is_pressed_only_inside_taobao_when_the_launching_page_is_still_on_top(self):
        # Observed on device: 闲鱼 opens through a Taobao webview, so resuming Taobao
        # lands on that webview and one back press returns to the task list.
        adb = Mock(control=Control())
        adb.foreground.return_value = PACKAGE
        webview = Snapshot('<hierarchy><node text="闲鱼币狂欢" bounds="[0,0][100,100]" /></hierarchy>',
                           PACKAGE, 1200, 2670)
        with tempfile.TemporaryDirectory() as folder:
            runner = Runner(adb, directory=Path(folder))
            now = [0.0]
            runner.control.clock = lambda: now[0]
            runner.control.wait = Mock(side_effect=lambda s: now.__setitem__(0, now[0] + s))
            frames = {"seen": 0}
            def observe(external=False):
                frames["seen"] += 1
                # The list only appears once the back press has been sent.
                return page([("去闲鱼签到省钱", 1, 1, 900)], 50) if adb.input.called else webview
            runner.observe = Mock(side_effect=observe)
            self.assertTrue(runner.jump_back("去闲鱼签到省钱").is_list)
        adb.launch_taobao.assert_not_called()
        self.assertEqual(adb.input.call_args_list[0].args[1:], ("keyevent", "4"))

    def test_global_counter_alone_decides_success(self):
        adb = Mock(control=Control())
        with tempfile.TemporaryDirectory() as folder:
            runner = Runner(adb, directory=Path(folder))
            now = [0.0]
            runner.control.clock = lambda: now[0]
            runner.control.wait = Mock(side_effect=lambda s: now.__setitem__(0, now[0] + s))
            runner.tap_task = Mock(return_value=(self.JUMP, 46))
            runner.await_target = Mock(return_value="com.eg.android.AlipayGphone")
            runner.observe = Mock(return_value=page([("逛一逛支付宝农场", 0, 1, 900)], 46))
            runner.jump_back = Mock(return_value=page([("逛一逛支付宝农场", 0, 1, 900)], 46))
            failed = runner.execute_one("逛一逛支付宝农场")
            self.assertFalse(failed.success)
            self.assertIn("总进度", failed.reason)
            self.assertEqual(runner.results, [])
            runner.jump_back = Mock(return_value=page([("逛一逛支付宝农场", 1, 1, 900)], 47))
            self.assertTrue(runner.execute_one("逛一逛支付宝农场").success)

    def test_daily_cap_does_not_accept_a_renamed_or_missing_row(self):
        # A missing caption can be caused by rotation; it is not completion evidence.
        adb = Mock(control=Control())
        with tempfile.TemporaryDirectory() as folder:
            runner = Runner(adb, directory=Path(folder))
            now = [0.0]
            runner.control.clock = lambda: now[0]
            runner.control.wait = Mock(side_effect=lambda s: now.__setitem__(0, now[0] + s))
            runner.tap_task = Mock(return_value=(self.JUMP, 50))
            runner.await_target = Mock(return_value="com.eg.android.AlipayGphone")
            runner.observe = Mock(return_value=page([("逛一逛支付宝农场", 0, 1, 900)], 50))
            runner.jump_back = Mock(return_value=page([("其他新任务", 0, 1, 900)], 50))
            result = runner.execute_one("逛一逛支付宝农场")
        self.assertFalse(result.success)

    def test_daily_cap_never_accepts_an_untouched_row(self):
        adb = Mock(control=Control())
        with tempfile.TemporaryDirectory() as folder:
            runner = Runner(adb, directory=Path(folder))
            now = [0.0]
            runner.control.clock = lambda: now[0]
            runner.control.wait = Mock(side_effect=lambda s: now.__setitem__(0, now[0] + s))
            runner.tap_task = Mock(return_value=(self.JUMP, 50))
            runner.await_target = Mock(return_value="com.eg.android.AlipayGphone")
            runner.observe = Mock(return_value=page([("逛一逛支付宝农场", 0, 1, 900)], 50))
            runner.jump_back = Mock(return_value=page([("逛一逛支付宝农场", 0, 1, 900)], 50))
            self.assertFalse(runner.execute_one("逛一逛支付宝农场").success)

    def test_failed_task_is_retried_once_then_left_alone(self):
        adb = Mock(control=Control())
        adb.lease.side_effect = lambda: nullcontext()
        adb.control.wait = Mock()
        adb.foreground.return_value = PACKAGE
        with tempfile.TemporaryDirectory() as folder:
            runner = Runner(adb, directory=Path(folder))
            runner.scan = Mock()
            runner.discover = Mock(side_effect=[self.JUMP, self.JUMP, None])
            runner.find = Mock(return_value=(page([("逛一逛支付宝农场", 0, 1, 900)]), self.JUMP, False))
            runner.execute_one = Mock(return_value=Result(self.JUMP.title, 0, None, False, "no"))
            runner.run([], all_browse=True)
        self.assertEqual(runner.execute_one.call_count, 2)
        self.assertIn(self.JUMP.title, runner.skip)

    def test_unusable_task_does_not_end_the_session(self):
        adb = Mock(control=Control())
        adb.lease.side_effect = lambda: nullcontext()
        adb.control.wait = Mock()
        adb.foreground.return_value = PACKAGE
        other = Task("看看#新商品", 0, 10, 15, "浏览15秒+400", Rect(900,900,1100,1000))
        with tempfile.TemporaryDirectory() as folder:
            runner = Runner(adb, directory=Path(folder))
            runner.scan = Mock()
            runner.discover = Mock(side_effect=[self.JUMP, other, None])
            runner.find = Mock(side_effect=lambda title: (None, self.JUMP if title == self.JUMP.title else other, False))
            runner.observe = Mock(return_value=page([(other.title, 0, 10, 900)], 50))
            runner.execute_one = Mock(side_effect=[TaskFailed("点击后仍停留在任务列表"), Result(other.title, 0, 1, True, "ok")])
            runner.run([], all_browse=True)
        self.assertEqual(runner.execute_one.call_count, 2)
        self.assertEqual(runner.execute_one.call_args_list[1].args[0], other.title)
        self.assertEqual(runner.failures.get(self.JUMP.title), 1)


    def test_external_switch_after_the_tap_is_finished_as_a_jump(self):
        # The real 闲鱼 run stopped here: the caption looked in-app, then the app switched.
        adb = Mock(control=Control())
        external = Snapshot('<hierarchy><node text="闲鱼" bounds="[0,0][100,100]" /></hierarchy>',
                            "com.taobao.idlefish", 1200, 2670)
        with tempfile.TemporaryDirectory() as folder:
            runner = Runner(adb, directory=Path(folder))
            now = [0.0]
            runner.control.clock = lambda: now[0]
            runner.control.wait = Mock(side_effect=lambda s: now.__setitem__(0, now[0] + s))
            runner.tap_task = Mock(return_value=(self.JUMP, 46))
            runner.await_target = Mock(return_value="")
            runner.observe = Mock(return_value=external)
            runner.jump_back = Mock(return_value=page([("其他新任务", 0, 1, 900)], 47))
            result = runner.execute_one("逛一逛支付宝农场")
        self.assertTrue(result.success)
        self.assertIn("com.taobao.idlefish", result.reason)

    def test_generic_reward_panel_is_recognised_on_an_unknown_activity_page(self):
        root = ET.Element("hierarchy")
        window = node(root)
        node(window, "淘宝闪购")
        panel = node(window, "", "[120,1900][1080,2260]")
        node(panel, "浏览15秒", "[700,1950][900,2010]")
        node(panel, "得400", "[700,2010][900,2070]")
        s = Snapshot(ET.tostring(root, encoding="unicode"), PACKAGE, 1200, 2670)
        self.assertEqual(s.page_kind(), "overlay")
        self.assertTrue(s.task_in_progress(Task("逛逛闪购", 0, 5, 15, "浏览15秒+400", None)))
        self.assertFalse(s.task_completed(Task("逛逛闪购", 0, 5, 15, "浏览15秒+400", None)))

    def test_generic_panel_never_hijacks_the_feed_header(self):
        self.assertEqual(feed().page_kind(), "feed")
        self.assertEqual(feed(True).page_kind(), "feed")

    def test_back_never_relaunches_taobao_from_inside_taobao(self):
        adb = Mock(control=Control())
        unknown = Snapshot('<hierarchy><node text="车库页" bounds="[0,0][100,100]" /></hierarchy>',
                           PACKAGE, 1200, 2670)
        with tempfile.TemporaryDirectory() as folder:
            runner = Runner(adb, directory=Path(folder))
            now = [0.0]
            runner.control.clock = lambda: now[0]
            runner.control.wait = Mock(side_effect=lambda s: now.__setitem__(0, now[0] + s))
            runner.observe = Mock(return_value=unknown)
            runner.action = Mock()
            with self.assertRaises(Halt):
                runner.back("overlay", "list", "逛逛活动页")
        adb.launch_taobao.assert_not_called()

    def test_resume_taobao_is_skipped_when_taobao_is_already_in_front(self):
        adb = Mock(control=Control())
        adb.foreground.return_value = PACKAGE
        with tempfile.TemporaryDirectory() as folder:
            Runner(adb, directory=Path(folder)).resume_taobao()
        adb.launch_taobao.assert_not_called()

    def test_activity_page_without_reward_component_is_browsed_by_timer(self):
        adb = Mock(control=Control())
        garage = Snapshot('<hierarchy><node text="车库页" bounds="[0,0][1200,2600]" /></hierarchy>',
                          PACKAGE, 1200, 2670)
        task = Task("去省钱卡领红包", 0, 1, 5, "浏览5秒+600", None)
        with tempfile.TemporaryDirectory() as folder:
            runner = Runner(adb, directory=Path(folder))
            now = [0.0]
            runner.control.clock = lambda: now[0]
            runner.control.wait = Mock(side_effect=lambda s: now.__setitem__(0, now[0] + s))
            runner.observe = Mock(return_value=garage)
            runner.tap_task = Mock(return_value=(task, 49))
            runner.back = Mock(return_value=page([("看看#依视路", 10, 10, 900)], 50))
            self.assertTrue(runner.execute_one("去省钱卡领红包").success)
            self.assertEqual(runner.page, "timed")

    def test_activity_page_with_a_countdown_is_still_browsed_by_timer(self):
        # The 闪购 page changes every second; identical frames can never be required.
        adb = Mock(control=Control())
        task = Task("领闪购红包下单直接省", 0, 1, None, "点击去逛+600", None)
        with tempfile.TemporaryDirectory() as folder:
            runner = Runner(adb, directory=Path(folder))
            now = [0.0]
            runner.control.clock = lambda: now[0]
            runner.control.wait = Mock(side_effect=lambda s: now.__setitem__(0, now[0] + s))
            runner.observe = Mock(side_effect=[shop_page("42"), shop_page("22"), shop_page("02")])
            runner.enter(task)
        self.assertEqual(runner.page, "timed")

    def test_download_page_is_skipped_permanently(self):
        adb = Mock(control=Control())
        task = Task("去快手打卡领手机", 0, 1, None, "点击去逛+600", None)
        with tempfile.TemporaryDirectory() as folder:
            runner = Runner(adb, directory=Path(folder))
            runner.observe = Mock(return_value=app_store())
            with self.assertRaises(TaskFailed) as caught:
                runner.enter(task)
        self.assertTrue(caught.exception.permanent)

    def test_app_store_page_needs_all_of_its_markers(self):
        self.assertTrue(app_store().app_store_page())
        self.assertFalse(feed().app_store_page())
        self.assertFalse(immersive().app_store_page())


class InstalledAppTests(unittest.TestCase):
    """Taobao requires many apps; a task whose app is absent only opens a download page."""

    def tearDown(self):
        model.set_installed(None)

    def test_task_is_skipped_only_when_its_app_cannot_be_opened(self):
        model.set_installed(["com.smile.gifmaker", "com.baidu.searchbox"])
        for title in ("去快手打卡领手机", "去百度App领现金"):
            self.assertEqual(Task(title, 0, 1, None, "点击去逛+600", None).kind, "open")
        lite = Task("去百度极速版领现金", 0, 1, None, "去百度极速版领现金+600", None)
        self.assertFalse(lite.supported)
        self.assertIn("百度极速版", lite.skip_reason)

    def test_missing_app_is_named_in_the_skip_reason(self):
        model.set_installed([])
        task = Task("去快手打卡领手机", 0, 1, None, "点击去逛+600", None)
        self.assertFalse(task.supported)
        self.assertIn("快手", task.skip_reason)

    def test_installed_baidu_lite_is_enabled_without_requiring_regular_baidu(self):
        model.set_installed(["com.baidu.searchbox.lite"])
        task = Task("去百度极速版领现金", 0, 1, None, "去百度极速版领现金+600", None)
        self.assertEqual(task.kind, "open")
        self.assertEqual(task.skip_reason, "")
        self.assertEqual(model.missing_app("去百度App领现金"), "百度")

    def test_additional_observed_apps_use_their_own_installed_packages(self):
        for name, package in (("点淘", "com.taobao.live"), ("一淘", "com.taobao.etao"),
                              ("支付宝", "com.eg.android.AlipayGphone")):
            with self.subTest(name=name):
                model.set_installed([package])
                task = Task(f"去{name}签到领红包", 0, 1, None, "点击去逛+600", None)
                self.assertEqual(task.kind, "open")
                model.set_installed([])
                self.assertFalse(task.supported)
                self.assertIn(name, task.skip_reason)

    def test_unknown_device_state_never_blocks_a_task(self):
        model.set_installed(None)
        self.assertEqual(Task("去快手打卡领手机", 0, 1, None, "点击去逛+600", None).kind, "open")


class RecoveryTests(unittest.TestCase):
    def test_foreground_stolen_between_tasks_is_recovered_without_stopping(self):
        # Observed after two jumps to 百度: it resumed itself during the reading pause.
        adb = Mock(control=Control())
        adb.foreground.side_effect = ["com.baidu.searchbox", PACKAGE]
        with tempfile.TemporaryDirectory() as folder:
            runner = Runner(adb, directory=Path(folder))
            runner.jump_back = Mock(return_value=page([], 50))
            runner.ensure_front()
        runner.jump_back.assert_called_once()

    def test_nothing_is_launched_while_taobao_is_already_in_front(self):
        adb = Mock(control=Control())
        adb.foreground.return_value = PACKAGE
        with tempfile.TemporaryDirectory() as folder:
            runner = Runner(adb, directory=Path(folder))
            runner.jump_back = Mock()
            runner.ensure_front()
        runner.jump_back.assert_not_called()


if __name__ == "__main__":
    unittest.main()
