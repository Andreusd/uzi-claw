"""Scheduler system for recurring and one-off tasks in UziClaw."""

import asyncio
import logging
import re
import sqlite3
import time
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any

from croniter import croniter

from .config import SCHEDULES_DB_PATH, SCHEDULE_CHECK_INTERVAL, INJECT_TELEGRAM_CONTEXT
from .formatters import split_message, safe_reply, safe_edit, RateLimitedMessageUpdater

logger = logging.getLogger(__name__)


@dataclass
class Schedule:
    id: int
    chat_id: int
    prompt: str
    schedule_type: str  # "cron" | "once"
    expression: str     # cron expression (e.g. "0 18 * * 5") or ISO timestamp / offset
    human_desc: str
    next_run: float     # Unix timestamp
    last_run: Optional[float]
    created_at: float
    status: str         # "active" | "completed" | "cancelled"
    target_cwd: Optional[str] = None

    def is_active(self) -> bool:
        return self.status == "active"


def parse_relative_time(expr: str, base_time: Optional[float] = None) -> Optional[float]:
    """Parse relative time offset such as '30s', '10m', '2h', '1d', or seconds string."""
    clean = expr.strip().lower()
    base = base_time if base_time is not None else time.time()

    units = {
        "s": 1,
        "m": 60,
        "h": 3600,
        "d": 86400,
        "w": 604800,
    }

    match = re.match(r"^(\d+)\s*([smhdw]?)$", clean)
    if not match:
        return None

    val = int(match.group(1))
    unit = match.group(2) or "s"
    multiplier = units.get(unit, 1)
    return base + (val * multiplier)


def parse_datetime_str(expr: str) -> Optional[float]:
    """Parse common datetime strings into Unix timestamp."""
    clean = expr.strip()
    formats = [
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
    ]
    for fmt in formats:
        try:
            dt = datetime.strptime(clean, fmt)
            # Interpret as local time
            return dt.astimezone().timestamp()
        except ValueError:
            continue
    return None


def compute_next_run(schedule_type: str, expression: str, base_time: Optional[float] = None) -> Optional[float]:
    """Compute next run timestamp based on type and expression."""
    base = base_time if base_time is not None else time.time()
    stype = schedule_type.strip().lower()

    if stype == "cron":
        try:
            # croniter expects datetime object in local time
            dt_base = datetime.fromtimestamp(base)
            itr = croniter(expression.strip(), dt_base)
            next_dt = itr.get_next(datetime)
            return next_dt.timestamp()
        except Exception as e:
            logger.error("Failed to parse cron expression '%s': %s", expression, e)
            return None

    elif stype == "once":
        # Check relative offset first (e.g. 10m, 2h)
        rel = parse_relative_time(expression, base_time=base)
        if rel is not None:
            return rel

        # Check explicit timestamp (digits)
        try:
            val = float(expression.strip())
            return val
        except ValueError:
            pass

        # Check datetime strings
        dt_val = parse_datetime_str(expression)
        if dt_val is not None:
            return dt_val

        return None

    return None


def format_timestamp(ts: Optional[float]) -> str:
    """Format Unix timestamp into friendly readable local string."""
    if ts is None or ts <= 0:
        return "N/A"
    try:
        dt = datetime.fromtimestamp(ts)
        return dt.strftime("%d/%m/%Y %H:%M:%S")
    except Exception:
        return str(ts)


