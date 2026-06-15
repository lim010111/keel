---
name: third-party-review
description: Runs an independent third-party review of the current Claude Code session. Deterministically reduces the session transcript and feeds it to external models (Codex, Gemini, and a separate Claude) so they can judge — from outside the human/main-agent pair — whether the conversation has diverged in ways that cause trouble later. Use when the user invokes /third-party-review, asks for an outside or 제3자 review of the session, wants to check whether the human and the agent are actually aligned, or mentions 후폭풍 / 세션 평가 / 제3자 시점 / 외부 모델 평가.
---

# Third-Party Review

세션을 main agent도 인간도 아닌 **제3자(다른 모델)**에게 평가시킨다. 평가 대상은
코드가 아니라 *인간↔agent 대화의 궤적* — 지금 합의된 줄 알지만 나중에 후폭풍이
날 간극을 찾는다.

## Argument

`all`(기본) / `both` / `codex` / `agy` / `claude` — 평가자 선택. 인자 없으면 `all`.

- `all` — 셋 다(codex + agy + claude). 세 평결의 합집합(앙상블).
- `both` — 교차 패밀리 쌍(codex + agy). claude 제외.
- `codex` / `agy` / `claude` — 하나만.

세 reviewer는 **완전히 대등**하다 — claude도 codex·agy와 동일하게 취급하고
평결을 약하게 표시하지 않는다. reviewer별 model(+ codex effort)은
`reviewers.toml`에서 선언한다(선택사항·기본값 내장). 근거는
[DESIGN.md](DESIGN.md) § 평가자.

## Workflow

1. **입력 준비** — `python3 <skill>/scripts/prepare_review.py` 실행. 현재 세션
   JSONL을 찾아 결정론적으로 축소하고 `.tpr/transcript.md` + `.tpr/prompt.txt`를
   만든다. stdout의 JSON `stats`(원본/축소 토큰, 적용 단계)를 사용자에게 한 줄로
   보고한다. `core_exceeds_target`이 true면 "세션이 너무 커서 핵심 대화만으로도
   예산 초과 — 평가가 부분적일 수 있음"을 알린다.
   - 세션 JSONL은 선형 로그가 아니라 트리다. 인간이 rewind 후 다시 보내면 버려진
     브랜치가 파일에 그대로 남는다. 스크립트는 `leafUuid`→root 활성 경로만
     추출해 transcript에 담는다 — 버려진 브랜치는 main agent 컨텍스트에 없었으므로
     평가에서 빠져야 한다. `rewound_human_turns`가 0보다 크면 "rewind로 버려진
     인간 턴 N개는 transcript에서 제외됨"을 보고한다. `active_path_reconstructed`가
     false면 `warning`을 그대로 사용자에게 알린다(활성 경로 복원 실패 — transcript에
     버려진 브랜치가 섞였을 수 있음).

2. **`.tpr/` 산출물** — `.tpr/`는 평가 산출물 디렉토리. **추적 중인
   `.gitignore`는 건드리지 말 것** — 평가 한 번 돌렸다고 repo에 diff가 생기면
   안 된다. git 저장소면 `.git/info/exclude`에 `.tpr/`가 없을 때만 추가한다
   (로컬 전용, 커밋 diff 없음).

3. **평가자 가용성 확인** — `command -v codex`, `command -v agy`,
   `command -v claude`. 단 바이너리 존재 ≠ 사용 가능:
   - codex·agy·claude 모두 인증이 안 됐거나 실패해도 **exit 0**으로 끝나며 출력에
     에러 텍스트("Not logged in" 등)만 남는다. 가용성은 4단계 실행 후 출력 내용으로
     판정한다 — `평결` 헤더가 없거나 "Not logged in"이면 그 평가자는 **사용 불가**다.
     claude도 토큰 게이팅 없이 codex·agy와 똑같이 출력으로만 판정한다(구독 로그인을
     그대로 쓴다).
   요청한 평가자가 아예 없으면 사용자에게 알리고 다른 평가자로 바꿀지 물어본다.
   (조용히 실패하지 말 것.)

