"""
core/ai_provider.py - 마이스터 역량인증제 Multi-Provider AI 서비스 연동 코어 모듈

지원 프로바이더:
1. Google Gemini (REST API v1beta)
2. OpenAI (Chat Completions API: gpt-4o, gpt-4o-mini 등)
3. Anthropic Claude (Messages API: claude-3-5-sonnet 등)
4. OpenAI-Compatible Custom Endpoint (Groq, vLLM, Ollama, DeepSeek 등 - AI_BASE_URL 기반)

기능:
- 학생/교사/관리자 역할별 모델/API 키/Base URL 계층적 분리
- API 키 미설정/타임아웃/오류 발생 시 규칙 기반 Graceful Fallback 정합성 유지
- 표준 메타데이터 생성 (analysisSource, fallbackUsed, fallbackReason, model, capabilityMode)
"""

import asyncio
from dataclasses import dataclass
import json
import logging
import os
import re
from typing import Any, Dict, List, Literal, Optional, Tuple

import httpx

logger = logging.getLogger("meister.ai.provider")

AIProviderType = Literal["gemini", "openai", "claude", "openai_compatible"]

# ==============================================================================
# 기본 모델 및 프로바이더 기본값
# ==============================================================================
DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
DEFAULT_CLAUDE_MODEL = "claude-3-5-sonnet-20241022"
DEFAULT_COMPATIBLE_MODEL = "gpt-4o-mini"


def normalize_provider_name(raw: Optional[str]) -> str:
    """프로바이더 명칭 정규화"""
    if not raw:
        return ""
    cleaned = raw.strip().lower().replace("_", "").replace("-", "")
    if cleaned in ("gemini", "google", "googlegemini"):
        return "gemini"
    if cleaned in ("openai", "chatgpt", "gpt"):
        return "openai"
    if cleaned in ("claude", "anthropic"):
        return "claude"
    if cleaned in ("openaicompatible", "compatible", "vllm", "ollama", "groq", "deepseek", "custom"):
        return "openai_compatible"
    return cleaned


