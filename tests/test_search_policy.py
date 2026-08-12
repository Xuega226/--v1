import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent import Agent, requires_web_search


def _chunk(*, content=None, tool_calls=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def _web_search_call():
    function = SimpleNamespace(
        name="web_search",
        arguments='{"query":"今日新闻","count":5}',
    )
    return SimpleNamespace(
        index=0,
        type="function",
        id="call-search-1",
        function=function,
    )


class SearchPolicyTests(unittest.TestCase):
    def test_explicit_search_request_requires_web_search(self):
        self.assertTrue(requires_web_search("【主人】说：帮我搜索一下今日新闻"))
        self.assertTrue(requires_web_search("【主人】说：搜索 OpenAI"))

    def test_fresh_information_requires_web_search(self):
        self.assertTrue(requires_web_search("【主人】说：现在谁是美国总统？"))
        self.assertTrue(requires_web_search("【主人】说：最新的模型版本是什么？"))

    def test_stable_question_keeps_autonomous_tool_choice(self):
        self.assertFalse(requires_web_search("【主人】说：水的化学式是什么？"))
        self.assertFalse(requires_web_search("【群友】说：我搜索房间里的线索"))

    def test_required_search_is_forced_only_until_it_is_attempted(self):
        calls = []

        def fake_chat_completion(messages, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return iter([_chunk(tool_calls=[_web_search_call()])])
            return iter([_chunk(content="查到结果了喵")])

        agent = Agent(safe_mode=True)
        with (
            patch("agent.chat_completion", side_effect=fake_chat_completion),
            patch.object(
                agent,
                "_execute_tool",
                return_value="[搜索结果] 关键词：今日新闻",
            ),
        ):
            events = list(
                agent.run(
                    "【当前要回应的消息】\n【主人】说：帮我搜索一下今日新闻",
                    history_input="【主人】说：帮我搜索一下今日新闻",
                )
            )

        self.assertEqual(
            calls[0]["tool_choice"],
            {"type": "function", "function": {"name": "web_search"}},
        )
        self.assertIsNone(calls[1]["tool_choice"])
        self.assertTrue(
            any(
                event.get("type") == "tool_call"
                and event.get("name") == "web_search"
                for event in events
            )
        )

    def test_stable_question_does_not_force_a_tool(self):
        calls = []

        def fake_chat_completion(messages, **kwargs):
            calls.append(kwargs)
            return iter([_chunk(content="H₂O 喵")])

        agent = Agent(safe_mode=True)
        with patch("agent.chat_completion", side_effect=fake_chat_completion):
            list(agent.run("【主人】说：水的化学式是什么？"))

        self.assertIsNone(calls[0]["tool_choice"])


if __name__ == "__main__":
    unittest.main()
