from __future__ import annotations

from datetime import datetime
import html
import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any
import urllib.parse
import urllib.request

from sqlalchemy import func
from sqlalchemy.exc import OperationalError as SQLAlchemyOperationalError
from sqlalchemy.orm import Session

from .cache import cached_value, invalidate_many
from .client_connect import SUPPORTED_CLIENTS, build_client_connection_plan, connect_clients
from .database import ensure_db_ready
from .models import AgentProfile, Artifact, ChatMessage, ChatSession, Memory, Profile
from .paths import PROJECT_ROOT

PREFERRED_CHAT_BACKENDS = ("codex", "gemini", "claude")
PREFERRED_CONVERSATION_BACKENDS = ("gemini", "claude", "codex")
PREFERRED_TASK_BACKENDS = ("codex", "gemini", "claude")
FAST_CHAT_TIMEOUT_SECONDS = 6
FAST_LOOKUP_TIMEOUT_SECONDS = 10
FACTUAL_CHAT_TIMEOUT_SECONDS = 6
SECONDARY_CHAT_TIMEOUT_SECONDS = 4
SECONDARY_LOOKUP_TIMEOUT_SECONDS = 5
SECONDARY_FACTUAL_CHAT_TIMEOUT_SECONDS = 5
FAST_WEB_FACT_TIMEOUT_SECONDS = 3
FAST_WEB_FACT_CACHE_TTL_SECONDS = 900
LOCAL_FIRST_FACT_INTENTS = {"identity", "date", "time", "availability"}
FAST_CHAT_MODEL_ENV_VARS = {
    "codex": "PEXO_CHAT_FAST_MODEL_CODEX",
    "gemini": "PEXO_CHAT_FAST_MODEL_GEMINI",
    "claude": "PEXO_CHAT_FAST_MODEL_CLAUDE",
}
DEFAULT_FAST_CHAT_MODELS = {
    "codex": "gpt-5.4-mini",
    "gemini": "gemini-2.5-flash",
    "claude": "",
}
CONVERSATION_HINTS = (
    "hello",
    "hi",
    "hey",
    "good morning",
    "good afternoon",
    "good evening",
    "how are you",
    "thanks",
    "thank you",
    "testing",
    "test",
    "this is a test",
    "are you there",
    "are you online",
    "who are you",
    "what can you do",
    "help",
)
TASK_HINTS = (
    "build",
    "create",
    "design",
    "implement",
    "fix",
    "refactor",
    "review",
    "audit",
    "analyze",
    "plan",
    "write",
    "edit",
    "update",
    "change",
    "debug",
    "optimize",
    "scaffold",
    "generate",
    "develop",
    "start a new",
    "new website",
    "landing page",
    "dashboard",
    "repo",
    "repository",
    "codebase",
)
BRAIN_LOOKUP_HINTS = (
    "memory",
    "remember",
    "recall",
    "artifact",
    "artifacts",
    "stored",
    "store",
    "session",
    "sessions",
    "agent",
    "agents",
    "profile",
    "runtime",
    "telemetry",
    "readme",
    "what do you know",
    "what do we know",
    "what is stored",
    "what's stored",
)


def serialize_chat_session(session: ChatSession, *, message_count: int | None = None) -> dict:
    payload = {
        "id": session.id,
        "title": session.title,
        "backend": session.backend,
        "workspace_path": session.workspace_path,
        "pexo_session_id": session.pexo_session_id,
        "status": session.status,
        "details": session.details or {},
        "created_at": session.created_at.isoformat() if session.created_at else None,
        "updated_at": session.updated_at.isoformat() if session.updated_at else None,
    }
    if message_count is not None:
        payload["message_count"] = int(message_count)
    return payload


def serialize_chat_message(message: ChatMessage) -> dict:
    return {
        "id": message.id,
        "chat_session_id": message.chat_session_id,
        "role": message.role,
        "content": message.content,
        "details": message.details or {},
        "created_at": message.created_at.isoformat() if message.created_at else None,
    }


def _session_title(message: str) -> str:
    compact = " ".join((message or "").split()).strip()
    if not compact:
        return "New Chat"
    if len(compact) <= 72:
        return compact
    return f"{compact[:72].rstrip()}..."


def _normalize_chat_text(message: str) -> str:
    return " ".join((message or "").strip().lower().split())


def _contains_hint(text: str, hint: str) -> bool:
    normalized_hint = hint.strip().lower()
    if not normalized_hint:
        return False
    if " " in normalized_hint:
        return normalized_hint in text
    return re.search(rf"\b{re.escape(normalized_hint)}\b", text) is not None


def _looks_like_conversation(text: str) -> bool:
    if not text:
        return True
    if any(_contains_hint(text, hint) for hint in CONVERSATION_HINTS):
        return True
    words = text.split()
    if len(words) <= 10 and not any(_contains_hint(text, hint) for hint in TASK_HINTS + BRAIN_LOOKUP_HINTS):
        return True
    if text.endswith("?") and not any(_contains_hint(text, hint) for hint in TASK_HINTS):
        return True
    return False


def _looks_like_brain_lookup(text: str) -> bool:
    if not text:
        return False
    return any(_contains_hint(text, hint) for hint in BRAIN_LOOKUP_HINTS)


def _looks_like_task(text: str) -> bool:
    if not text:
        return False
    return any(_contains_hint(text, hint) for hint in TASK_HINTS)


def _looks_like_general_knowledge_question(text: str) -> bool:
    if not text:
        return False
    if _looks_like_task(text) or _looks_like_brain_lookup(text):
        return False
    prefixes = (
        "who ",
        "what ",
        "when ",
        "where ",
        "why ",
        "how ",
        "which ",
        "is ",
        "are ",
        "do ",
        "does ",
        "did ",
        "can ",
        "could ",
        "would ",
        "will ",
    )
    return text.endswith("?") or text.startswith(prefixes)


def _infer_chat_mode(chat_session: ChatSession, latest_user_message: str) -> str:
    text = _normalize_chat_text(latest_user_message)
    previous_mode = str((chat_session.details or {}).get("mode") or "").strip().lower()

    if _looks_like_brain_lookup(text):
        return "brain_lookup"
    if _looks_like_task(text):
        return "task"
    if previous_mode == "task" and text and not _looks_like_conversation(text):
        return "task"
    return "conversation"


def _profile_summary(db: Session) -> str:
    profile = db.query(Profile).filter(Profile.name == "default_user").first()
    if not profile:
        return "No user profile is configured."
    personality = " ".join((profile.personality_prompt or "").split()).strip()
    scripting = ""
    if isinstance(profile.scripting_preferences, dict):
        scripting = str(profile.scripting_preferences.get("scripting_preferences") or "").strip()
    parts = [part for part in (personality, scripting) if part]
    summary = " | ".join(parts) if parts else "Profile is configured."
    return f"Profile {profile.name}: {summary}"


