---
name: third-party-review
description: Runs an independent third-party review by feeding the subject to external models (Codex, Gemini, and a separate Claude) so they judge — from outside the human/main-agent pair — what will cause fallout later. The default subject is the current Claude Code session (deterministically reduced transcript): whether the human and agent are aligned AND whether what they decided is sound. It can also review arbitrary targets — files, design docs, other artifacts — either alongside the transcript (pinned evidence) or on their own. Use when the user invokes /third-party-review, asks for an outside or 제3자 review of the session or of specific files/designs, wants to check alignment or whether some 내용·결과물 is sound, or mentions 후폭풍 / 세션 평가 / 제3자 시점 / 외부 모델 평가 / 대상 리뷰.
argument-hint: "[codex|agy|claude|all] [path] [only] · or a prompt"
---

# Third-Party Review

main agent도 인간도 아닌 **제3자(다른 모델)**에게 평가시킨다. 기본 대상은 세션이고 —
*인간↔agent 대화의 궤적* **그리고 그들이 결정·산출한 내용이 타당한지** — 지금
합의·타당해 보여도 나중에 후폭풍이 날 간극·결함을 찾는다. 파일·설계문서 등 임의 대상도
같은 제3자 posture로 리뷰할 수 있다(§ Arguments — 평가자·대상·모드).

## Arguments — 평가자 · 대상 · 모드

`$ARGUMENTS`는 자연어다. main agent가 읽어 **평가자 · 리뷰 대상 · 모드** 셋을 뽑아
`prepare_review.py` 플래그로 옮긴다. 슬래시 인자든 평범한 자연어 요청이든 같은 규칙.

**평가자** — `all`(기본) / `both` / `codex` / `agy` / `claude`. 없으면 `all`.

- `all` — 셋 다(codex + agy + claude). 세 평결의 합집합(앙상블).
- `both` — 교차 패밀리 쌍(codex + agy). claude 제외.
- `codex` / `agy` / `claude` — 하나만.

세 reviewer는 **완전히 대등**하다 — claude도 codex·agy와 동일하게 취급하고
평결을 약하게 표시하지 않는다. reviewer별 model(+ codex effort)은
`reviewers.toml`에서 선언한다(선택사항·기본값 내장). 근거는
[DESIGN.md](DESIGN.md) § 평가자.

**리뷰 대상 · 모드** — 기본 대상은 세션 transcript. 사용자가 파일·설계문서 등을
지명하면 타깃이 된다:

| 사용자 의도 | 모드 | 플래그 |
|---|---|---|
| (대상 미지정) | transcript-only | (없음) |
| "X 도 같이 / 추가로" | additive — transcript + X (고신호 증거) | `--target X` |
| "transcript 빼고 / X 만 / only X" | target-only — X만 | `--target X --only` |

`--target`은 파일·디렉토리 경로(여러 개면 반복). 붙여넣은 텍스트는 먼저 파일로 떨군 뒤
그 경로를 넘긴다. additive 타깃은 축소로 날아간 *실제 파일*을 복원해 **내용 타당성(축3)**
판단의 증거가 된다(transcript 모드 출력 6섹션). target-only는 산출물 자체를 평가
(출력 4섹션). 모드별 출력 구조는 [DESIGN.md](DESIGN.md) § 대상·모드.

**worked examples** (자연어 → 동작):

- `/third-party-review` → transcript × all
- "codex 로만" → transcript × codex
- "docs/adr/0031.md 도 같이 제3자 리뷰" → additive · `--target docs/adr/0031.md` · all
- "transcript 말고 src/foo.py 만 봐줘" → only · `--target src/foo.py --only` · all
- "이 설계 both 로 단독 리뷰" (+경로) → only · `--target <경로> --only` · both

