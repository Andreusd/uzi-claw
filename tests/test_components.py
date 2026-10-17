"""Verification tests for UziClaw Telegram Bot components."""

import asyncio
import sys
import unittest
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.auth import load_authorized_users, is_user_authorized
from src.session_manager import session_manager
from src.formatters import split_message, format_tool_description, LiveStatusTracker, RateLimitedMessageUpdater
from src.antigravity_runner import AntigravityRunner


class DummyUser:
    def __init__(self, id_val: int, username: str = None, full_name: str = None):
        self.id = id_val
        self.username = username
        self.full_name = full_name


class TestBotComponents(unittest.TestCase):
    def test_auth(self):
        from unittest.mock import patch
        from src import auth
        with patch("src.auth.load_authorized_users"):
            auth._AUTHORIZED_USERS = {"123456789"}
            # Test authorized user with numeric ID
            auth_user1 = DummyUser(123456789, username="TestUser")
            self.assertTrue(is_user_authorized(auth_user1))

            auth_user2 = DummyUser(123456789, username="different_handle")
            self.assertTrue(is_user_authorized(auth_user2))

            # Test unauthorized user
            bad_user = DummyUser(99999, username="bad_actor")
            self.assertFalse(is_user_authorized(bad_user))

            none_user = None
            self.assertFalse(is_user_authorized(none_user))

    def test_unauthorized_notification(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        from src.auth import restricted, _ALERT_TIMESTAMPS

        _ALERT_TIMESTAMPS.clear()

        async def run_test():
            handler_mock = AsyncMock()
            decorated_handler = restricted(handler_mock)

            bot_mock = MagicMock()
            bot_mock.send_message = AsyncMock()
            context = MagicMock(bot=bot_mock)

            chat = MagicMock(type="private", id=99999)
            user = DummyUser(99999, username="stranger", full_name="Stranger Danger")
            msg = MagicMock(text="Hello bot!", caption=None, photo=None, voice=None, audio=None, document=None, video=None, sticker=None, contact=None, location=None)
            update = MagicMock(effective_chat=chat, effective_user=user, effective_message=msg)

            with patch("src.auth.load_authorized_users", return_value={"123456789"}):
                with patch("src.auth.is_user_authorized", return_value=False):
                    await decorated_handler(update, context)

            # Handler itself should NOT have been executed
            handler_mock.assert_not_called()

            # send_message should have been called on authorized user chat_id
            bot_mock.send_message.assert_called_once()
            call_kwargs = bot_mock.send_message.call_args[1]
            self.assertEqual(call_kwargs["chat_id"], 123456789)
            self.assertEqual(call_kwargs["parse_mode"], "HTML")
            self.assertIn("Stranger Danger", call_kwargs["text"])
            self.assertIn("99999", call_kwargs["text"])
            self.assertIn("Hello bot!", call_kwargs["text"])

        asyncio.run(run_test())

    def test_message_splitter(self):
        short_text = "Hello world"
        chunks = split_message(short_text, max_length=50)
        self.assertEqual(chunks, ["Hello world"])

        # Long text
        long_text = "Paragraph 1\n\n" + ("a" * 80) + "\n\nParagraph 3"
        chunks = split_message(long_text, max_length=60)
        self.assertTrue(len(chunks) > 1)
        reconstructed = "".join(chunks)
        self.assertIn("Paragraph 1", reconstructed)
        self.assertIn("Paragraph 3", reconstructed)

    def test_session_manager(self):
        session = session_manager.get_session(1001)
        self.assertEqual(session.chat_id, 1001)
        self.assertTrue(session.autopilot)

        session.conversation_id = "test-uuid"
        session.reset_conversation()
        self.assertIsNone(session.conversation_id)

        # Test listing recent conversations
        recent = session_manager.list_recent_conversations(limit=5)
        self.assertIsInstance(recent, list)

    def test_resume_command_handler(self):
        from unittest.mock import MagicMock
        from telegram import MessageEntity, Update, Message, Chat
        from src.bot import ResumeCommandHandler

        handler = ResumeCommandHandler(None)
        cases = [
            ("/resume", ([], True)),
            ("/resume_1", (["1"], True)),
            ("/resume_7", (["7"], True)),
            ("/resume 2", (["2"], True)),
            ("/resume_test-uuid", (["test-uuid"], True)),
            ("/status", None),
        ]
        for text, expected in cases:
            token = text.split()[0]
            ent = [MessageEntity(type=MessageEntity.BOT_COMMAND, offset=0, length=len(token))]
            msg = Message(message_id=1, date=None, chat=Chat(id=123456789, type="private"), text=text, entities=ent)
            up = Update(update_id=1, message=msg)
            res = handler.check_update(up)
            self.assertEqual(res, expected)

    def test_recent_and_resume_flow(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        from src.bot import cmd_recent, cmd_resume

        async def run_flow():
            chat_id = 123456789
            user = MagicMock(id=chat_id)
            chat = MagicMock(id=chat_id, type="private")

            msg = MagicMock()
            msg.reply_text = AsyncMock()
            up = MagicMock(effective_message=msg, effective_chat=chat, effective_user=user)
            ctx = MagicMock()

            with patch("src.auth.is_user_authorized", return_value=True):
                await cmd_recent(up, ctx)
                self.assertTrue(msg.reply_text.called)
                sent_text = msg.reply_text.call_args[0][0]
                self.assertIn("👉 /resume_1", sent_text)

                session = session_manager.get_session(chat_id)
                self.assertGreater(len(session.recent_conversations), 0)
                target_cid = session.recent_conversations[0]

                # Resume by index 1
                msg2 = MagicMock()
                msg2.reply_text = AsyncMock()
                up2 = MagicMock(effective_message=msg2, effective_chat=chat, effective_user=user)
                ctx2 = MagicMock(args=["1"])

                await cmd_resume(up2, ctx2)
                self.assertTrue(msg2.reply_text.called)
                # Test /resume without args (should display recent list)
                msg3 = MagicMock()
                msg3.reply_text = AsyncMock()
                up3 = MagicMock(effective_message=msg3, effective_chat=chat, effective_user=user)
                ctx3 = MagicMock(args=[])
                await cmd_resume(up3, ctx3)
                self.assertTrue(msg3.reply_text.called)
                sent_text3 = msg3.reply_text.call_args[0][0]
                self.assertIn("Recent Conversations", sent_text3)

        asyncio.run(run_flow())

    def test_is_conversation_running(self):
        # Non-existent conversation should return False
        self.assertFalse(session_manager.is_conversation_running("fake-uuid-12345"))
        # Empty string should return False
        self.assertFalse(session_manager.is_conversation_running(""))

    def test_antigravity_runner(self):
        async def run_test():
            runner = AntigravityRunner()
            events = []

            async def on_event(etype, data):
                events.append(etype)

            res = await runner.run(
                prompt="Reply with the single word: PONG",
                cwd=str(Path.home()),
                autopilot=False,
                event_callback=on_event,
            )
            self.assertEqual(res["status"], "SUCCESS")
            self.assertIn("PONG", res["response"].upper())
            self.assertIsNotNone(res["conversation_id"])

        asyncio.run(run_test())

    def test_build_prompt_with_context(self):
        from src.bot import build_prompt_with_context
        from unittest.mock import patch

        user_with_username = DummyUser(12345, username="TestUser", full_name="Test User")
        res1 = build_prompt_with_context("Check status", user=user_with_username)
        self.assertIn("[Context: You are UziClaw", res1)
        self.assertIn("@TestUser", res1)
        self.assertIn("Check status", res1)

        user_no_username = DummyUser(12345, username=None, full_name="Alice")
        user_no_username.first_name = "Alice"
        res2 = build_prompt_with_context("Hello", user=user_no_username)
        self.assertIn("[Context: You are UziClaw", res2)
        self.assertIn("Alice", res2)
        self.assertIn("Hello", res2)

        with patch("src.bot.INJECT_TELEGRAM_CONTEXT", False):
            res_disabled = build_prompt_with_context("Check status", user=user_with_username)
            self.assertEqual(res_disabled, "Check status")

        # Test subsequent turn (is_first_turn=False) does not inject context
        res_subsequent = build_prompt_with_context("Check status", user=user_with_username, is_first_turn=False)
        self.assertEqual(res_subsequent, "Check status")

    def test_prepare_prompt_from_message(self):
        from unittest.mock import AsyncMock, MagicMock
        from src.bot import prepare_prompt_from_message

        async def run_tests():
            # 1. Plain text
            msg_text = MagicMock(text="Hello world", voice=None, photo=None, audio=None, document=None)
            up_text = MagicMock(effective_message=msg_text)
            ctx = MagicMock()
            prompt, status = await prepare_prompt_from_message(up_text, ctx)
            self.assertEqual(prompt, "Hello world")
            self.assertIn("Initializing", status)

            # 2. Voice note
            mock_file = MagicMock()
            mock_file.download_to_drive = AsyncMock()
            ctx.bot.get_file = AsyncMock(return_value=mock_file)

            voice_mock = MagicMock(file_id="voice12345")
            msg_voice = MagicMock(
                text=None,
                voice=voice_mock,
                photo=None,
                audio=None,
                document=None,
                message_id=42,
                caption="Please summarize this",
            )
            up_voice = MagicMock(effective_message=msg_voice)
            prompt_v, status_v = await prepare_prompt_from_message(up_voice, ctx)
            self.assertIn("[Attached Voice Note:", prompt_v)
            self.assertIn("voice_42_voice123.ogg", prompt_v)
            self.assertIn("Please summarize this", prompt_v)
            self.assertIn("Processing voice message", status_v)
            self.assertTrue(mock_file.download_to_drive.called)

            # 3. Photo
            mock_file.download_to_drive.reset_mock()
            photo_mock = MagicMock(file_id="photo99999")
            msg_photo = MagicMock(
                text=None,
                voice=None,
                photo=[photo_mock],
                audio=None,
                document=None,
                message_id=43,
                caption="What is in this picture?",
            )
            up_photo = MagicMock(effective_message=msg_photo)
            prompt_p, status_p = await prepare_prompt_from_message(up_photo, ctx)
            self.assertIn("[Attached Image:", prompt_p)
            self.assertIn("photo_43_photo999.jpg", prompt_p)
            self.assertIn("What is in this picture?", prompt_p)
            self.assertIn("Processing image", status_p)
            self.assertTrue(mock_file.download_to_drive.called)

            # 4. Document
            mock_file.download_to_drive.reset_mock()
            doc_mock = MagicMock(file_id="doc88888", file_name="sample.png")
            msg_doc = MagicMock(
                text=None,
                voice=None,
                photo=None,
                audio=None,
                document=doc_mock,
                message_id=44,
                caption=None,
            )
            up_doc = MagicMock(effective_message=msg_doc)
            prompt_d, status_d = await prepare_prompt_from_message(up_doc, ctx)
            self.assertIn("[Attached Document:", prompt_d)
            self.assertIn("doc_44_sample.png", prompt_d)
            self.assertIn("Processing document (sample.png)", status_d)
            self.assertTrue(mock_file.download_to_drive.called)

        asyncio.run(run_tests())

    def test_format_usage_data(self):
        from src.formatters import format_usage_data
        sample_data = {
            "groups": [
                {
                    "name": "Gemini Models",
                    "description": "Models within this group: Gemini Flash, Gemini Pro",
                    "buckets": [
                        {
                            "id": "gemini-weekly",
                            "name": "Weekly Limit Remaining",
                            "description": "You have used some of your weekly limit, it will fully refresh in 2 days, 8 hours.",
                            "remaining_fraction": 0.8,
                            "reset_time": "2026-10-13T23:45:30Z",
                        },
                        {
                            "id": "gemini-5h",
                            "name": "Five Hour Limit Remaining",
                            "remaining_fraction": 1.0,
                        },
                    ],
                }
            ]
        }
        formatted = format_usage_data(sample_data)
        self.assertIn("UziClaw Quota Usage", formatted)
        self.assertIn("Gemini Models", formatted)
        self.assertIn("Gemini Flash, Gemini Pro", formatted)
        self.assertIn("Weekly Limit", formatted)
        self.assertIn("80%", formatted)
        self.assertIn("Refreshes in 2 days, 8 hours", formatted)
        self.assertIn("100%", formatted)

    def test_cmd_usage(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        from src.bot import cmd_usage

        async def run_test():
            status_msg_mock = MagicMock()
            status_msg_mock.edit_text = AsyncMock()

            msg_mock = MagicMock()
            msg_mock.reply_text = AsyncMock(return_value=status_msg_mock)

            chat_mock = MagicMock(type="private", id=123456789)
            update = MagicMock(effective_message=msg_mock, effective_user=DummyUser(123456789), effective_chat=chat_mock)
            context = MagicMock()

            sample_usage = {
                "groups": [
                    {
                        "name": "Gemini Models",
                        "buckets": [
                            {"name": "Weekly Limit Remaining", "remaining_fraction": 0.9}
                        ]
                    }
                ]
            }

            with patch("src.auth.is_user_authorized", return_value=True):
                with patch("src.bot.get_usage", return_value=(sample_usage, None)):
                    await cmd_usage(update, context)

            msg_mock.reply_text.assert_called_once()
            status_msg_mock.edit_text.assert_called_once()
            edited_text = status_msg_mock.edit_text.call_args[0][0]
            self.assertIn("UziClaw Quota Usage", edited_text)
            self.assertIn("90%", edited_text)

        asyncio.run(run_test())

    def test_terminal_runner(self):
        from src.terminal_runner import TerminalRunner

        async def run_test():
            runner = TerminalRunner()
            # 1. Normal command
            res = await runner.run("echo 'hello terminal'", cwd=str(Path.home()))
            self.assertEqual(res["status"], "SUCCESS")
            self.assertEqual(res["exit_code"], 0)
            self.assertEqual(res["output"].strip(), "hello terminal")

            # 2. Command with error
            res_err = await runner.run("ls /nonexistent_test_directory_404", cwd=str(Path.home()))
            self.assertEqual(res_err["status"], "ERROR")
            self.assertNotEqual(res_err["exit_code"], 0)
            err_lower = res_err["output"].lower()
            self.assertTrue(any(term in err_lower for term in ["cannot find", "no such", "not found", "does not exist"]))

            # 3. Command with cd
            parent_dir = str(Path.home().parent)
            res_cd = await runner.run(f"cd '{parent_dir}'", cwd=str(Path.home()))
            self.assertEqual(res_cd["status"], "SUCCESS")
            self.assertEqual(Path(res_cd["new_cwd"]).resolve(), Path(parent_dir).resolve())

            # 4. Cancel
            async def run_cancel():
                sleep_cmd = 'python -c "import time; time.sleep(10)"'
                task = asyncio.create_task(runner.run(sleep_cmd, cwd=str(Path.home())))
                await asyncio.sleep(0.3)
                self.assertTrue(runner.is_running)
                cancelled = await runner.cancel()
                self.assertTrue(cancelled)
                res_canc = await task
                self.assertEqual(res_canc["status"], "CANCELLED")

            await run_cancel()

        asyncio.run(run_test())

    def test_format_terminal_output(self):
        from src.formatters import format_terminal_output

        # 1. Success with output
        res1 = {"status": "SUCCESS", "exit_code": 0, "output": "hello world", "new_cwd": None}
        chunks1 = format_terminal_output("echo hello", res1)
        self.assertEqual(len(chunks1), 1)
        self.assertIn("$ echo hello", chunks1[0])
        self.assertIn("```\nhello world\n```", chunks1[0])

        # 2. Success without output (e.g. cd or mkdir)
        res2 = {"status": "SUCCESS", "exit_code": 0, "output": "", "new_cwd": "/tmp"}
        chunks2 = format_terminal_output("cd /tmp", res2)
        self.assertEqual(len(chunks2), 1)
        self.assertIn("Done (exit code 0)", chunks2[0])
        self.assertIn("/tmp", chunks2[0])

        # 3. Error with output
        res3 = {"status": "ERROR", "exit_code": 2, "output": "command not found", "new_cwd": None}
        chunks3 = format_terminal_output("foobar", res3)
        self.assertEqual(len(chunks3), 1)
        self.assertIn("Exit code 2", chunks3[0])
        self.assertIn("command not found", chunks3[0])

        # 4. Cancelled
        res4 = {"status": "CANCELLED", "exit_code": None, "output": "", "new_cwd": None}
        chunks4 = format_terminal_output("sleep 10", res4)
        self.assertEqual(len(chunks4), 1)
        self.assertIn("cancelled", chunks4[0].lower())

        # 5. Timeout
        res5 = {"status": "TIMEOUT", "exit_code": None, "output": "", "new_cwd": None, "timeout": 30}
        chunks5 = format_terminal_output("sleep 60", res5)
        self.assertEqual(len(chunks5), 1)
        self.assertIn("timed out after 30s", chunks5[0])

        # 6. Large output chunking
        res6 = {"status": "SUCCESS", "exit_code": 0, "output": "A" * 6000, "new_cwd": None}
        chunks6 = format_terminal_output("large_cmd", res6)
        self.assertGreater(len(chunks6), 1)
        for chunk in chunks6:
            self.assertIn("```", chunk)

    def test_cmd_terminal_and_execution_flow(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        from src.bot import cmd_terminal, handle_message

        async def run_test():
            chat_id = 987654321
            user = DummyUser(chat_id)
            chat = MagicMock(type="private", id=chat_id)

            session = session_manager.get_session(chat_id)
            session.terminal_mode = False

            status_msg = MagicMock()
            status_msg.edit_text = AsyncMock()

            msg = MagicMock()
            msg.reply_text = AsyncMock(return_value=status_msg)
            msg.text = "/terminal"
            update = MagicMock(effective_message=msg, effective_user=user, effective_chat=chat)
            ctx = MagicMock(args=[])

            with patch("src.auth.is_user_authorized", return_value=True):
                # 1. Toggle ON
                await cmd_terminal(update, ctx)
                self.assertTrue(session.terminal_mode)
                sent = msg.reply_text.call_args[0][0]
                self.assertIn("Terminal Mode Activated", sent)

                # 2. Run shell command via message
                msg.reply_text.reset_mock()
                status_msg.edit_text.reset_mock()
                msg.text = "echo 'testing terminal mode'"
                msg.photo = None
                msg.voice = None
                msg.audio = None
                msg.document = None

                await handle_message(update, ctx)
                self.assertTrue(msg.reply_text.called)
                self.assertTrue(status_msg.edit_text.called)
                final_text = status_msg.edit_text.call_args[0][0]
                self.assertIn("testing terminal mode", final_text)

                # 3. Exit via "exit" text
                msg.reply_text.reset_mock()
                msg.text = "exit"
                await handle_message(update, ctx)
                self.assertFalse(session.terminal_mode)
                sent = msg.reply_text.call_args[0][0]
                self.assertIn("Exited Terminal Mode", sent)

                # 4. Toggle back ON then exit via /terminal
                ctx.args = ["on"]
                await cmd_terminal(update, ctx)
                self.assertTrue(session.terminal_mode)

                ctx.args = []
                await cmd_terminal(update, ctx)
                self.assertFalse(session.terminal_mode)

        asyncio.run(run_test())

    def test_device_stats(self):
        from src.device_stats import (
            format_device_stats,
            make_progress_bar,
            format_bytes,
            get_battery_info,
            get_cpu_info,
            get_memory_info,
            get_disk_info,
            get_system_uptime,
        )

        # Progress bar
        self.assertEqual(make_progress_bar(0), "░" * 10)
        self.assertEqual(make_progress_bar(100), "█" * 10)
        self.assertEqual(len(make_progress_bar(50)), 10)

        # Format bytes
        self.assertEqual(format_bytes(1024), "1 KB")
        self.assertEqual(format_bytes(1024 * 1024 * 500), "500 MB")
        self.assertIn("GB", format_bytes(1024 * 1024 * 1024 * 2))

        # CPU info
        cpu = get_cpu_info()
        self.assertIn("cores", cpu)
        self.assertGreater(cpu["cores"], 0)

        # Memory info
        mem = get_memory_info()
        self.assertIn("total_bytes", mem)

        # Disk info
        disk = get_disk_info("/")
        self.assertIn("total_bytes", disk)

        # Full formatting
        output = format_device_stats()
        self.assertIn("Device & System Stats", output)
        self.assertIn("CPU Load", output)
        self.assertIn("RAM", output)
        self.assertIn("Disk", output)

    def test_cmd_device(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        from src.bot import cmd_device, cmd_stats

        async def run_test():
            msg = MagicMock()
            msg.reply_text = AsyncMock()
            update = MagicMock(effective_message=msg, effective_user=DummyUser(123456789), effective_chat=MagicMock(type="private", id=123456789))
            ctx = MagicMock()

            with patch("src.auth.is_user_authorized", return_value=True):
                await cmd_device(update, ctx)

            msg.reply_text.assert_called_once()
            sent_text = msg.reply_text.call_args[0][0]
            self.assertIn("System Stats", sent_text)
            self.assertIs(cmd_stats, cmd_device)

        asyncio.run(run_test())

    def test_markdown_to_telegram_html(self):
        from src.formatters import markdown_to_telegram_html, sanitize_and_balance_html

        sample = """# Title
**Bold text** and *italic text* and ***both***.
`inline_code`
* Item 1
* Item 2
```python
x < 10 && y > 20
```
---
"""
        converted = markdown_to_telegram_html(sample)
        self.assertIn("<b>Title</b>", converted)
        self.assertIn("<b>Bold text</b>", converted)
        self.assertIn("<i>italic text</i>", converted)
        self.assertIn("<code>inline_code</code>", converted)
        self.assertIn("• Item 1", converted)
        self.assertIn('<pre><code class="language-python">x &lt; 10 &amp;&amp; y &gt; 20</code></pre>', converted)
        self.assertIn("───────────────", converted)

        # Tag balancing
        unbalanced = "<b>Hello <i>world"
        balanced = sanitize_and_balance_html(unbalanced)
        self.assertEqual(balanced, "<b>Hello <i>world</i></b>")

    def test_cmd_interrupt(self):
        from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
        from src.bot import cmd_interrupt
        from src.antigravity_runner import AntigravityRunner
        from src.terminal_runner import TerminalRunner

        async def run_test():
            chat_id = 11223344
            user = DummyUser(chat_id)
            chat = MagicMock(type="private", id=chat_id)
            msg = MagicMock()
            msg.reply_text = AsyncMock()
            update = MagicMock(effective_message=msg, effective_user=user, effective_chat=chat)
            ctx = MagicMock()

            session = session_manager.get_session(chat_id)

            with patch("src.auth.is_user_authorized", return_value=True):
                # 1. Idle session
                with patch.object(AntigravityRunner, "is_running", new_callable=PropertyMock, return_value=False), \
                     patch.object(TerminalRunner, "is_running", new_callable=PropertyMock, return_value=False):
                    await cmd_interrupt(update, ctx)
                    sent = msg.reply_text.call_args[0][0]
                    self.assertIn("No task or prompt is currently running", sent)

                # 2. Agent runner running
                msg.reply_text.reset_mock()
                with patch.object(AntigravityRunner, "is_running", new_callable=PropertyMock, return_value=True), \
                     patch.object(session.runner, "interrupt", AsyncMock(return_value=True)), \
                     patch.object(TerminalRunner, "is_running", new_callable=PropertyMock, return_value=False):
                    await cmd_interrupt(update, ctx)
                    session.runner.interrupt.assert_called_once()
                    sent = msg.reply_text.call_args[0][0]
                    self.assertIn("Interrupted active prompt", sent)

                # 3. Terminal runner running
                msg.reply_text.reset_mock()
                with patch.object(TerminalRunner, "is_running", new_callable=PropertyMock, return_value=True), \
                     patch.object(session.terminal_runner, "interrupt", AsyncMock(return_value=True)), \
                     patch.object(AntigravityRunner, "is_running", new_callable=PropertyMock, return_value=False):
                    await cmd_interrupt(update, ctx)
                    session.terminal_runner.interrupt.assert_called_once()
                    sent = msg.reply_text.call_args[0][0]
                    self.assertIn("Sent interrupt", sent)

                # 4. Session is busy but process not yet launched
                msg.reply_text.reset_mock()
                session.is_busy = True
                session.cancel_requested = False
                with patch.object(AntigravityRunner, "is_running", new_callable=PropertyMock, return_value=False), \
                     patch.object(TerminalRunner, "is_running", new_callable=PropertyMock, return_value=False):
                    await cmd_interrupt(update, ctx)
                    self.assertTrue(session.cancel_requested)
                    sent = msg.reply_text.call_args[0][0]
                    self.assertIn("Interrupted pending prompt before startup", sent)
                session.is_busy = False
                session.cancel_requested = False

        asyncio.run(run_test())

    def test_create_application_concurrent_updates(self):
        from unittest.mock import patch
        from src.bot import create_application
        with patch("src.bot.TELEGRAM_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11"), \
             patch("src.bot.load_authorized_users", return_value={"123456"}):
            app = create_application()
            self.assertGreater(app.concurrent_updates, 1)

    def test_download_telegram_file_with_retry(self):
        from unittest.mock import AsyncMock, MagicMock
        from telegram.error import TimedOut
        from src.bot import download_telegram_file_with_retry
        import tempfile

        async def run_test():
            bot = MagicMock()
            file_mock = MagicMock()
            file_mock.download_to_drive = AsyncMock()

            # 1. Success on attempt 2 after initial timeout
            attempts = 0
            async def fake_get_file(file_id, **kwargs):
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise TimedOut("Timed out on first attempt")
                return file_mock

            bot.get_file = AsyncMock(side_effect=fake_get_file)

            with tempfile.TemporaryDirectory() as tmpdir:
                target_path = Path(tmpdir) / "test_voice.ogg"
                result = await download_telegram_file_with_retry(bot, "file_123", target_path, max_retries=2)
                self.assertEqual(result, target_path)
                self.assertEqual(attempts, 2)
                file_mock.download_to_drive.assert_called_once()

            # 2. Complete failure raises TimedOut
            bot.get_file = AsyncMock(side_effect=TimedOut("Persistent timeout"))
            with tempfile.TemporaryDirectory() as tmpdir:
                target_path = Path(tmpdir) / "test_voice.ogg"
                with self.assertRaises(TimedOut):
                    await download_telegram_file_with_retry(bot, "file_456", target_path, max_retries=2)

        asyncio.run(run_test())

    def test_runner_interrupt_methods(self):
        from src.antigravity_runner import AntigravityRunner
        from src.terminal_runner import TerminalRunner
        from unittest.mock import MagicMock
        import signal

        async def run_test():
            # AntigravityRunner interrupt
            agy_runner = AntigravityRunner()
            # Idle returns False
            self.assertFalse(await agy_runner.interrupt())

            mock_proc = MagicMock()
            mock_proc.returncode = None
            mock_proc.pid = 9999
            agy_runner._current_process = mock_proc

            expected_sig = signal.CTRL_BREAK_EVENT if sys.platform == "win32" else signal.SIGINT

            # Once interrupted, simulate process exit
            def fake_send_signal(sig):
                self.assertEqual(sig, expected_sig)
                mock_proc.returncode = 0
            mock_proc.send_signal = fake_send_signal

            res = await agy_runner.interrupt()
            self.assertTrue(res)
            self.assertTrue(agy_runner._is_cancelled)

            # TerminalRunner interrupt
            term_runner = TerminalRunner()
            self.assertFalse(await term_runner.interrupt())

            mock_term_proc = MagicMock()
            mock_term_proc.returncode = None
            mock_term_proc.pid = 9998
            term_runner._current_process = mock_term_proc

            def fake_term_send_signal(sig):
                self.assertEqual(sig, expected_sig)
                mock_term_proc.returncode = 0
            mock_term_proc.send_signal = fake_term_send_signal

            term_res = await term_runner.interrupt()
            self.assertTrue(term_res)
            self.assertTrue(term_runner._is_cancelled)

        asyncio.run(run_test())

    def test_message_queueing_sequential_execution(self):
        from unittest.mock import AsyncMock, MagicMock, patch
        from src.bot import handle_message
        from src.session_manager import session_manager

        async def run_test():
            chat_id = 55667788
            user = DummyUser(chat_id)
            chat = MagicMock(type="private", id=chat_id)

            session = session_manager.get_session(chat_id)
            session.reset_conversation()

            executed_prompts = []

            async def fake_run(*args, **kwargs):
                prompt = kwargs.get("prompt", "")
                executed_prompts.append(prompt)
                # Sleep briefly to simulate turn execution time
                await asyncio.sleep(0.05)
                return {"status": "SUCCESS", "response": f"Reply to {prompt}", "conversation_id": "test-cid"}

            msg1 = MagicMock(text="First prompt", photo=None, voice=None, audio=None, document=None)
            status_msg1 = MagicMock()
            status_msg1.edit_text = AsyncMock()
            msg1.reply_text = AsyncMock(return_value=status_msg1)
            update1 = MagicMock(effective_message=msg1, effective_user=user, effective_chat=chat)

            msg2 = MagicMock(text="Second prompt", photo=None, voice=None, audio=None, document=None)
            status_msg2 = MagicMock()
            status_msg2.edit_text = AsyncMock()
            msg2.reply_text = AsyncMock(return_value=status_msg2)
            update2 = MagicMock(effective_message=msg2, effective_user=user, effective_chat=chat)

            ctx = MagicMock()

            with patch("src.auth.is_user_authorized", return_value=True), \
                 patch.object(session.runner, "run", side_effect=fake_run):
                # Launch both concurrently
                t1 = asyncio.create_task(handle_message(update1, ctx))
                # Slight yield so t1 acquires lock and starts running
                await asyncio.sleep(0.01)
                t2 = asyncio.create_task(handle_message(update2, ctx))

                await asyncio.gather(t1, t2)

            # Neither should have been rejected with "turn already in progress"
            for call in msg1.reply_text.call_args_list:
                self.assertNotIn("An agent turn is already in progress", call[0][0])
            for call in msg2.reply_text.call_args_list:
                self.assertNotIn("An agent turn is already in progress", call[0][0])

            # Both prompts should have executed sequentially
            self.assertEqual(len(executed_prompts), 2)
            self.assertIn("First prompt", executed_prompts[0])
            self.assertIn("Second prompt", executed_prompts[1])

        asyncio.run(run_test())

    def test_message_queueing_with_interrupt(self):
        from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
        from src.bot import handle_message, cmd_interrupt
        from src.session_manager import session_manager
        from src.antigravity_runner import AntigravityRunner

        async def run_test():
            chat_id = 55667799
            user = DummyUser(chat_id)
            chat = MagicMock(type="private", id=chat_id)

            session = session_manager.get_session(chat_id)
            session.reset_conversation()

            executed_prompts = []
            turn1_interrupted = asyncio.Event()

            async def fake_run(*args, **kwargs):
                prompt = kwargs.get("prompt", "")
                executed_prompts.append(prompt)
                if "First" in prompt:
                    # Wait until interrupted
                    await turn1_interrupted.wait()
                    return {"status": "CANCELLED", "response": "", "conversation_id": "test-cid"}
                return {"status": "SUCCESS", "response": "Reply to second", "conversation_id": "test-cid"}

            msg1 = MagicMock(text="First prompt", photo=None, voice=None, audio=None, document=None)
            status_msg1 = MagicMock()
            status_msg1.edit_text = AsyncMock()
            msg1.reply_text = AsyncMock(return_value=status_msg1)
            update1 = MagicMock(effective_message=msg1, effective_user=user, effective_chat=chat)

            msg2 = MagicMock(text="Second prompt", photo=None, voice=None, audio=None, document=None)
            status_msg2 = MagicMock()
            status_msg2.edit_text = AsyncMock()
            msg2.reply_text = AsyncMock(return_value=status_msg2)
            update2 = MagicMock(effective_message=msg2, effective_user=user, effective_chat=chat)

            interrupt_msg = MagicMock()
            interrupt_msg.reply_text = AsyncMock()
            interrupt_update = MagicMock(effective_message=interrupt_msg, effective_user=user, effective_chat=chat)

            ctx = MagicMock()

            async def fake_interrupt():
                turn1_interrupted.set()
                return True

            with patch("src.auth.is_user_authorized", return_value=True), \
                 patch.object(session.runner, "run", side_effect=fake_run), \
                 patch.object(session.runner, "interrupt", side_effect=fake_interrupt):
                t1 = asyncio.create_task(handle_message(update1, ctx))
                await asyncio.sleep(0.01)
                t2 = asyncio.create_task(handle_message(update2, ctx))
                await asyncio.sleep(0.01)

                with patch.object(AntigravityRunner, "is_running", new_callable=PropertyMock, return_value=True):
                    await cmd_interrupt(interrupt_update, ctx)

                await asyncio.gather(t1, t2)

            self.assertEqual(len(executed_prompts), 2)
            self.assertIn("First prompt", executed_prompts[0])
            self.assertIn("Second prompt", executed_prompts[1])
            self.assertIn("Interrupted active prompt", interrupt_msg.reply_text.call_args[0][0])

        asyncio.run(run_test())

    def test_message_queueing_with_cancel(self):
        from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock
        from src.bot import handle_message, cmd_cancel
        from src.session_manager import session_manager
        from src.antigravity_runner import AntigravityRunner

        async def run_test():
            chat_id = 55667700
            user = DummyUser(chat_id)
            chat = MagicMock(type="private", id=chat_id)

            session = session_manager.get_session(chat_id)
            session.reset_conversation()

            executed_prompts = []
            turn1_cancelled = asyncio.Event()

            async def fake_run(*args, **kwargs):
                prompt = kwargs.get("prompt", "")
                executed_prompts.append(prompt)
                if "First" in prompt:
                    await turn1_cancelled.wait()
                    return {"status": "CANCELLED", "response": "", "conversation_id": "test-cid"}
                return {"status": "SUCCESS", "response": "Reply to second", "conversation_id": "test-cid"}

            msg1 = MagicMock(text="First prompt", photo=None, voice=None, audio=None, document=None)
            status_msg1 = MagicMock()
            status_msg1.edit_text = AsyncMock()
            msg1.reply_text = AsyncMock(return_value=status_msg1)
            update1 = MagicMock(effective_message=msg1, effective_user=user, effective_chat=chat)

            msg2 = MagicMock(text="Second prompt", photo=None, voice=None, audio=None, document=None)
            status_msg2 = MagicMock()
            status_msg2.edit_text = AsyncMock()
            msg2.reply_text = AsyncMock(return_value=status_msg2)
            update2 = MagicMock(effective_message=msg2, effective_user=user, effective_chat=chat)

            cancel_msg = MagicMock()
            cancel_msg.reply_text = AsyncMock()
            cancel_update = MagicMock(effective_message=cancel_msg, effective_user=user, effective_chat=chat)

            ctx = MagicMock()

            async def fake_cancel():
                turn1_cancelled.set()
                return True

            with patch("src.auth.is_user_authorized", return_value=True), \
                 patch.object(session.runner, "run", side_effect=fake_run), \
                 patch.object(session.runner, "cancel", side_effect=fake_cancel):
                t1 = asyncio.create_task(handle_message(update1, ctx))
                await asyncio.sleep(0.01)
                t2 = asyncio.create_task(handle_message(update2, ctx))
                await asyncio.sleep(0.01)

                with patch.object(AntigravityRunner, "is_running", new_callable=PropertyMock, return_value=True):
                    await cmd_cancel(cancel_update, ctx)

                await asyncio.gather(t1, t2)

            # Turn 1 should execute, Turn 2 should be cancelled/dropped without executing
            self.assertEqual(len(executed_prompts), 1)
            self.assertIn("First prompt", executed_prompts[0])
            self.assertIn("cleared queued messages", cancel_msg.reply_text.call_args[0][0])

        asyncio.run(run_test())

    def test_cmd_new_while_running_detaches_to_background(self):
        """Test that calling /new while a turn is running detaches it to background and clears conversation_id immediately."""
        from src.bot import cmd_new
        from src.session_manager import session_manager
        from unittest.mock import MagicMock, AsyncMock, patch

        session = session_manager.get_session(7771)
        session.conversation_id = "running-conv-id"
        session.is_busy = True

        msg = MagicMock()
        msg.reply_text = AsyncMock()
        msg.message_thread_id = None
        user = DummyUser(7771, username="testuser")
        chat = MagicMock(id=7771, type="private")
        update = MagicMock(effective_message=msg, effective_user=user, effective_chat=chat)
        ctx = MagicMock()

        with patch("src.auth.is_user_authorized", return_value=True):
            asyncio.run(cmd_new(update, ctx))

        # Session should be detached with fresh conversation_id None and not busy
        self.assertIsNone(session.conversation_id)
        self.assertFalse(session.is_busy)
        reply_call = msg.reply_text.call_args[0][0]
        self.assertIn("Nova conversa iniciada", reply_call)
        self.assertIn("segundo plano", reply_call)

    def test_forum_topics_session_isolation(self):
        """Test that different message_thread_ids in the same group or chat get isolated sessions."""
        from src.session_manager import session_manager

        s_topic1 = session_manager.get_session(9999, thread_id=101)
        s_topic2 = session_manager.get_session(9999, thread_id=202)
        s_main = session_manager.get_session(9999, thread_id=None)

        self.assertNotEqual(s_topic1, s_topic2)
        self.assertNotEqual(s_topic1, s_main)

        s_topic1.conversation_id = "conv-101"
        s_topic2.conversation_id = "conv-202"

        self.assertEqual(session_manager.get_session(9999, thread_id=101).conversation_id, "conv-101")
        self.assertEqual(session_manager.get_session(9999, thread_id=202).conversation_id, "conv-202")

    def test_antigravity_runner_interrupted_handled_as_cancelled(self):
        """Test that a SIGINT/SIGTERM or 'error: interrupted' in runner returns CANCELLED."""
        from src.antigravity_runner import AntigravityRunner
        from unittest.mock import AsyncMock, patch, MagicMock

        runner = AntigravityRunner()

        async def run_test():
            fake_proc = MagicMock()
            fake_proc.returncode = 130  # Bash SIGINT code
            fake_proc.stdout.readline = AsyncMock(side_effect=[b"", b""])
            fake_proc.stderr.readline = AsyncMock(side_effect=[b"error: interrupted\n", b""])
            fake_proc.wait = AsyncMock(return_value=130)

            with patch("asyncio.create_subprocess_exec", return_value=fake_proc):
                res = await runner.run("test prompt")
                self.assertEqual(res["status"], "CANCELLED")
                self.assertIn("interrompida", res["response"])

        asyncio.run(run_test())

    def test_format_tool_description(self):
        # run_command with CommandLine and toolAction
        desc1 = format_tool_description("run_command", {"CommandLine": "git diff", "toolAction": "Checking diff"})
        self.assertIn("Checking diff", desc1)
        self.assertIn("git diff", desc1)

        # view_file with AbsolutePath
        desc2 = format_tool_description("view_file", {"AbsolutePath": "/home/user/project/src/main.py"})
        self.assertEqual(desc2, "Lendo `main.py`")

        # replace_file_content with TargetFile
        desc3 = format_tool_description("replace_file_content", {"TargetFile": "/home/user/config.json"})
        self.assertEqual(desc3, "Editando `config.json`")

        # write_to_file with TargetFile
        desc4 = format_tool_description("write_to_file", {"TargetFile": "/home/user/new.py"})
        self.assertEqual(desc4, "Criando `new.py`")

        # search_web
        desc5 = format_tool_description("search_web", {"query": "python telegram bot"})
        self.assertIn("Buscando na web", desc5)

        # Generic fallback
        desc6 = format_tool_description("custom_tool", {"toolSummary": "Custom task summary"})
        self.assertEqual(desc6, "Custom task summary")

    def test_live_status_tracker(self):
        from unittest.mock import MagicMock, AsyncMock

        msg_mock = MagicMock()
        msg_mock.edit_text = AsyncMock()
        msg_mock.text = "Initial"
        updater = RateLimitedMessageUpdater(msg_mock, min_interval=0.0)
        tracker = LiveStatusTracker(updater, max_history=2)

        async def run_tracker():
            # 1. Thought arrives
            await tracker.on_event("thought", "Analyzing system logs to find error root cause")
            text1 = tracker.build_dashboard_text()
            self.assertIn("💭 _Analyzing system logs to find error root cause_", text1)

            # 2. Tool starts
            await tracker.on_event("tool_start", {
                "name": "run_command",
                "parameters": {"CommandLine": "pytest tests/", "toolAction": "Running test suite"}
            })
            text2 = tracker.build_dashboard_text()
            self.assertIn("⚙️ *Agora:* Running test suite", text2)

            # 3. Tool finishes -> moves to history
            await tracker.on_event("tool_done", {"name": "run_command", "output": "OK"})
            text3 = tracker.build_dashboard_text()
            self.assertIn("✅ Running test suite", text3)
            self.assertNotIn("⚙️ *Agora:*", text3)

            # 4. Text response streaming
            await tracker.on_event("text_delta", "Testes concluídos com sucesso!")
            text4 = tracker.build_dashboard_text()
            self.assertIn("💬 Testes concluídos com sucesso!", text4)

        asyncio.run(run_tracker())

    def test_system_tray(self):
        from src.tray import tray_manager, generate_tray_icon
        img = generate_tray_icon()
        if sys.platform == "win32":
            self.assertIsNotNone(img)
            self.assertEqual(img.size, (64, 64))
            self.assertTrue(tray_manager.is_available)
        else:
            if img is not None:
                self.assertEqual(img.size, (64, 64))

    def test_cmd_restart_and_shutdown(self):
        from src.handlers.commands import cmd_restart, cmd_shutdown
        from unittest.mock import patch, AsyncMock, MagicMock

        async def run_test():
            msg = MagicMock()
            msg.reply_text = AsyncMock()
            chat = MagicMock(type="private")
            user = DummyUser(12345, username="admin")
            update = MagicMock(effective_chat=chat, effective_message=msg, effective_user=user)
            context = MagicMock()

            with patch("src.auth.is_user_authorized", return_value=True), \
                 patch("subprocess.Popen"), \
                 patch("asyncio.sleep", new_callable=AsyncMock):

                # 1. Test cmd_restart
                await cmd_restart(update, context)
                msg.reply_text.assert_called()
                self.assertIn("Reiniciando UziClaw", msg.reply_text.call_args[0][0])

                # 2. Test cmd_shutdown
                msg.reply_text.reset_mock()
                await cmd_shutdown(update, context)
                msg.reply_text.assert_called()
                self.assertIn("Desligando UziClaw", msg.reply_text.call_args[0][0])

        asyncio.run(run_test())


if __name__ == "__main__":
    unittest.main()