def _agent_summary(db: Session, limit: int = 8) -> str:
    agents = (
        db.query(AgentProfile)
        .order_by(AgentProfile.is_core.desc(), AgentProfile.name.asc())
        .limit(max(1, min(limit, 12)))
        .all()
    )
    if not agents:
        return "No agents are registered."
    rendered = []
    for agent in agents:
        role = (agent.role or "").strip()
        rendered.append(f"{agent.name} ({role})" if role else agent.name)
    return "Agents: " + ", ".join(rendered)


def _memory_summary(db: Session, query: str, limit: int = 3) -> str:
    needle = _normalize_chat_text(query)
    query_obj = db.query(Memory).filter(Memory.is_archived.is_(False))
    memories = []
    if needle and not any(phrase in needle for phrase in ("what do you know", "what do we know", "what is stored", "what's stored")):
        term = f"%{needle}%"
        memories = (
            query_obj.filter((Memory.content.ilike(term)) | (Memory.task_context.ilike(term)))
            .order_by(Memory.updated_at.desc().nullslast(), Memory.created_at.desc(), Memory.id.desc())
            .limit(max(1, min(limit, 6)))
            .all()
        )
    if not memories:
        memories = (
            query_obj.order_by(Memory.updated_at.desc().nullslast(), Memory.created_at.desc(), Memory.id.desc())
            .limit(max(1, min(limit, 6)))
            .all()
        )
    if not memories:
        return "Matching memories: none."
    lines = []
    for memory in memories:
        compact = " ".join((memory.content or "").split()).strip()
        snippet = compact if len(compact) <= 180 else f"{compact[:180].rstrip()}..."
        lines.append(f"- [{memory.task_context or 'general'}] {snippet}")
    return "Matching memories:\n" + "\n".join(lines)


def _artifact_summary(db: Session, query: str, limit: int = 3) -> str:
    needle = _normalize_chat_text(query)
    query_obj = db.query(Artifact)
    artifacts = []
    if needle and not any(phrase in needle for phrase in ("what do you know", "what do we know", "what is stored", "what's stored")):
        term = f"%{needle}%"
        artifacts = (
            query_obj.filter(
                (Artifact.name.ilike(term))
                | (Artifact.source_uri.ilike(term))
                | (Artifact.extracted_text.ilike(term))
                | (Artifact.task_context.ilike(term))
            )
            .order_by(Artifact.updated_at.desc().nullslast(), Artifact.created_at.desc(), Artifact.id.desc())
            .limit(max(1, min(limit, 6)))
            .all()
        )
    if not artifacts:
        artifacts = (
            query_obj.order_by(Artifact.updated_at.desc().nullslast(), Artifact.created_at.desc(), Artifact.id.desc())
            .limit(max(1, min(limit, 6)))
            .all()
        )
    if not artifacts:
        return "Matching artifacts: none."
    lines = []
    for artifact in artifacts:
        preview = " ".join((artifact.extracted_text or "").split()).strip()
        snippet = preview if len(preview) <= 140 else f"{preview[:140].rstrip()}..."
        descriptor = artifact.name or artifact.source_uri or f"artifact:{artifact.id}"
        if snippet:
            lines.append(f"- {descriptor}: {snippet}")
        else:
            lines.append(f"- {descriptor}")
    return "Matching artifacts:\n" + "\n".join(lines)


def _http_request(url: str, *, timeout_seconds: int, headers: dict[str, str] | None = None) -> urllib.request.Request:
    request_headers = {
        "User-Agent": "Pexo/1.0 (+https://github.com/ParadoxGods/pexo-agent)",
        "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
    }
    if headers:
        request_headers.update(headers)
    return urllib.request.Request(url, headers=request_headers)


def _strip_html_text(value: str) -> str:
    text = re.sub(r"<.*?>", " ", value or "", flags=re.S)
    text = html.unescape(text)
    return " ".join(text.split()).strip()


def _truncate_sentence(value: str, limit: int = 220) -> str:
    text = " ".join((value or "").split()).strip()
    if len(text) <= limit:
        return text
    clipped = text[:limit].rstrip()
    if "." in clipped:
        clipped = clipped.rsplit(".", 1)[0].rstrip()
    return f"{clipped}."


def _fact_query_terms(query: str) -> list[str]:
    stopwords = {
        "a",
        "an",
        "are",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "how",
        "in",
        "is",
        "of",
        "on",
        "the",
        "to",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "would",
    }
    return [
        token
        for token in re.findall(r"[a-z0-9]+", (query or "").lower())
        if len(token) >= 4 and token not in stopwords
    ]


def _score_fact_result(query: str, *, title: str, snippet: str) -> int:
    query_terms = _fact_query_terms(query)
    normalized_title = (title or "").lower()
    normalized_snippet = (snippet or "").lower()
    combined = f"{normalized_title} {normalized_snippet}"
    score = sum(3 for term in query_terms if term in combined)
    if any(hint in normalized_snippet for hint in ("incumbent", "currently", "current", "as of", "assumed office")):
        score += 8
    if re.search(r"\b[A-Z][a-z]+ [A-Z][a-z]+\b", snippet or ""):
        score += 4
    if any(
        bad_hint in combined
        for bad_hint in (
            "actors who have played",
            "fictitious",
            "vice presidents",
            "ran for president",
        )
    ):
        score -= 10
    if title.lower().startswith("list of") and "incumbent" not in normalized_snippet:
        score -= 2
    if "*" in (snippet or ""):
        score -= 2
    return score


def _focus_fact_snippet(query: str, snippet: str) -> str:
    text = " ".join((snippet or "").split()).strip()
    if not text:
        return text
    sentences = [
        sentence.strip()
        for sentence in re.split(r"(?<=[.!?])\s+", text)
        if sentence.strip()
    ]
    preferred_hints = ("incumbent", "currently", "current", "assumed office", "is the")
    for hint in preferred_hints:
        for sentence in sentences:
            if hint in sentence.lower():
                incumbent_match = re.search(
                    r"(The incumbent [^.?!]*? is [A-Z][A-Za-z.'-]+(?: [A-Z][A-Za-z.'-]+){0,4})",
                    sentence,
                )
                if incumbent_match:
                    return f"{incumbent_match.group(1).rstrip('.') }."
                return sentence
    query_terms = _fact_query_terms(query)
    for sentence in sentences:
        normalized = sentence.lower()
        if any(term in normalized for term in query_terms):
            return sentence
    return sentences[0] if sentences else text


def _wikipedia_search_fact(query: str, *, timeout_seconds: int) -> dict[str, str] | None:
    params = urllib.parse.urlencode(
        {
            "action": "query",
            "list": "search",
            "srsearch": query,
            "utf8": "1",
            "format": "json",
            "srlimit": "3",
        }
    )
    request = _http_request(f"https://en.wikipedia.org/w/api.php?{params}", timeout_seconds=timeout_seconds)
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        payload = json.load(response)
    results = ((payload or {}).get("query") or {}).get("search") or []
    ranked: list[tuple[int, dict[str, str]]] = []
    for entry in results[:5]:
        title = str(entry.get("title") or "").strip()
        snippet = _strip_html_text(str(entry.get("snippet") or ""))
        if not snippet:
            continue
        focused = _focus_fact_snippet(query, snippet)
        ranked.append(
            (
                _score_fact_result(query, title=title, snippet=focused),
                {
                    "answer": f"According to Wikipedia, {_truncate_sentence(focused)}",
                    "source": "wikipedia_search",
                    "title": title,
                },
            )
        )
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0], reverse=True)
    return ranked[0][1]


