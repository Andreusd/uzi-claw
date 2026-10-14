"""Schedule management command handlers and callbacks for UziClaw."""

import asyncio
import logging
import re
import sys
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes

from ..auth import restricted
from ..formatters import safe_reply
from ..scheduler import (
    schedule_store as default_schedule_store,
    scheduler_service,
    format_timestamp,
    parse_relative_time,
)
from .commands import get_session_from_update

logger = logging.getLogger(__name__)


def _get_schedule_store():
    """Retrieve schedule_store, honoring unittest monkeypatches on src.bot.schedule_store."""
    bot_mod = sys.modules.get("src.bot")
    if bot_mod and hasattr(bot_mod, "schedule_store"):
        return getattr(bot_mod, "schedule_store")
    return default_schedule_store


@restricted
async def cmd_schedules(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """View and manage scheduled tasks programmatically (no LLM invocation)."""
    msg = update.effective_message
    if not msg:
        return

    store = _get_schedule_store()
    chat_id = update.effective_chat.id
    schedules = store.list_schedules(chat_id=chat_id, status="active")
    if not schedules:
        await safe_reply(
            msg,
            "📅 **Nenhum agendamento ativo no momento.**\n\n"
            "💡 Você pode agendar conversando normalmente (ex: _'Toda sexta às 18h baixe o novo ep de Dexter'_) "
            "ou usar o comando direto: `/schedule \"0 18 * * 5\" Prompt do torrent`.",
        )
        return

    lines = [f"📅 **Agendamentos Ativos ({len(schedules)}):**\n"]
    inline_keyboard = []

    for s in schedules:
        stype_label = "🔄 Recorrente" if s.schedule_type == "cron" else "⏱️ Disparo único"
        preview_prompt = s.prompt if len(s.prompt) <= 80 else s.prompt[:77] + "..."
        lines.append(
            f"**#{s.id}** • {stype_label}\n"
            f"📌 _{s.human_desc}_\n"
            f"⏰ Próxima: `{format_timestamp(s.next_run)}`\n"
            f"📝 Tarefa: `{preview_prompt}`\n"
            f"👉 /cancel_schedule_{s.id} | /run_schedule_{s.id}\n"
        )
        inline_keyboard.append([
            InlineKeyboardButton(f"❌ Cancelar #{s.id}", callback_data=f"sched_cancel:{s.id}"),
            InlineKeyboardButton(f"▶️ Executar #{s.id}", callback_data=f"sched_run:{s.id}"),
        ])

    keyboard = InlineKeyboardMarkup(inline_keyboard) if inline_keyboard else None
    await safe_reply(msg, "\n".join(lines), reply_markup=keyboard)


@restricted
async def cmd_cancel_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel a scheduled task by ID programmatically (zero LLM)."""
    msg = update.effective_message
    if not msg:
        return

    if not context.args:
        await safe_reply(msg, "Usage: `/cancel_schedule <id>` (ex: `/cancel_schedule 1`)")
        return

    raw_arg = context.args[0].strip()
    if not raw_arg.isdigit():
        await safe_reply(msg, f"⚠️ ID inválido `{raw_arg}`. O ID deve ser um número inteiro.")
        return

    store = _get_schedule_store()
    sched_id = int(raw_arg)
    chat_id = update.effective_chat.id
    await scheduler_service.cancel_running_job(sched_id)
    success = store.cancel_schedule(sched_id, chat_id=chat_id)
    if success:
        await safe_reply(msg, f"✅ Agendamento **#{sched_id}** cancelado com sucesso.")
    else:
        await safe_reply(msg, f"⚠️ Agendamento **#{sched_id}** não encontrado ou já cancelado.")


@restricted
async def cmd_run_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Trigger a scheduled task immediately for testing."""
    msg = update.effective_message
    if not msg:
        return

    if not context.args:
        await safe_reply(msg, "Usage: `/run_schedule <id>` (ex: `/run_schedule 1`)")
        return

    raw_arg = context.args[0].strip()
    if not raw_arg.isdigit():
        await safe_reply(msg, f"⚠️ ID inválido `{raw_arg}`. O ID deve ser um número inteiro.")
        return

    store = _get_schedule_store()
    sched_id = int(raw_arg)
    schedule = store.get_schedule(sched_id)
    if not schedule:
        await safe_reply(msg, f"⚠️ Agendamento **#{sched_id}** não encontrado.")
        return

    await safe_reply(msg, f"▶️ Disparando execução manual do agendamento **#{sched_id}**...")
    asyncio.create_task(scheduler_service.execute_job(schedule, is_manual=True))


@restricted
async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Create a scheduled task directly without invoking LLM."""
    msg = update.effective_message
    if not msg:
        return

    full_text = msg.text.strip()
    parts = full_text.split(None, 1)
    if len(parts) < 2:
        await safe_reply(
            msg,
            "📌 **Como usar /schedule (direto):**\n\n"
            "• **Recorrente (Cron):**\n"
            "  `/schedule \"0 18 * * 5\" Procurar torrent novo Dexter`\n\n"
            "• **Disparo único relativo:**\n"
            "  `/schedule 2h Verificar se terminou o download`\n"
            "  `/schedule 30m Me lembre de olhar o qBittorrent`\n\n"
            "• **Ver agendamentos:** `/schedules`\n"
            "• **Cancelar:** `/cancel_schedule <id>`",
        )
        return

    body = parts[1].strip()
    cron_match = re.match(r'^["\']([^"\']+)["\']\s+(.+)$', body, re.DOTALL)

    if cron_match:
        expr = cron_match.group(1).strip()
        prompt = cron_match.group(2).strip()
        sched_type = "cron"
        desc = f"Recorrente: {expr}"
    else:
        tokens = body.split(None, 1)
        first_token = tokens[0].strip()
        prompt = tokens[1].strip() if len(tokens) > 1 else ""

        if parse_relative_time(first_token) is not None:
            sched_type = "once"
            expr = first_token
            desc = f"Disparo único em {first_token}"
        elif len(first_token.split()) == 5:
            sched_type = "cron"
            expr = first_token
            desc = f"Recorrente: {expr}"
        else:
            await safe_reply(
                msg,
                "⚠️ Formato não reconhecido.\n"
                "Exemplo cron: `/schedule \"0 18 * * 5\" Meu prompt`\n"
                "Exemplo offset: `/schedule 2h Meu prompt`",
            )
            return

    try:
        store = _get_schedule_store()
        session = get_session_from_update(update)
        schedule = store.add_schedule(
            chat_id=update.effective_chat.id,
            prompt=prompt,
            schedule_type=sched_type,
            expression=expr,
            human_desc=desc,
            target_cwd=session.cwd,
        )
        await safe_reply(
            msg,
            f"✅ **Agendamento #{schedule.id} Criado!**\n\n"
            f"• **Frequência:** `{schedule.human_desc}`\n"
            f"• **Próxima execução:** `{format_timestamp(schedule.next_run)}`\n"
            f"• **Tarefa:** `{schedule.prompt}`\n\n"
            f"👉 Ver todos: `/schedules` | Cancelar: `/cancel_schedule_{schedule.id}`",
        )
    except Exception as e:
        logger.error("Error creating schedule via command: %s", e)
        await safe_reply(msg, f"❌ Erro ao criar agendamento: `{e}`")


@restricted
async def handle_schedule_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle inline button clicks from /schedules list."""
    query = update.callback_query
    if not query or not query.data:
        return

    await query.answer()
    data = query.data
    chat_id = update.effective_chat.id
    store = _get_schedule_store()

    if data.startswith("sched_cancel:"):
        sched_id_str = data.split(":", 1)[1]
        if sched_id_str.isdigit():
            sched_id = int(sched_id_str)
            await scheduler_service.cancel_running_job(sched_id)
            success = store.cancel_schedule(sched_id, chat_id=chat_id)
            if success:
                if query.message:
                    await safe_reply(query.message, f"✅ Agendamento **#{sched_id}** cancelado com sucesso.")
            else:
                if query.message:
                    await safe_reply(query.message, f"⚠️ Agendamento **#{sched_id}** já havia sido cancelado.")

    elif data.startswith("sched_run:"):
        sched_id_str = data.split(":", 1)[1]
        if sched_id_str.isdigit():
            sched_id = int(sched_id_str)
            schedule = store.get_schedule(sched_id)
            if schedule and query.message:
                await safe_reply(query.message, f"▶️ Disparando agendamento **#{sched_id}** manualmente...")
                asyncio.create_task(scheduler_service.execute_job(schedule, is_manual=True))
