"""Comprehensive unit tests for Telegram Forum Topics isolation and 1:1 conversation mapping."""

import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, AsyncMock, patch

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.handlers.forum_helper import (
    is_forum_general_chat,
    generate_topic_name,
    format_user_prompt_preview,
    get_topic_icon_id,
    FORUM_TOPIC_ICONS,
    create_forum_topic_with_retry,
)
from src.formatters import safe_send, safe_reply
from src.session_manager import session_manager, SessionState


class DummyUser:
    def __init__(self, id_val: int, username: str = None, full_name: str = None):
        self.id = id_val
        self.username = username
        self.full_name = full_name


class DummyForumTopic:
    def __init__(self, message_thread_id: int, name: str):
        self.message_thread_id = message_thread_id
        self.name = name


class TestForumTopics(unittest.TestCase):
    def setUp(self):
        # Reset in-memory sessions for clean tests
        session_manager._sessions.clear()
        s = session_manager.get_session(123456789, None)
        s.forum_topic_id = None
        s.conversation_id = None
        session_manager.save_session(s)
        session_manager._sessions.clear()

    def test_is_forum_general_chat(self):
        # 1. Private chat -> False
        priv_chat = MagicMock(type="private", is_forum=False)
        msg_priv = MagicMock(message_thread_id=None, is_topic_message=False)
        self.assertFalse(is_forum_general_chat(priv_chat, msg_priv))

        # 2. Forum supergroup, message in General (thread_id None, not is_topic_message) -> True
        forum_chat = MagicMock(type="supergroup", is_forum=True)
        msg_gen1 = MagicMock(message_thread_id=None, is_topic_message=False)
        self.assertTrue(is_forum_general_chat(forum_chat, msg_gen1))

        # 3. Forum supergroup, message in General (thread_id 1) -> True
        msg_gen2 = MagicMock(message_thread_id=1, is_topic_message=False)
        self.assertTrue(is_forum_general_chat(forum_chat, msg_gen2))

        # 4. Forum supergroup, message in custom topic (thread_id 42, is_topic_message True) -> False
        msg_topic = MagicMock(message_thread_id=42, is_topic_message=True)
        self.assertFalse(is_forum_general_chat(forum_chat, msg_topic))

        # 5. Non-supergroup with is_forum mock -> False
        group_chat = MagicMock(type="group", is_forum=True)
        self.assertFalse(is_forum_general_chat(group_chat, msg_gen1))

    def test_generate_topic_name(self):
        # Neutral text -> default chat icon
        name_neutral = generate_topic_name("Olá como vai você")
        self.assertTrue(name_neutral.startswith("💬 Olá como vai você"))
        self.assertLessEqual(len(name_neutral), 128)

        # Code / dev text -> computer icon
        name_code = generate_topic_name("Deploy application to production")
        self.assertTrue(name_code.startswith("💻 Deploy application to production"))
        self.assertLessEqual(len(name_code), 128)

        # Long text truncated
        long_prompt = "A" * 200
        name2 = generate_topic_name(long_prompt)
        self.assertLessEqual(len(name2), 128)
        self.assertTrue(name2.endswith("..."))

        # Voice note with user note (semantic detection on note)
        name_voice = generate_topic_name("[Attached Voice Note: /path/voice.ogg]\nUser's note: Deploy script")
        self.assertEqual(name_voice, "💻 Deploy script")

        # Voice note without user note
        name_voice_raw = generate_topic_name("[Attached Voice Note: /path/voice.ogg]")
        self.assertTrue(name_voice_raw.startswith("🎙️ Áudio"))

        # Image with note
        name_img = generate_topic_name("[Attached Image: /path/img.jpg]\nFix this UI bug")
        self.assertEqual(name_img, "💻 Fix this UI bug")

        # Image without note
        name_img_raw = generate_topic_name("[Attached Image: /path/img.jpg]")
        self.assertTrue(name_img_raw.startswith("🖼️ Imagem"))

    def test_get_topic_icon_id(self):
        # Default / neutral text -> chat icon
        self.assertEqual(get_topic_icon_id("Hello there"), FORUM_TOPIC_ICONS["chat"])
        self.assertEqual(get_topic_icon_id(""), FORUM_TOPIC_ICONS["chat"])
        self.assertEqual(get_topic_icon_id(None), FORUM_TOPIC_ICONS["chat"])

        # Semantic keywords detection
        self.assertEqual(get_topic_icon_id("Deploy python script to server"), FORUM_TOPIC_ICONS["code"])
        self.assertEqual(get_topic_icon_id("Quanto está a cotação do dólar hoje?"), FORUM_TOPIC_ICONS["money"])
        self.assertEqual(get_topic_icon_id("Baixar novo episódio de Dexter"), FORUM_TOPIC_ICONS["video"])
        self.assertEqual(get_topic_icon_id("Pesquisar novidades sobre Gemini e LLMs"), FORUM_TOPIC_ICONS["ai"])
        self.assertEqual(get_topic_icon_id("Agenda reunião amanhã às 14h"), FORUM_TOPIC_ICONS["calendar"])

        # Voice note without note -> voice icon
        self.assertEqual(
            get_topic_icon_id("[Attached Voice Note: /path/voice.ogg]"),
            FORUM_TOPIC_ICONS["voice"],
        )

        # Voice note with note -> semantic detection
        self.assertEqual(
            get_topic_icon_id("[Attached Voice Note: /path/voice.ogg]\nUser's note: Deploy script"),
            FORUM_TOPIC_ICONS["code"],
        )

        # Image without caption -> image icon
        self.assertEqual(
            get_topic_icon_id("[Attached Image: /path/img.jpg]"),
            FORUM_TOPIC_ICONS["image"],
        )

        # Audio file
        self.assertEqual(
            get_topic_icon_id("[Attached Audio File: /path/audio.mp3]"),
            FORUM_TOPIC_ICONS["audio"],
        )

        # Document: python script -> code
        self.assertEqual(
            get_topic_icon_id("[Attached Document: /path/doc_123_main.py (Use view_file)]"),
            FORUM_TOPIC_ICONS["code"],
        )

        # Document: generic -> document
        self.assertEqual(
            get_topic_icon_id("[Attached Document: /path/doc_123_notes.txt (Use view_file)]"),
            FORUM_TOPIC_ICONS["document"],
        )

        # Explicit topic_type
        self.assertEqual(get_topic_icon_id(topic_type="terminal"), FORUM_TOPIC_ICONS["terminal"])
        self.assertEqual(get_topic_icon_id(topic_type="command"), FORUM_TOPIC_ICONS["command"])

    def test_general_topic_message_creates_new_topic_and_never_replies_to_general(self):
        """Test that a message sent in General topic creates a new topic and never replies to General."""
        from src.handlers.chat import handle_message

        chat = MagicMock(type="supergroup", is_forum=True, id=-1001234567890)
        msg = MagicMock(text="Create a new microservice", message_thread_id=None, is_topic_message=False)
        msg.reply_text = AsyncMock()
        msg.photo = []
        msg.voice = None
        msg.audio = None
        msg.document = None

        user = DummyUser(123456789, username="testuser")
        update = MagicMock(effective_chat=chat, effective_message=msg, effective_user=user)

        bot_mock = MagicMock()
        created_topic = DummyForumTopic(message_thread_id=888, name="💬 Create a new microservice")
        bot_mock.create_forum_topic = AsyncMock(return_value=created_topic)

        status_msg_mock = MagicMock()
        status_msg_mock.edit_text = AsyncMock()
        bot_mock.send_message = AsyncMock(return_value=status_msg_mock)

        context = MagicMock(bot=bot_mock)

        with patch("src.auth.is_user_authorized", return_value=True), \
             patch("src.antigravity_runner.AntigravityRunner.run", new_callable=AsyncMock) as mock_runner_run:
            mock_runner_run.return_value = {
                "status": "SUCCESS",
                "response": "Microservice created successfully!",
                "conversation_id": "conv-general-123",
            }

            asyncio.run(handle_message(update, context))

        # 1. create_forum_topic was called in the supergroup
        bot_mock.create_forum_topic.assert_called_once()
        call_kwargs = bot_mock.create_forum_topic.call_args[1]
        self.assertEqual(call_kwargs["chat_id"], -1001234567890)

        # 2. msg.reply_text was NEVER called (zero replies in General!)
        msg.reply_text.assert_not_called()

        # 3. bot.send_message was called to send preview and status to thread 888
        send_calls = bot_mock.send_message.call_args_list
        self.assertTrue(any(c[1].get("message_thread_id") == 888 for c in send_calls))

        # 4. Session for (chat.id, 888) was saved with the conversation_id
        session = session_manager.get_session(-1001234567890, 888)
        self.assertEqual(session.conversation_id, "conv-general-123")

    def test_private_chat_does_not_create_topic_nor_mirror_response(self):
        """Test that in private DM, responses are sent ONLY to private chat with no topic creation or mirroring."""
        from src.handlers.chat import handle_message

        chat = MagicMock(type="private", is_forum=False, id=123456789)
        msg = MagicMock(text="Refactor authentication system", message_thread_id=None, is_topic_message=False)
        private_status_msg = MagicMock()
        private_status_msg.edit_text = AsyncMock()
        msg.reply_text = AsyncMock(return_value=private_status_msg)
        msg.photo = []
        msg.voice = None
        msg.audio = None
        msg.document = None

        user = DummyUser(123456789, username="testuser")
        update = MagicMock(effective_chat=chat, effective_message=msg, effective_user=user)

        bot_mock = MagicMock()
        bot_mock.create_forum_topic = AsyncMock()
        bot_mock.send_message = AsyncMock()

        context = MagicMock(bot=bot_mock)

        with patch("src.auth.is_user_authorized", return_value=True), \
             patch("src.handlers.chat.FORUM_GROUP_ID", -1001234567890), \
             patch("src.antigravity_runner.AntigravityRunner.run", new_callable=AsyncMock) as mock_runner_run:
            mock_runner_run.return_value = {
                "status": "SUCCESS",
                "response": "Authentication refactored!",
                "conversation_id": "conv-private-456",
            }

            asyncio.run(handle_message(update, context))

        # 1. Topic was NOT created in FORUM_GROUP_ID
        bot_mock.create_forum_topic.assert_not_called()

        # 2. Private message received initial status and edited with response
        msg.reply_text.assert_called_once()
        private_status_msg.edit_text.assert_called()

        # 3. Topic in forum group received NO messages
        bot_mock.send_message.assert_not_called()

        # 4. Private session has conversation_id set and forum_topic_id is None
        session = session_manager.get_session(123456789, None)
        self.assertIsNone(session.forum_topic_id)
        self.assertEqual(session.conversation_id, "conv-private-456")

    def test_cmd_new_in_general_creates_topic_and_never_replies_to_general(self):
        """Test that /new in General topic creates a new topic and never posts to General."""
        from src.handlers.commands import cmd_new

        chat = MagicMock(type="supergroup", is_forum=True, id=-1001234567890)
        msg = MagicMock(text="/new", message_thread_id=None, is_topic_message=False)
        msg.reply_text = AsyncMock()
        user = DummyUser(123456789, username="testuser")
        update = MagicMock(effective_chat=chat, effective_message=msg, effective_user=user)

        bot_mock = MagicMock()
        created_topic = DummyForumTopic(message_thread_id=777, name="💬 Nova Conversa")
        bot_mock.create_forum_topic = AsyncMock(return_value=created_topic)
        bot_mock.send_message = AsyncMock()
        context = MagicMock(bot=bot_mock)

        with patch("src.auth.is_user_authorized", return_value=True):
            asyncio.run(cmd_new(update, context))

        # 1. create_forum_topic called in supergroup
        bot_mock.create_forum_topic.assert_called_once()

        # 2. msg.reply_text was NEVER called (zero in General!)
        msg.reply_text.assert_not_called()

        # 3. Welcome message was sent to thread 777
        bot_mock.send_message.assert_called_once()
        self.assertEqual(bot_mock.send_message.call_args[1]["message_thread_id"], 777)

    def test_cmd_new_in_private_resets_locally_without_topic_creation(self):
        """Test that /new in private DM resets conversation locally without creating topics or notifying forum group."""
        from src.handlers.commands import cmd_new

        chat = MagicMock(type="private", is_forum=False, id=123456789)
        msg = MagicMock(text="/new", message_thread_id=None, is_topic_message=False)
        msg.reply_text = AsyncMock()
        user = DummyUser(123456789, username="testuser")
        update = MagicMock(effective_chat=chat, effective_message=msg, effective_user=user)

        bot_mock = MagicMock()
        bot_mock.create_forum_topic = AsyncMock()
        bot_mock.send_message = AsyncMock()
        context = MagicMock(bot=bot_mock)

        with patch("src.auth.is_user_authorized", return_value=True), \
             patch("src.handlers.commands.FORUM_GROUP_ID", -1001234567890):
            asyncio.run(cmd_new(update, context))

        # 1. Topic was NOT created in FORUM_GROUP_ID
        bot_mock.create_forum_topic.assert_not_called()

        # 2. User received confirmation directly in private
        msg.reply_text.assert_called_once()
        self.assertIn("Nova conversa iniciada", msg.reply_text.call_args[0][0])

        # 3. No notification was sent to forum group
        bot_mock.send_message.assert_not_called()

        # 4. Private session forum_topic_id is None and conversation_id is None
        session = session_manager.get_session(123456789, None)
        self.assertIsNone(session.forum_topic_id)
        self.assertIsNone(session.conversation_id)

    def test_command_in_general_replies_directly_without_creating_topic(self):
        """Test that administrative commands like /status and /recent in General reply directly in General without creating topics."""
        from src.handlers.commands import cmd_status

        chat = MagicMock(type="supergroup", is_forum=True, id=-1001234567890)
        msg = MagicMock(text="/status", message_thread_id=None, is_topic_message=False)
        msg.reply_text = AsyncMock()
        user = DummyUser(123456789, username="testuser")
        update = MagicMock(effective_chat=chat, effective_message=msg, effective_user=user)

        bot_mock = MagicMock()
        bot_mock.create_forum_topic = AsyncMock()
        context = MagicMock(bot=bot_mock)

        with patch("src.auth.is_user_authorized", return_value=True):
            asyncio.run(cmd_status(update, context))

        # 1. Topic MUST NOT be created for commands executed in General
        bot_mock.create_forum_topic.assert_not_called()

        # 2. Reply sent directly in General via msg.reply_text
        msg.reply_text.assert_called_once()
        self.assertIn("Status", msg.reply_text.call_args[0][0])

    def test_create_forum_topic_with_retry_recovers_from_transient_error(self):
        """Test that create_forum_topic_with_retry recovers after transient network error."""
        import httpx

        bot_mock = MagicMock()
        topic_ok = DummyForumTopic(message_thread_id=123, name="Test Topic")
        # First call fails with httpx.ConnectError, second succeeds
        bot_mock.create_forum_topic = AsyncMock(side_effect=[httpx.ConnectError("Connection refused"), topic_ok])

        res = asyncio.run(
            create_forum_topic_with_retry(
                bot=bot_mock,
                chat_id=-1001234567890,
                name="Test Topic",
                max_retries=3,
                initial_delay=0.01,
            )
        )
        self.assertIsNotNone(res)
        self.assertEqual(res.message_thread_id, 123)
        self.assertEqual(bot_mock.create_forum_topic.call_count, 2)

    def test_create_forum_topic_with_retry_fails_gracefully(self):
        """Test that create_forum_topic_with_retry exhausts retries and returns None without raising."""
        import httpx

        bot_mock = MagicMock()
        bot_mock.create_forum_topic = AsyncMock(side_effect=httpx.ConnectError("Persistent timeout"))

        res = asyncio.run(
            create_forum_topic_with_retry(
                bot=bot_mock,
                chat_id=-1001234567890,
                name="Test Topic",
                max_retries=2,
                initial_delay=0.01,
            )
        )
        self.assertIsNone(res)
        self.assertEqual(bot_mock.create_forum_topic.call_count, 2)

    def test_safe_send_recovers_from_network_error(self):
        """Test that safe_send recovers from transient network errors."""
        import httpx

        bot_mock = MagicMock()
        sent_msg = MagicMock()
        bot_mock.send_message = AsyncMock(side_effect=[httpx.ConnectError("Connection reset"), sent_msg])

        res = asyncio.run(
            safe_send(
                bot=bot_mock,
                chat_id=12345,
                text="Hello world",
                max_retries=3,
            )
        )
        self.assertEqual(res, sent_msg)
        self.assertEqual(bot_mock.send_message.call_count, 2)

    def test_private_chat_ignores_any_legacy_forum_topic_id(self):
        """Test that private chat stays isolated even if a legacy forum_topic_id was previously stored."""
        from src.handlers.chat import handle_message

        chat = MagicMock(type="private", is_forum=False, id=123456789)
        msg = MagicMock(text="Você está bem?", message_thread_id=None, is_topic_message=False)
        private_status_msg = MagicMock()
        private_status_msg.edit_text = AsyncMock()
        msg.reply_text = AsyncMock(return_value=private_status_msg)
        msg.photo = []
        msg.voice = None
        msg.audio = None
        msg.document = None

        user = DummyUser(123456789, username="testuser")
        update = MagicMock(effective_chat=chat, effective_message=msg, effective_user=user)

        # Pre-set session with legacy forum_topic_id
        session = session_manager.get_session(123456789, None)
        session.forum_topic_id = 555
        session.conversation_id = None
        session_manager.save_session(session)

        bot_mock = MagicMock()
        bot_mock.create_forum_topic = AsyncMock()
        bot_mock.edit_forum_topic = AsyncMock()
        bot_mock.send_message = AsyncMock()

        context = MagicMock(bot=bot_mock)

        with patch("src.auth.is_user_authorized", return_value=True), \
             patch("src.handlers.chat.FORUM_GROUP_ID", -1001234567890), \
             patch("src.antigravity_runner.AntigravityRunner.run", new_callable=AsyncMock) as mock_runner_run:
            mock_runner_run.return_value = {
                "status": "SUCCESS",
                "response": "Tudo ótimo!",
                "conversation_id": "conv-123",
            }

            asyncio.run(handle_message(update, context))

        # Forum topics are never created or edited in private chat
        bot_mock.create_forum_topic.assert_not_called()
        bot_mock.edit_forum_topic.assert_not_called()
        bot_mock.send_message.assert_not_called()
        private_status_msg.edit_text.assert_called()

    def test_resume_in_private_attaches_locally_without_topic_creation(self):
        """When /resume is sent in private DM, attach conversation locally without creating or touching forum topics."""
        from src.handlers.commands import cmd_resume

        chat = MagicMock(type="private", id=123456789)
        msg = MagicMock(message_thread_id=None, is_topic_message=False)
        msg.reply_text = AsyncMock()
        user = DummyUser(123456789, username="testuser")
        update = MagicMock(effective_chat=chat, effective_message=msg, effective_user=user)

        target_cid = "conv-private-only-1234"
        bot_mock = MagicMock()
        bot_mock.create_forum_topic = AsyncMock()
        bot_mock.send_message = AsyncMock()
        context = MagicMock(bot=bot_mock, args=[target_cid])

        with patch("src.auth.is_user_authorized", return_value=True), \
             patch("src.handlers.commands.FORUM_GROUP_ID", -1001234567890), \
             patch.object(session_manager, "get_conversation_turns", return_value=[{"user": "hello private", "assistant": "hi there", "tools": []}]):
            asyncio.run(cmd_resume(update, context))

        # create_forum_topic MUST NOT be called in private DM
        bot_mock.create_forum_topic.assert_not_called()
        bot_mock.send_message.assert_not_called()

        priv_session = session_manager.get_session(123456789, None)
        self.assertEqual(priv_session.conversation_id, target_cid)
        self.assertIsNone(priv_session.forum_topic_id)
        self.assertTrue(msg.reply_text.called)

    def test_resume_in_general_reuses_existing_topic_without_creating(self):
        """When resuming in General a conversation that already has a forum topic, do NOT create a new one."""
        from src.handlers.commands import cmd_resume

        chat = MagicMock(type="supergroup", is_forum=True, id=-1001234567890)
        msg = MagicMock(message_thread_id=None, is_topic_message=False)
        msg.reply_text = AsyncMock()
        user = DummyUser(123456789, username="testuser")
        update = MagicMock(effective_chat=chat, effective_message=msg, effective_user=user)

        # Set an existing topic in DB for cid "conv-test-exists"
        target_cid = "conv-test-exists"
        s = session_manager.get_session(-1001234567890, 888)
        s.conversation_id = target_cid
        session_manager.save_session(s)

        bot_mock = MagicMock()
        bot_mock.create_forum_topic = AsyncMock()
        bot_mock.send_message = AsyncMock()
        bot_mock.unpin_all_forum_topic_messages = AsyncMock()
        context = MagicMock(bot=bot_mock, args=[target_cid])

        with patch("src.auth.is_user_authorized", return_value=True), \
             patch("src.handlers.commands.FORUM_GROUP_ID", -1001234567890):
            asyncio.run(cmd_resume(update, context))

        # create_forum_topic MUST NOT be called because topic 888 already existed
        bot_mock.create_forum_topic.assert_not_called()

        # History and confirmation should be sent to thread 888
        topic_sends = [c for c in bot_mock.send_message.call_args_list if c[1].get("message_thread_id") == 888]
        self.assertGreaterEqual(len(topic_sends), 1)

    def test_resume_in_general_creates_topic_when_none_exists(self):
        """When resuming in General a conversation with NO existing topic, create one in FORUM_GROUP_ID."""
        from src.handlers.commands import cmd_resume

        chat = MagicMock(type="supergroup", is_forum=True, id=-1001234567890)
        msg = MagicMock(message_thread_id=None, is_topic_message=False)
        msg.reply_text = AsyncMock()
        user = DummyUser(123456789, username="testuser")
        update = MagicMock(effective_chat=chat, effective_message=msg, effective_user=user)

        target_cid = "conv-pc-only-1234"
        bot_mock = MagicMock()
        dummy_topic = DummyForumTopic(message_thread_id=999, name="💻 PC CLI: Test")
        bot_mock.create_forum_topic = AsyncMock(return_value=dummy_topic)
        bot_mock.send_message = AsyncMock()
        bot_mock.unpin_all_forum_topic_messages = AsyncMock()
        context = MagicMock(bot=bot_mock, args=[target_cid])

        with patch("src.auth.is_user_authorized", return_value=True), \
             patch("src.handlers.commands.FORUM_GROUP_ID", -1001234567890), \
             patch.object(session_manager, "find_topic_for_conversation", return_value=None), \
             patch.object(session_manager, "get_conversation_turns", return_value=[{"user": "hello pc", "assistant": "hi there", "tools": []}]):
            asyncio.run(cmd_resume(update, context))

        # create_forum_topic MUST be called because no topic existed
        bot_mock.create_forum_topic.assert_called_once()
        topic_session = session_manager.get_session(-1001234567890, 999)
        self.assertEqual(topic_session.conversation_id, target_cid)

        # History should have been sent to thread 999 and brief notice replied in General
        self.assertTrue(msg.reply_text.called)
        topic_sends = [c for c in bot_mock.send_message.call_args_list if c[1].get("message_thread_id") == 999]
        self.assertGreaterEqual(len(topic_sends), 1)

    def test_resume_in_existing_topic_does_not_create(self):
        """When /resume is sent inside an existing forum topic, NEVER create a new topic."""
        from src.handlers.commands import cmd_resume

        chat = MagicMock(type="supergroup", is_forum=True, id=-1001234567890)
        msg = MagicMock(message_thread_id=77, is_topic_message=True)
        msg.reply_text = AsyncMock()
        user = DummyUser(123456789, username="testuser")
        update = MagicMock(effective_chat=chat, effective_message=msg, effective_user=user)

        bot_mock = MagicMock()
        bot_mock.create_forum_topic = AsyncMock()
        bot_mock.unpin_all_forum_topic_messages = AsyncMock()
        context = MagicMock(bot=bot_mock, args=["conv-test-xyz"])

        with patch("src.auth.is_user_authorized", return_value=True), \
             patch("src.handlers.commands.FORUM_GROUP_ID", -1001234567890):
            asyncio.run(cmd_resume(update, context))

        bot_mock.create_forum_topic.assert_not_called()
        topic_session = session_manager.get_session(-1001234567890, 77)
        self.assertEqual(topic_session.conversation_id, "conv-test-xyz")


if __name__ == "__main__":
    unittest.main()