def _duckduckgo_lite_fact(query: str, *, timeout_seconds: int) -> dict[str, str] | None:
    params = urllib.parse.urlencode({"q": query})
    request = _http_request(
        f"https://lite.duckduckgo.com/lite/?{params}",
        timeout_seconds=timeout_seconds,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        html_text = response.read().decode("utf-8", "ignore")
    matches = re.findall(r"<td class='result-snippet'>(.*?)</td>", html_text, flags=re.S | re.I)
    for match in matches[:3]:
        snippet = _strip_html_text(match)
        if not snippet:
            continue
        return {
            "answer": f"Search results say {_truncate_sentence(snippet)}",
            "source": "duckduckgo_lite",
            "title": "",
        }
    return None


def _fast_web_fact_lookup(query: str, *, timeout_seconds: int = FAST_WEB_FACT_TIMEOUT_SECONDS) -> dict[str, str] | None:
    normalized_query = " ".join((query or "").split()).strip()
    if not normalized_query:
        return None

    def _loader() -> dict[str, str] | None:
        for loader in (_wikipedia_search_fact, _duckduckgo_lite_fact):
            try:
                result = loader(normalized_query, timeout_seconds=timeout_seconds)
            except Exception:
                result = None
            if result and result.get("answer"):
                return result
        return None

    return cached_value("web_fact_search", normalized_query.lower(), FAST_WEB_FACT_CACHE_TTL_SECONDS, _loader)


def _build_brain_lookup_context(db: Session, query: str) -> str:
    sections = [
        _profile_summary(db),
        _agent_summary(db),
        _memory_summary(db, query),
        _artifact_summary(db, query),
    ]
    return "\n\n".join(section for section in sections if section)


def _format_local_date() -> str:
    now = datetime.now().astimezone()
    day = now.day
    return f"{now.strftime('%A')}, {now.strftime('%B')} {day}, {now.year}"


def _format_local_time() -> str:
    return datetime.now().astimezone().strftime("%I:%M %p").lstrip("0")


def _local_chat_facts() -> str:
    now = datetime.now().astimezone()
    timezone_name = str(now.tzinfo or "").strip() or "local time"
    return (
        "Local facts you may rely on for this reply:\n"
        "- Assistant name: Pexo\n"
        f"- Today: {_format_local_date()}\n"
        f"- Current time: {_format_local_time()}\n"
        f"- Timezone: {timezone_name}\n"
        "- Status: online and ready\n"
    )


def _infer_direct_fact_intent(user_message: str) -> str | None:
    text = _normalize_chat_text(user_message)
    if not text:
        return None

    if any(_contains_hint(text, hint) for hint in ("what is your name", "what's your name", "your name")):
        return "identity"
    if any(
        phrase in text
        for phrase in (
            "what day is it",
            "what day is it today",
            "what day is today",
            "what is todays day",
            "what is today's day",
            "what is today",
            "what's today",
            "todays date",
            "today's date",
            "what is todays date",
            "what is today's date",
        )
    ):
        return "date"
    if any(
        phrase in text
        for phrase in (
            "what time is it",
            "what's the time",
            "what is the time",
            "current time",
        )
    ):
        return "time"
    if any(_contains_hint(text, hint) for hint in ("are you there", "are you online", "hello", "hi", "hey")):
        return "availability"
    if any(_contains_hint(text, hint) for hint in ("how are you",)):
        return "status"
    return None


def _reply_satisfies_direct_fact_intent(intent: str | None, assistant_text: str) -> bool:
    if not intent:
        return True

    normalized = _normalize_chat_text(assistant_text)
    if not normalized:
        return False

    if intent == "identity":
        return "pexo" in normalized or "my name is" in normalized

    if intent == "date":
        return bool(
            re.search(r"\b20\d{2}\b", normalized)
            or "today is" in normalized
            or any(month in normalized for month in (
                "january",
                "february",
                "march",
                "april",
                "may",
                "june",
                "july",
                "august",
                "september",
                "october",
                "november",
                "december",
            ))
            or any(day in normalized for day in (
                "monday",
                "tuesday",
                "wednesday",
                "thursday",
                "friday",
                "saturday",
                "sunday",
            ))
        )

    if intent == "time":
        return bool(
            re.search(r"\b\d{1,2}:\d{2}\b", assistant_text)
            or "am" in normalized
            or "pm" in normalized
        )

    if intent in {"availability", "status"}:
        return any(word in normalized for word in ("online", "ready", "here", "available", "good"))

    return True


def _build_local_conversation_reply(user_message: str) -> str | None:
    text = _normalize_chat_text(user_message)
    if not text:
        return "Pexo is online and ready."

    if any(_contains_hint(text, hint) for hint in ("what is your name", "what's your name", "your name")):
        return "My name is Pexo."

    if any(
        phrase in text
        for phrase in (
            "what day is it",
            "what day is it today",
            "what day is today",
            "what is todays day",
            "what is today's day",
            "what is today",
            "what's today",
            "todays date",
            "today's date",
            "what is todays date",
            "what is today's date",
        )
    ):
        return f"Today is {_format_local_date()}."

    if any(
        phrase in text
        for phrase in (
            "what time is it",
            "what's the time",
            "what is the time",
            "current time",
        )
    ):
        return f"It is {_format_local_time()}."

    if any(_contains_hint(text, hint) for hint in ("thank you", "thanks")):
        return "You're welcome. Pexo is ready for the next step."

    if any(_contains_hint(text, hint) for hint in ("how are you",)):
        return "I'm online, responsive, and ready to help."

    if any(_contains_hint(text, hint) for hint in ("bye", "goodbye", "see you")):
        return "Understood. I'll be here when you need me."

    if any(
        phrase in text
        for phrase in (
            "favorite",
            "favourite",
            "do you like",
            "what do you like",
            "what's your favorite",
            "what is your favorite",
        )
    ):
        return "I don't have personal preferences, but I can help you choose based on what you want."

    if any(
        phrase in text
        for phrase in (
            "this is shit",
            "this is bad",
            "this sucks",
            "not good",
            "this is terrible",
        )
    ):
        return "Understood. I'll keep it simpler and more direct. Tell me what you want changed."

    if any(_contains_hint(text, hint) for hint in ("what can you do", "help")):
        return (
            "Pexo is online. I can keep local memory, search stored artifacts, manage agents, "
            "and coordinate real work when a task actually needs it."
        )

    if _contains_hint(text, "who are you"):
        return "I'm Pexo, your local-first control plane for memory, artifacts, agents, and task flow."

    if any(_contains_hint(text, hint) for hint in ("are you there", "are you online", "testing", "test", "hello", "hi", "hey")):
        return "Pexo is online and ready. Ask for a task, stored context, or an agent change when you're ready."

    return None


def _build_session_aware_conversation_reply(chat_session: ChatSession, user_message: str) -> str | None:
    text = _normalize_chat_text(user_message)
    if not text:
        return None

    if not any(
        phrase in text
        for phrase in (
            "how did you get that answer",
            "where did you get that answer",
            "where did that answer come from",
            "what source was that",
            "what was the source",
            "where did that come from",
            "how do you know that",
        )
    ):
        return None

    details = dict(chat_session.details or {})
    response_path = str(details.get("response_path") or "").strip().lower()
    if response_path == "web_fact":
        source = str(details.get("web_fact_source") or "").strip().lower()
        title = str(details.get("web_fact_title") or "").strip()
        if source == "wikipedia_search":
            if title:
                return f"I got it from a fast Wikipedia fact lookup, using the result titled '{title}'."
            return "I got it from a fast Wikipedia fact lookup."
        if source == "duckduckgo_lite":
            return "I got it from a fast web search snippet lookup."
        return "I got it from a fast web fact lookup."

    backend_name = str(chat_session.backend or details.get("connected_backend") or "").strip()
    if response_path.startswith("backend") and backend_name:
        return f"I got it from the {backend_name} chat backend for this session."
    if response_path == "local_direct":
        return "I answered that directly from local facts Pexo already had."
    if response_path == "local_fallback":
        return "I answered from Pexo's local fallback logic after the backend did not give a usable answer."
    return None


def _build_local_lookup_reply(db: Session, user_message: str) -> str:
    text = _normalize_chat_text(user_message)
    broad_lookup = any(
        _contains_hint(text, phrase) for phrase in ("what do you know", "what do we know", "what is stored", "what's stored")
    )

    sections: list[str] = []
    if broad_lookup or any(_contains_hint(text, hint) for hint in ("profile", "runtime")):
        sections.append(_profile_summary(db))
    if broad_lookup or any(_contains_hint(text, hint) for hint in ("agent", "agents")):
        sections.append(_agent_summary(db))
    if broad_lookup or any(_contains_hint(text, hint) for hint in ("memory", "remember", "recall")):
        sections.append(_memory_summary(db, user_message))
    if broad_lookup or any(_contains_hint(text, hint) for hint in ("artifact", "artifacts", "readme", "stored")):
        sections.append(_artifact_summary(db, user_message))

    if not sections:
        sections.extend(
            [
                _memory_summary(db, user_message),
                _artifact_summary(db, user_message),
            ]
        )

    return "Here's what Pexo has locally:\n\n" + "\n\n".join(section for section in sections if section)


def _looks_like_generic_backend_filler(text: str) -> bool:
    normalized = _normalize_chat_text(text)
    if not normalized:
        return True
    generic_phrases = (
        "ill act as the user-facing pexo assistant",
        "ill operate as the user-facing pexo assistant",
        "ill speak directly to you as pexo",
        "ill reply as pexo from here",
        "i'll act as the user-facing pexo assistant",
        "i'll operate as the user-facing pexo assistant",
        "i'll speak directly to you as pexo",
        "i'll reply as pexo from here",
        "i am pexo speaking directly to the user",
        "i am pexo speaking directly to you",
        "send the text or task you want handled",
        "send the text or question you want handled",
        "send the task you want handled",
        "send the task, question, or workflow you want handled",
        "what do you want to do next",
        "what do you want to do",
        "what do you need",
        "how can i help",
        "how may i help",
        "tell me what you need",
        "tell me what you want",
    )
    return any(phrase in normalized for phrase in generic_phrases)


def _normalize_backend_reply(
    db: Session,
    *,
    mode: str,
    user_message: str,
    assistant_text: str,
    direct_fact_intent: str | None = None,
) -> str:
    cleaned = (assistant_text or "").strip()
    if cleaned and not _looks_like_generic_backend_filler(cleaned) and _reply_satisfies_direct_fact_intent(
        direct_fact_intent,
        cleaned,
    ):
        return cleaned

    local_reply = _maybe_build_local_reply(db, mode=mode, user_message=user_message)
    if local_reply:
        return local_reply

    if mode == "task":
        return "Pexo is ready to handle the task. Tell me the outcome you want, and I'll continue from there."
    return "Pexo is online and ready."


def _build_backend_retry_prompt(original_prompt: str, *, mode: str, user_message: str) -> str:
    correction = (
        "Critical correction: your previous draft was meta filler.\n"
        "Answer the user's latest message directly.\n"
        "Do not describe your role.\n"
        "Do not say you are speaking directly as Pexo.\n"
        "Do not say you will reply as Pexo from here.\n"
        "Do not ask what they want to do unless they explicitly asked for that.\n"
    )
    if mode == "brain_lookup":
        correction += "Use the local Pexo context already provided and answer in one short practical paragraph.\n"
    elif mode == "conversation":
        correction += "Reply in one short natural sentence using the local facts already provided when relevant.\n"
    else:
        correction += "Reply with the next useful action or answer.\n"
    correction += f"\nLatest user message to answer directly:\n{user_message}\n"
    return f"{original_prompt}\n\n{correction}"


def _maybe_build_local_reply(db: Session, *, mode: str, user_message: str) -> str | None:
    if mode == "conversation":
        return _build_local_conversation_reply(user_message)
    if mode == "brain_lookup":
        return _build_local_lookup_reply(db, user_message)
    return None


def _prefer_local_reply_first(mode: str, *, direct_fact_intent: str | None) -> bool:
    if mode != "conversation":
        return False
    return direct_fact_intent in LOCAL_FIRST_FACT_INTENTS


def _is_general_knowledge_turn(user_message: str, direct_fact_intent: str | None = None) -> bool:
    normalized = _normalize_chat_text(user_message)
    if not _looks_like_general_knowledge_question(normalized):
        return False
    if direct_fact_intent is None:
        direct_fact_intent = _infer_direct_fact_intent(user_message)
    if direct_fact_intent is not None:
        return False
    return _build_local_conversation_reply(user_message) is None


def _conversation_timeout_seconds(user_message: str, timeout_seconds: int) -> int:
    if _is_general_knowledge_turn(user_message):
        return min(timeout_seconds, FACTUAL_CHAT_TIMEOUT_SECONDS)
    return min(timeout_seconds, FAST_CHAT_TIMEOUT_SECONDS)


def _conversation_timeout_for_attempt(user_message: str, timeout_seconds: int, attempt_index: int) -> int:
    if attempt_index <= 0:
        return _conversation_timeout_seconds(user_message, timeout_seconds)
    if _is_general_knowledge_turn(user_message):
        return min(timeout_seconds, SECONDARY_FACTUAL_CHAT_TIMEOUT_SECONDS)
    return min(timeout_seconds, SECONDARY_CHAT_TIMEOUT_SECONDS)


def _lookup_timeout_for_attempt(timeout_seconds: int, attempt_index: int) -> int:
    if attempt_index <= 0:
        return min(timeout_seconds, FAST_LOOKUP_TIMEOUT_SECONDS)
    return min(timeout_seconds, SECONDARY_LOOKUP_TIMEOUT_SECONDS)


def _build_backend_unavailable_reply(backend_name: str, *, mode: str) -> str:
    label = backend_name.capitalize()
    if mode == "brain_lookup":
        return (
            f"I couldn't get a quick retrieval answer from {label} just now, but Pexo is still running. "
            "Try again in a moment or switch backends with /backend <name>."
        )
    return (
        f"I couldn't get a quick answer from {label} just now, but Pexo is still running. "
        "Try again, switch backends with /backend <name>, or give me a more concrete task."
    )


def _should_retry_without_model(exc: RuntimeError) -> bool:
    message = str(exc).strip().lower()
    if not message or "timed out" in message:
        return False
    retryable_hints = (
        "unknown model",
        "unsupported model",
        "model not found",
        "invalid model",
        "unrecognized",
        "invalid choice",
        "no such option",
    )
    return any(hint in message for hint in retryable_hints)


def _default_workspace_path(explicit_path: str | None = None) -> str:
    if explicit_path:
        return str(Path(explicit_path).expanduser().resolve(strict=False))
    try:
        candidate = Path.cwd().resolve(strict=False)
    except OSError:
        candidate = PROJECT_ROOT
    windows_dir = Path(os.environ.get("WINDIR") or r"C:\Windows").resolve(strict=False)
    if candidate == windows_dir or windows_dir in candidate.parents:
        return str(Path.home().resolve(strict=False))
    return str(candidate)


def list_chat_backends(scope: str = "user") -> dict:
    results = []
    for client in SUPPORTED_CLIENTS:
        plan = build_client_connection_plan(client, scope=scope)
        results.append(
            {
                "name": client,
                "available": bool(plan["available"]),
                "binary": plan["binary"],
                "target_command": plan["target"]["display"],
                "manual_command": plan["manual_command"],
            }
        )
    default_backend = next((entry["name"] for entry in results if entry["available"]), None)
    return {
        "default_backend": default_backend,
        "results": results,
    }


def _resolve_backend_name(preferred: str | None = None, *, mode: str | None = None) -> str:
    normalized = (preferred or "auto").strip().lower()
    if normalized and normalized != "auto":
        plan = build_client_connection_plan(normalized, scope="user")
        if not plan["available"]:
            raise RuntimeError(f"{normalized} is not installed or not visible in PATH.")
        return normalized

    preferred_order = PREFERRED_CHAT_BACKENDS
    if mode in {"conversation", "brain_lookup"}:
        preferred_order = PREFERRED_CONVERSATION_BACKENDS
    elif mode == "task":
        preferred_order = PREFERRED_TASK_BACKENDS

    for candidate in preferred_order:
        plan = build_client_connection_plan(candidate, scope="user")
        if plan["available"]:
            return candidate
    raise RuntimeError("No supported direct-chat backend is installed. Install Codex, Gemini, or Claude and connect Pexo first.")


def _fast_chat_model_for_backend(backend_name: str) -> str | None:
    env_var = FAST_CHAT_MODEL_ENV_VARS.get(backend_name, "")
    if env_var:
        configured = os.environ.get(env_var, "").strip()
        if configured:
            return configured
    default_model = DEFAULT_FAST_CHAT_MODELS.get(backend_name, "").strip()
    return default_model or None


def _select_backend_model(backend_name: str, mode: str) -> str | None:
    if mode in {"conversation", "brain_lookup"}:
        return _fast_chat_model_for_backend(backend_name)
    return None


def _ensure_backend_connected(backend_name: str) -> None:
    report = connect_clients(target=backend_name, scope="user", dry_run=False)
    if report["status"] == "failed":
        raise RuntimeError(f"Unable to connect {backend_name} to the Pexo MCP server.")


def _best_effort_backend_connection(backend_name: str) -> str | None:
    try:
        _ensure_backend_connected(backend_name)
    except RuntimeError as exc:
        return str(exc)
    return None


def _wrap_command(invoker: str, args: list[str]) -> list[str]:
    if os.name == "nt" and invoker.lower().endswith((".cmd", ".bat")):
        return ["cmd.exe", "/c", invoker, *args]
    return [invoker, *args]


def _terminate_process_tree(process: subprocess.Popen[Any]) -> None:
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        else:
            process.kill()
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def _run_command_with_timeout(
    command: list[str],
    *,
    cwd: str | None = None,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    popen_kwargs: dict[str, Any] = {
        "cwd": cwd,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
    }
    if os.name == "nt":
        popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    process = subprocess.Popen(command, **popen_kwargs)
    try:
        stdout, stderr = process.communicate(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        _terminate_process_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=2)
        except Exception:
            stdout, stderr = "", ""
        raise subprocess.TimeoutExpired(command, timeout_seconds, output=stdout, stderr=stderr) from exc
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def _history_excerpt(db: Session, chat_session_id: str, limit: int = 10) -> str:
    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.chat_session_id == chat_session_id)
        .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
        .limit(limit)
        .all()
    )
    if not messages:
        return "No prior chat turns."
    ordered = list(reversed(messages))
    return "\n".join(f"{message.role.upper()}: {message.content}" for message in ordered)


