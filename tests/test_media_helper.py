"""Unit tests for src/media_helper.py and media sending capabilities."""

import asyncio
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

# Add project root to path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from telegram.error import BadRequest, TimedOut

from src.media_helper import (
    build_parser,
    cmd_send,
    dispatch_media_tags,
    main,
    parse_chat_id,
    parse_media_tags,
    send_media,
)


class TestMediaHelperCli(unittest.TestCase):
    """Test CLI argument parsing and execution."""

    def test_parse_chat_id(self):
        self.assertEqual(parse_chat_id(12345), 12345)
        self.assertEqual(parse_chat_id("12345"), 12345)
        self.assertEqual(parse_chat_id("-100123456789"), -100123456789)
        self.assertEqual(parse_chat_id("@my_channel"), "@my_channel")

    def test_cli_arg_parsing_send(self):
        parser = build_parser()
        args = parser.parse_args([
            "send",
            "--chat-id", "123456",
            "--thread-id", "789",
            "--file", "build/app.apk",
            "--caption", "Release build",
            "--type", "document",
            "--json",
        ])
        self.assertEqual(args.command, "send")
        self.assertEqual(args.chat_id, "123456")
        self.assertEqual(args.thread_id, 789)
        self.assertEqual(args.file, "build/app.apk")
        self.assertEqual(args.caption, "Release build")
        self.assertEqual(args.type, "document")
        self.assertTrue(args.json)

    def test_cli_topic_id_alias(self):
        parser = build_parser()
        args = parser.parse_args([
            "send",
            "--chat-id", "123456",
            "--topic-id", "999",
            "--file", "image.png",
        ])
        self.assertEqual(args.thread_id, 999)
        self.assertEqual(args.type, "auto")

    def test_cli_cmd_send_success(self):
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            f.write(b"Hello world")
            temp_path = f.name

        try:
            mock_res = {
                "status": "success",
                "chat_id": 123456,
                "thread_id": None,
                "file": temp_path,
                "filename": Path(temp_path).name,
                "type": "document",
                "message_id": 42,
                "caption": "Test file",
            }
            with patch("src.media_helper.send_media", new_callable=AsyncMock) as mock_send:
                mock_send.return_value = mock_res
                parser = build_parser()
                args = parser.parse_args([
                    "send",
                    "--chat-id", "123456",
                    "--file", temp_path,
                    "--caption", "Test file",
                ])
                ret = cmd_send(args)
                self.assertEqual(ret, 0)
                mock_send.assert_awaited_once()

                # Test JSON output mode
                args.json = True
                with patch("sys.stdout", new_callable=io.StringIO) as fake_out:
                    ret_json = cmd_send(args)
                    self.assertEqual(ret_json, 0)
                    out_obj = json.loads(fake_out.getvalue())
                    self.assertEqual(out_obj["status"], "success")
                    self.assertEqual(out_obj["message_id"], 42)
        finally:
            Path(temp_path).unlink(missing_ok=True)

    def test_cli_cmd_send_failure(self):
        mock_res = {
            "status": "error",
            "message": "File not found",
            "file": "/nonexistent.txt",
            "chat_id": 123,
        }
        with patch("src.media_helper.send_media", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = mock_res
            parser = build_parser()
            args = parser.parse_args([
                "send",
                "--chat-id", "123",
                "--file", "/nonexistent.txt",
            ])
            ret = cmd_send(args)
            self.assertEqual(ret, 1)

    def test_main_without_send_subcommand(self):
        with patch("sys.exit") as mock_exit, patch("src.media_helper.cmd_send", return_value=0) as mock_cmd:
            main(["--chat-id", "123", "--file", "test.txt"])
            mock_cmd.assert_called_once()
            mock_exit.assert_called_with(0)


class TestSendMedia(unittest.IsolatedAsyncioTestCase):
    """Test send_media logic with mocks."""

    async def asyncSetUp(self):
        self.temp_file = tempfile.NamedTemporaryFile(suffix=".txt", delete=False)
        self.temp_file.write(b"sample file content for testing")
        self.temp_file.close()
        self.file_path = Path(self.temp_file.name)

    async def asyncTearDown(self):
        self.file_path.unlink(missing_ok=True)

    async def test_file_not_found(self):
        res = await send_media(chat_id=123, file_path="/tmp/definitely_not_existing_uziclaw_file.xyz")
        self.assertEqual(res["status"], "error")
        self.assertIn("Arquivo não encontrado", res["message"])

    async def test_file_empty(self):
        empty_file = tempfile.NamedTemporaryFile(delete=False)
        empty_file.close()
        try:
            res = await send_media(chat_id=123, file_path=empty_file.name)
            self.assertEqual(res["status"], "error")
            self.assertIn("vazio", res["message"])
        finally:
            Path(empty_file.name).unlink(missing_ok=True)

    async def test_send_document_auto(self):
        mock_bot = MagicMock()
        mock_msg = MagicMock(message_id=101)
        mock_bot.send_document = AsyncMock(return_value=mock_msg)

        res = await send_media(
            chat_id=12345,
            file_path=self.file_path,
            caption="Here is your document",
            thread_id=55,
            media_type="auto",
            bot=mock_bot,
        )

        self.assertEqual(res["status"], "success")
        self.assertEqual(res["type"], "document")
        self.assertEqual(res["message_id"], 101)
        mock_bot.send_document.assert_awaited_once()
        call_kwargs = mock_bot.send_document.call_args[1]
        self.assertEqual(call_kwargs["chat_id"], 12345)
        self.assertEqual(call_kwargs["message_thread_id"], 55)
        self.assertEqual(call_kwargs["caption"], "Here is your document")

    async def test_send_photo_auto(self):
        photo_file = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        photo_file.write(b"\x89PNG\r\n\x1a\nfakeimagebytes")
        photo_file.close()
        try:
            mock_bot = MagicMock()
            mock_msg = MagicMock(message_id=202)
            mock_bot.send_photo = AsyncMock(return_value=mock_msg)

            res = await send_media(
                chat_id=12345,
                file_path=photo_file.name,
                caption="Screenshot",
                media_type="auto",
                bot=mock_bot,
            )

            self.assertEqual(res["status"], "success")
            self.assertEqual(res["type"], "photo")
            self.assertEqual(res["message_id"], 202)
            mock_bot.send_photo.assert_awaited_once()
        finally:
            Path(photo_file.name).unlink(missing_ok=True)

    async def test_send_photo_fallback_to_document(self):
        """When sending as photo fails (e.g. invalid photo or too large), fall back to document."""
        photo_file = tempfile.NamedTemporaryFile(suffix=".jpg", delete=False)
        photo_file.write(b"not a valid jpeg content")
        photo_file.close()
        try:
            mock_bot = MagicMock()
            mock_bot.send_photo = AsyncMock(side_effect=BadRequest("Photo invalid format"))
            mock_msg = MagicMock(message_id=303)
            mock_bot.send_document = AsyncMock(return_value=mock_msg)

            res = await send_media(
                chat_id=12345,
                file_path=photo_file.name,
                media_type="auto",
                bot=mock_bot,
            )

            self.assertEqual(res["status"], "success")
            self.assertEqual(res["type"], "document")
            self.assertEqual(res["message_id"], 303)
            mock_bot.send_photo.assert_awaited_once()
            mock_bot.send_document.assert_awaited_once()
        finally:
            Path(photo_file.name).unlink(missing_ok=True)

    async def test_send_audio_auto(self):
        audio_file = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
        audio_file.write(b"ID3fakeaudiocontent")
        audio_file.close()
        try:
            mock_bot = MagicMock()
            mock_msg = MagicMock(message_id=404)
            mock_bot.send_audio = AsyncMock(return_value=mock_msg)

            res = await send_media(
                chat_id=12345,
                file_path=audio_file.name,
                media_type="auto",
                bot=mock_bot,
            )

            self.assertEqual(res["status"], "success")
            self.assertEqual(res["type"], "audio")
            mock_bot.send_audio.assert_awaited_once()
        finally:
            Path(audio_file.name).unlink(missing_ok=True)

    async def test_send_video_auto(self):
        video_file = tempfile.NamedTemporaryFile(suffix=".mp4", delete=False)
        video_file.write(b"fakevideobytes")
        video_file.close()
        try:
            mock_bot = MagicMock()
            mock_msg = MagicMock(message_id=505)
            mock_bot.send_video = AsyncMock(return_value=mock_msg)

            res = await send_media(
                chat_id=12345,
                file_path=video_file.name,
                media_type="auto",
                bot=mock_bot,
            )

            self.assertEqual(res["status"], "success")
            self.assertEqual(res["type"], "video")
            mock_bot.send_video.assert_awaited_once()
        finally:
            Path(video_file.name).unlink(missing_ok=True)

    async def test_retry_on_network_timeout(self):
        mock_bot = MagicMock()
        mock_msg = MagicMock(message_id=606)
        # First attempt times out, second attempt succeeds
        mock_bot.send_document = AsyncMock(side_effect=[TimedOut("Connection timeout"), mock_msg])

        with patch("asyncio.sleep", new_callable=AsyncMock):
            res = await send_media(
                chat_id=12345,
                file_path=self.file_path,
                bot=mock_bot,
                max_retries=2,
            )

        self.assertEqual(res["status"], "success")
        self.assertEqual(res["message_id"], 606)
        self.assertEqual(mock_bot.send_document.await_count, 2)

    async def test_caption_truncation(self):
        mock_bot = MagicMock()
        mock_msg = MagicMock(message_id=707)
        mock_bot.send_document = AsyncMock(return_value=mock_msg)

        huge_caption = "A" * 1500
        res = await send_media(
            chat_id=12345,
            file_path=self.file_path,
            caption=huge_caption,
            bot=mock_bot,
        )

        self.assertEqual(res["status"], "success")
        call_kwargs = mock_bot.send_document.call_args[1]
        self.assertLessEqual(len(call_kwargs["caption"]), 1024)
        self.assertTrue(call_kwargs["caption"].endswith("..."))


class TestMediaTags(unittest.IsolatedAsyncioTestCase):
    """Test inline media tags parsing and dispatch."""

    def test_parse_media_tags(self):
        text = (
            "Here is the APK: [send_file: /path/to/app.apk (caption: Build v1.0)]\n"
            "And here is the image: [attach: /path/to/icon.png]\n"
            "And here is another: [send_file: \"/path with space/doc.pdf\" caption: \"Important document\"]"
        )
        tags = parse_media_tags(text)
        self.assertEqual(len(tags), 3)

        self.assertEqual(tags[0]["path"], "/path/to/app.apk")
        self.assertEqual(tags[0]["caption"], "Build v1.0")

        self.assertEqual(tags[1]["path"], "/path/to/icon.png")
        self.assertIsNone(tags[1]["caption"])

        self.assertEqual(tags[2]["path"], "/path with space/doc.pdf")
        self.assertEqual(tags[2]["caption"], "Important document")

    async def test_dispatch_media_tags_success(self):
        mock_bot = MagicMock()
        text = "Hello! [send_file: /tmp/test.txt (caption: My file)] Please take a look."

        with patch("src.media_helper.send_media", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {
                "status": "success",
                "chat_id": 123,
                "file": "/tmp/test.txt",
                "filename": "test.txt",
                "type": "document",
                "message_id": 888,
            }

            cleaned_text, results = await dispatch_media_tags(text, mock_bot, chat_id=123, thread_id=10)

            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["status"], "success")
            self.assertNotIn("[send_file:", cleaned_text)
            self.assertIn("Hello! Please take a look.", cleaned_text)
            mock_send.assert_awaited_once_with(
                chat_id=123,
                file_path="/tmp/test.txt",
                caption="My file",
                thread_id=10,
                media_type="auto",
                bot=mock_bot,
            )

    async def test_dispatch_media_tags_empty_after_clean(self):
        mock_bot = MagicMock()
        text = "[send_file: /tmp/test.apk]"

        with patch("src.media_helper.send_media", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {
                "status": "success",
                "chat_id": 123,
                "file": "/tmp/test.apk",
                "filename": "test.apk",
                "type": "document",
                "message_id": 999,
            }

            cleaned_text, results = await dispatch_media_tags(text, mock_bot, chat_id=123)
            self.assertEqual(len(results), 1)
            self.assertIn("📎 Arquivo(s) enviado(s): `test.apk`", cleaned_text)

    async def test_dispatch_media_tags_failure(self):
        mock_bot = MagicMock()
        text = "Check this: [send_file: /tmp/missing.txt]"

        with patch("src.media_helper.send_media", new_callable=AsyncMock) as mock_send:
            mock_send.return_value = {
                "status": "error",
                "message": "Arquivo não encontrado",
                "file": "/tmp/missing.txt",
                "chat_id": 123,
            }

            cleaned_text, results = await dispatch_media_tags(text, mock_bot, chat_id=123)
            self.assertEqual(len(results), 1)
            self.assertIn("⚠️ [Falha ao enviar anexo 'missing.txt': Arquivo não encontrado]", cleaned_text)


if __name__ == "__main__":
    unittest.main()
