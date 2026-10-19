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


if __name__ == '__main__':
    unittest.main()
