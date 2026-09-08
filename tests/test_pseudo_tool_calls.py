"""Regression: a tool call the model typed out is not an answer.

2026-09-08, «найди в интернете какие новости» → the admin got this and nothing else:

    <tool_call>curl -s "https://html.duckduckgo.com/html/?q=главные+новости+сегодня" …
    </arg_value>
    </tool_call>

The model had no tool schema in the request at all — a text model's capabilities row
never contains "tools", so `use_tools` was False — while the system prompt told it it
was an admin with full internet access who should reach for curl. It did as it was told
the only way left to it. Probed on the box the same hour, three more live models did
their own version of it, so the shapes below are measured, not imagined.

Stdlib only, like the rest of the project.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest import mock

_BOT_DIR = Path(__file__).resolve().parent.parent / "bot"
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

from agent import tool_calls  # noqa: E402

_spec = importlib.util.spec_from_file_location("bot_pseudo", _BOT_DIR / "smolevich-ai-bot.py")
assert _spec and _spec.loader
bot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bot)

REPORTED = ('<tool_call>curl\n-s "https://html.duckduckgo.com/html/?q=главные+новости+сегодня" '
            "-H \"User-Agent: Mozilla/5.0\" | head -10\n</arg_value>\n</tool_call>")
LFM = ("<|tool_call_start|>[bash(command='curl -s \"https://html.duckduckgo.com/html/?q=news\" "
       "| head -200')]<|tool_call_end|>")
JSON_CALL = '<tool_call>{"name": "execute_bash", "arguments": {"command": "date -u"}}</tool_call>'


def sent(text):
    said = []
    with mock.patch.object(bot, "tg_send_long_text",
                           side_effect=lambda t, u, txt, **k: said.append(txt) or {"ok": True}):
        bot.send_model_answer("token", 1, text)
    return said[0]


class NothingRawReachesTheChat(unittest.TestCase):
    def test_the_reported_answer_leaves_nothing_behind(self):
        self.assertEqual(sent(REPORTED), "")

    def test_the_orphaned_closing_tag_goes_too(self):
        self.assertNotIn("arg_value", sent(REPORTED + "\nВот новости."))

    def test_the_words_around_the_call_survive(self):
        self.assertEqual(sent("Сейчас посмотрю.\n" + REPORTED), "Сейчас посмотрю.")

    def test_the_liquid_shape_is_recognised(self):
        self.assertEqual(sent(LFM), "")

    def test_a_json_call_is_recognised(self):
        self.assertEqual(sent(JSON_CALL), "")


class ShapesWeHaveActuallySeen(unittest.TestCase):
    def check(self, text):
        self.assertEqual(tool_calls.strip_pseudo_calls(text), "")

    def test_hermes_block(self):
        self.check("<tool_call>{\"name\": \"x\"}</tool_call>")

    def test_function_call_block(self):
        self.check("<function_call>run()</function_call>")

    def test_tool_use_block(self):
        self.check("<tool_use>run()</tool_use>")

    def test_llama_python_tag(self):
        self.check("<|python_tag|>execute_bash(command='ls')")

    def test_mistral_bracket_marker(self):
        self.check('[TOOL_CALLS] [{"name": "execute_bash"}]')

    def test_gemma_tool_code_fence(self):
        self.check("```tool_code\nprint(search('news'))\n```")

    def test_an_unclosed_opener_takes_the_rest_with_it(self):
        self.check("<tool_call>curl -s https://example.com")


class OrdinaryAnswersAreLeftAlone(unittest.TestCase):
    def test_plain_prose_is_untouched(self):
        text = "В интернет я не хожу, поэтому свежих новостей не назову."
        self.assertEqual(tool_calls.strip_pseudo_calls(text), text)

    def test_a_real_bash_code_block_is_not_a_tool_call(self):
        """Someone asking for a command must still get one."""
        text = "Смотри:\n```bash\ncurl -s https://example.com\n```"
        self.assertEqual(tool_calls.strip_pseudo_calls(text), text)

    def test_a_comparison_operator_is_not_a_tag(self):
        self.assertEqual(tool_calls.strip_pseudo_calls("если a < b, то верно"), "если a < b, то верно")


class WhatCommandItAsksFor(unittest.TestCase):
    def test_a_bare_shell_line_is_read_as_the_command(self):
        inner = tool_calls.find_pseudo_calls(REPORTED)[0][2]
        self.assertTrue(tool_calls.command_from_pseudo_call(inner).startswith("curl"))

    def test_a_keyword_argument_is_read_as_the_command(self):
        inner = tool_calls.find_pseudo_calls(LFM)[0][2]
        self.assertTrue(tool_calls.command_from_pseudo_call(inner).startswith("curl"))

    def test_a_json_call_is_read_as_the_command(self):
        inner = tool_calls.find_pseudo_calls(JSON_CALL)[0][2]
        self.assertEqual(tool_calls.command_from_pseudo_call(inner), "date -u")

    def test_prose_is_not_read_as_a_command(self):
        """Guessing is worse than dropping the block."""
        self.assertIsNone(tool_calls.command_from_pseudo_call("I will look this up for you"))

    def test_a_call_to_something_else_is_not_a_shell_command(self):
        self.assertIsNone(tool_calls.command_from_pseudo_call('{"name": "get_weather", "arguments": {}}'))


def ask(content, use_tools=False):
    """One ask_llm turn with a scripted model reply."""
    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

        def read(self):
            import json
            return json.dumps({"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                               "usage": {"prompt_tokens": 1, "completion_tokens": 1}}).encode()

    opener = mock.Mock()
    opener.open.return_value = Response()
    with mock.patch.object(bot, "make_opener", return_value=opener), \
         mock.patch.object(bot.DB, "mark_tools_unsupported") as marked, \
         mock.patch.object(bot, "tool_run_in_container", return_value="{}"):
        result = bot.ask_llm("https://openrouter.ai/api/v1/chat/completions", "k", "m/model",
                             [{"role": "user", "content": "новости"}], uid=1, admin_id=1,
                             use_tools=use_tools)
    return result, marked


class AnEmptyPretendCallIsAFailedAttempt(unittest.TestCase):
    def test_no_answer_comes_back(self):
        (ans, _, _), _ = ask(REPORTED)
        self.assertIsNone(ans)

    def test_the_reason_says_what_happened(self):
        (_, _, meta), _ = ask(REPORTED)
        self.assertEqual(meta["error"], "pseudo_tool_call")

    def test_the_model_loses_the_tool_schema(self):
        _, marked = ask(REPORTED)
        marked.assert_called_once_with("openrouter", "m/model")

    def test_a_run_up_to_the_call_is_not_an_answer_either(self):
        """Live on the box: stripping the block left «Ищу новости прямо сейчас.» — a promise."""
        (ans, _, meta), _ = ask("Ищу новости прямо сейчас.\n" + REPORTED)
        self.assertIsNone(ans)

    def test_a_real_answer_around_the_call_survives(self):
        (ans, _, _), _ = ask("Свежих новостей назвать не могу — в интернет я не хожу, "
                             "знания заканчиваются раньше. Загляни в Google Новости.\n" + REPORTED)
        self.assertTrue(ans.startswith("Свежих новостей"))


class TheChainMovesOn(unittest.TestCase):
    def test_a_pretend_call_sends_the_question_to_the_next_model(self):
        """The bug: nothing was retried, though live models were sitting right there."""
        tried = []

        def fake_ask(api_url, api_key, model, messages, **kw):
            tried.append(model)
            if len(tried) == 1:
                return None, {"prompt_tokens": 0, "completion_tokens": 0}, {
                    "finish_reason": "stop", "tool_calls_total": 0, "error": "pseudo_tool_call",
                    "http_latency_ms": 1, "rate_limits": {}, "status": None, "retry_after_sec": None}
            return "В интернет я не хожу.", {"prompt_tokens": 1, "completion_tokens": 1}, {
                "finish_reason": "stop", "tool_calls_total": 0, "error": None,
                "http_latency_ms": 1, "rate_limits": {}, "status": None, "retry_after_sec": None}

        session = {"provider": "openrouter", "model": "a/one", "ui_lang": "ru", "history": [],
                   "tools_enabled": True, "engine_mode": "native", "model_pinned": False}
        with mock.patch.object(bot, "live_pairs", return_value=set()), \
             mock.patch.object(bot, "live_model_ranking",
                               return_value=[("openrouter", "a/one"), ("openrouter", "b/two")]), \
             mock.patch.object(bot, "ask_llm", side_effect=fake_ask), \
             mock.patch.object(bot, "load_provider_key", return_value="k"), \
             mock.patch.object(bot, "capabilities_for_model", return_value=["text"]), \
             mock.patch.object(bot.DB, "log_request"):
            ans, _, _, _, model = bot.answer_with_fallback(1, 1, session, [], "новости", "sys")
        self.assertEqual((ans, model), ("В интернет я не хожу.", "b/two"))


class ThePromptMatchesReality(unittest.TestCase):
    def test_without_tools_the_model_is_told_it_has_none(self):
        self.assertIn("NO tools", bot.build_system_prompt(is_admin=True, has_tools=False))

    def test_without_tools_the_admin_is_not_promised_the_internet(self):
        """This promise is what made the model type a curl call out."""
        self.assertNotIn("full internet access",
                         bot.build_system_prompt(is_admin=True, has_tools=False))

    def test_without_tools_the_model_is_told_not_to_invent_output(self):
        prompt = bot.build_system_prompt(is_admin=True, has_tools=False)
        self.assertIn("invent its output", prompt)

    def test_without_tools_it_is_not_told_to_just_execute(self):
        self.assertNotIn("Just execute", bot.build_system_prompt(is_admin=True, has_tools=False))

    def test_with_tools_the_admin_still_gets_the_shell(self):
        self.assertIn("full internet access", bot.build_system_prompt(is_admin=True, has_tools=True))


class WhichProviderTheUrlBelongsTo(unittest.TestCase):
    def test_the_openrouter_endpoint_is_named(self):
        self.assertEqual(bot.provider_of(bot.PROVIDERS["openrouter"]["url"]), "openrouter")

    def test_an_unknown_endpoint_falls_back_to_the_default(self):
        self.assertEqual(bot.provider_of("https://example.com/v1/chat/completions"),
                         bot.PROVIDER_DEFAULT)


if __name__ == "__main__":
    unittest.main()
