"""Unit tests for UziClaw scheduler engine and schedule commands."""

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from src.scheduler import (
    ScheduleStore,
    Schedule,
    compute_next_run,
    parse_relative_time,
    format_timestamp,
    SchedulerService,
)
from src.bot import (
    cmd_schedules,
    cmd_cancel_schedule,
    cmd_run_schedule,
    cmd_schedule,
    handle_schedule_callback,
    SuffixCommandHandler,
)


class TestSchedulerCalculations(unittest.TestCase):
    def test_parse_relative_time(self):
        base = 1000.0
        self.assertEqual(parse_relative_time("30s", base_time=base), 1030.0)
        self.assertEqual(parse_relative_time("10m", base_time=base), 1600.0)
        self.assertEqual(parse_relative_time("2h", base_time=base), 8200.0)
        self.assertEqual(parse_relative_time("1d", base_time=base), 87400.0)
        self.assertEqual(parse_relative_time("60", base_time=base), 1060.0)
        self.assertIsNone(parse_relative_time("invalid", base_time=base))

    def test_compute_next_run_cron(self):
        # Every Friday at 18:00
        base = 1760450000.0  # arbitrary timestamp
        next_run = compute_next_run("cron", "0 18 * * 5", base_time=base)
        self.assertIsNotNone(next_run)
        self.assertGreater(next_run, base)

    def test_compute_next_run_once(self):
        base = 1000.0
        next_run = compute_next_run("once", "5m", base_time=base)
        self.assertEqual(next_run, 1300.0)


class TestScheduleStore(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_schedules.db"
        self.store = ScheduleStore(db_path=self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_add_and_list_schedules(self):
        sched1 = self.store.add_schedule(
            chat_id=101,
            prompt="Baixar torrent Dexter",
            schedule_type="cron",
            expression="0 18 * * 5",
            human_desc="Toda sexta às 18h",
        )
        self.assertEqual(sched1.id, 1)
        self.assertEqual(sched1.chat_id, 101)
        self.assertEqual(sched1.status, "active")

        sched2 = self.store.add_schedule(
            chat_id=101,
            prompt="Verificar download",
            schedule_type="once",
            expression="10m",
            human_desc="Em 10 minutos",
        )
        self.assertEqual(sched2.id, 2)

        # List active
        active = self.store.list_schedules(chat_id=101, status="active")
        self.assertEqual(len(active), 2)

        # Cancel #1
        ok = self.store.cancel_schedule(1, chat_id=101)
        self.assertTrue(ok)

        # Now only 1 active
        active_after = self.store.list_schedules(chat_id=101, status="active")
        self.assertEqual(len(active_after), 1)
        self.assertEqual(active_after[0].id, 2)

    def test_get_due_schedules(self):
        now = time.time()
        # Schedule in past
        self.store.add_schedule(
            chat_id=101,
            prompt="Tarefa vencida",
            schedule_type="once",
            expression="1s",
        )
        # Advance time by 5s
        due = self.store.get_due_schedules(now_ts=now + 5.0)
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0].prompt, "Tarefa vencida")

        # After run update
        self.store.update_schedule_after_run(due[0].id, next_run=None, status="completed")
        due_after = self.store.get_due_schedules(now_ts=now + 5.0)
        self.assertEqual(len(due_after), 0)


