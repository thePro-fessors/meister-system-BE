"""
core/text_analyzer.py - 경량 한국어 형태소 분해 및 N-gram 유사도 분석 모듈
"""

import re
from typing import Dict, List, Set, Tuple

# 한국어 주요 조사 및 어미 목록 (제거 및 분리용)
KOREAN_STOPWORDS_SUFFIXES = [
    "을", "를", "이", "가", "은", "는", "에", "에서", "의", "으로", "로", "과", "와",
    "하고", "이나", "나", "에게", "한테", "께", "부터", "까지", "마다",
    "했습니다", "합니다", "했다", "한다", "하여", "취득함", "취득했음", "취득", "합격함", "합격했음", "합격",
    "이수함", "이수했음", "이수", "수료함", "수료했음", "수료", "참가함", "참가했음", "참가",
    "제출함", "제출했음", "제출", "활동함", "활동했음", "활동", "참여함", "참여했음", "참여",
]


def extract_keywords_and_nouns(text: str) -> List[str]:
    """
    비정형 한국어 문장에서 기호 및 불용어를 정제하고 핵심 키워드/명사 토큰을 추출합니다.
    (1글자 단어 제외, 2글자 이상 의미 토큰만 추출)
    """
    if not text:
        return []

    # 1. 한글, 영문, 숫자 외 기호 공백 치환
    clean = re.sub(r"[^가-힣a-zA-Z0-9\s]", " ", text)
    tokens = clean.split()

    extracted: Set[str] = set()

    for token in tokens:
        token_lower = token.lower()
        if len(token_lower) >= 2:
            extracted.add(token_lower)

        # 조사/어미 분리
        for suffix in KOREAN_STOPWORDS_SUFFIXES:
            if token_lower.endswith(suffix) and len(token_lower) > len(suffix) + 1:
                base = token_lower[: -len(suffix)]
                if len(base) >= 2:
                    extracted.add(base)

    return sorted(list(extracted), key=lambda x: -len(x))


def calculate_ngram_similarity(s1: str, s2: str, n: int = 2) -> float:
    """
    문자열 간 N-gram 자카드 유사도(0.0 ~ 1.0)를 계산하여 오탈자 및 띄어쓰기 차이를 보정합니다.
    """
    clean1 = s1.replace(" ", "").lower()
    clean2 = s2.replace(" ", "").lower()

    if not clean1 or not clean2:
        return 0.0

    if clean1 == clean2:
        return 1.0

    if len(clean1) < n or len(clean2) < n:
        return 0.0

    ngrams1 = set(clean1[i:i + n] for i in range(len(clean1) - n + 1))
    ngrams2 = set(clean2[i:i + n] for i in range(len(clean2) - n + 1))

    intersection = ngrams1.intersection(ngrams2)
    union = ngrams1.union(ngrams2)

    return len(intersection) / len(union) if union else 0.0


def match_keywords_with_morphology(
    text: str,
    target_keywords: List[str],
    similarity_threshold: float = 0.75,
) -> Tuple[List[str], float]:
    """
    비정형 텍스트와 대상 키워드 목록 간의 형태소 분해 및 N-gram 유사도를 종합 매칭합니다.
    - 반환값: (매칭된 키워드 목록, 종합 신뢰도 점수 0.0 ~ 1.0)
    """
    extracted_tokens = extract_keywords_and_nouns(text)
    text_compact = text.replace(" ", "").lower()

    matched_keywords: Set[str] = set()
    max_score = 0.0

    for kw in target_keywords:
        kw_lower = kw.lower()
        kw_compact = kw_lower.replace(" ", "")

        if len(kw_compact) < 2:
            continue

        # 1. 완전 일치 또는 부분 포함
        if kw_compact in text_compact:
            matched_keywords.add(kw)
            max_score = max(max_score, 0.95)
            continue

        # 2. 토큰 단위 형태소 매칭
        for token in extracted_tokens:
            if kw_lower == token:
                matched_keywords.add(kw)
                max_score = max(max_score, 0.95)
                break

            if kw_lower in token:
                matched_keywords.add(kw)
                max_score = max(max_score, 0.85)
                break

            if len(token) >= 2 and token in kw_lower and len(token) >= len(kw_lower) * 0.6:
                matched_keywords.add(kw)
                max_score = max(max_score, 0.8)
                break

            # 3. N-gram 오탈자 유사도 검사
            sim = calculate_ngram_similarity(kw_compact, token, n=2)
            if sim >= similarity_threshold:
                matched_keywords.add(kw)
                max_score = max(max_score, round(sim, 2))
                break

    matched_list = sorted(list(matched_keywords))
    if matched_list and max_score == 0.0:
        max_score = 0.7

    return matched_list, max_score
