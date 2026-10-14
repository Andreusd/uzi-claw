"""CLI helper tool to manage UziClaw schedules programmatically and via LLM tools."""

import argparse
import json
import sys
from pathlib import Path

# Ensure project root is in sys.path
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.scheduler import schedule_store, format_timestamp, compute_next_run


def cmd_add(args: argparse.Namespace) -> int:
    try:
        schedule = schedule_store.add_schedule(
            chat_id=args.chat_id,
            prompt=args.prompt,
            schedule_type=args.type,
            expression=args.expr,
            human_desc=args.desc or "",
            target_cwd=args.cwd or None,
        )
        out = {
            "status": "success",
            "id": schedule.id,
            "chat_id": schedule.chat_id,
            "type": schedule.schedule_type,
            "expression": schedule.expression,
            "human_desc": schedule.human_desc,
            "next_run": format_timestamp(schedule.next_run),
            "prompt": schedule.prompt,
        }
        if args.json:
            print(json.dumps(out, ensure_ascii=False, indent=2))
        else:
            print(f"✅ Agendamento #{schedule.id} criado com sucesso!")
            print(f"📌 Descrição: {schedule.human_desc}")
            print(f"⏰ Próxima execução: {format_timestamp(schedule.next_run)}")
            print(f"📝 Prompt: {schedule.prompt}")
        return 0
    except Exception as e:
        err = {"status": "error", "message": str(e)}
        if args.json:
            print(json.dumps(err, ensure_ascii=False))
        else:
            print(f"❌ Erro ao criar agendamento: {e}", file=sys.stderr)
        return 1


def cmd_list(args: argparse.Namespace) -> int:
    schedules = schedule_store.list_schedules(
        chat_id=args.chat_id,
        status=args.status if args.status != "all" else None,
    )
    items = []
    for s in schedules:
        items.append({
            "id": s.id,
            "chat_id": s.chat_id,
            "type": s.schedule_type,
            "expression": s.expression,
            "human_desc": s.human_desc,
            "next_run": format_timestamp(s.next_run),
            "last_run": format_timestamp(s.last_run),
            "status": s.status,
            "prompt": s.prompt,
        })

    if args.json:
        print(json.dumps(items, ensure_ascii=False, indent=2))
    else:
        if not items:
            print("Nenhum agendamento encontrado.")
            return 0
        print(f"📅 Total de agendamentos: {len(items)}\n")
        for it in items:
            status_emoji = "🟢" if it["status"] == "active" else "⚪"
            print(f"{status_emoji} #{it['id']} [{it['type'].upper()}] {it['human_desc']}")
            print(f"   Próxima: {it['next_run']} | Última: {it['last_run']}")
            print(f"   Prompt: {it['prompt']}")
            print(f"   Status: {it['status']}\n")
    return 0


def cmd_cancel(args: argparse.Namespace) -> int:
    success = schedule_store.cancel_schedule(args.id, chat_id=args.chat_id)
    if success:
        out = {"status": "success", "message": f"Agendamento #{args.id} cancelado."}
        if args.json:
            print(json.dumps(out))
        else:
            print(f"✅ Agendamento #{args.id} cancelado com sucesso.")
        return 0
    else:
        out = {"status": "error", "message": f"Agendamento #{args.id} não encontrado."}
        if args.json:
            print(json.dumps(out))
        else:
            print(f"❌ Agendamento #{args.id} não encontrado ou já cancelado.", file=sys.stderr)
        return 1


def cmd_delete(args: argparse.Namespace) -> int:
    success = schedule_store.delete_schedule(args.id, chat_id=args.chat_id)
    if success:
        out = {"status": "success", "message": f"Agendamento #{args.id} excluído."}
        if args.json:
            print(json.dumps(out))
        else:
            print(f"✅ Agendamento #{args.id} excluído com sucesso.")
        return 0
    else:
        out = {"status": "error", "message": f"Agendamento #{args.id} não encontrado."}
        if args.json:
            print(json.dumps(out))
        else:
            print(f"❌ Agendamento #{args.id} não encontrado.", file=sys.stderr)
        return 1


def main() -> None:
    common_parent = argparse.ArgumentParser(add_help=False)
    common_parent.add_argument("--json", action="store_true", help="Output in JSON format")

    parser = argparse.ArgumentParser(description="UziClaw Schedule Helper CLI", parents=[common_parent])
    subparsers = parser.add_subparsers(dest="command", required=True)

    # ADD
    p_add = subparsers.add_parser("add", parents=[common_parent], help="Add a new schedule")
    p_add.add_argument("--chat-id", type=int, required=True, help="Telegram Chat ID")
    p_add.add_argument("--prompt", type=str, required=True, help="Prompt/task to execute")
    p_add.add_argument("--type", choices=["cron", "once"], default="cron", help="Schedule type")
    p_add.add_argument("--expr", type=str, required=True, help="Cron expression or offset/datetime")
    p_add.add_argument("--desc", type=str, default="", help="Human friendly description")
    p_add.add_argument("--cwd", type=str, default="", help="Target working directory")

    # LIST
    p_list = subparsers.add_parser("list", parents=[common_parent], help="List schedules")
    p_list.add_argument("--chat-id", type=int, default=None, help="Filter by Chat ID")
    p_list.add_argument("--status", choices=["active", "completed", "cancelled", "all"], default="active")

    # CANCEL
    p_cancel = subparsers.add_parser("cancel", parents=[common_parent], help="Cancel a schedule")
    p_cancel.add_argument("--id", type=int, required=True, help="Schedule ID")
    p_cancel.add_argument("--chat-id", type=int, default=None, help="Validate Chat ID")

    # DELETE
    p_delete = subparsers.add_parser("delete", parents=[common_parent], help="Permanently delete a schedule")
    p_delete.add_argument("--id", type=int, required=True, help="Schedule ID")
    p_delete.add_argument("--chat-id", type=int, default=None, help="Validate Chat ID")

    args = parser.parse_args()

    if args.command == "add":
        sys.exit(cmd_add(args))
    elif args.command == "list":
        sys.exit(cmd_list(args))
    elif args.command == "cancel":
        sys.exit(cmd_cancel(args))
    elif args.command == "delete":
        sys.exit(cmd_delete(args))


if __name__ == "__main__":
    main()
