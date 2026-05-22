---
name: third-party-review
description: Runs an independent third-party review of the current Claude Code session. Deterministically reduces the session transcript and feeds it to external models (Codex, Gemini) so they can judge — from outside the human/main-agent pair — whether the conversation has diverged in ways that cause trouble later. Use when the user invokes /third-party-review, asks for an outside or 제3자 review of the session, wants to check whether the human and the agent are actually aligned, or mentions 후폭풍 / 세션 평가 / 제3자 시점 / 외부 모델 평가.
---

# Third-Party Review

세션을 main agent도 인간도 아닌 **제3자(다른 모델)**에게 평가시킨다. 평가 대상은
코드가 아니라 *인간↔agent 대화의 궤적* — 지금 합의된 줄 알지만 나중에 후폭풍이
날 간극을 찾는다.

## Argument

`both`(기본) / `codex` / `agy` — 평가자 선택. 인자 없으면 `both`.

## Workflow

1. **입력 준비** — `python3 <skill>/scripts/prepare_review.py` 실행. 현재 세션
   JSONL을 찾아 결정론적으로 축소하고 `.tpr/transcript.md` + `.tpr/prompt.txt`를
   만든다. stdout의 JSON `stats`(원본/축소 토큰, 적용 단계)를 사용자에게 한 줄로
   보고한다. `core_exceeds_target`이 true면 "세션이 너무 커서 핵심 대화만으로도
   예산 초과 — 평가가 부분적일 수 있음"을 알린다.

2. **`.tpr/` 산출물** — `.tpr/`는 평가 산출물 디렉토리. **추적 중인
   `.gitignore`는 건드리지 말 것** — 평가 한 번 돌렸다고 repo에 diff가 생기면
   안 된다. git 저장소면 `.git/info/exclude`에 `.tpr/`가 없을 때만 추가한다
   (로컬 전용, 커밋 diff 없음).

3. **평가자 가용성 확인** — `command -v codex`, `command -v agy`. 단 바이너리
   존재 ≠ 사용 가능: codex·agy 모두 인증이 안 됐거나 실패해도 **exit 0**으로
   끝나며 출력에 에러 텍스트만 남는다. 가용성은 4단계 실행 후 출력 내용으로
   판정한다. 요청한 평가자가 아예 없으면 사용자에게 알리고, 다른 하나로 바꿀지
   / Claude 서브에이전트 fallback을 쓸지 물어본다. (조용히 실패하지 말 것.)

4. **평가 실행** — 요청 ∩ 가용 평가자마다. `both`면 둘을 동시(병렬)로:
   ```
   codex exec "$(cat .tpr/prompt.txt)" --sandbox read-only      > .tpr/eval-codex.md
   agy   -p   "$(cat .tpr/prompt.txt)" --sandbox                 > .tpr/eval-agy.md
   ```
   - 둘 다 **read-only**: 평가자는 판사이지 작업자가 아니다. write/실행 플래그
     (`--dangerously-*`) 절대 금지.
   - 평가자가 transcript를 못 읽으면 프로젝트 디렉토리를 명시 추가해 재시도
     (`agy --add-dir <cwd>`).
   - 결과 파일은 stdout 리다이렉트로 *셸*이 만든다 — 평가자가 쓰는 게 아니다.

5. **결과 검증 후 제시 — 순서가 핵심**:
   - 각 `.tpr/eval-*.md`가 *실제 평가*인지 먼저 확인한다: `평결` 헤더(5섹션
     구조)가 있어야 한다. 없으면(예: "Authentication required", 빈 파일, 에러
     덤프) 그 평가자는 **실패**다 — 평가인 척 보여주지 말고 실패 사실과 stderr를
     사용자에게 알린다.
   - 검증을 통과한 평가만 **그대로, 평가자별로 따로** 사용자에게 보여준다.
     요약·수정·필터링 금지. 두 평가를 하나로 머지하지 말 것 — 불일치 자체가 신호다.
   - **그다음에야** main agent가 그 평가에 대한 자기 입장을 *별도 섹션*으로 덧붙인다.
     이건 평가를 *대체*하는 게 아니라 *추가*하는 레이어다.

## Claude 서브에이전트 fallback

codex·agy 둘 다 없을 때만. Agent 툴(general-purpose)에 `.tpr/prompt.txt` 내용을
프롬프트로 넘긴다. 단 사용자에게 "같은 모델 패밀리라 약한 제3자"임을 명시한다.

## 부품

- `scripts/prepare_review.py` — JSONL 탐색 + 결정론적 축소 + 프롬프트 조립
- `reduction_config.py` — 축소 파라미터 (자기검증). 튜닝은 여기만.
- `evaluator-prompt.md` — 평가자 페르소나 프롬프트 템플릿
- 설계 근거는 [DESIGN.md](DESIGN.md)
