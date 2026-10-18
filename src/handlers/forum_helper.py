"""Forum topics helpers, isolation logic, and title generation for UziClaw."""

import asyncio
import inspect
import logging
import re
from datetime import datetime
from typing import Optional, Any
import httpx
from telegram import Message, Chat
from telegram.error import TimedOut, NetworkError, RetryAfter

from ..config import FORUM_GROUP_ID

logger = logging.getLogger(__name__)

# Telegram Forum Topic Custom Emoji IDs (from official topic icons sticker pack)
# Telegram Forum Topic Custom Emoji IDs (from official topic icons sticker pack)
FORUM_TOPIC_ICONS = {
    "chat": "5417915203100613993",     # 💬
    "voice": "5377544228505134960",    # 🎙
    "image": "5310039132297242441",    # 🎨
    "audio": "5310045076531978942",    # 🎵
    "document": "5357315181649076022", # 📁
    "terminal": "5350554349074391003", # 💻
    "command": "5312016608254762256",  # ⚡️
    "code": "5350554349074391003",     # 💻
    "ai": "5309832892262654231",       # 🤖
    "brain": "5237889595894414384",    # 🧠
    "video": "5368653135101310687",    # 🎬
    "music": "5310045076531978942",    # 🎵
    "money": "5350452584119279096",    # 💰
    "stats": "5350305691942788490",    # 📈
    "notes": "5373251851074415873",    # 📝
    "books": "5350481781306958339",    # 📚
    "check": "5237699328843200968",    # ✅
    "calendar": "5433614043006903194", # 📆
    "search": "5309965701241379366",   # 🔎
    "idea": "5312536423851630001",     # 💡
    "games": "5309950797704865693",    # 🎮
    "travel": "5348436127038579546",   # ✈️
    "food": "5350444672789519765",     # 🍕
    "health": "5350307998340226571",   # 🩺
    "question": "5377316857231450742", # ❓
    "alert": "5379748062124056162",    # ❗️
}

EMOJI_TO_TOPIC_STICKER = {
    "💻": "5350554349074391003",
    "🤖": "5309832892262654231",
    "🧠": "5237889595894414384",
    "🎬": "5368653135101310687",
    "🎵": "5310045076531978942",
    "🔎": "5309965701241379366",
    "🔍": "5309965701241379366",
    "💡": "5312536423851630001",
    "📝": "5373251851074415873",
    "✍️": "5238156910363950406",
    "✍": "5238156910363950406",
    "📚": "5350481781306958339",
    "✅": "5237699328843200968",
    "📆": "5433614043006903194",
    "📅": "5433614043006903194",
    "💰": "5350452584119279096",
    "📈": "5350305691942788490",
    "📉": "5350713563512052787",
    "🎮": "5309950797704865693",
    "✈️": "5348436127038579546",
    "✈": "5348436127038579546",
    "🚗": "5312322066328853156",
    "🏠": "5312486108309757006",
    "☕️": "5350392020785437399",
    "☕": "5350392020785437399",
    "🍕": "5350444672789519765",
    "🩺": "5350307998340226571",
    "❓": "5377316857231450742",
    "❗️": "5379748062124056162",
    "🔥": "5312241539987020022",
    "⚡️": "5312016608254762256",
    "⚡": "5312016608254762256",
    "🎨": "5310039132297242441",
    "🖼️": "5310039132297242441",
    "🖼": "5310039132297242441",
    "📁": "5357315181649076022",
    "📄": "5357315181649076022",
    "🎙️": "5377544228505134960",
    "🎙": "5377544228505134960",
    "💬": "5417915203100613993",
}