class ScheduleStore:
    """SQLite-backed persistent store for schedules."""

    def __init__(self, db_path: Path = SCHEDULES_DB_PATH):
        self.db_path = db_path
        self._ensure_db()

    from contextlib import contextmanager

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.db_path)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def _ensure_db(self) -> None:
        """Create database directory and table schema if not existing."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS schedules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    chat_id INTEGER NOT NULL,
                    prompt TEXT NOT NULL,
                    schedule_type TEXT NOT NULL,
                    expression TEXT NOT NULL,
                    human_desc TEXT NOT NULL,
                    next_run REAL NOT NULL,
                    last_run REAL,
                    created_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    target_cwd TEXT
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_schedules_next_run 
                ON schedules(status, next_run);
            """)

    def add_schedule(
        self,
        chat_id: int,
        prompt: str,
        schedule_type: str,
        expression: str,
        human_desc: str = "",
        target_cwd: Optional[str] = None,
    ) -> Schedule:
        """Add and persist a new schedule."""
        now = time.time()
        next_run = compute_next_run(schedule_type, expression, base_time=now)
        if next_run is None:
            raise ValueError(f"Invalid schedule expression '{expression}' for type '{schedule_type}'")

        if not human_desc:
            if schedule_type == "cron":
                human_desc = f"Recorrente (cron: {expression})"
            else:
                human_desc = f"Disparo único em {format_timestamp(next_run)}"

        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO schedules (
                    chat_id, prompt, schedule_type, expression, human_desc,
                    next_run, last_run, created_at, status, target_cwd
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    chat_id,
                    prompt.strip(),
                    schedule_type.strip().lower(),
                    expression.strip(),
                    human_desc.strip(),
                    next_run,
                    None,
                    now,
                    "active",
                    target_cwd,
                ),
            )
            sched_id = cursor.lastrowid

        return Schedule(
            id=sched_id,
            chat_id=chat_id,
            prompt=prompt.strip(),
            schedule_type=schedule_type.strip().lower(),
            expression=expression.strip(),
            human_desc=human_desc.strip(),
            next_run=next_run,
            last_run=None,
            created_at=now,
            status="active",
            target_cwd=target_cwd,
        )

    def list_schedules(self, chat_id: Optional[int] = None, status: Optional[str] = "active") -> List[Schedule]:
        """List schedules filtered optionally by chat_id and status."""
        query = "SELECT id, chat_id, prompt, schedule_type, expression, human_desc, next_run, last_run, created_at, status, target_cwd FROM schedules WHERE 1=1"
        params: List[Any] = []

        if chat_id is not None:
            query += " AND chat_id = ?"
            params.append(chat_id)

        if status is not None:
            query += " AND status = ?"
            params.append(status)

        query += " ORDER BY next_run ASC"

        results = []
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            for row in cursor.fetchall():
                results.append(
                    Schedule(
                        id=row[0],
                        chat_id=row[1],
                        prompt=row[2],
                        schedule_type=row[3],
                        expression=row[4],
                        human_desc=row[5],
                        next_run=row[6],
                        last_run=row[7],
                        created_at=row[8],
                        status=row[9],
                        target_cwd=row[10],
                    )
                )
        return results

    def get_schedule(self, schedule_id: int) -> Optional[Schedule]:
        """Fetch schedule by its primary key ID."""
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT id, chat_id, prompt, schedule_type, expression, human_desc, next_run, last_run, created_at, status, target_cwd FROM schedules WHERE id = ?",
                (schedule_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return Schedule(
                id=row[0],
                chat_id=row[1],
                prompt=row[2],
                schedule_type=row[3],
                expression=row[4],
                human_desc=row[5],
                next_run=row[6],
                last_run=row[7],
                created_at=row[8],
                status=row[9],
                target_cwd=row[10],
            )

    def cancel_schedule(self, schedule_id: int, chat_id: Optional[int] = None) -> bool:
        """Mark schedule as cancelled."""
        query = "UPDATE schedules SET status = 'cancelled' WHERE id = ?"
        params: List[Any] = [schedule_id]
        if chat_id is not None:
            query += " AND chat_id = ?"
            params.append(chat_id)

        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return cursor.rowcount > 0

    def delete_schedule(self, schedule_id: int, chat_id: Optional[int] = None) -> bool:
        """Permanently delete a schedule from database."""
        query = "DELETE FROM schedules WHERE id = ?"
        params: List[Any] = [schedule_id]
        if chat_id is not None:
            query += " AND chat_id = ?"
            params.append(chat_id)

        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(query, params)
            return cursor.rowcount > 0

    def update_schedule_after_run(self, schedule_id: int, next_run: Optional[float], status: str = "active") -> None:
        """Update last_run timestamp, next_run, and status after execution."""
        now = time.time()
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE schedules
                SET last_run = ?, next_run = ?, status = ?
                WHERE id = ?
                """,
                (now, next_run if next_run is not None else 0.0, status, schedule_id),
            )

    def get_due_schedules(self, now_ts: Optional[float] = None) -> List[Schedule]:
        """Fetch all active schedules whose next_run <= now_ts."""
        now = now_ts if now_ts is not None else time.time()
        with self._connect() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, chat_id, prompt, schedule_type, expression, human_desc, next_run, last_run, created_at, status, target_cwd
                FROM schedules
                WHERE status = 'active' AND next_run <= ?
                ORDER BY next_run ASC
                """,
                (now,),
            )
            results = []
            for row in cursor.fetchall():
                results.append(
                    Schedule(
                        id=row[0],
                        chat_id=row[1],
                        prompt=row[2],
                        schedule_type=row[3],
                        expression=row[4],
                        human_desc=row[5],
                        next_run=row[6],
                        last_run=row[7],
                        created_at=row[8],
                        status=row[9],
                        target_cwd=row[10],
                    )
                )
            return results


schedule_store = ScheduleStore()


