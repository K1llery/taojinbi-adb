import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from unittest.mock import Mock

from taojinbi.adb import Control
from taojinbi.model import PACKAGE, Halt, RefreshRequired, Snapshot, Task
from taojinbi.quiz import QUIZ_TITLE, Option, QuizPage, answer, choose_option, run_quiz
from taojinbi.runner import Runner
from tests.test_core import node
from tests.test_jump_recovery import coin_home


def quiz(options=('短', '普通', '一般', '这是很长的答案'), selected=False,
         feedback=False, finished=False, question='1. 测试问题？单选题'):
    root = ET.Element('hierarchy')
    web = node(root, '淘金币趣味答题', **{'class': 'android.webkit.WebView'})
    node(web, '淘金币答题')
    node(web, question)
    for i, (letter, text) in enumerate(zip('ABCD', options)):
        row = node(web, '', f'[36,{879 + i * 207}][1164,{1059 + i * 207}]', clickable='true')
        node(row, letter)
        node(row, text)
    if feedback:
        node(web, '正确答案：B')
        node(web, '你的答案：D')
    node(web, '已成功领取奖励 回到主页' if finished else '我选好了',
         '[36,2418][1164,2598]', clickable='true', enabled='true' if selected or finished else 'false',
         **{'class': 'android.widget.Button'})
    return Snapshot(ET.tostring(root, encoding='unicode'), PACKAGE, 1200, 2670)


class QuizTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.adb = Mock(control=Control())
        self.runner = Runner(self.adb, directory=Path(directory.name))
        self.now = 0
        self.runner.control.clock = lambda: self.now
        self.runner.control.wait = Mock(side_effect=self.wait)
        self.task = Task(QUIZ_TITLE, 0, 1, None, '去答题最高得+1000', None)

    def wait(self, seconds):
        self.now += seconds

    def choose(self, strings):
        return choose_option(QuizPage(quiz(options=strings)).options).letter

    def test_three_short_one_long_chooses_longest(self):
        self.assertEqual(self.choose(('短', '普通', '一般', '这是很长的答案')), 'D')

    def test_three_long_one_short_chooses_shortest(self):
        self.assertEqual(self.choose(('内容比较长的选项', '短', '另一段比较长的内容', '同样很长的一段内容')), 'B')

    def test_ties_use_first_longest_and_ignore_whitespace(self):
        self.assertEqual(self.choose(('甲乙', '丙丁', '戊己', '庚辛')), 'A')
        self.assertEqual(self.choose(('甲 乙', '丙丁', '戊己', '庚辛')), 'A')
        self.assertEqual(self.choose(('121.07', '242.13', '48.43', '2421.30')), 'D')

    def test_only_the_observed_quiz_task_is_enabled(self):
        self.assertEqual(self.task.kind, 'quiz')
        self.assertFalse(Task('新的答题活动', 0, 1, None, '去答题最高得+1000', None).supported)
        self.assertFalse(Task(QUIZ_TITLE, 0, 1, None, '邀请好友答题+1000', None).supported)

    def test_result_requires_feedback_and_the_exact_success_button(self):
        self.assertIsNotNone(QuizPage(quiz(feedback=True, finished=True)).finish)
        self.assertIsNone(QuizPage(quiz(finished=True)).finish)
        self.assertIsNone(QuizPage(quiz(feedback=True)).finish)

    def test_shopping_page_or_multiple_choice_is_rejected(self):
        s = quiz()
        next(n for n in s.nodes if n.get('text') == '淘金币趣味答题').set('text', '购物活动')
        with self.assertRaises(Halt):
            QuizPage(Snapshot(ET.tostring(s.root, encoding='unicode'), PACKAGE, 1200, 2670))
        with self.assertRaises(Halt):
            QuizPage(quiz(question='1. 测试问题？多选题'))

    def test_ambiguous_options_are_rejected(self):
        s = quiz()
        web = next(n for n in s.nodes if n.get('class') == 'android.webkit.WebView')
        node(web, 'A')
        with self.assertRaises(Halt):
            QuizPage(Snapshot(ET.tostring(s.root, encoding='unicode'), PACKAGE, 1200, 2670))

    def test_answer_selects_option_before_submitting(self):
        self.adb.snapshot.side_effect = [quiz(), quiz(selected=True)]
        answer(self.runner, QuizPage(quiz()))
        self.assertEqual(self.adb.input.call_count, 2)
        self.assertEqual(self.adb.input.call_args_list[0].args[1:], ('tap', '600', '1590'))
        self.assertEqual(self.adb.input.call_args_list[1].args[1:], ('tap', '600', '2508'))

    def test_changed_question_after_selection_is_not_submitted(self):
        self.adb.snapshot.side_effect = [quiz(), quiz(selected=True, question='2. 另一个问题？单选题')]
        with self.assertRaises(Halt):
            answer(self.runner, QuizPage(quiz()))
        self.assertEqual(self.adb.input.call_count, 1)

    def test_pause_before_submission_reselects_instead_of_using_old_state(self):
        self.adb.snapshot.side_effect = [quiz(), quiz(selected=True), quiz(), quiz(selected=True)]
        self.adb.input.side_effect = [None, RefreshRequired(), None, None]
        answer(self.runner, QuizPage(quiz()))
        self.assertEqual(self.adb.input.call_count, 4)
        self.assertEqual(self.adb.input.call_args_list[2].args[1:], ('tap', '600', '1590'))

    def test_disabled_submit_is_not_clicked(self):
        self.adb.snapshot.side_effect = [quiz(), quiz()]
        with self.assertRaises(Halt):
            answer(self.runner, QuizPage(quiz()))
        self.assertEqual(self.adb.input.call_count, 1)

    def test_wrong_answer_with_real_reward_is_logged_as_wrong_and_complete(self):
        self.adb.snapshot.side_effect = [quiz(), quiz(), quiz(selected=True), quiz(feedback=True, finished=True)]
        self.runner.baseline_energy = 100000
        after = coin_home(list_page=True, energy=100200)
        self.runner.jump_back = Mock(return_value=after)
        self.runner.refresh_coin_list = Mock(return_value=after)
        result = run_quiz(self.runner, self.task, 50)
        self.assertTrue(result.success)
        self.assertIn('答案错误', result.reason)
        self.assertIn('实际增加200能量', result.reason)
        self.assertEqual(self.adb.input.call_count, 3)

    def test_result_button_without_reward_increase_does_not_confirm_completion(self):
        self.adb.snapshot.return_value = quiz(feedback=True, finished=True)
        self.runner.baseline_energy = 100000
        after = coin_home(list_page=True, energy=100000)
        self.runner.jump_back = Mock(return_value=after)
        self.runner.refresh_coin_list = Mock(return_value=after)
        self.assertFalse(run_quiz(self.runner, self.task, 50).success)

    def test_pause_invalidates_partial_reward_evidence(self):
        self.adb.snapshot.return_value = quiz(feedback=True, finished=True)
        self.runner.baseline_energy = 100000
        after = coin_home(list_page=True, energy=100200)
        after.epoch = 1
        self.runner.jump_back = Mock(return_value=after)
        self.runner.refresh_coin_list = Mock(return_value=after)
        self.assertFalse(run_quiz(self.runner, self.task, 50).success)

    def test_loading_page_is_waited_for_without_clicking(self):
        loading = Snapshot('<hierarchy/>', PACKAGE, 1200, 2670)
        self.adb.snapshot.side_effect = [loading, quiz(feedback=True, finished=True)]
        self.runner.baseline_energy = 100000
        after = coin_home(list_page=True, energy=100200)
        self.runner.jump_back = Mock(return_value=after)
        self.runner.refresh_coin_list = Mock(return_value=after)
        self.assertTrue(run_quiz(self.runner, self.task, 50).success)
        self.assertEqual(self.adb.input.call_count, 1)

    def test_feedback_delay_does_not_submit_the_same_question_again(self):
        self.adb.snapshot.side_effect = [quiz(), quiz(), quiz(selected=True),
                                        quiz(feedback=True), quiz(feedback=True, finished=True)]
        self.runner.baseline_energy = 100000
        after = coin_home(list_page=True, energy=100200)
        self.runner.jump_back = Mock(return_value=after)
        self.runner.refresh_coin_list = Mock(return_value=after)
        self.assertTrue(run_quiz(self.runner, self.task, 50).success)
        self.assertEqual(self.adb.input.call_count, 3)

    def test_a_different_question_after_submission_is_not_answered(self):
        self.adb.snapshot.side_effect = [quiz(), quiz(), quiz(selected=True),
                                        quiz(question='2. 另一场考试？单选题')]
        with self.assertRaisesRegex(Halt, '题目或选项已变化'):
            run_quiz(self.runner, self.task, 50)
        self.assertEqual(self.adb.input.call_count, 2)

    def test_three_option_question_is_parsed_and_uses_the_same_length_rule(self):
        page = QuizPage(quiz(options=('六个字的选项', '更长一些的选项', '另一个短选项')))
        self.assertEqual([o.letter for o in page.options], ['A', 'B', 'C'])
        self.assertEqual(choose_option(page.options).letter, 'B')
        self.assertEqual(self.choose(('非常长的一条选项', '短', '同样很长的一条选项')), 'B')

    def test_two_options_choose_the_first_longest(self):
        self.assertEqual(self.choose(('短', '长一点的选项')), 'B')
        self.assertEqual(self.choose(('同长', '一样')), 'A')

    def test_three_options_can_select_and_submit_without_a_d_marker(self):
        strings = ('普通答案', '明显更长的一条答案', '其他答案')
        self.adb.snapshot.side_effect = [quiz(options=strings), quiz(options=strings, selected=True)]
        answer(self.runner, QuizPage(quiz(options=strings)))
        self.assertEqual(self.adb.input.call_count, 2)
        self.assertEqual(self.adb.input.call_args_list[0].args[1:], ('tap', '600', '1176'))

    def test_hidden_fourth_option_cannot_turn_a_four_option_question_into_three(self):
        for attributes in ({'visible-to-user': 'false'}, {'bounds': '[81,2700][123,2778]'}):
            with self.subTest(attributes=attributes):
                s = quiz()
                next(n for n in s.nodes if n.get('text') == 'D').attrib.update(attributes)
                with self.assertRaisesRegex(Halt, '未完整显示'):
                    QuizPage(Snapshot(ET.tostring(s.root, encoding='unicode'), PACKAGE, 1200, 2670))

    def test_missing_middle_option_is_not_accepted_as_a_three_option_question(self):
        s = quiz()
        n = next(n for n in s.nodes if n.get('text') == 'B')
        s.parents[n].remove(n)
        with self.assertRaisesRegex(Halt, '不完整'):
            QuizPage(Snapshot(ET.tostring(s.root, encoding='unicode'), PACKAGE, 1200, 2670))

    def test_option_count_change_after_selection_is_not_submitted(self):
        self.adb.snapshot.side_effect = [quiz(), quiz(options=('短', '普通', '一般'), selected=True)]
        with self.assertRaisesRegex(Halt, '题目或答题状态已变化'):
            answer(self.runner, QuizPage(quiz()))
        self.assertEqual(self.adb.input.call_count, 1)

    def test_selection_and_feedback_images_do_not_change_option_text_or_identity(self):
        initial = quiz(options=('普通答案', '比较长的一条答案', '其他答案'))
        for label in ('selected', '正确', '错误'):
            with self.subTest(label=label):
                s = quiz(options=('普通答案', '比较长的一条答案', '其他答案'), selected=True)
                row = s.parents[next(n for n in s.nodes if n.get('text') == 'B')]
                node(row, label, '[1056,1134][1146,1221]', **{'class': 'android.widget.Image'})
                parsed = QuizPage(Snapshot(ET.tostring(s.root, encoding='unicode'), PACKAGE, 1200, 2670))
                self.assertEqual(parsed.signature, QuizPage(initial).signature)
                self.assertEqual(choose_option(parsed.options).letter, 'B')

    def test_extra_text_content_is_still_rejected(self):
        s = quiz()
        row = s.parents[next(n for n in s.nodes if n.get('text') == 'B')]
        node(row, '另一段真正的选项文字')
        with self.assertRaisesRegex(Halt, '内容有歧义'):
            QuizPage(Snapshot(ET.tostring(s.root, encoding='unicode'), PACKAGE, 1200, 2670))

    def test_five_option_question_is_not_silently_truncated(self):
        s = quiz()
        panel = next(n for n in s.nodes if n.get('class') == 'android.webkit.WebView')
        row = node(panel, '', '[36,1800][1164,1977]', clickable='true')
        node(row, 'E')
        node(row, '第五个选项')
        with self.assertRaisesRegex(Halt, '不完整'):
            QuizPage(Snapshot(ET.tostring(s.root, encoding='unicode'), PACKAGE, 1200, 2670))


if __name__ == '__main__':
    unittest.main()