def resolve_gemini_model_name(raw_model: Optional[str] = None) -> str:
    """구글 Gemini 모델 식별자 정규화"""
    model = (raw_model or os.getenv("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL).strip()
    normalized = model.lower().replace(" ", "-")
    alias_map = {
        "gemini-3.8-flash": "gemini-3.8-flash",
        "3.8-flash": "gemini-3.8-flash",
        "gemini-3.5-flash-lite": "gemini-3.5-flash-lite",
        "3.5-flash-lite": "gemini-3.5-flash-lite",
        "gemini-3.5-flash": "gemini-3.5-flash",
        "gemini-2.5-flash": "gemini-2.5-flash",
        "gemini-2.0-flash-lite": "gemini-2.0-flash-lite",
        "gemini-1.5-flash": "gemini-1.5-flash",
    }
    return alias_map.get(normalized, model)


@dataclass
class AIConfig:
    provider: str
    api_key: str
    model: str
    base_url: Optional[str] = None
    timeout_sec: float = 5.0


def resolve_ai_config(
    role: Optional[str] = None,
    provider_override: Optional[str] = None,
    model_override: Optional[str] = None,
    api_key_override: Optional[str] = None,
    base_url_override: Optional[str] = None,
    timeout_sec: float = 5.0,
) -> AIConfig:
    """
    역할(student/teacher/admin) 및 환경 변수 우선순위를 반영하여 최종 AI 설정을 안전하게 도출합니다.

    우선순위:
    1. Base URL:
       base_url_override > {ROLE}_AI_BASE_URL > AI_BASE_URL > OPENAI_BASE_URL
    2. Provider:
       provider_override > {ROLE}_AI_PROVIDER > AI_PROVIDER > (Base URL 설정 시 openai_compatible 자동 선택) > (Key 기반 감지) > 'gemini'
    3. API Key:
       api_key_override > {ROLE}_AI_API_KEY > Provider별 Key (OPENAI_API_KEY / ANTHROPIC_API_KEY / GEMINI_API_KEY) > AI_API_KEY
    4. Model:
       model_override > {ROLE}_AI_MODEL > Provider별 Model > Provider 기본 모델
    """
    norm_role = (role or "").strip().lower()
    prefix = ""
    if norm_role in ("teacher", "1", 1):
        prefix = "TEACHER_AI_"
    elif norm_role in ("admin", "2", 2):
        prefix = "ADMIN_AI_"
    elif norm_role in ("student", "0", 0):
        prefix = "STUDENT_AI_"

    # 1. Base URL 판정
    base_url = (
        base_url_override
        or (os.getenv(f"{prefix}BASE_URL") if prefix else None)
        or os.getenv("AI_BASE_URL")
        or os.getenv("OPENAI_BASE_URL")
    )
    if base_url:
        base_url = base_url.strip()

    # 2. Provider 판정
    raw_provider = (
        provider_override
        or (os.getenv(f"{prefix}PROVIDER") if prefix else None)
        or os.getenv("AI_PROVIDER")
    )
    provider = normalize_provider_name(raw_provider)

    if not provider:
        if base_url:
            provider = "openai_compatible"
        elif os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY"):
            if not (os.getenv("GEMINI_API_KEY") or os.getenv("OPENAI_API_KEY")):
                provider = "claude"
            else:
                provider = "gemini"
        elif os.getenv("OPENAI_API_KEY") and not os.getenv("GEMINI_API_KEY"):
            provider = "openai"
        else:
            provider = "gemini"

    # 3. API Key 판정
    api_key = (
        api_key_override
        or (os.getenv(f"{prefix}API_KEY") if prefix else None)
    )
    if not api_key:
        if provider == "openai":
            api_key = os.getenv("OPENAI_API_KEY")
        elif provider == "claude":
            api_key = os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY")
        elif provider == "openai_compatible":
            api_key = (
                os.getenv("COMPATIBLE_AI_API_KEY")
                or os.getenv("OPENAI_API_KEY")
                or os.getenv("AI_API_KEY")
                or "dummy-key"  # 로컬 vLLM/Ollama용 더미 허용
            )
        elif provider == "gemini":
            api_key = os.getenv("GEMINI_API_KEY")

    if not api_key:
        api_key = os.getenv("AI_API_KEY") or ""
    api_key = api_key.strip()

    # 4. Model 판정
    model = (
        model_override
        or (os.getenv(f"{prefix}MODEL") if prefix else None)
    )
    if not model:
        if prefix == "TEACHER_AI_":
            model = os.getenv("GEMINI_TEACHER_MODEL") or os.getenv("OPENAI_TEACHER_MODEL")
        elif prefix == "ADMIN_AI_":
            model = os.getenv("GEMINI_ADMIN_MODEL") or os.getenv("OPENAI_ADMIN_MODEL")

    if not model:
        if provider == "openai":
            model = os.getenv("OPENAI_MODEL") or DEFAULT_OPENAI_MODEL
        elif provider == "claude":
            model = os.getenv("CLAUDE_MODEL") or os.getenv("ANTHROPIC_MODEL") or DEFAULT_CLAUDE_MODEL
        elif provider == "openai_compatible":
            model = os.getenv("COMPATIBLE_AI_MODEL") or os.getenv("OPENAI_MODEL") or DEFAULT_COMPATIBLE_MODEL
        else:
            model = os.getenv("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL

    model = model.strip()
    if provider == "gemini":
        model = resolve_gemini_model_name(model)

    return AIConfig(
        provider=provider,
        api_key=api_key,
        model=model,
        base_url=base_url,
        timeout_sec=timeout_sec,
    )


@dataclass
class LLMResult:
    text: Optional[str]
    provider: str
    model: str
    is_fallback: bool
    fallback_reason: Optional[str] = None

    @property
    def analysis_source(self) -> str:
        """응답 메타데이터의 analysisSource 표준 식별자"""
        if self.is_fallback:
            return "rule_based_fallback"
        return self.provider

    def to_metadata(self) -> Dict[str, Any]:
        """API_CONTRACT 8.1 공통 응답 규격 메타데이터 딕셔너리"""
        return {
            "analysisSource": self.analysis_source,
            "fallbackUsed": self.is_fallback,
            "fallbackReason": self.fallback_reason,
            "model": self.model if not self.is_fallback else "rule_based_engine",
            "capabilityMode": "rule_based_engine" if self.is_fallback else "live_llm",
        }


# ==============================================================================
# 각 프로바이더별 실제 HTTP 호출기
# ==============================================================================

async def _call_gemini_rest(
    prompt: str,
    system_instruction: str,
    config: AIConfig,
    response_json: bool = False,
) -> Tuple[Optional[str], Optional[str]]:
    """Google Gemini REST API 호출"""
    if not config.api_key:
        return None, "Gemini API 키 미설정"

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{config.model}:generateContent?key={config.api_key}"
    generation_config: Dict[str, Any] = {
        "temperature": 0.2,
        "maxOutputTokens": 1024,
    }
    if response_json:
        generation_config["responseMimeType"] = "application/json"

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "systemInstruction": {"parts": [{"text": system_instruction}]},
        "generationConfig": generation_config,
    }
    headers = {
        "Content-Type": "application/json",
        "x-goog-api-key": config.api_key,
    }

    async with httpx.AsyncClient(timeout=config.timeout_sec) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            return None, f"Gemini API 오류 (HTTP {resp.status_code}): {resp.text[:120]}"
        data = resp.json()
        candidates = data.get("candidates", [])
        if not candidates:
            return None, "Gemini 응답 후보(candidates) 비어있음"
        parts = candidates[0].get("content", {}).get("parts", [])
        if not parts:
            return None, "Gemini 응답 내용(parts) 비어있음"
        return parts[0].get("text", "").strip(), None


async def _call_openai_rest(
    prompt: str,
    system_instruction: str,
    config: AIConfig,
    response_json: bool = False,
    is_compatible: bool = False,
) -> Tuple[Optional[str], Optional[str]]:
    """OpenAI 또는 OpenAI-Compatible REST API 호출"""
    if not config.api_key and not is_compatible:
        return None, "OpenAI API 키 미설정"

    if is_compatible and config.base_url:
        endpoint = config.base_url.rstrip("/")
        if not endpoint.endswith("/chat/completions"):
            endpoint = f"{endpoint}/chat/completions"
    else:
        endpoint = "https://api.openai.com/v1/chat/completions"

    headers = {
        "Content-Type": "application/json",
    }
    if config.api_key:
        headers["Authorization"] = f"Bearer {config.api_key}"

    payload: Dict[str, Any] = {
        "model": config.model,
        "messages": [
            {"role": "system", "content": system_instruction},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 1024,
    }
    if response_json:
        payload["response_format"] = {"type": "json_object"}

    async with httpx.AsyncClient(timeout=config.timeout_sec) as client:
        resp = await client.post(endpoint, json=payload, headers=headers)
        if resp.status_code != 200:
            provider_label = "OpenAI-Compatible" if is_compatible else "OpenAI"
            return None, f"{provider_label} API 오류 (HTTP {resp.status_code}): {resp.text[:120]}"
        data = resp.json()
        choices = data.get("choices", [])
        if not choices:
            return None, "응답 choices 비어있음"
        msg = choices[0].get("message", {})
        return (msg.get("content") or "").strip(), None


async def _call_claude_rest(
    prompt: str,
    system_instruction: str,
    config: AIConfig,
    response_json: bool = False,
) -> Tuple[Optional[str], Optional[str]]:
    """Anthropic Claude REST Messages API 호출"""
    if not config.api_key:
        return None, "Anthropic Claude API 키 미설정"

    url = "https://api.anthropic.com/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": config.api_key,
        "anthropic-version": "2023-06-01",
    }

    # Claude JSON 지시사항 보강
    eff_system = system_instruction
    if response_json and "JSON" not in eff_system:
        eff_system += "\nOutput strict valid JSON format only."

    payload = {
        "model": config.model,
        "system": eff_system,
        "messages": [
            {"role": "user", "content": prompt},
        ],
        "temperature": 0.2,
        "max_tokens": 1024,
    }

    async with httpx.AsyncClient(timeout=config.timeout_sec) as client:
        resp = await client.post(url, json=payload, headers=headers)
        if resp.status_code != 200:
            return None, f"Claude API 오류 (HTTP {resp.status_code}): {resp.text[:120]}"
        data = resp.json()
        content = data.get("content", [])
        if not content:
            return None, "Claude 응답 content 비어있음"
        texts = [c.get("text", "") for c in content if c.get("type") == "text"]
        return "".join(texts).strip(), None


# ==============================================================================
# 통합 Multi-Provider 실행 진입점
# ==============================================================================

async def call_multi_provider_llm(
    prompt: str,
    system_instruction: str,
    response_json: bool = False,
    role: Optional[str] = None,
    provider: Optional[str] = None,
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
    timeout_sec: float = 5.0,
    gemini_caller: Optional[Any] = None,
) -> LLMResult:
    """
    구성된 프로바이더에 따라 안전하게 LLM API를 호출하고,
    미설정/타임아웃/오류 시 규칙 기반 Fallback 메타데이터를 온전히 반환합니다.
    gemini_caller가 지정되면 (단위 테스트 mock 등) 해당 함수를 최우선으로 경유합니다.
    """
    config = resolve_ai_config(
        role=role,
        provider_override=provider,
        model_override=model,
        api_key_override=api_key,
        base_url_override=base_url,
        timeout_sec=timeout_sec,
    )

    try:
        # 1. Google Gemini
        if config.provider == "gemini":
            if gemini_caller is not None:
                text = await gemini_caller(
                    prompt=prompt,
                    system_instruction=system_instruction,
                    response_json=response_json,
                    timeout_sec=config.timeout_sec,
                    model_override=config.model,
                    api_key_override=config.api_key,
                )
                if text is not None:
                    return LLMResult(
                        text=text,
                        provider="gemini",
                        model=config.model,
                        is_fallback=False,
                    )
                return LLMResult(
                    text=None,
                    provider="gemini",
                    model=config.model,
                    is_fallback=True,
                    fallback_reason="Gemini API 키 미설정 또는 호출 오류 (규칙 기반 엔진으로 폴백)",
                )

            text, err = await _call_gemini_rest(prompt, system_instruction, config, response_json)
            if text is not None:
                return LLMResult(text=text, provider="gemini", model=config.model, is_fallback=False)
            return LLMResult(text=None, provider="gemini", model=config.model, is_fallback=True, fallback_reason=err)

        # 2. OpenAI
        elif config.provider == "openai":
            text, err = await _call_openai_rest(prompt, system_instruction, config, response_json, is_compatible=False)
            if text is not None:
                return LLMResult(text=text, provider="openai", model=config.model, is_fallback=False)
            return LLMResult(text=None, provider="openai", model=config.model, is_fallback=True, fallback_reason=err)

        # 3. Anthropic Claude
        elif config.provider == "claude":
            text, err = await _call_claude_rest(prompt, system_instruction, config, response_json)
            if text is not None:
                return LLMResult(text=text, provider="claude", model=config.model, is_fallback=False)
            return LLMResult(text=None, provider="claude", model=config.model, is_fallback=True, fallback_reason=err)

        # 4. OpenAI-Compatible Custom Endpoint
        elif config.provider == "openai_compatible":
            text, err = await _call_openai_rest(prompt, system_instruction, config, response_json, is_compatible=True)
            if text is not None:
                return LLMResult(text=text, provider="openai_compatible", model=config.model, is_fallback=False)
            return LLMResult(text=None, provider="openai_compatible", model=config.model, is_fallback=True, fallback_reason=err)

        else:
            return LLMResult(
                text=None,
                provider=config.provider,
                model=config.model,
                is_fallback=True,
                fallback_reason=f"지원되지 않는 AI 프로바이더: {config.provider}",
            )

    except httpx.TimeoutException:
        logger.warning(f"[{config.provider.upper()}_TIMEOUT] 호출 타임아웃 ({config.timeout_sec}s 초과) - 규칙 기반 폴백 전환")
        return LLMResult(
            text=None,
            provider=config.provider,
            model=config.model,
            is_fallback=True,
            fallback_reason=f"{config.provider} API 호출 타임아웃 ({config.timeout_sec}s 초과)",
        )
    except Exception as e:
        logger.warning(f"[{config.provider.upper()}_EXCEPTION] API 호출 실패 ({e}) - 규칙 기반 폴백 전환")
        return LLMResult(
            text=None,
            provider=config.provider,
            model=config.model,
            is_fallback=True,
            fallback_reason=f"{config.provider} API 호출 예외: {str(e)}",
        )