def _build_worker_prompt(
    *,
    backend_name: str,
    chat_session: ChatSession,
    role: str | None,
    instruction: str,
    latest_user_message: str,
    history_excerpt: str,
) -> str:
    return (
        "You are completing one internal Pexo worker turn.\n"
        "Do not talk to the user directly.\n"
        "Return only the work product needed by the instruction.\n"
        "If raw JSON is required, return raw JSON only with no markdown fences.\n"
        "Pexo already owns the session state. Do not start a new Pexo task, do not continue the current task with Pexo control tools, and do not restate orchestration internals.\n"
        "You may use the connected Pexo MCP server for memory lookup, artifact lookup, tool execution, or agent management if that materially improves the result.\n\n"
        f"Direct chat backend: {backend_name}\n"
        f"Direct chat session: {chat_session.id}\n"
        f"Pexo task session: {chat_session.pexo_session_id or 'none'}\n"
        f"Workspace path: {chat_session.workspace_path or str(PROJECT_ROOT)}\n"
        f"Assigned role: {role or 'Worker'}\n\n"
        f"Recent direct chat transcript:\n{history_excerpt}\n\n"
        f"Latest user message:\n{latest_user_message}\n\n"
        f"Internal instruction:\n{instruction}\n"
    )

def _build_conversation_prompt(
    *,
    backend_name: str,
    chat_session: ChatSession,
    latest_user_message: str,
    history_excerpt: str,
) -> str:
    return (
        "Reply as Pexo in a natural, direct way.\n"
        "This is normal conversation, not task orchestration.\n"
        "Answer the latest user message directly.\n"
        "If it is a simple factual question, answer with one short factual sentence.\n"
        "If you are uncertain, say so plainly in one short sentence instead of stalling.\n"
        "Do not narrate your role, mode, or internal process.\n"
        "Do not tell the user you are acting as Pexo. Just answer.\n"
        "Do not ask what they want to do unless they explicitly asked for that.\n"
        "Keep the reply short and human.\n\n"
        f"{_local_chat_facts()}\n"
        f"Recent direct chat transcript:\n{history_excerpt}\n\n"
        f"Latest user message:\n{latest_user_message}\n"
    )


