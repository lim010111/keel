# reduction_config.py — 전사본 축소 파라미터. 여기만 고친다.
#
# prepare_review.py(축소 스크립트)와 evaluator-transcript.md(프롬프트 템플릿)가
# 둘 다 이 파일을 읽는다. 값을 바꾸면 코드 수정 없이 양쪽에 동시 반영된다.
# 잘못 고치면 import 시점에 _validate()가 즉시·명확하게 실패시킨다.

# --- 전역 ---
TARGET_TOKENS   = 120_000   # 단계 축소의 유일한 멈춤 조건 (토큰)
CHARS_PER_TOKEN = 4         # 토큰 추정 제수 (chars / 4)

# --- A4: 항상 적용 ---
READ_HEAD_LINES = 10        # Read 도구 결과 본문에서 남길 줄수

# --- S1: tool_result 본문 (Read·error 제외) ---
S1_HEAD_LINES, S1_TAIL_LINES, S1_MAX_CHARS = 30, 10, 2000

# --- S2: Write/NotebookEdit content, Edit old/new_string ---
S2_FIELD_MAX_CHARS = 400

# --- S3: Bash 결과 / Grep·Glob 결과 ---
S3_BASH_HEAD, S3_BASH_TAIL, S3_GREP_FIRST = 20, 8, 5

# --- S4: tool_result 전체 (error 포함), 더 공격적으로 ---
S4_HEAD_LINES, S4_TAIL_LINES, S4_MAX_CHARS = 8, 3, 600

# --- S5: thinking 블록 ---
S5_THINKING_MAX_CHARS = 600
# (S6 = thinking 통째 삭제. 파라미터 없음.)


def _validate():
    g = globals()
    required = ["TARGET_TOKENS", "CHARS_PER_TOKEN", "READ_HEAD_LINES",
                "S1_HEAD_LINES", "S1_TAIL_LINES", "S1_MAX_CHARS",
                "S2_FIELD_MAX_CHARS", "S3_BASH_HEAD", "S3_BASH_TAIL",
                "S3_GREP_FIRST", "S4_HEAD_LINES", "S4_TAIL_LINES",
                "S4_MAX_CHARS", "S5_THINKING_MAX_CHARS"]
    for k in required:
        if k not in g:
            raise ValueError(f"reduction_config: 키 누락 — {k}")
        if not isinstance(g[k], int) or g[k] <= 0:
            raise ValueError(
                f"reduction_config: {k}는 양의 정수여야 함 (현재 {g[k]!r})")
    # 단조성: 후행 S4가 선행 S1보다 약하게 자르면 S4가 죽은 단계가 된다.
    if S4_HEAD_LINES > S1_HEAD_LINES or S4_MAX_CHARS > S1_MAX_CHARS:
        raise ValueError(
            "reduction_config: S4_HEAD_LINES <= S1_HEAD_LINES 이고 "
            "S4_MAX_CHARS <= S1_MAX_CHARS 여야 함 — 아니면 S4 단계가 무의미.")


_validate()