# Semantic rules for classifying prompts into topic categories
CATEGORIES_RULES = [
    # (name, emoji, sticker_id, weight, compiled_regex)
    ("code", "💻", "5350554349074391003", 2, re.compile(r"\b(código|codigo|script|programar|programação|programacao|desenvolver|função|funcao|classe|class|debug|bug|erro|error|fix|patch|git|github|commit|branch|repo|repository|terminal|bash|zsh|shell|linux|fedora|ubuntu|debian|windows|macos|python|javascript|typescript|node|npm|pnpm|yarn|docker|container|compose|kubernetes|k8s|systemd|service|processo|daemon|servidor|server|ssh|nginx|caddy|apache|api|rest|fastapi|flask|django|express|sql|sqlite|postgres|postgresql|mysql|database|banco de dados|redis|orm|sqlalchemy|alembic|html|css|tailwind|react|vue|svelte|nextjs|vite|webpack|compilar|build|deploy|ci\/cd|pipeline|teste|testes|pytest|unittest|benchmark|otimizar|otimização|otimizacao|recurso|recursos|memoria|memória|cpu|ram|swap|disco|disk|cgroup|uziclaw)\b", re.IGNORECASE)),
    ("ai", "🤖", "5309832892262654231", 2, re.compile(r"\b(ia|ai|inteligência artificial|inteligencia artificial|llm|gpt|gemini|claude|chatgpt|deepseek|llama|mistral|ollama|anthropic|openai|huggingface|prompt|antigravity|agy|subagente|subagent|agente|agent|bot|assistente|machine learning|deep learning|neural|treinamento|fine-tuning|rag|embedding|embeddings|automação|automacao)\b", re.IGNORECASE)),
    ("video", "🎬", "5368653135101310687", 2, re.compile(r"\b(vídeo|video|vídeos|videos|youtube|yt|filme|filmes|cinema|movie|movies|série|serie|séries|series|episódio|episodio|temporada|season|netflix|prime video|hbo|disney|torrent|torrents|qbittorrent|legendas|legenda|stream|streaming|assistir|trailer|dexter)\b", re.IGNORECASE)),
    ("music", "🎵", "5310045076531978942", 2, re.compile(r"\b(música|musica|músicas|musicas|som|áudio|audio|mp3|wav|flac|playlist|spotify|deezer|apple music|banda|cantor|cantora|álbum|album|faixa|track|violão|guitarra|piano|bateria|ritmo|melodia|podcast)\b", re.IGNORECASE)),
    ("money", "💰", "5350452584119279096", 2, re.compile(r"\b(dinheiro|grana|custo|custos|preço|preco|preços|precos|cotação|cotacao|valor|valores|orçamento|orcamento|gasto|gastos|despesa|despesas|comprar|compra|compras|pagar|pagamento|boleto|fatura|cartão|cartao|banco|pix|investimento|investir|ações|acoes|renda|salário|salario|bitcoin|btc|ethereum|crypto|cripto|dólar|dolar|real|reais)\b", re.IGNORECASE)),
    ("stats", "📈", "5350305691942788490", 2, re.compile(r"\b(métrica|metrica|métricas|metricas|estatística|estatistica|estatísticas|estatisticas|gráfico|grafico|gráficos|graficos|analytics|dashboard|relatório|relatorio|kpi|conversão|performance|desempenho)\b", re.IGNORECASE)),
    ("notes", "📝", "5373251851074415873", 2, re.compile(r"\b(nota|notas|anotação|anotações|anotacao|anotacoes|anotar|obsidian|resumo|resumir|resuma|escrever|escrita|redigir|redação|redacao|artigo|post|blog|texto|documentação|documentacao|documento|docs|manual|tutorial|guia|readme)\b", re.IGNORECASE)),
    ("books", "📚", "5350481781306958339", 2, re.compile(r"\b(livro|livros|ler|leitura|ebook|estudo|estudos|estudar|estude|aprender|aprendizado|aula|curso|faculdade|universidade|escola|educação|educacao|disciplina|prova|exame|concurso|paper)\b", re.IGNORECASE)),
    ("check", "✅", "5237699328843200968", 2, re.compile(r"\b(tarefa|tarefas|task|tasks|todo|to-do|checklist|planejar|planejamento|plano|meta|metas|objetivo|objetivos|organizar|organização|organizacao|concluir|concluído|concluido|feito|resolver|solucionar)\b", re.IGNORECASE)),
    ("calendar", "📆", "5433614043006903194", 1, re.compile(r"\b(agenda|agendar|agendamento|calendário|calendario|horário|horario|hora|horas|amanhã|amanha|ontem|semana|mês|mes|ano|data|prazo|deadline|lembrete|lembrar|lembre|cron|rotina|alarme)\b", re.IGNORECASE)),
    ("search", "🔎", "5309965701241379366", 2, re.compile(r"\b(pesquisa|pesquisar|pesquise|busca|buscar|busque|procura|procurar|investigar|investigação|investigacao|analisar|análise|analise|verificar|checar|descobrir|encontrar|rastrear|search|find|lookup|survey|explore)\b", re.IGNORECASE)),
    ("idea", "💡", "5312536423851630001", 2, re.compile(r"\b(ideia|ideias|sugestão|sugestao|sugestões|sugestoes|sugerir|proposta|brainstorm|criatividade|pensar|insight|insights|dica|dicas|conselho|alternativa|alternativas)\b", re.IGNORECASE)),
    ("games", "🎮", "5309950797704865693", 2, re.compile(r"\b(jogo|jogos|game|games|gaming|jogar|steam|playstation|ps5|xbox|nintendo|switch|gamer|rpg|gameplay|fase|boss)\b", re.IGNORECASE)),
    ("travel", "✈️", "5348436127038579546", 2, re.compile(r"\b(viagem|viagens|viajar|viaje|turismo|voo|voos|avião|aviao|aeroporto|passagem|passagens|hotel|hospedagem|mala|roteiro|praia|carro|veículo|veiculo|automóvel|trânsito|transito|uber|ônibus|onibus|estrada)\b", re.IGNORECASE)),
    ("food", "🍕", "5350444672789519765", 2, re.compile(r"\b(comida|comer|almoço|almoco|jantar|café|cafe|lanche|restaurante|receita|receitas|cozinhar|culinária|culinaria|pizza|hambúrguer|hamburguer|bebida|cardápio|cardapio|delivery|ifood)\b", re.IGNORECASE)),
    ("health", "🩺", "5350307998340226571", 2, re.compile(r"\b(saúde|saude|médico|medico|hospital|remédio|remedio|medicamento|doença|sintoma|sintomas|dor|exame|treino|academia|exercício|exercicio|dieta|nutrição|nutricao|peso|sono)\b", re.IGNORECASE)),
    ("question", "❓", "5377316857231450742", 2, re.compile(r"\b(dúvida|duvida|dúvidas|duvidas|ajuda|ajudar|ajude|socorro|help|como fazer|como posso|o que é|o que significa|qual a diferença|por que|porque|por quê|quem é|onde fica)\b", re.IGNORECASE)),
    ("alert", "❗️", "5379748062124056162", 2, re.compile(r"\b(urgente|urgência|urgencia|emergência|emergencia|perigo|crítico|critico|alerta|atenção|atencao|cuidado|grave|falha grave)\b", re.IGNORECASE)),
]


