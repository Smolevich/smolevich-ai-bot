"""What reaches the chat must be the answer, not the plumbing.

Two things a user hit on 2026-08-14:
- a bullet list came out mangled, because `* item` is indistinguishable from an
  italic opener and the parser swallowed the star, leaving the closing one dangling;
- the agent mode forwarded acpx's handshake verbatim: `[client] session/new (running)`,
  `[done] end_turn`.

Stdlib only, like the rest of the project.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

_BOT_DIR = Path(__file__).resolve().parent.parent / "bot"
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

from agent.entities import normalize_list_markers, parse_markdown_to_entities  # noqa: E402

_spec = importlib.util.spec_from_file_location("bot_fmt", _BOT_DIR / "smolevich-ai-bot.py")
assert _spec and _spec.loader
bot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bot)

AGENT_OUTPUT = """[client] initialize (running)

[client] session/new (running)
⚙️ Модель: nvidia/llama-3.3-nemotron-super-49b-v1
* Провайдер: OpenRouter
* Доступ: через агента

[done] end_turn"""


class ListMarkers(unittest.TestCase):
    def test_bullets_become_dots(self):
        self.assertEqual(normalize_list_markers("* один\n* два"), "• один\n• два")

    def test_indented_bullets_keep_their_indent(self):
        self.assertEqual(normalize_list_markers("  * вложенный"), "  • вложенный")

    def test_italic_still_works(self):
        text, entities = parse_markdown_to_entities(normalize_list_markers("это *важно* очень"))
        self.assertEqual(text, "это важно очень")
        self.assertEqual(entities[0]["type"], "italic")

    def test_a_bullet_list_no_longer_loses_its_star(self):
        text, _ = parse_markdown_to_entities(normalize_list_markers("* Провайдер: OpenRouter\n* Доступ"))
        self.assertNotIn("*", text)
        self.assertIn("• Провайдер: OpenRouter", text)

    def test_multiplication_inside_a_line_is_untouched(self):
        self.assertEqual(normalize_list_markers("2 * 2 = 4"), "2 * 2 = 4")


class AgentNoise(unittest.TestCase):
    def test_protocol_lines_are_dropped(self):
        cleaned = bot.strip_acp_noise(AGENT_OUTPUT)
        self.assertNotIn("[client]", cleaned)
        self.assertNotIn("[done]", cleaned)

    def test_the_actual_answer_survives(self):
        cleaned = bot.strip_acp_noise(AGENT_OUTPUT)
        self.assertIn("⚙️ Модель: nvidia/llama-3.3-nemotron-super-49b-v1", cleaned)
        self.assertIn("* Провайдер: OpenRouter", cleaned)

    def test_no_gaping_blank_runs_left_behind(self):
        self.assertNotIn("\n\n\n", bot.strip_acp_noise(AGENT_OUTPUT))

    def test_plain_answer_is_unchanged(self):
        self.assertEqual(bot.strip_acp_noise("просто ответ"), "просто ответ")

    def test_square_brackets_in_prose_survive(self):
        text = "смотри [1] и массив a[0]"
        self.assertEqual(bot.strip_acp_noise(text), text)


class LegacyKeyboard(unittest.TestCase):
    """A reply keyboard lives on the client until replaced, so removed buttons keep arriving."""

    def test_old_model_button_is_still_routed(self):
        self.assertEqual(bot.quick_action_for("🤖 Model"), "model")
        self.assertEqual(bot.quick_action_for("🤖 Модель"), "model")

    def test_a_question_about_a_model_is_not_a_button(self):
        self.assertEqual(bot.quick_action_for("какая модель лучше?"), "")


if __name__ == "__main__":
    unittest.main()
