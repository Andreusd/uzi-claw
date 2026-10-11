"""Verification tests for Antigravity Telegram Bot components."""

import asyncio
import sys
import unittest
from pathlib import Path

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.auth import load_authorized_users, is_user_authorized
from src.session_manager import session_manager
from src.formatters import split_message
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

        user_with_username = DummyUser(12345, username="Andreusd", full_name="Andreus D")
        res1 = build_prompt_with_context("Check status", user=user_with_username)
        self.assertIn("[Context: Prompt sent by @Andreusd via Telegram bot.", res1)
        self.assertIn("Check status", res1)

        user_no_username = DummyUser(12345, username=None, full_name="Andreus")
        user_no_username.first_name = "Andreus"
        res2 = build_prompt_with_context("Hello", user=user_no_username)
        self.assertIn("[Context: Prompt sent by Andreus via Telegram bot.", res2)
        self.assertIn("Hello", res2)

        with patch("src.bot.INJECT_TELEGRAM_CONTEXT", False):
            res_disabled = build_prompt_with_context("Check status", user=user_with_username)
            self.assertEqual(res_disabled, "Check status")

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


if __name__ == "__main__":
    unittest.main()