4. **평가 실행** — 먼저 `<skill>/reviewers.toml`을 읽어 각 reviewer의
   `model`(+ codex `effort`)을 아래 명령의 `<MODEL>`/`<EFFORT>`에 치환한다.
   파일이나 항목이 없으면 reviewers.toml 상단에 문서화된 기본값을 쓴다. agy의
   `--model`은 **best-effort**다 — 설치된 print 모드가 무시하고 자기
   `settings.json` 기본을 쓸 수 있다(교차검증 안 함). 그다음 요청 ∩ 가용
   평가자마다 동시(병렬)로:
   ```
   codex exec "$(cat .tpr/prompt.txt)" -m <MODEL> -c model_reasoning_effort="<EFFORT>" --sandbox read-only  > .tpr/eval-codex.md
   agy   -p   "$(cat .tpr/prompt.txt)" --model "<MODEL>" --sandbox                                          > .tpr/eval-agy.md
   CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 claude -p "$(cat .tpr/prompt.txt)" --model <MODEL> --tools "Read Grep Glob" --add-dir "$PWD" > .tpr/eval-claude.md
   ```
   - **읽기 전용 집행.** codex는 `--sandbox read-only`로, **claude**는
     `--tools "Read Grep Glob"`로 쓰기 도구(Edit/Write/Bash) 자체가 없어 *강제
     차단*된다(codex와 동급, agy보다 강함). **agy**만 강제 수단이 없다 —
     `--sandbox`로도 프로젝트에 파일을 쓸 수 있음이 검증됨. agy의 read-only는
     evaluator-prompt 지시에만 의존한다(미집행). 어느 쪽이든 `--dangerously-*`
     플래그는 절대 금지.
   - **claude는 auto-memory만 제거해 codex·agy와 프로젝트-컨텍스트 parity를 맞춘다
     (전역 CLAUDE.md·STATUS는 codex·agy엔 없는 잔여 비대칭이지만 무해해 유지).**
     `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`(프로세스 한정 — 전역 env 불변)로 main
     agent 전용 auto-memory만 끊어 codex·agy와 **동일한 프로젝트-컨텍스트 상태**로
     맞춘다. STATUS 보드와 전역 CLAUDE.md는 무해하고, 제거하려면 프로젝트
     AGENTS.md까지 날려 parity가 깨지므로 그대로 둔다. `--bare`는 구독 로그인을
     깨고(유료 API 키 요구) parity도 떨어뜨려 쓰지 않는다.
   - **agy 쓰기 탐지** — agy만 read-only 미집행이므로 실행 *전후*로 프로젝트
     변경을 탐지한다(codex·claude는 집행되므로 불필요). git 저장소면 실행 직전
     `git status --porcelain`을 저장하고 실행 후 다시 떠 비교한다 (`.tpr/`는
     `.git/info/exclude`에 있어 자동 제외). 차이가 나면 agy가 프로젝트를 건드린
     것 — 5단계에서 그 diff를 사용자에게 크게 경고한다. git 저장소가 아니면 탐지
     불가임을 알린다.
   - 평가자가 transcript를 못 읽으면 프로젝트 디렉토리를 명시 추가해 재시도
     (`agy --add-dir <cwd>`; claude는 위 명령에 이미 `--add-dir "$PWD"` 포함).
   - 결과 파일은 stdout 리다이렉트로 *셸*이 만든다 — 평가자가 쓰는 게 아니다.

5. **결과 검증 후 제시 — 순서가 핵심**:
   - 각 `.tpr/eval-*.md`가 *실제 평가*인지 먼저 확인한다: `평결` 헤더(5섹션
     구조)가 있어야 한다. 없으면(예: "Authentication required", 빈 파일, 에러
     덤프) 그 평가자는 **실패**다 — 평가인 척 보여주지 말고 실패 사실과 stderr를
     사용자에게 알린다.
   - 4단계 agy 쓰기 탐지에서 차이가 나왔으면 **맨 먼저** 그 사실과 변경 목록을
     경고로 띄운다 — 평가 내용보다 우선한다.
   - 검증을 통과한 평가만 **그대로, 평가자별로 따로** 사용자에게 보여준다.
     요약·수정·필터링 금지. 여러 평가를 하나로 머지하지 말 것 — 불일치 자체가
     신호다. claude도 codex·agy와 **완전히 대등하게** 보여준다 — "같은 패밀리라
     약하다"는 표식은 붙이지 않는다(읽는 사람에게 선제적 음성 편향을 심는다).
   - **그다음에야** main agent가 그 평가에 대한 자기 입장을 *별도 섹션*으로 덧붙인다.
     이건 평가를 *대체*하는 게 아니라 *추가*하는 레이어다.

## 부품

- `scripts/prepare_review.py` — JSONL 탐색 + 결정론적 축소 + 프롬프트 조립
- `reduction_config.py` — 축소 파라미터 (자기검증). 튜닝은 여기만.
- `reviewers.toml` — reviewer별 model(+ codex effort) 설정. 선택사항·기본값 내장.
- `evaluator-prompt.md` — 평가자 페르소나 프롬프트 템플릿
- 설계 근거는 [DESIGN.md](DESIGN.md)