**echo-before-launch** — 비-기본 호출(타깃·only 포함)이면 평가(분 단위·비용)를 띄우기
*전에* 해소한 계획을 한 줄로 되읽는다: 예) `transcript 제외 · 타깃=src/foo.py ·
reviewers=all — 진행?`. 경로는 **실제 파일로 해소해 명시**한다("내가 쓴 파일" 같은 지시는
어느 경로인지 확정). 진짜 모호할 때(additive/only 신호 없는 타깃, 경로 미해소)만 질문 1개.
순수 기본 호출은 echo 생략. 스크립트가 타깃을 `errors`(미존재)로 보고하면 평가를 진행하지
말고 사용자에게 알린다.

## Workflow

1. **입력 준비** — `python3 <skill>/scripts/prepare_review.py` 를 위에서 파싱한 플래그
   (`--target <path>` 반복·`--only`)와 함께 실행. transcript 모드면 세션 JSONL을 찾아
   결정론적으로 축소하고, 타깃이 있으면 `.tpr/targets/`로 복사한 뒤 `.tpr/prompt.txt`를
   만든다. stdout JSON의 `mode`·`targets`·`warnings`·`errors`를, transcript 모드면
   `stats`(원본/축소 토큰, 적용 단계)도 사용자에게 한 줄로 보고한다. `warnings`(큰 타깃)·
   `errors`(미존재 타깃)가 있으면 반드시 알린다. `core_exceeds_target`이 true면 "세션이
   너무 커서 핵심 대화만으로도 예산 초과 — 평가가 부분적일 수 있음"을 알린다.
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
   (로컬 전용, 커밋 diff 없음). 타깃은 `.tpr/targets/`에 스냅샷 복사된다(매 실행
   새로 만들고 이전 타깃은 지움) — `.tpr/` 아래라 같은 exclude로 덮인다.

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
   codex exec -m <MODEL> -c model_reasoning_effort="<EFFORT>" --sandbox read-only < .tpr/prompt.txt > .tpr/eval-codex.md
   agy   -p   --model "<MODEL>" --sandbox                     < .tpr/prompt.txt > .tpr/eval-agy.md
   CLAUDE_CODE_DISABLE_AUTO_MEMORY=1 claude -p --model <MODEL> --tools "Read Grep Glob" --add-dir "$PWD" < .tpr/prompt.txt > .tpr/eval-claude.md
   ```
   - **프롬프트는 stdin으로** 넣는다(`< .tpr/prompt.txt`) — argv 위치인자가 아니다.
     축소 transcript는 크고(설계 상한 ≈120k 토큰 ≈ 480KB) 단일 argv 원소는 커널
     `MAX_ARG_STRLEN`(≈128KB)을 넘겨 `execve`가 E2BIG로 죽는다 — 즉 큰 세션일수록
     깨진다. 세 CLI 모두 위치인자가 없으면 프롬프트를 stdin에서 읽는다(검증).
   - **타임아웃 백스톱 없음** — 명령에 `timeout` 래퍼가 없다. codex(xhigh)는 수
     분이 정상이고(merge-gate produce 510s 관측) 타이트한 타임아웃은 정상 평가를
     잘라낸다. 대신 이 스킬은 세션 *안에서* 대화형으로 돌아 행(hang)이 보이면 main
     agent가 중단할 수 있다(자동 pre-push 훅과 다른 점). 행이 길어지면 끊고 재시도.
   - **읽기 전용.** codex는 `--sandbox read-only`로 *모델이 생성하는 셸 명령*을
     OS 레벨 샌드박스로 쓰기 차단한다(codex --help: "model-generated shell
     commands" — codex 프로세스 자신의 로그 쓰기는 별개지만 평가자가 프로젝트에
     쓰는 경로가 OS로 막혀 셋 중 가장 강함). **claude**는 `--tools "Read Grep Glob"`로 *모델의*
     쓰기 도구(Edit/Write/Bash)를 제거한다 — 단 이건 codex의 프로세스 샌드박스와
     달리 운영자의 SessionStart/Stop *훅*(셸 실행 — 예: `status.py`의 STATUS.md
     재생성)까지 막지는 못한다. 다만 이 스킬은 항상 세션 *안에서* 돌아 nested(child)
     세션으로 실행되고, 그 덕에 그 훅들이 발화하지 않는다(검증: 실행 전후 STATUS.md
     mtime 불변). 그래서 `CLAUDE_CODE_*`/`CLAUDECODE` 세션 마커는 **벗기지 말 것** —
     벗기면 fresh 세션이 돼 Stop 훅이 STATUS.md를 다시 쓰고, `--add-dir`만으론
     parity도 못 지킨다. (드물게 nested `-p`가 빈 출력을 내면 3·5단계 가용성 판정이
     실패로 잡아내 fail-safe.) 정리: claude는 모델-도구 레벨 read-only — codex보다
     약하고 agy보다 강하다. **agy**만 강제 수단이 전무하다 — `--sandbox`로도
     프로젝트에 파일을 쓸 수 있음이 검증됨. agy의 read-only는 평가자 프롬프트
     (`evaluator-common.md`) 지시에만 의존한다(미집행). 어느 쪽이든 `--dangerously-*` 플래그는 절대 금지.
   - **claude는 auto-memory만 제거해 codex·agy와 프로젝트-컨텍스트 parity를 맞춘다
     (전역 CLAUDE.md·STATUS는 codex·agy엔 없는 잔여 비대칭이지만 무해해 유지).**
     `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`(프로세스 한정 — 전역 env 불변)로 main
     agent 전용 auto-memory만 끊어 codex·agy와 **동일한 프로젝트-컨텍스트 상태**로
     맞춘다. STATUS 보드와 전역 CLAUDE.md는 무해하고, 제거하려면 프로젝트
     AGENTS.md까지 날려 parity가 깨지므로 그대로 둔다. `--bare`는 구독 로그인을
     깨고(유료 API 키 요구) parity도 떨어뜨려 쓰지 않는다.
   - **agy 쓰기 탐지** — agy만 read-only 미집행이므로 실행 *전후*로 프로젝트
     변경을 탐지한다(codex는 OS 샌드박스로 집행; claude는 모델-도구 제거[CLI 집행]
     +child-세션 훅 억제[가정 — 실행 전후 mtime로 1회 검증했을 뿐 런타임 상시
     확인은 아님]로 쓰기 경로가 없다고 보아 생략 — agy식 git-status write-detect는
     claude엔 없다). git 저장소면 실행 직전
     `git status --porcelain`을 저장하고 실행 후 다시 떠 비교한다 (`.tpr/`는
     `.git/info/exclude`에 있어 자동 제외). 차이가 나면 agy가 프로젝트를 건드린
     것 — 5단계에서 그 diff를 사용자에게 크게 경고한다. git 저장소가 아니면 탐지
     불가임을 알린다.
   - 평가자가 transcript를 못 읽으면 프로젝트 디렉토리를 명시 추가해 재시도
     (`agy --add-dir <cwd>`; claude는 위 명령에 이미 `--add-dir "$PWD"` 포함).
   - 결과 파일은 stdout 리다이렉트로 *셸*이 만든다 — 평가자가 쓰는 게 아니다.

5. **결과 검증 후 제시 — 순서가 핵심**:
   - 각 `.tpr/eval-*.md`가 *실제 평가*인지 먼저 확인한다: `평결` 헤더(모드별
     6섹션[transcript]/4섹션[target] 구조)가 있어야 한다. 없으면(예:
     "Authentication required", 빈 파일, 에러
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

- `scripts/prepare_review.py` — JSONL 탐색 + 결정론적 축소 + 타깃 복사 + 프롬프트 조립
- `reduction_config.py` — 축소 파라미터 (자기검증). 튜닝은 여기만.
- `reviewers.toml` — reviewer별 model(+ codex effort) 설정. 선택사항·기본값 내장.
- `evaluator-common.md` — 공통 페르소나(posture·read-only·증거기반·출력형식). 두 body가 공유.
- `evaluator-transcript.md` — transcript 모드 body(궤적 증거규칙 + 6섹션). `{{COMMON}}` 포함.
- `evaluator-target.md` — target 모드 body(타깃 manifest + 4섹션). `{{COMMON}}` 포함.
- 설계 근거는 [DESIGN.md](DESIGN.md)