def detect_topic_icon_from_prompt(
    prompt_text: Optional[str] = None,
    topic_type: Optional[str] = None,
) -> tuple[str, str]:
    """
    Intelligently select the topic emoji and custom emoji sticker ID based on prompt content or topic type.
    Returns: (emoji_char, custom_emoji_id)
    """
    if topic_type and topic_type in FORUM_TOPIC_ICONS:
        icon_id = FORUM_TOPIC_ICONS[topic_type]
        for em, sid in EMOJI_TO_TOPIC_STICKER.items():
            if sid == icon_id:
                return em, icon_id
        return "💬", icon_id

    if not prompt_text:
        return "💬", FORUM_TOPIC_ICONS["chat"]

    cleaned = clean_prompt_for_display(prompt_text)

    # 1. Attachment inspection
    if cleaned.startswith("[Attached Voice Note:"):
        if "User's note:" in cleaned:
            note = cleaned.split("User's note:")[-1].strip()
            if note:
                return detect_topic_icon_from_prompt(note)
        return "🎙️", FORUM_TOPIC_ICONS["voice"]

    if cleaned.startswith("[Attached Image:"):
        lines = [line.strip() for line in cleaned.splitlines() if line.strip() and not line.startswith("[Attached Image:")]
        if lines:
            return detect_topic_icon_from_prompt(" ".join(lines))
        return "🎨", FORUM_TOPIC_ICONS["image"]

    if cleaned.startswith("[Attached Audio File:"):
        if "User's note:" in cleaned:
            note = cleaned.split("User's note:")[-1].strip()
            if note:
                return detect_topic_icon_from_prompt(note)
        return "🎵", FORUM_TOPIC_ICONS["audio"]

    if cleaned.startswith("[Attached Document:"):
        doc_m = re.search(r"doc_\d+_(.+?)(?:\s+\(Use view_file|\n)", cleaned)
        doc_name = doc_m.group(1).lower() if doc_m else ""
        if any(doc_name.endswith(ext) for ext in [".py", ".js", ".ts", ".sh", ".json", ".yaml", ".yml", ".sql", ".c", ".cpp", ".rs", ".go"]):
            return "💻", FORUM_TOPIC_ICONS["code"]
        if any(doc_name.endswith(ext) for ext in [".pdf", ".epub", ".mobi"]):
            return "📚", FORUM_TOPIC_ICONS["books"]
        return "📁", FORUM_TOPIC_ICONS["document"]

    # 2. Check if the text starts with an explicit emoji
    for em, sid in EMOJI_TO_TOPIC_STICKER.items():
        if cleaned.startswith(em):
            return em, sid

    # 3. Match against semantic categories with scoring
    best_score = 0
    best_match = ("💬", FORUM_TOPIC_ICONS["chat"])

    for cat_name, emoji_char, sticker_id, weight, rx in CATEGORIES_RULES:
        matches = rx.findall(cleaned)
        score = len(matches) * weight
        if score > best_score:
            best_score = score
            best_match = (emoji_char, sticker_id)

    return best_match


