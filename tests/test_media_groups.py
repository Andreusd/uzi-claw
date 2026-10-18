"""Unit tests for Telegram Media Group (album) batching and handling in UziClaw."""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.handlers.chat import handle_message, prepare_prompt_from_messages
import src.handlers.chat as chat_module
from src.session_manager import session_manager


class DummyUser:
    def __init__(self, id_val: int, username: str = None, full_name: str = None):
        self.id = id_val
        self.username = username
        self.full_name = full_name


class DummyForumTopic:
    def __init__(self, message_thread_id: int, name: str):
        self.message_thread_id = message_thread_id
        self.name = name


class TestMediaGroups(unittest.TestCase):
    def setUp(self):
        session_manager._sessions.clear()
        chat_module._media_group_cache.clear()
        self.orig_debounce = chat_module.MEDIA_GROUP_DEBOUNCE_SECONDS
        chat_module.MEDIA_GROUP_DEBOUNCE_SECONDS = 0.05

    def tearDown(self):
        chat_module.MEDIA_GROUP_DEBOUNCE_SECONDS = self.orig_debounce
        chat_module._media_group_cache.clear()

    def test_media_group_in_general_creates_single_topic_with_all_images(self):
        """Test that sending 2 images in an album in General topic opens exactly ONE forum topic."""
        chat = MagicMock(type="supergroup", is_forum=True, id=-1001234567890)
        user = DummyUser(123456789, username="testuser")

        photo1 = MagicMock(file_id="fid_photo1_abc")
        msg1 = MagicMock(
            message_id=101,
            media_group_id="mg_12345",
            message_thread_id=None,
            is_topic_message=False,
            photo=[photo1],
            voice=None,
            audio=None,
            document=None,
            caption=None,
        )
        update1 = MagicMock(effective_chat=chat, effective_message=msg1, effective_user=user)

        photo2 = MagicMock(file_id="fid_photo2_def")
        msg2 = MagicMock(
            message_id=102,
            media_group_id="mg_12345",
            message_thread_id=None,
            is_topic_message=False,
            photo=[photo2],
            voice=None,
            audio=None,
            document=None,
            caption="Compare both designs",
        )
        update2 = MagicMock(effective_chat=chat, effective_message=msg2, effective_user=user)

        bot_mock = MagicMock()
        mock_file = MagicMock()
        mock_file.download_to_drive = AsyncMock()
        bot_mock.get_file = AsyncMock(return_value=mock_file)

        created_topic = DummyForumTopic(message_thread_id=888, name="🖼️ Compare both designs")
        bot_mock.create_forum_topic = AsyncMock(return_value=created_topic)

        status_msg_mock = MagicMock()
        status_msg_mock.edit_text = AsyncMock()
        bot_mock.send_message = AsyncMock(return_value=status_msg_mock)
        bot_mock.unpin_all_forum_topic_messages = AsyncMock()

        ctx = MagicMock(bot=bot_mock)

        executed_prompts = []

        async def fake_runner_run(*args, **kwargs):
            executed_prompts.append(kwargs.get("prompt", ""))
            return {
                "status": "SUCCESS",
                "response": "Here is the comparison between both designs.",
                "conversation_id": "conv-album-123",
            }

        with patch("src.auth.is_user_authorized", return_value=True), \
             patch("src.antigravity_runner.AntigravityRunner.run", new_callable=AsyncMock, side_effect=fake_runner_run):
            async def run_both():
                t1 = asyncio.create_task(handle_message(update1, ctx))
                await asyncio.sleep(0.01)
                t2 = asyncio.create_task(handle_message(update2, ctx))
                await asyncio.gather(t1, t2)

            asyncio.run(run_both())

        # 1. create_forum_topic was called EXACTLY ONCE
        bot_mock.create_forum_topic.assert_called_once()
        self.assertEqual(bot_mock.create_forum_topic.call_args[1]["chat_id"], -1001234567890)

        # 2. Antigravity was run EXACTLY ONCE
        self.assertEqual(len(executed_prompts), 1)
        prompt = executed_prompts[0]
        # Both photos must be in prompt
        self.assertIn("photo_101_fid_phot.jpg", prompt)
        self.assertIn("photo_102_fid_phot.jpg", prompt)
        # Caption from message 2 must be in prompt
        self.assertIn("Compare both designs", prompt)

        # 3. Topic name was generated appropriately
        self.assertIn("Compare both designs", bot_mock.create_forum_topic.call_args[1]["name"])

    def test_media_group_in_private_chat_runs_single_turn_with_both_images(self):
        """Test that sending 2 images in an album in private chat executes a single turn."""
        chat = MagicMock(type="private", is_forum=False, id=123456789)
        user = DummyUser(123456789, username="testuser")

        photo1 = MagicMock(file_id="fid_priv1_abc")
        status_msg1 = MagicMock()
        status_msg1.edit_text = AsyncMock()
        msg1 = MagicMock(
            message_id=201,
            media_group_id="mg_priv_99",
            photo=[photo1],
            voice=None,
            audio=None,
            document=None,
            caption="Here is the first photo note",
        )
        msg1.reply_text = AsyncMock(return_value=status_msg1)
        update1 = MagicMock(effective_chat=chat, effective_message=msg1, effective_user=user)

        photo2 = MagicMock(file_id="fid_priv2_def")
        status_msg2 = MagicMock()
        status_msg2.edit_text = AsyncMock()
        msg2 = MagicMock(
            message_id=202,
            media_group_id="mg_priv_99",
            photo=[photo2],
            voice=None,
            audio=None,
            document=None,
            caption=None,
        )
        msg2.reply_text = AsyncMock(return_value=status_msg2)
        update2 = MagicMock(effective_chat=chat, effective_message=msg2, effective_user=user)

        bot_mock = MagicMock()
        mock_file = MagicMock()
        mock_file.download_to_drive = AsyncMock()
        bot_mock.get_file = AsyncMock(return_value=mock_file)
        ctx = MagicMock(bot=bot_mock)

        executed_prompts = []

        async def fake_runner_run(*args, **kwargs):
            executed_prompts.append(kwargs.get("prompt", ""))
            return {
                "status": "SUCCESS",
                "response": "Both images analyzed!",
                "conversation_id": "conv-album-priv",
            }

        with patch("src.auth.is_user_authorized", return_value=True), \
             patch("src.antigravity_runner.AntigravityRunner.run", new_callable=AsyncMock, side_effect=fake_runner_run):
            async def run_both():
                t1 = asyncio.create_task(handle_message(update1, ctx))
                await asyncio.sleep(0.01)
                t2 = asyncio.create_task(handle_message(update2, ctx))
                await asyncio.gather(t1, t2)

            asyncio.run(run_both())

        # No forum topic created
        bot_mock.create_forum_topic.assert_not_called()

        # Runner called exactly once
        self.assertEqual(len(executed_prompts), 1)
        prompt = executed_prompts[0]
        self.assertIn("photo_201_fid_priv.jpg", prompt)
        self.assertIn("photo_202_fid_priv.jpg", prompt)
        self.assertIn("Here is the first photo note", prompt)

    def test_prepare_prompt_from_messages_multiple_documents(self):
        """Test prepare_prompt_from_messages with an album of multiple documents."""
        doc1 = MagicMock(file_id="doc111", file_name="script1.py")
        msg1 = MagicMock(message_id=301, photo=None, voice=None, audio=None, document=doc1, caption=None)

        doc2 = MagicMock(file_id="doc222", file_name="script2.py")
        msg2 = MagicMock(message_id=302, photo=None, voice=None, audio=None, document=doc2, caption="Review these scripts")

        bot_mock = MagicMock()
        mock_file = MagicMock()
        mock_file.download_to_drive = AsyncMock()
        bot_mock.get_file = AsyncMock(return_value=mock_file)
        ctx = MagicMock(bot=bot_mock)

        prompt, status = asyncio.run(prepare_prompt_from_messages([msg1, msg2], ctx))

        self.assertIn("[Attached Document:", prompt)
        self.assertIn("doc_301_script1.py", prompt)
        self.assertIn("doc_302_script2.py", prompt)
        self.assertIn("Review these scripts", prompt)
        self.assertIn("Processing 2 files", status)


if __name__ == "__main__":
    unittest.main()
