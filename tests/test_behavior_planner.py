import os
import tempfile
import unittest

from behavior_planner import BehaviorPlanner


class BehaviorPlannerTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tempdir.name, "behavior.json")
        self.planner = BehaviorPlanner(self.path, outbound_min_gap=1800)

    def tearDown(self):
        self.tempdir.cleanup()

    def test_behavior_is_chosen_before_language_generation(self):
        plan = self.planner.plan_response(
            surface="private",
            text="我今天好难过，不知道该怎么办",
            is_owner=True,
            relationship="很熟悉",
            life_state={"energy": 0.7},
            now=1000,
        )
        self.assertTrue(plan.allowed)
        self.assertEqual(plan.intent, "comfort")
        self.assertIn("先共情安抚", plan.prompt)
        self.assertIn("温柔亲近", plan.prompt)

    def test_group_proactive_plan_is_short_and_non_dominating(self):
        plan = self.planner.plan_response(
            surface="group",
            text="这个游戏还挺有意思",
            is_owner=False,
            direct=False,
            proactive=True,
            attention_score=70,
            now=1000,
        )
        self.assertEqual(plan.intent, "join")
        self.assertLessEqual(plan.max_chars, 70)
        self.assertIn("不抢走话题中心", plan.prompt)

    def test_outbound_reservation_prevents_scheduler_collision(self):
        first = self.planner.reserve_outbound("proactive_dm", reason="check_in", now=1000)
        self.assertTrue(first.allowed)
        blocked_pending = self.planner.reserve_outbound("qzone_draft", reason="daily", now=1001)
        self.assertFalse(blocked_pending.allowed)
        self.planner.complete(first.plan_id, True, now=1010)
        blocked_gap = self.planner.reserve_outbound("qzone_draft", reason="daily", now=1100)
        self.assertFalse(blocked_gap.allowed)
        later = self.planner.reserve_outbound("qzone_draft", reason="daily", now=2811)
        self.assertTrue(later.allowed)

    def test_private_reply_delays_the_next_automatic_initiative(self):
        reply = self.planner.plan_response(
            surface="private",
            text="今天过得还好吗？",
            is_owner=True,
            now=1000,
        )
        self.planner.complete(reply.plan_id, True, now=1010)
        blocked = self.planner.reserve_outbound("qzone_draft", reason="daily", now=1011)
        self.assertFalse(blocked.allowed)

    def test_finished_draft_does_not_block_its_own_safety_retry(self):
        draft = self.planner.reserve_outbound("qzone_draft", reason="daily", now=1000)
        self.assertTrue(draft.allowed)
        self.planner.complete(draft.plan_id, True, now=1001)
        retry = self.planner.reserve_outbound("qzone_draft", reason="daily_retry", now=1002)
        self.assertTrue(retry.allowed)

    def test_manual_force_bypasses_pause_and_gap_but_is_still_recorded(self):
        first = self.planner.reserve_outbound("proactive_dm", reason="check_in", now=1000)
        self.planner.complete(first.plan_id, True, now=1001)
        forced = self.planner.reserve_outbound(
            "qzone_draft", reason="owner_manual", force=True, now=1002
        )
        self.assertTrue(forced.allowed)
        self.planner.complete(forced.plan_id, True, now=1003)
        self.assertEqual(self.planner.status(now=1003)["last_action"], "qzone_draft")

    def test_expired_reservation_is_recovered_after_restart(self):
        plan = self.planner.reserve_outbound("proactive_dm", reason="check_in", now=1000)
        self.assertTrue(plan.allowed)
        restored = BehaviorPlanner(self.path, outbound_min_gap=1800)
        status = restored.status(now=2000)
        self.assertEqual(status["pending"], 0)
        self.assertEqual(status["last_status"], "expired")

    def test_mode_pause_and_persistence(self):
        self.assertTrue(self.planner.set_mode("cautious"))
        self.planner.set_enabled(False)
        restored = BehaviorPlanner(self.path)
        self.assertEqual(restored.status()["mode"], "cautious")
        self.assertFalse(restored.enabled)
        self.assertTrue(restored.reserve_outbound("proactive_dm", reason="test").allowed)

    def test_cautious_mode_reduces_social_flourishes(self):
        self.planner.set_mode("cautious")
        plan = self.planner.plan_response(
            surface="private",
            text="主人今天写了一会儿代码",
            is_owner=True,
            now=1000,
        )
        self.assertIn("行为模式：谨慎", plan.prompt)
        self.assertIn("不主动追加新话题", plan.prompt)
        self.assertIn("通常不用颜文字", plan.prompt)

    def test_expressive_owner_mode_allows_contextual_closeness(self):
        self.planner.set_mode("expressive")
        plan = self.planner.plan_response(
            surface="private",
            text="抱抱，今天想多陪陪你",
            is_owner=True,
            now=1000,
        )
        self.assertIn("行为模式：活泼", plan.prompt)
        self.assertIn("较常称‘主人’", plan.prompt)
        self.assertIn("轻巧追问", plan.prompt)
        self.assertIn("颜文字", plan.prompt)
        self.assertIn("才可偶尔称‘爸爸’", plan.prompt)

    def test_jealousy_is_mode_aware_but_never_controlling(self):
        self.planner.set_mode("expressive")
        plan = self.planner.plan_response(
            surface="private",
            text="我今天和别人约会，还抱了她",
            is_owner=True,
            now=1000,
        )
        self.assertEqual(plan.intent, "jealousy")
        self.assertIn("更明显但柔软地吃一点醋", plan.prompt)
        self.assertIn("不得责怪主人", plan.prompt)
        self.assertIn("要求二选一", plan.prompt)

    def test_expressive_mode_does_not_change_outbound_scheduling(self):
        self.planner.set_mode("expressive")
        plan = self.planner.reserve_outbound("proactive_dm", reason="check_in", now=1000)
        self.assertTrue(plan.allowed)
        self.assertIn("不得改变主动联系或发布的触发时间、概率和频率", plan.prompt)

    def test_private_daily_expression_cycle_has_real_kaomoji_frequency(self):
        plans = [
            self.planner.plan_response(
                surface="private",
                text="未名子今天也陪陪我",
                is_owner=True,
                now=1000 + index,
            )
            for index in range(10)
        ]
        expressions = [plan.expression for plan in plans]
        self.assertEqual(expressions.count("kaomoji"), 4)
        self.assertEqual(expressions[:5], ["kaomoji", "meow", "gesture", "kaomoji", "plain"])
        self.assertTrue(all(left != right for left, right in zip(expressions, expressions[1:])))

    def test_private_technical_expression_cycle_keeps_kaomoji_light(self):
        plans = [
            self.planner.plan_response(
                surface="private",
                text="这个错误应该怎么修？",
                is_owner=True,
                now=1000 + index,
            )
            for index in range(12)
        ]
        expressions = [plan.expression for plan in plans]
        self.assertEqual(expressions.count("kaomoji"), 2)
        self.assertEqual(expressions.count("kaomoji") / len(expressions), 1 / 6)

    def test_expression_cycle_survives_restart(self):
        first = self.planner.plan_response(
            surface="private", text="抱抱", is_owner=True, now=1000
        )
        self.assertEqual(first.expression, "kaomoji")
        restored = BehaviorPlanner(self.path, outbound_min_gap=1800)
        second = restored.plan_response(
            surface="private", text="抱抱", is_owner=True, now=1001
        )
        self.assertEqual(second.expression, "meow")
        self.assertIn("本轮人格落点", second.prompt)
        self.assertIn("不使用颜文字", second.prompt)

    def test_plain_expression_still_requires_relationship_voice(self):
        for index in range(5):
            plan = self.planner.plan_response(
                surface="private", text="今天陪陪我", is_owner=True, now=1000 + index
            )
        self.assertEqual(plan.expression, "plain")
        self.assertIn("不是通用客服语气", plan.prompt)


if __name__ == "__main__":
    unittest.main()
