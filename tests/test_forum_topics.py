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

        chat = MagicMock(type="supergroup", is_forum=True, id=-1004330361961)
        msg = MagicMock(text="Create a new microservice", message_thread_id=None, is_topic_message=False)
        msg.reply_text = AsyncMock()
        msg.photo = []
        msg.voice = None
        msg.audio = None
        msg.document = None

        user = DummyUser(740821374, username="andreusd")
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
        self.assertEqual(call_kwargs["chat_id"], -1004330361961)

        # 2. msg.reply_text was NEVER called (zero replies in General!)
        msg.reply_text.assert_not_called()

        # 3. bot.send_message was called to send preview and status to thread 888
        send_calls = bot_mock.send_message.call_args_list
        self.assertTrue(any(c[1].get("message_thread_id") == 888 for c in send_calls))

        # 4. Session for (chat.id, 888) was saved with the conversation_id
        session = session_manager.get_session(-1004330361961, 888)
        self.assertEqual(session.conversation_id, "conv-general-123")

    def test_private_chat_creates_topic_and_mirrors_response(self):
        """Test that in private DM, responses are sent to private AND mirrored to linked topic."""
        from src.handlers.chat import handle_message

        chat = MagicMock(type="private", is_forum=False, id=740821374)
        msg = MagicMock(text="Refactor authentication system", message_thread_id=None, is_topic_message=False)
        private_status_msg = MagicMock()
        private_status_msg.edit_text = AsyncMock()
        msg.reply_text = AsyncMock(return_value=private_status_msg)
        msg.photo = []
        msg.voice = None
        msg.audio = None
        msg.document = None

        user = DummyUser(740821374, username="andreusd")
        update = MagicMock(effective_chat=chat, effective_message=msg, effective_user=user)

        bot_mock = MagicMock()
        created_topic = DummyForumTopic(message_thread_id=999, name="💬 Refactor authentication")
        bot_mock.create_forum_topic = AsyncMock(return_value=created_topic)
        bot_mock.send_message = AsyncMock()

        context = MagicMock(bot=bot_mock)

        with patch("src.auth.is_user_authorized", return_value=True), \
             patch("src.handlers.chat.FORUM_GROUP_ID", -1004330361961), \
             patch("src.antigravity_runner.AntigravityRunner.run", new_callable=AsyncMock) as mock_runner_run:
            mock_runner_run.return_value = {
                "status": "SUCCESS",
                "response": "Authentication refactored!",
                "conversation_id": "conv-private-456",
            }

            asyncio.run(handle_message(update, context))

        # 1. Topic was created in FORUM_GROUP_ID
        bot_mock.create_forum_topic.assert_called_once_with(
            chat_id=-1004330361961,
            name="💬 Refactor authentication system",
            icon_custom_emoji_id="5417915203100613993",
        )

        # 2. Private message received initial status and edited with response
        msg.reply_text.assert_called_once()
        private_status_msg.edit_text.assert_called()

        # 3. Topic in forum group received mirrored response
        topic_send_calls = [
            c for c in bot_mock.send_message.call_args_list
            if c[1].get("message_thread_id") == 999
        ]
        self.assertGreaterEqual(len(topic_send_calls), 1)

        # 4. Private session has forum_topic_id set
        session = session_manager.get_session(740821374, None)
        self.assertEqual(session.forum_topic_id, 999)
        self.assertEqual(session.conversation_id, "conv-private-456")

    def test_cmd_new_in_general_creates_topic_and_never_replies_to_general(self):
        """Test that /new in General topic creates a new topic and never posts to General."""
        from src.handlers.commands import cmd_new

        chat = MagicMock(type="supergroup", is_forum=True, id=-1004330361961)
        msg = MagicMock(text="/new", message_thread_id=None, is_topic_message=False)
        msg.reply_text = AsyncMock()
        user = DummyUser(740821374, username="andreusd")
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

    def test_cmd_new_in_private_creates_topic_and_notifies_both(self):
        """Test that /new in private DM creates a topic in forum group and notifies both."""
        from src.handlers.commands import cmd_new

        chat = MagicMock(type="private", is_forum=False, id=740821374)
        msg = MagicMock(text="/new", message_thread_id=None, is_topic_message=False)
        msg.reply_text = AsyncMock()
        user = DummyUser(740821374, username="andreusd")
        update = MagicMock(effective_chat=chat, effective_message=msg, effective_user=user)

        bot_mock = MagicMock()
        created_topic = DummyForumTopic(message_thread_id=555, name="💬 Nova Conversa")
        bot_mock.create_forum_topic = AsyncMock(return_value=created_topic)
        bot_mock.send_message = AsyncMock()
        context = MagicMock(bot=bot_mock)

        with patch("src.auth.is_user_authorized", return_value=True), \
             patch("src.handlers.commands.FORUM_GROUP_ID", -1004330361961):
            asyncio.run(cmd_new(update, context))

        # 1. Topic created in FORUM_GROUP_ID
        bot_mock.create_forum_topic.assert_called_once()
        self.assertEqual(bot_mock.create_forum_topic.call_args[1]["chat_id"], -1004330361961)

        # 2. User received confirmation in private
        msg.reply_text.assert_called_once()
        self.assertIn("Nova conversa iniciada", msg.reply_text.call_args[0][0])

        # 3. Notification sent to thread 555 in the forum group
        bot_mock.send_message.assert_called_once()
        self.assertEqual(bot_mock.send_message.call_args[1]["message_thread_id"], 555)

        # 4. Private session forum_topic_id is updated to 555 and conversation_id is None
        session = session_manager.get_session(740821374, None)
        self.assertEqual(session.forum_topic_id, 555)
        self.assertIsNone(session.conversation_id)

    def test_command_in_general_redirected_to_new_topic(self):
        """Test that administrative commands like /status in General redirect to a new topic."""
        from src.handlers.commands import cmd_status

        chat = MagicMock(type="supergroup", is_forum=True, id=-1004330361961)
        msg = MagicMock(text="/status", message_thread_id=None, is_topic_message=False)
        msg.reply_text = AsyncMock()
        user = DummyUser(740821374, username="andreusd")
        update = MagicMock(effective_chat=chat, effective_message=msg, effective_user=user)

        bot_mock = MagicMock()
        created_topic = DummyForumTopic(message_thread_id=333, name="🛠️ /status")
        bot_mock.create_forum_topic = AsyncMock(return_value=created_topic)
        bot_mock.send_message = AsyncMock()
        context = MagicMock(bot=bot_mock)

        with patch("src.auth.is_user_authorized", return_value=True):
            asyncio.run(cmd_status(update, context))

        # 1. Topic created for the command
        bot_mock.create_forum_topic.assert_called_once()

        # 2. Nothing sent directly to General (original reply_text was redirected)
        # 3. Status was sent to the redirected topic (thread 333)
        bot_mock.send_message.assert_called()
        self.assertEqual(bot_mock.send_message.call_args[1]["message_thread_id"], 333)

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
                chat_id=-1004330361961,
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
                chat_id=-1004330361961,
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


    def test_private_chat_reuses_and_renames_existing_topic_from_new(self):
        """Test that if a forum topic was already pre-created (e.g. by /new), the first message renames it rather than creating a duplicate."""
        from src.handlers.chat import handle_message

        chat = MagicMock(type="private", is_forum=False, id=740821374)
        msg = MagicMock(text="Você está bem?", message_thread_id=None, is_topic_message=False)
        private_status_msg = MagicMock()
        private_status_msg.edit_text = AsyncMock()
        msg.reply_text = AsyncMock(return_value=private_status_msg)
        msg.photo = []
        msg.voice = None
        msg.audio = None
        msg.document = None

        user = DummyUser(740821374, username="andreusd")
        update = MagicMock(effective_chat=chat, effective_message=msg, effective_user=user)

        # Pre-set session as if /new was called: forum_topic_id is set, conversation_id is None
        session = session_manager.get_session(740821374, None)
        session.forum_topic_id = 555
        session.conversation_id = None
        session_manager.save_session(session)

        bot_mock = MagicMock()
        bot_mock.create_forum_topic = AsyncMock()
        bot_mock.edit_forum_topic = AsyncMock(return_value=True)
        bot_mock.send_message = AsyncMock()

        context = MagicMock(bot=bot_mock)

        with patch("src.auth.is_user_authorized", return_value=True), \
             patch("src.handlers.chat.FORUM_GROUP_ID", -1004330361961), \
             patch("src.antigravity_runner.AntigravityRunner.run", new_callable=AsyncMock) as mock_runner_run:
            mock_runner_run.return_value = {
                "status": "SUCCESS",
                "response": "Tudo ótimo!",
                "conversation_id": "conv-123",
            }

            asyncio.run(handle_message(update, context))

        # 1. create_forum_topic MUST NOT be called!
        bot_mock.create_forum_topic.assert_not_called()

        # 2. edit_forum_topic MUST be called to rename the pre-existing topic 555
        bot_mock.edit_forum_topic.assert_called_once()
        self.assertEqual(bot_mock.edit_forum_topic.call_args[1]["chat_id"], -1004330361961)
        self.assertEqual(bot_mock.edit_forum_topic.call_args[1]["message_thread_id"], 555)
        self.assertIn("Você está bem?", bot_mock.edit_forum_topic.call_args[1]["name"])

        # 3. Response was sent to topic 555
        topic_send_calls = [
            c for c in bot_mock.send_message.call_args_list
            if c[1].get("message_thread_id") == 555
        ]
        self.assertGreaterEqual(len(topic_send_calls), 1)


if __name__ == "__main__":
    unittest.main()