def is_forum_general_chat(chat: Optional[Chat], msg: Optional[Message]) -> bool:
    """
    Check if a message was sent to the General topic of a Telegram forum supergroup.
    In Telegram Bot API, the General topic in a forum supergroup is identified when
    chat.is_forum is True, chat.type is 'supergroup', and message_thread_id is None or 1.
    """
    if not chat:
        return False

    chat_type = getattr(chat, "type", None)
    if chat_type and str(chat_type).lower() != "supergroup":
        return False

    is_forum = getattr(chat, "is_forum", False)
    if is_forum is not True:
        return False

    thread_id = getattr(msg, "message_thread_id", None) if msg else None
    is_topic = getattr(msg, "is_topic_message", False) if msg else False

    # A custom forum topic has is_topic=True and thread_id > 1
    if is_topic is True and thread_id is not None and thread_id > 1:
        return False

    return True


def clean_prompt_for_display(prompt_text: str) -> str:
    """Clean internal prompt prefixes, context injection tags, and metadata."""
    if not prompt_text:
        return ""
    text = prompt_text
    m = re.search(r"<USER_REQUEST>(.*?)</USER_REQUEST>", text, re.DOTALL)
    if m:
        text = m.group(1).strip()
    text = re.sub(r"^\[Context:.*?\]\s*", "", text, flags=re.DOTALL).strip()
    return text


