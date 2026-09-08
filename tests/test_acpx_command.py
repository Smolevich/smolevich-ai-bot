"""Regression: what the sandbox is actually told to run.

Two things went into the container wrong on 2026-09-08.

The model was passed only through ANTHROPIC_DEFAULT_*_MODEL, and claude-code 2.1.138
resolves a 1M-context model from those to `<id>[1m]` — verified on the server, the same
id with `--model` goes out over ACP `session/set_model` verbatim while the env-only run
asks OpenRouter for `minimax/minimax-m3:free[1m]` and is told no such model exists.

And the system prompt was claude-code's own, so the bot answered a greeting with
"This is just a casual greeting, not a software engineering task."

Stdlib only, like the rest of the project.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

_BOT_DIR = Path(__file__).resolve().parent.parent / "bot"
if str(_BOT_DIR) not in sys.path:
    sys.path.insert(0, str(_BOT_DIR))

_spec = importlib.util.spec_from_file_location("bot_acpx_cmd", _BOT_DIR / "smolevich-ai-bot.py")
assert _spec and _spec.loader
bot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(bot)

WHITELISTED = bot.model_routing.CLAUDE_CLI_MODELS[0]
SESSION = {"provider": "openrouter", "model": "minimax/minimax-m3:free", "engine_mode": "claude",
           "ui_lang": "ru", "tools_enabled": True, "history": []}


def run_command(sys_prompt="Smolevich AI Bot. Answer in Russian.", returncode=0, stdout="ok"):
    """The argv ask_via_acpx would have executed, without executing it."""
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env") or {}
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr="")

    with mock.patch.object(bot.subprocess, "run", side_effect=fake_run), \
         mock.patch.object(bot, "load_provider_key", return_value="sk-or-secret"), \
         mock.patch.object(bot, "acpx_lock"), \
         mock.patch.object(bot, "touch_active"), \
         mock.patch.object(bot.DB, "set_last_session_id"), \
         mock.patch.object(bot, "harness_target", return_value=(WHITELISTED[0], WHITELISTED[1], True)):
        bot.acpx_lock.return_value.__enter__ = lambda *_: True
        bot.acpx_lock.return_value.__exit__ = lambda *_: False
        bot.ask_via_acpx(1, "Даров", dict(SESSION), sys_prompt=sys_prompt)
    return captured


class ModelIsPassedExplicitly(unittest.TestCase):
    def test_the_model_is_named_on_the_command_line(self):
        cmd = run_command()["cmd"]
        self.assertIn("--model", cmd)

    def test_the_model_named_is_the_whitelisted_one(self):
        cmd = run_command()["cmd"]
        self.assertEqual(cmd[cmd.index("--model") + 1], WHITELISTED[1])

    def test_no_argument_carries_the_one_million_context_suffix(self):
        for part in run_command()["cmd"]:
            self.assertNotIn("[1m]", part)


class OurSystemPromptGoesIn(unittest.TestCase):
    def test_the_bots_own_instructions_are_appended(self):
        cmd = run_command(sys_prompt="Smolevich AI Bot. Answer in Russian.")
        appended = cmd["cmd"][cmd["cmd"].index("--append-system-prompt") + 1]
        self.assertIn("Smolevich AI Bot", appended)


class SecretsInTheLog(unittest.TestCase):
    def test_the_provider_key_is_masked_before_logging(self):
        """journald had the OpenRouter key in plain text on every sandbox run."""
        argv = ["podman", "run", "-e", "ANTHROPIC_API_KEY=sk-or-secret"]
        masked = bot.mask_secrets(argv, {"ANTHROPIC_API_KEY": "sk-or-secret"})
        self.assertNotIn("sk-or-secret", " ".join(masked))


class FailureIsNotAnAnswer(unittest.TestCase):
    def test_a_nonzero_exit_returns_no_text_at_all(self):
        ans, _, _ = mock_failed_run()
        self.assertIsNone(ans)

    def test_the_reason_survives_in_the_metadata(self):
        _, _, meta = mock_failed_run()
        self.assertEqual(meta["finish_reason"], "acpx_error")

    def test_a_diagnostic_printed_on_a_clean_exit_is_not_an_answer(self):
        """acpx exits 0 while claude-code writes the error to stdout."""
        ans, _, _ = mock_failed_run(
            returncode=0,
            stdout="Internal error: There's an issue with the selected model (m[1m]).",
            stderr="")
        self.assertIsNone(ans)

    def test_an_ordinary_answer_on_a_clean_exit_is_still_an_answer(self):
        ans, _, _ = mock_failed_run(returncode=0, stdout="Привет! Чем могу помочь?", stderr="")
        self.assertEqual(ans, "Привет! Чем могу помочь?")


def mock_failed_run(returncode=1, stdout="", stderr="Internal error: issue with the selected model"):
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, returncode, stdout=stdout, stderr=stderr)

    with mock.patch.object(bot.subprocess, "run", side_effect=fake_run), \
         mock.patch.object(bot, "load_provider_key", return_value="k"), \
         mock.patch.object(bot, "acpx_lock"), \
         mock.patch.object(bot, "touch_active"), \
         mock.patch.object(bot.DB, "set_last_session_id"), \
         mock.patch.object(bot, "harness_target", return_value=(WHITELISTED[0], WHITELISTED[1], True)):
        bot.acpx_lock.return_value.__enter__ = lambda *_: True
        bot.acpx_lock.return_value.__exit__ = lambda *_: False
        return bot.ask_via_acpx(1, "Даров", dict(SESSION))


if __name__ == "__main__":
    unittest.main()