def _build_quick_conversation_prompt(*, latest_user_message: str) -> str:
    return (
        "Reply as Pexo in one short direct answer.\n"
        "Answer the user's latest message directly.\n"
        "Do not narrate your role, mode, or process.\n"
        "If the user asked a simple factual question, answer with the fact plainly.\n"
        "If you are uncertain, say so in one short sentence.\n"
        "Do not ask a follow-up question unless the user explicitly asked for options or help deciding.\n\n"
        f"{_local_chat_facts()}\n"
        f"Latest user message:\n{latest_user_message}\n"
    )


def _build_lookup_prompt(
    *,
    backend_name: str,
    chat_session: ChatSession,
    latest_user_message: str,
    history_excerpt: str,
    local_context: str,
) -> str:
    return (
        "Reply as Pexo in a natural, direct way.\n"
        "The user is asking what Pexo already knows, stores, or remembers.\n"
        "Answer from the local Pexo context below.\n"
        "Do not start or continue structured task orchestration for this turn.\n"
        "Do not narrate your role or process.\n"
        "If the local context does not contain the answer, say that plainly.\n"
        "Keep the reply concise and practical.\n\n"
        f"{_local_chat_facts()}\n"
        f"Recent direct chat transcript:\n{history_excerpt}\n\n"
        f"Local Pexo context:\n{local_context}\n\n"
        f"Latest user message:\n{latest_user_message}\n"
    )