def generate_topic_name(prompt_text: str, default_prefix: str = "Conversa") -> str:
    """
    Generate a clean, readable topic name for Telegram forum topic with dynamic emoji icon.
    """
    cleaned = clean_prompt_for_display(prompt_text)
    now_str = datetime.now().strftime("%d/%m %H:%M")

    emoji_char, _ = detect_topic_icon_from_prompt(prompt_text)

    if not cleaned:
        return f"{emoji_char} {default_prefix} {now_str}"

    if cleaned.startswith("[Attached Voice Note:"):
        if "User's note:" in cleaned:
            note = cleaned.split("User's note:")[-1].strip()
            title = f"{emoji_char} {note}"
        else:
            title = f"🎙️ Áudio {now_str}"
        return title[:128]

    if cleaned.startswith("[Attached Image:"):
        # Check if there is user text
        lines = [line.strip() for line in cleaned.splitlines() if line.strip() and not line.startswith("[Attached Image:")]
        if lines:
            title = f"{emoji_char} {lines[0]}"
        else:
            img_count = cleaned.count("[Attached Image:")
            img_desc = f"{img_count} Imagens" if img_count > 1 else "Imagem"
            title = f"🖼️ {img_desc} {now_str}"
        return title[:128]

    if cleaned.startswith("[Attached Audio File:"):
        if "User's note:" in cleaned:
            note = cleaned.split("User's note:")[-1].strip()
            title = f"{emoji_char} {note}"
        else:
            title = f"🎵 Áudio {now_str}"
        return title[:128]

    if cleaned.startswith("[Attached Document:"):
        doc_m = re.search(r"doc_\d+_(.+?)(?:\s+\(Use view_file|\n)", cleaned)
        if doc_m:
            title = f"{emoji_char} {doc_m.group(1)}"
        else:
            title = f"📄 Documento {now_str}"
        return title[:128]

    # Plain text: pick first non-empty line
    first_line = ""
    for line in cleaned.splitlines():
        line = line.strip()
        if line:
            first_line = line
            break

    # Strip markdown symbols
    first_line = re.sub(r"[*_`#~\[\]\(\)]", "", first_line).strip()
    if not first_line:
        first_line = f"{default_prefix} {now_str}"

    # If first_line already starts with an emoji, don't duplicate
    if any(first_line.startswith(e) for e in [emoji_char, "💬", "🎙️", "🖼️", "🎵", "📄", "💻", "🤖", "🎬"]):
        title = first_line
    else:
        title = f"{emoji_char} {first_line}"

    if len(title) > 120:
        title = title[:117] + "..."
    return title[:128]


def get_topic_icon_id(prompt_text: Optional[str] = None, topic_type: Optional[str] = None) -> str:
    """
    Get the official Telegram forum topic custom emoji ID based on topic type or prompt content.
    """
    _, icon_id = detect_topic_icon_from_prompt(prompt_text=prompt_text, topic_type=topic_type)
    return icon_id


async def create_forum_topic_with_retry(
    bot: Any,
    chat_id: int | str,
    name: str,
    icon_custom_emoji_id: Optional[str] = None,
    icon_color: Optional[int] = None,
    max_retries: int = 3,
    initial_delay: float = 1.0,
) -> Optional[Any]:
    """
    Create a forum topic with automatic retries for transient network/connection errors and rate limits.
    """
    last_err: Optional[Exception] = None
    kwargs: dict[str, Any] = {"chat_id": chat_id, "name": name}
    if icon_custom_emoji_id is not None:
        kwargs["icon_custom_emoji_id"] = icon_custom_emoji_id
    if icon_color is not None:
        kwargs["icon_color"] = icon_color

    create_fn = getattr(bot, "create_forum_topic", None)
    if not create_fn or not callable(create_fn):
        logger.error("Bot instance does not have a callable create_forum_topic method")
        return None

    for attempt in range(1, max_retries + 1):
        try:
            res = create_fn(**kwargs)
            topic = await res if inspect.isawaitable(res) else res
            return topic
        except RetryAfter as e:
            last_err = e
            delay = float(e.retry_after) + 0.5
            logger.warning(
                "Rate limit hit on create_forum_topic (attempt %d/%d). Sleeping %.1fs...",
                attempt,
                max_retries,
                delay,
            )
            await asyncio.sleep(delay)
        except (TimedOut, NetworkError, httpx.HTTPError, ConnectionError, asyncio.TimeoutError) as e:
            last_err = e
            delay = initial_delay * (2 ** (attempt - 1))
            logger.warning(
                "Network error on create_forum_topic: %s (attempt %d/%d). Retrying in %.1fs...",
                e,
                attempt,
                max_retries,
                delay,
            )
            await asyncio.sleep(delay)
        except Exception as e:
            logger.error("Non-retriable error creating forum topic for chat %s: %s", chat_id, e)
            return None

    logger.error("Failed to create forum topic after %d attempts for chat %s: %s", max_retries, chat_id, last_err)
    return None