class SchedulerService:
    """Background engine that periodically evaluates and executes due schedules."""

    def __init__(self, store: ScheduleStore = schedule_store, check_interval: int = SCHEDULE_CHECK_INTERVAL):
        self.store = store
        self.check_interval = check_interval
        self._running = False
        self._task: Optional[asyncio.Task] = None
        self._bot = None
        self._session_manager = None

    def start(self, bot, session_manager) -> None:
        """Start the background scheduler task."""
        if self._running:
            return
        self._bot = bot
        self._session_manager = session_manager
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("SchedulerService started (check_interval=%ds, db=%s)", self.check_interval, self.store.db_path)

    async def stop(self) -> None:
        """Stop the background scheduler gracefully."""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("SchedulerService stopped.")

    async def _run_loop(self) -> None:
        """Main evaluation loop."""
        while self._running:
            try:
                await self._check_and_trigger_due_jobs()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Error in SchedulerService loop: %s", e, exc_info=True)

            try:
                await asyncio.sleep(self.check_interval)
            except asyncio.CancelledError:
                break

    async def _check_and_trigger_due_jobs(self) -> None:
        """Find and trigger due jobs."""
        now = time.time()
        due_jobs = self.store.get_due_schedules(now_ts=now)
        for job in due_jobs:
            if not self._running:
                break
            # Run job in background task to not block checking others
            asyncio.create_task(self.execute_job(job))

    async def execute_job(self, job: Schedule, is_manual: bool = False) -> None:
        """Execute a single scheduled job."""
        logger.info("Executing scheduled job #%d (%s): %s", job.id, job.human_desc, job.prompt)

        # 1. Update schedule state immediately to avoid double firing
        now = time.time()
        if job.schedule_type == "cron":
            next_run = compute_next_run("cron", job.expression, base_time=now)
            self.store.update_schedule_after_run(job.id, next_run=next_run, status="active")
        else:
            self.store.update_schedule_after_run(job.id, next_run=None, status="completed")

        if not self._bot or not self._session_manager:
            logger.error("SchedulerService bot or session_manager not initialized.")
            return

        session = self._session_manager.get_session(job.chat_id)
        tag = "[Manual Trigger]" if is_manual else "[Agendado]"

        # 2. Acquire session lock and execute via runner
        async with session.lock:
            session.is_busy = True
            cwd = job.target_cwd or session.cwd

            # Notify Telegram that the scheduled task actually started
            status_text = f"⏰ *{tag} Tarefa #{job.id} Iniciada:*\n_{job.human_desc or job.prompt}_\n\n⏳ Executando..."
            try:
                status_msg = await self._bot.send_message(
                    chat_id=job.chat_id,
                    text=status_text,
                    parse_mode="Markdown",
                )
            except Exception as e:
                logger.error("Failed to send schedule start notification to chat_id=%s: %s", job.chat_id, e)
                status_msg = None

            # Context injection
            full_prompt = (
                f"[Context: Scheduled Task #{job.id} triggered automatically via UziClaw Scheduler. "
                f"Description: {job.human_desc}. "
                f"Please execute the requested action and report concise results to the user.]\n\n"
                f"{job.prompt}"
            )

            updater = RateLimitedMessageUpdater(status_msg, min_interval=1.5) if status_msg else None
            streaming_text: list[str] = []
            current_tool: str = ""

            async def on_event(event_type: str, data: Any) -> None:
                if not updater:
                    return
                nonlocal current_tool
                if event_type == "tool_start":
                    tname = data.get("name", "tool")
                    current_tool = tname
                    await updater.update(f"⏰ *{tag} Tarefa #{job.id}:*\n⚙️ Running tool: `{tname}`...")
                elif event_type == "tool_done":
                    current_tool = ""
                    await updater.update(f"⏰ *{tag} Tarefa #{job.id}:*\n🤖 Processando resultados...")
                elif event_type == "text_delta":
                    streaming_text.append(data)
                    accumulated = "".join(streaming_text)
                    if len(accumulated) > 10 and not current_tool:
                        preview = accumulated if len(accumulated) < 1200 else "..." + accumulated[-1200:]
                        await updater.update(f"⏰ *{tag} Tarefa #{job.id}:*\n💬 {preview}")

            try:
                # Use a fresh turn for scheduled runs
                result = await session.runner.run(
                    prompt=full_prompt,
                    conversation_id=None,
                    cwd=cwd,
                    autopilot=True,
                    mode=session.mode,
                    model=session.model,
                    effort=session.effort,
                    event_callback=on_event,
                )

                response_text = result.get("response", "").strip()
                status = result.get("status", "SUCCESS")
                error_msg = result.get("error")

                if status == "SUCCESS":
                    completion_header = f"✅ *[Tarefa #{job.id} Concluída]*\n\n"
                    final_text = completion_header + (response_text or "Operação finalizada com sucesso.")
                elif status == "CANCELLED":
                    final_text = f"⏹️ *[Tarefa #{job.id}]* Execução cancelada."
                else:
                    final_text = f"❌ *[Tarefa #{job.id} Falhou]*: {error_msg or 'Erro desconhecido.'}"

                # Send final result
                chunks = split_message(final_text)
                if status_msg and chunks:
                    await safe_edit(status_msg, chunks[0])
                    for chunk in chunks[1:]:
                        await self._bot.send_message(chat_id=job.chat_id, text=chunk, parse_mode="Markdown")
                else:
                    for chunk in chunks:
                        await self._bot.send_message(chat_id=job.chat_id, text=chunk, parse_mode="Markdown")

            except Exception as e:
                logger.error("Exception during scheduled task execution #%d: %s", job.id, e, exc_info=True)
                err_text = f"❌ *[Tarefa #{job.id} Erro]*: `{e}`"
                if status_msg:
                    await safe_edit(status_msg, err_text)
                else:
                    await self._bot.send_message(chat_id=job.chat_id, text=err_text, parse_mode="Markdown")
            finally:
                session.is_busy = False


scheduler_service = SchedulerService()