def _build_task_prompt(
    *,
    backend_name: str,
    chat_session: ChatSession,
    latest_user_message: str,
    history_excerpt: str,
) -> str:
    return (
        "Reply as Pexo in a natural, direct way.\n"
        "The user is asking Pexo to accomplish real work.\n"
        "Treat the connected Pexo MCP server as your default local brain and control plane.\n"
        "Prefer handling straightforward one-step work directly.\n"
        "Use structured Pexo task flow only when the work is clearly multi-step, needs durable coordination, or truly needs one clarification question.\n"
        "Do not expose raw orchestration internals unless the user explicitly asks for them.\n"
        "Do not narrate your role or process.\n"
        "Keep the reply natural, direct, and outcome-focused.\n\n"
        f"{_local_chat_facts()}\n"
        f"Recent direct chat transcript:\n{history_excerpt}\n\n"
        f"Latest user message:\n{latest_user_message}\n"
    )


def _backend_needs_mcp(mode: str) -> bool:
    return mode == "task"


def _backend_needs_workspace(mode: str) -> bool:
    return mode == "task"


def _available_backends_for_mode(mode: str) -> list[str]:
    order = PREFERRED_CHAT_BACKENDS
    if mode in {"conversation", "brain_lookup"}:
        order = PREFERRED_CONVERSATION_BACKENDS
    elif mode == "task":
        order = PREFERRED_TASK_BACKENDS
    available: list[str] = []
    for candidate in order:
        plan = build_client_connection_plan(candidate, scope="user")
        if plan["available"]:
            available.append(candidate)
    return available


def _conversation_backend_candidates(primary_backend: str, *, mode: str) -> list[str]:
    candidates = [primary_backend]
    if mode not in {"conversation", "brain_lookup"}:
        return candidates
    for candidate in _available_backends_for_mode(mode):
        if candidate not in candidates:
            candidates.append(candidate)
    return candidates


def _run_codex_turn(plan: dict, prompt: str, workspace_path: str, timeout_seconds: int, model_override: str | None = None, *, mode: str = "task") -> str:
    with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False, encoding="utf-8") as handle:
        output_path = Path(handle.name)
    args = [
        "exec",
        "--skip-git-repo-check",
        "--color",
        "never",
    ]
    if mode == "task":
        args.append("--full-auto")
    if model_override:
        args.extend(["-m", model_override])
    if workspace_path and _backend_needs_workspace(mode):
        args.extend(["-C", workspace_path])
    args.extend(["-o", str(output_path), prompt])
    command = _wrap_command(plan["invoker"], args)
    try:
        try:
            completed = _run_command_with_timeout(
                command,
                cwd=workspace_path if workspace_path else None,
                timeout_seconds=timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"Codex direct chat timed out after {timeout_seconds} seconds."
            ) from exc
        if completed.returncode != 0:
            raise RuntimeError((completed.stderr or completed.stdout or "Codex direct chat turn failed.").strip())
        if output_path.exists():
            result = output_path.read_text(encoding="utf-8", errors="ignore").strip()
            if result:
                return result
        return (completed.stdout or "").strip()
    finally:
        output_path.unlink(missing_ok=True)


def _run_gemini_turn(plan: dict, prompt: str, workspace_path: str, timeout_seconds: int, model_override: str | None = None, *, mode: str = "task") -> str:
    args = [
        "--prompt",
        prompt,
        "--output-format",
        "text",
    ]
    if mode == "task":
        args.extend(["--yolo"])
    if _backend_needs_mcp(mode):
        args.extend(["--allowed-mcp-server-names", "pexo"])
    if model_override:
        args.extend(["-m", model_override])
    if workspace_path and _backend_needs_workspace(mode):
        args.extend(["--include-directories", workspace_path])
    command = _wrap_command(plan["invoker"], args)
    try:
        completed = _run_command_with_timeout(
            command,
            timeout_seconds=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Gemini direct chat timed out after {timeout_seconds} seconds."
        ) from exc
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "Gemini direct chat turn failed.").strip())
    return (completed.stdout or "").strip()


def _run_claude_turn(plan: dict, prompt: str, timeout_seconds: int, model_override: str | None = None, *, mode: str = "task") -> str:
    args = []
    if model_override:
        args.extend(["--model", model_override])
    args.extend(["-p", prompt])
    command = _wrap_command(plan["invoker"], args)
    try:
        completed = _run_command_with_timeout(
            command,
            timeout_seconds=timeout_seconds,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Claude direct chat timed out after {timeout_seconds} seconds."
        ) from exc
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or completed.stdout or "Claude direct chat turn failed.").strip())
    return (completed.stdout or "").strip()


def run_direct_chat_backend(
    backend_name: str,
    prompt: str,
    workspace_path: str,
    timeout_seconds: int = 300,
    *,
    mode: str = "task",
) -> str:
    plan = build_client_connection_plan(backend_name, scope="user")
    if not plan["available"]:
        raise RuntimeError(f"{backend_name} is not installed or not visible in PATH.")
    model_override = _select_backend_model(backend_name, mode)
    if backend_name == "codex":
        try:
            return _run_codex_turn(plan, prompt, workspace_path, timeout_seconds, model_override=model_override, mode=mode)
        except RuntimeError as exc:
            if model_override and _should_retry_without_model(exc):
                return _run_codex_turn(plan, prompt, workspace_path, timeout_seconds, model_override=None, mode=mode)
            raise
    if backend_name == "gemini":
        try:
            return _run_gemini_turn(plan, prompt, workspace_path, timeout_seconds, model_override=model_override, mode=mode)
        except RuntimeError as exc:
            if model_override and _should_retry_without_model(exc):
                return _run_gemini_turn(plan, prompt, workspace_path, timeout_seconds, model_override=None, mode=mode)
            raise
    if backend_name == "claude":
        try:
            return _run_claude_turn(plan, prompt, timeout_seconds, model_override=model_override, mode=mode)
        except RuntimeError as exc:
            if model_override and _should_retry_without_model(exc):
                return _run_claude_turn(plan, prompt, timeout_seconds, model_override=None, mode=mode)
            raise
    raise RuntimeError(f"Unsupported backend '{backend_name}'.")