async def edit_forum_topic_with_retry(
    bot: Any,
    chat_id: int | str,
    message_thread_id: int,
    name: Optional[str] = None,
    icon_custom_emoji_id: Optional[str] = None,
    max_retries: int = 2,
    initial_delay: float = 1.0,
) -> bool:
    """
    Edit a forum topic's name and/or icon with automatic retries for rate limits and network errors.
    """
    kwargs: dict[str, Any] = {"chat_id": chat_id, "message_thread_id": message_thread_id}
    if name is not None:
        kwargs["name"] = name[:128]
    if icon_custom_emoji_id is not None:
        kwargs["icon_custom_emoji_id"] = icon_custom_emoji_id

    edit_fn = getattr(bot, "edit_forum_topic", None)
    if not edit_fn or not callable(edit_fn):
        logger.error("Bot instance does not have a callable edit_forum_topic method")
        return False

    for attempt in range(1, max_retries + 1):
        try:
            res = edit_fn(**kwargs)
            if inspect.isawaitable(res):
                await res
            return True
        except RetryAfter as e:
            delay = float(e.retry_after) + 0.5
            logger.warning("Rate limit hit on edit_forum_topic (attempt %d/%d). Sleeping %.1fs...", attempt, max_retries, delay)
            await asyncio.sleep(delay)
        except (TimedOut, NetworkError, httpx.HTTPError, ConnectionError, asyncio.TimeoutError) as e:
            delay = initial_delay * (2 ** (attempt - 1))
            logger.warning("Network error on edit_forum_topic: %s (attempt %d/%d). Retrying in %.1fs...", e, attempt, max_retries, delay)
            await asyncio.sleep(delay)
        except Exception as e:
            logger.debug("Non-retriable error editing forum topic %s:%s: %s", chat_id, message_thread_id, e)
            return False

    return False


async def unpin_topic_messages_quietly(bot: Any, chat_id: int | str, message_thread_id: int) -> None:
    """Unpin all pinned messages in a forum topic quietly without raising exceptions."""
    try:
        unpin_fn = getattr(bot, "unpin_all_forum_topic_messages", None)
        if unpin_fn and callable(unpin_fn):
            res = unpin_fn(chat_id=chat_id, message_thread_id=message_thread_id)
            if inspect.isawaitable(res):
                await res
    except Exception as e:
        logger.debug("Failed to unpin forum topic messages (%s:%s): %s", chat_id, message_thread_id, e)


def format_user_prompt_preview(msg: Optional[Message], prompt_text: str, origin: Optional[str] = None) -> str:
    """Format a clean user prompt preview to be displayed inside a forum topic."""
    cleaned = clean_prompt_for_display(prompt_text)
    user_label = f"👤 **User{f' ({origin})' if origin else ''}:**"

    if not cleaned:
        return f"{user_label} _(Sem texto)_"

    if cleaned.startswith("[Attached Voice Note:"):
        note = ""
        if "User's note:" in cleaned:
            note = f"\n_Nota: {cleaned.split('User\'s note:')[-1].strip()}_"
        return f"{user_label} 🎙️ _(Mensagem de voz){note}_"

    if cleaned.startswith("[Attached Image:"):
        img_count = cleaned.count("[Attached Image:")
        img_label = f"({img_count} Imagens)" if img_count > 1 else "(Imagem)"
        lines = [line.strip() for line in cleaned.splitlines() if line.strip() and not line.startswith("[Attached Image:")]
        extra = f"\n_{lines[0]}_" if lines else ""
        return f"{user_label} 🖼️ _{img_label}{extra}_"

    if cleaned.startswith("[Attached Document:"):
        return f"{user_label} 📄 _(Documento anexado)_"

    if cleaned.startswith("[Attached Audio File:"):
        return f"{user_label} 🎵 _(Arquivo de áudio)_"

    # Truncate text if very long for initial header
    preview = cleaned if len(cleaned) <= 1000 else cleaned[:997] + "..."
    return f"{user_label}\n{preview}"