class TestBotScheduleHandlers(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "test_bot_schedules.db"
        self.store = ScheduleStore(db_path=self.db_path)
        self.patcher = patch("src.bot.schedule_store", self.store)
        self.patcher.start()

    async def asyncTearDown(self):
        self.patcher.stop()
        self.temp_dir.cleanup()

    async def test_cmd_schedules_empty(self):
        update = MagicMock()
        update.effective_chat.id = 999
        update.effective_chat.type = "private"
        update.effective_user.id = 999
        msg = MagicMock()
        msg.reply_text = AsyncMock()
        update.effective_message = msg
        context = MagicMock()

        with patch("src.auth.is_user_authorized", return_value=True):
            await cmd_schedules(update, context)
            msg.reply_text.assert_called_once()
            called_text = msg.reply_text.call_args[0][0]
            self.assertIn("Nenhum agendamento ativo", called_text)

    async def test_cmd_schedule_and_list_and_cancel(self):
        update = MagicMock()
        update.effective_chat.id = 999
        update.effective_chat.type = "private"
        update.effective_user.id = 999
        msg = MagicMock()
        msg.reply_text = AsyncMock()
        update.effective_message = msg
        context = MagicMock()

        with patch("src.auth.is_user_authorized", return_value=True):
            # 1. Create schedule via /schedule
            msg.text = '/schedule "0 18 * * 5" Baixar novo ep Dexter'
            context.args = ['"0 18 * * 5"', "Baixar", "novo", "ep", "Dexter"]
            await cmd_schedule(update, context)
            self.assertIn("Agendamento #1 Criado", msg.reply_text.call_args[0][0])

            # 2. View with /schedules (programmatic, no LLM)
            msg.reply_text.reset_mock()
            await cmd_schedules(update, context)
            list_text = msg.reply_text.call_args[0][0]
            self.assertIn("Agendamentos Ativos", list_text)
            self.assertIn("Baixar novo ep Dexter", list_text)

            # 3. Cancel with /cancel_schedule 1
            msg.reply_text.reset_mock()
            context.args = ["1"]
            await cmd_cancel_schedule(update, context)
            self.assertIn("cancelado com sucesso", msg.reply_text.call_args[0][0])

            # 4. View again -> empty
            msg.reply_text.reset_mock()
            await cmd_schedules(update, context)
            self.assertIn("Nenhum agendamento ativo", msg.reply_text.call_args[0][0])

    async def test_scheduler_service_execute_job(self):
        job = self.store.add_schedule(
            chat_id=999,
            prompt="Download episode",
            schedule_type="once",
            expression="1s",
            human_desc="Test once",
        )
        service = SchedulerService(store=self.store, check_interval=1)
        mock_status_msg = MagicMock()
        mock_status_msg.edit_text = AsyncMock()
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock(return_value=mock_status_msg)

        mock_session = MagicMock()
        mock_session.lock = asyncio.Lock()
        mock_session.cwd = "/tmp"
        mock_session.mode = None
        mock_session.model = None
        mock_session.effort = None
        mock_session.runner.run = AsyncMock()

        mock_isolated_runner = MagicMock()
        mock_isolated_runner.run = AsyncMock(return_value={"status": "SUCCESS", "response": "Torrent added"})
        mock_isolated_runner.is_running = False
        service.create_runner = MagicMock(return_value=mock_isolated_runner)

        mock_session_manager = MagicMock()
        mock_session_manager.get_session.return_value = mock_session

        service._bot = mock_bot
        service._session_manager = mock_session_manager

        await service.execute_job(job)

        # Isolated runner was used, session.runner was NEVER touched
        mock_isolated_runner.run.assert_called_once()
        mock_session.runner.run.assert_not_called()

        # Verify initial status message was sent with parse_mode='HTML'
        mock_bot.send_message.assert_called_once()
        self.assertEqual(mock_bot.send_message.call_args[1].get("parse_mode"), "HTML")

        # Verify final edit was called with parse_mode='HTML'
        mock_status_msg.edit_text.assert_called_once()
        self.assertEqual(mock_status_msg.edit_text.call_args[1].get("parse_mode"), "HTML")

        # Verify job is completed in store
        updated = self.store.get_schedule(job.id)
        self.assertEqual(updated.status, "completed")
        self.assertIsNotNone(updated.last_run)

    async def test_scheduler_service_execute_job_with_markdown_and_underscores(self):
        """Verify markdown with underscores and code blocks is properly converted to HTML using isolated runner."""
        job = self.store.add_schedule(
            chat_id=999,
            prompt="Download episode",
            schedule_type="once",
            expression="1s",
            human_desc="Test once",
        )
        service = SchedulerService(store=self.store, check_interval=1)
        mock_status_msg = MagicMock()
        mock_status_msg.edit_text = AsyncMock()
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock(return_value=mock_status_msg)

        mock_session = MagicMock()
        mock_session.lock = asyncio.Lock()
        mock_session.cwd = "/tmp"
        mock_session.mode = None
        mock_session.model = None
        mock_session.effort = None
        mock_session.runner.run = AsyncMock()

        mock_isolated_runner = MagicMock()
        # Response with characters that would crash raw Telegram Markdown
        mock_isolated_runner.run = AsyncMock(return_value={
            "status": "SUCCESS",
            "response": "Downloaded file: `dexter_s01e01_1080p.mkv` in /path_with_underscores/.\n**Result**: Done!",
        })
        mock_isolated_runner.is_running = False
        service.create_runner = MagicMock(return_value=mock_isolated_runner)

        mock_session_manager = MagicMock()
        mock_session_manager.get_session.return_value = mock_session

        service._bot = mock_bot
        service._session_manager = mock_session_manager

        await service.execute_job(job)

        mock_isolated_runner.run.assert_called_once()
        mock_session.runner.run.assert_not_called()

        mock_status_msg.edit_text.assert_called_once()
        edit_kwargs = mock_status_msg.edit_text.call_args[1]
        self.assertEqual(edit_kwargs.get("parse_mode"), "HTML")
        edited_text = mock_status_msg.edit_text.call_args[0][0]
        # Should be valid HTML: <code> tags instead of raw markdown backticks, <b> for bold
        self.assertIn("<code>dexter_s01e01_1080p.mkv</code>", edited_text)
        self.assertIn("<b>Result</b>", edited_text)

    async def test_scheduler_service_cancel_running_job(self):
        """Verify cancel_running_job cancels the active runner and cleans up tracking."""
        service = SchedulerService(store=self.store, check_interval=1)
        mock_runner = MagicMock()
        mock_runner.is_running = True
        mock_runner.cancel = AsyncMock(return_value=True)

        service._active_runners[42] = mock_runner

        # Cancel existing job
        res = await service.cancel_running_job(42)
        self.assertTrue(res)
        mock_runner.cancel.assert_called_once()

        # Non-existent job
        res_nonexistent = await service.cancel_running_job(999)
        self.assertFalse(res_nonexistent)

    async def test_scheduler_service_creates_forum_topic_in_forum_supergroup(self):
        """Verify execute_job creates dedicated topic when running in FORUM_GROUP_ID."""
        from src.config import FORUM_GROUP_ID
        chat_id = FORUM_GROUP_ID if FORUM_GROUP_ID else -1004330361961

        job = self.store.add_schedule(
            chat_id=chat_id,
            prompt="Baixar VisionQuest S01E01",
            schedule_type="once",
            expression="1s",
            human_desc="VisionQuest Download",
        )
        self.assertIsNone(job.forum_topic_id)

        service = SchedulerService(store=self.store, check_interval=1)
        mock_status_msg = MagicMock()
        mock_status_msg.edit_text = AsyncMock()
        mock_bot = MagicMock()
        mock_bot.send_message = AsyncMock(return_value=mock_status_msg)

        mock_topic = MagicMock()
        mock_topic.message_thread_id = 777

        mock_session = MagicMock()
        mock_session.lock = asyncio.Lock()
        mock_session.cwd = "/tmp"
        mock_session.mode = None
        mock_session.model = None
        mock_session.effort = None
        mock_session.background_runners = []

        mock_isolated_runner = MagicMock()
        mock_isolated_runner.run = AsyncMock(return_value={"status": "SUCCESS", "response": "Downloaded"})
        mock_isolated_runner.is_running = False
        service.create_runner = MagicMock(return_value=mock_isolated_runner)

        mock_session_manager = MagicMock()
        mock_session_manager.get_session.return_value = mock_session

        service._bot = mock_bot
        service._session_manager = mock_session_manager

        with patch("src.config.FORUM_GROUP_ID", chat_id), \
             patch("src.handlers.forum_helper.create_forum_topic_with_retry", AsyncMock(return_value=mock_topic)) as mock_create_topic, \
             patch("src.handlers.forum_helper.unpin_topic_messages_quietly", AsyncMock()):
            await service.execute_job(job)

            mock_create_topic.assert_called_once()
            # Verify status message sent with message_thread_id=777
            self.assertEqual(mock_bot.send_message.call_args[1].get("message_thread_id"), 777)
            # Verify schedule store was updated with forum_topic_id
            updated = self.store.get_schedule(job.id)
            self.assertEqual(updated.forum_topic_id, 777)

    async def test_cancel_active_jobs_for_chat(self):
        """Verify cancel_active_jobs_for_chat cancels runners matching chat and thread."""
        service = SchedulerService(store=self.store, check_interval=1)
        runner1 = MagicMock(is_running=True, cancel=AsyncMock(return_value=True))
        runner2 = MagicMock(is_running=True, cancel=AsyncMock(return_value=True))

        service._active_runners[10] = runner1
        service._active_job_threads[10] = (12345, 555)

        service._active_runners[20] = runner2
        service._active_job_threads[20] = (12345, 999)

        # Cancel thread 555
        cancelled = await service.cancel_active_jobs_for_chat(12345, 555)
        self.assertEqual(cancelled, [10])
        runner1.cancel.assert_called_once()
        runner2.cancel.assert_not_called()

        # Cancel chat 12345 (thread None cancels all in that chat)
        cancelled_all = await service.cancel_active_jobs_for_chat(12345, None)
        self.assertEqual(cancelled_all, [20])
        runner2.cancel.assert_called_once()


if __name__ == "__main__":
    unittest.main()