def _store_message(db: Session, chat_session_id: str, role: str, content: str, details: dict[str, Any] | None = None) -> ChatMessage:
    message = ChatMessage(
        chat_session_id=chat_session_id,
        role=role,
        content=content,
        details=details or {},
    )
    db.add(message)
    db.flush()
    return message


def _commit_with_retry(db: Session, *objects: Any, attempts: int = 5) -> None:
    tracked = [obj for obj in objects if obj is not None]
    for attempt in range(attempts):
        try:
            db.commit()
            return
        except SQLAlchemyOperationalError as exc:
            if "locked" not in str(exc).lower() or attempt == attempts - 1:
                raise
            db.rollback()
            for obj in tracked:
                db.add(obj)
            time.sleep(0.15 * (attempt + 1))


def create_chat_session(
    db: Session,
    *,
    backend: str = "auto",
    workspace_path: str | None = None,
    title: str | None = None,
) -> dict:
    ensure_db_ready()
    backend_policy = "auto" if (backend or "auto").strip().lower() == "auto" else "manual"
    backend_name = _resolve_backend_name(backend, mode="conversation")
    details = {
        "connected_backend": backend_name,
        "backend_policy": backend_policy,
        "backend_verified": False,
    }
    session = ChatSession(
        id=str(uuid.uuid4()),
        title=title or "New Chat",
        backend=backend_name,
        workspace_path=_default_workspace_path(workspace_path),
        pexo_session_id=None,
        status="idle",
        details=details,
    )
    db.add(session)
    _commit_with_retry(db, session)
    db.refresh(session)
    invalidate_many("chat_sessions", "admin_snapshot")
    return serialize_chat_session(session, message_count=0)


def update_chat_session(
    db: Session,
    *,
    session_id: str,
    title: str | None = None,
    backend: str | None = None,
    workspace_path: str | None = None,
) -> dict:
    ensure_db_ready()
    session = db.query(ChatSession).filter(ChatSession.id == session_id).first()
    if session is None:
        raise RuntimeError("Chat session not found.")

    if title is not None:
        session.title = title.strip() or session.title
    if backend is not None:
        backend_policy = "auto" if backend.strip().lower() == "auto" else "manual"
        backend_name = _resolve_backend_name(backend, mode="conversation")
        session.backend = backend_name
        details = dict(session.details or {})
        details["connected_backend"] = backend_name
        details["backend_policy"] = backend_policy
        details["backend_verified"] = False
        details.pop("backend_warning", None)
        session.details = details
    if workspace_path is not None:
        session.workspace_path = _default_workspace_path(workspace_path)

    _commit_with_retry(db, session)
    db.refresh(session)
    invalidate_many("chat_sessions", "admin_snapshot")
    message_count = db.query(func.count(ChatMessage.id)).filter(ChatMessage.chat_session_id == session.id).scalar() or 0
    return serialize_chat_session(session, message_count=message_count)


def delete_chat_session(db: Session, session_id: str) -> dict:
    ensure_db_ready()
    session = db.query(ChatSession).filter(ChatSession.id == session_id).first()
    if session is None:
        raise RuntimeError("Chat session not found.")
    db.query(ChatMessage).filter(ChatMessage.chat_session_id == session_id).delete()
    db.delete(session)
    db.commit()
    invalidate_many("chat_sessions", "admin_snapshot")
    return {"status": "success", "session_id": session_id}


def get_chat_session_payload(db: Session, session_id: str, *, message_limit: int = 120) -> dict:
    ensure_db_ready()
    session = db.query(ChatSession).filter(ChatSession.id == session_id).first()
    if session is None:
        raise RuntimeError("Chat session not found.")
    messages = (
        db.query(ChatMessage)
        .filter(ChatMessage.chat_session_id == session_id)
        .order_by(ChatMessage.created_at.asc(), ChatMessage.id.asc())
        .limit(max(1, min(message_limit, 300)))
        .all()
    )
    message_count = db.query(func.count(ChatMessage.id)).filter(ChatMessage.chat_session_id == session_id).scalar() or 0
    return {
        "session": serialize_chat_session(session, message_count=message_count),
        "messages": [serialize_chat_message(message) for message in messages],
        "available_backends": list_chat_backends(),
    }


def list_chat_sessions(db: Session, limit: int = 20) -> dict:
    ensure_db_ready()
    safe_limit = max(1, min(limit, 100))
    sessions = (
        db.query(ChatSession)
        .order_by(ChatSession.updated_at.desc().nullslast(), ChatSession.created_at.desc(), ChatSession.id.desc())
        .limit(safe_limit)
        .all()
    )
    if not sessions:
        return {
            "sessions": [],
            "available_backends": list_chat_backends(),
        }

    counts = {
        session_id: count
        for session_id, count in (
            db.query(ChatMessage.chat_session_id, func.count(ChatMessage.id))
            .filter(ChatMessage.chat_session_id.in_([session.id for session in sessions]))
            .group_by(ChatMessage.chat_session_id)
            .all()
        )
    }
    return {
        "sessions": [serialize_chat_session(session, message_count=counts.get(session.id, 0)) for session in sessions],
        "available_backends": list_chat_backends(),
    }


def send_chat_message(
    db: Session,
    *,
    session_id: str,
    message: str,
    timeout_seconds: int = 300,
) -> dict:
    ensure_db_ready()
    session = db.query(ChatSession).filter(ChatSession.id == session_id).first()
    if session is None:
        raise RuntimeError("Chat session not found.")
    if not session.workspace_path:
        session.workspace_path = _default_workspace_path()

    user_message = (message or "").strip()
    if not user_message:
        raise RuntimeError("Chat message cannot be empty.")

    if session.title in {None, "", "New Chat"}:
        session.title = _session_title(user_message)

    user_record = _store_message(db, session.id, "user", user_message)
    history_excerpt = _history_excerpt(db, session.id)
    mode = _infer_chat_mode(session, user_message)
    backend_policy = str((session.details or {}).get("backend_policy") or "manual").strip().lower()
    backend_name = _resolve_backend_name("auto" if backend_policy == "auto" else (session.backend or "auto"), mode=mode)
    session.backend = backend_name
    direct_fact_intent = _infer_direct_fact_intent(user_message) if mode == "conversation" else None
    session_local_reply = _build_session_aware_conversation_reply(session, user_message) if mode == "conversation" else None
    general_knowledge_turn = mode == "conversation" and _is_general_knowledge_turn(
        user_message,
        direct_fact_intent=direct_fact_intent,
    )
    local_first = bool(session_local_reply) or _prefer_local_reply_first(mode, direct_fact_intent=direct_fact_intent)
    details = dict(session.details or {})
    details["backend_policy"] = backend_policy
    if mode == "task" and (
        details.get("connected_backend") != backend_name
        or details.get("backend_warning")
        or not details.get("backend_verified")
    ):
        details["connected_backend"] = backend_name
        backend_warning = _best_effort_backend_connection(backend_name)
        if backend_warning:
            details["backend_warning"] = backend_warning
            details["backend_verified"] = False
        else:
            details.pop("backend_warning", None)
            details["backend_verified"] = True
        session.details = details
    if mode == "brain_lookup":
        assistant_prompt = _build_lookup_prompt(
            backend_name=backend_name,
            chat_session=session,
            latest_user_message=user_message,
            history_excerpt=history_excerpt,
            local_context=_build_brain_lookup_context(db, user_message),
        )
    elif mode == "task":
        assistant_prompt = _build_task_prompt(
            backend_name=backend_name,
            chat_session=session,
            latest_user_message=user_message,
            history_excerpt=history_excerpt,
        )
    else:
        if general_knowledge_turn:
            assistant_prompt = _build_quick_conversation_prompt(latest_user_message=user_message)
        else:
            assistant_prompt = _build_conversation_prompt(
                backend_name=backend_name,
                chat_session=session,
                latest_user_message=user_message,
                history_excerpt=history_excerpt,
            )
    assistant_text = None
    response_path = "backend"
    backend_elapsed_ms: int | None = None
    attempted_backends: list[str] = []
    backend_errors: dict[str, str] = {}
    web_fact_source: str | None = None
    web_fact_title: str | None = None
    started_at = time.monotonic()
    backend_timeout = timeout_seconds
    if mode == "brain_lookup":
        backend_timeout = _lookup_timeout_for_attempt(timeout_seconds, 0)
    elif mode == "conversation":
        backend_timeout = _conversation_timeout_for_attempt(user_message, timeout_seconds, 0)

    if local_first:
        assistant_text = session_local_reply or _build_local_conversation_reply(user_message)
        response_path = "local_direct"
    else:
        web_fact = _fast_web_fact_lookup(user_message) if general_knowledge_turn else None
        if web_fact and web_fact.get("answer"):
            assistant_text = str(web_fact["answer"]).strip()
            response_path = "web_fact"
            web_fact_source = str(web_fact.get("source") or "").strip() or None
            web_fact_title = str(web_fact.get("title") or "").strip() or None
        if assistant_text is None:
            backend_candidates = [backend_name]
            should_try_backend_fallbacks = (
                backend_policy == "auto"
                and (
                    mode == "brain_lookup"
                )
            )
            if should_try_backend_fallbacks:
                backend_candidates = _conversation_backend_candidates(backend_name, mode=mode)
            for attempt_index, candidate_backend in enumerate(backend_candidates):
                attempted_backends.append(candidate_backend)
                candidate_timeout = timeout_seconds
                if mode == "brain_lookup":
                    candidate_timeout = _lookup_timeout_for_attempt(timeout_seconds, attempt_index)
                elif mode == "conversation":
                    candidate_timeout = _conversation_timeout_for_attempt(user_message, timeout_seconds, attempt_index)
                try:
                    backend_started_at = time.monotonic()
                    raw_result = run_direct_chat_backend(
                        candidate_backend,
                        assistant_prompt,
                        session.workspace_path,
                        timeout_seconds=candidate_timeout,
                        mode=mode,
                    )
                    candidate_elapsed_ms = int((time.monotonic() - backend_started_at) * 1000)
                    backend_elapsed_ms = (backend_elapsed_ms or 0) + candidate_elapsed_ms
                    if _looks_like_generic_backend_filler(raw_result or "") or not _reply_satisfies_direct_fact_intent(
                        direct_fact_intent,
                        raw_result or "",
                    ):
                        retry_started_at = time.monotonic()
                        raw_result = run_direct_chat_backend(
                            candidate_backend,
                            _build_backend_retry_prompt(
                                assistant_prompt,
                                mode=mode,
                                user_message=user_message,
                            ),
                            session.workspace_path,
                            timeout_seconds=candidate_timeout,
                            mode=mode,
                        )
                        backend_elapsed_ms += int((time.monotonic() - retry_started_at) * 1000)
                        response_path = "backend_retry" if attempt_index == 0 else "backend_fallback_retry"
                        if general_knowledge_turn and _looks_like_generic_backend_filler(raw_result or ""):
                            raise RuntimeError(f"{candidate_backend} returned generic filler for a factual question.")
                    elif attempt_index > 0:
                        response_path = "backend_fallback"
                    assistant_text = _normalize_backend_reply(
                        db,
                        mode=mode,
                        user_message=user_message,
                        assistant_text=raw_result or "",
                        direct_fact_intent=direct_fact_intent,
                    )
                    backend_name = candidate_backend
                    session.backend = candidate_backend
                    break
                except RuntimeError as exc:
                    backend_errors[candidate_backend] = str(exc)
                    if attempt_index == len(backend_candidates) - 1:
                        assistant_text = _maybe_build_local_reply(
                            db,
                            mode=mode,
                            user_message=user_message,
                        )
                        response_path = "local_fallback"
                        if assistant_text is None:
                            assistant_text = _build_backend_unavailable_reply(backend_name, mode=mode)
                            response_path = "backend_unavailable"
                    continue

    total_latency_ms = int((time.monotonic() - started_at) * 1000)

    session.status = "answered"
    details = dict(session.details or {})
    details["last_user_message"] = user_message
    details["last_assistant_message"] = assistant_text
    details["mode"] = mode
    details["connected_backend"] = backend_name
    details["response_path"] = response_path
    details["total_latency_ms"] = total_latency_ms
    if response_path == "web_fact":
        if web_fact_source:
            details["web_fact_source"] = web_fact_source
        if web_fact_title:
            details["web_fact_title"] = web_fact_title
    else:
        details.pop("web_fact_source", None)
        details.pop("web_fact_title", None)
    if not local_first and response_path != "web_fact":
        details["attempted_backends"] = attempted_backends
        if backend_errors:
            details["backend_errors"] = backend_errors
        else:
            details.pop("backend_errors", None)
    elif response_path == "web_fact":
        details.pop("attempted_backends", None)
        details.pop("backend_errors", None)
    if backend_elapsed_ms is not None:
        details["backend_latency_ms"] = backend_elapsed_ms
    else:
        details.pop("backend_latency_ms", None)
    session.details = details

    assistant_record = _store_message(
        db,
        session.id,
        "assistant",
        assistant_text,
        details={
            "status": "answered",
            "backend": backend_name,
            "mode": mode,
            "response_path": response_path,
            "total_latency_ms": total_latency_ms,
            "backend_latency_ms": backend_elapsed_ms,
        },
    )

    _commit_with_retry(db, session, user_record, assistant_record)
    db.refresh(session)
    invalidate_many("chat_sessions", "admin_snapshot", "telemetry")
    return {
        **get_chat_session_payload(db, session.id),
        "reply": {
            "status": "answered",
            "user_message": assistant_text,
            "question": None,
            "role": None,
            "next_action": None,
            "pexo_session_id": session.pexo_session_id,
        },
    }
