# Design notes

이 스킬의 설계 결정과 근거. 왜 이렇게 만들었는지 잊지 않기 위한 기록.

## 무엇을 하는 스킬인가

**제3자(main agent도 인간도 아닌 외부 모델)**에게 평가시킨다. 스킬의 정체성은 평가
*posture*(독립·blunt·read-only·외부모델 앙상블)이고 **세션 리뷰가 핵심** —
transcript는 그 posture가 적용되는 *기본 대상*이다. 단 파일·설계문서 등 임의 대상도
일급으로 리뷰할 수 있다(§ 대상·모드).

transcript 모드의 평가 단위는 *인간↔agent 대화의 궤적* **그리고 그들이 결정·산출한
내용의 타당성**. 세 축을 본다:

1. 인간 쪽 — 요구·전제·가정이 적절한가
2. agent 쪽 — 솔루션·방향이 적절하고 인간 요구에 fit하는가, 더 나은 대안은 없나
3. 내용·결과물 자체 — 누가 냈고 둘이 합의했는지와 무관하게, 정착한 결정·추론·산출물이
   실제로 옳고 근거 있는가. **둘 다 합의한 결함도 결함이다.**

메타 목표: 지금은 합의·타당해 보이지만 나중에 **후폭풍**이 날 간극(divergence)과
실질 결함을 조기 탐지. 회고가 아니라 예측.

축3은 원래 명제 *"평가 단위는 코드가 아니라 궤적"*의 **개정**이다. 초기 설계는 코드
리뷰를 배제했으나, 진짜 컨텍스트는 대화뿐 아니라 *에이전트가 다룬·작성한 실제 파일*에
있어 — 그 내용 타당성까지 봐야 정렬을 제대로 판정한다. 단 "무프롬프트 whole-project
audit"은 여전히 배제하고 궤적·핀으로 박은 타깃을 따른다.

## 핵심 결정

**컨텍스트 전달.** sub-agent는 빈 컨텍스트로 시작하므로 세션 내용을 자동 상속하지
않는다. 두 경로 중 — (A) main agent가 요약해 넘김, (B) 세션 JSONL 원본 직독 —
**(B)**. 평가 대상(main agent)이 증거를 편집하면 평가가 무의미하기 때문.

**축소.** 긴 세션 JSONL은 평가자 컨텍스트를 넘긴다. 축소는 *기계적·결정론적*으로,
main agent가 아니라 스크립트가 한다(편향 차단). 구조는 두 종류:
- A(항상): 구조적 노이즈 제거 — file-history/isMeta/isCompactSummary/isSidechain
  삭제, Read 결과 본문은 head N줄로 붕괴.
- A3b(항상): rewind 브랜치 제거. 세션 JSONL은 append-only 트리 — 인간이 rewind 후
  재전송하면 버려진 브랜치가 파일에 남는다(파일 끝에 올 수도 있어 file-order로는
  못 거른다). `leafUuid`→`parentUuid` 활성 경로만 추출한다. 버려진 브랜치는 main
  agent 컨텍스트에 없었으므로 평가에 들어가면 거짓 divergence를 만든다. 버려진
  인간 턴 수는 `rewound_human_turns`로 헤더에 남겨 마찰 신호로 보존한다.
  `leafUuid`가 없으면 복원 실패 — 선형 전사를 유지하고 헤더 `warning`으로 경고.
- B(예산 초과 시): 단일 전역 예산 `TARGET_TOKENS` 밑으로 갈 때까지 S1~S6을 고정
  순서로 적용. 단계별 예산은 없다 — 예산은 최종 출력물의 속성이고, 단계는 *무엇을
  먼저 희생하나*의 우선순위. 저신호(tool 결과)부터, 고신호(`thinking`)는 최후.
- user 턴 원문과 AI text는 **불가침**. 이것만으로 예산 초과 시 잘라내지 않고
  헤더에 경고만 남긴다.

**파라미터 분리.** 축소 상수는 `reduction_config.py` 한 곳. 스크립트와 프롬프트
템플릿이 둘 다 읽어 단일 진실 출처가 된다(예: `READ_HEAD_LINES`가 프롬프트의
"first N lines" 문장과 동기화). import 시 `_validate()`가 잘못된 값을 즉시 차단.

**평가자.** 셋 다 외부 CLI 셸 호출 — codex(gpt 패밀리), agy(Gemini 패밀리),
claude. 플러그인 대신 셸인 이유: codex 플러그인은 Codex 전용이라 셋을 한 방식으로
못 부른다.

다른 모델 패밀리(Codex/Gemini)는 가중치가 달라 자명한 제3자다. claude는 가중치를
main agent와 공유하지만 **컨텍스트가 분리**돼 독립적 관점을 낸다 — 제3자성을 만드는
건 가중치가 아니라 *컨텍스트의 분리*라는 판단. 그래서 claude도 1급 reviewer이고,
출력에서 codex·agy와 **완전히 대등하게** 다룬다 — "같은 패밀리라 약하다"는 표식은
읽는 사람에게 선제적 음성 편향을 심으므로 붙이지 않는다. 세 평결의 합집합(앙상블)이
목적.

목표는 "최대한 빈 컨텍스트"가 아니라 **codex·agy와 동일한 컨텍스트 상태(parity)**다.
codex·agy는 프로젝트 cwd에서 프로젝트 AGENTS.md + 소스를 읽고 돈다(이 repo AGENTS.md:
*"AGENTS.md is the canonical agent guidance — read directly by Codex CLI and
antigravity"*). 그래서 claude도 비-bare `claude -p`를 구독 인증(`/login`)으로,
프로젝트 cwd에서 돌려 같은 프로젝트 컨텍스트(AGENTS.md/CLAUDE.md + 소스)를 갖게 한다.
단 하나의 비대칭 — main agent *전용* auto-memory(codex/agy에 대응물 없는 사적
교차세션 저장소)만 `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1`(프로세스 한정·전역 env
불변)로 제거한다. STATUS 보드(SessionStart 훅, 커밋된 STATUS.md 유래)와 전역
CLAUDE.md는 무해하고, 끄려면 중립 cwd나 `DISABLE_CLAUDE_MDS`가 필요한데 그건 프로젝트
AGENTS.md까지 날려 parity 아래로 떨어뜨리므로 **유지**한다. `--bare`는 (1) OAuth를
안 읽어 유료 `ANTHROPIC_API_KEY`를 요구하고 (2) 프로젝트 AGENTS.md까지 끊어 parity를
깨므로 쓰지 않는다. (이전의 in-session Agent-툴 fallback은 평가 대상인 main agent가
자기 채점자를 띄우는 구조라 폐기 — headless claude가 같은 역할을 독립 컨텍스트·
read-only 집행으로 더 낫게 한다.)

**권한.** 평가자는 판사이지 작업자가 아니다 → read-only. 프로젝트 *읽기*는 허용
(축 2 판단 + Read 본문을 축소로 버렸으므로 복구 경로). `--dangerously-*` 금지.
codex는 `--sandbox read-only`로 *모델이 생성하는 셸 명령*을 OS 레벨 샌드박스로 쓰기
차단한다(codex --help: "model-generated shell commands"; codex 프로세스 자신의 로그
쓰기는 별개지만 평가자가 프로젝트에 쓰는 경로가 OS로 막힌다). claude는
`--tools "Read Grep Glob"`로 *모델의* 쓰기 도구(Edit/Write/Bash)를 없앤다 — 단 codex의
프로세스 샌드박스와 달리 운영자 SessionStart/Stop 훅(셸 — 예: `status.py`의 STATUS.md
재생성)은 못 막는다. 이 스킬은 세션 안에서 nested(child) 세션으로 돌아 그 훅이
발화하지 않으므로(검증: 실행 전후 STATUS.md mtime 불변) 모델-도구 레벨 read-only가
성립한다 — codex(프로세스 샌드박스)보다 약하고 agy보다 강하다. 그래서 `CLAUDE_CODE_*`
세션 마커는 벗기지 않는다(fresh 세션이 되면 Stop 훅이 STATUS.md를 다시 쓴다). agy는
read-only 강제 플래그가 없다(검증함 — `--sandbox`로도 프로젝트에 씀). 그래서 agy의
read-only는 평가자 프롬프트(`evaluator-common.md`) 지시에만 의존하며, 보완책으로 agy 실행 전후 `git status`
스냅샷을 비교해 쓰기를 *탐지*한다(예방이 아니라 탐지).

**입력/출력 경로.** 페르소나 프롬프트는 codex·agy·claude에 동일 투입(다른 건
모델, 그리고 codex의 effort뿐). reviewer별 모델/effort는 `reviewers.toml` 한
곳에서 선언한다 — 선택사항이며 기본값 내장. 세 CLI는 비대칭이다: codex만 model과
effort가 둘 다 진짜 per-invocation 플래그, agy는 effort가 모델 라벨에 박혀 있고
per-invocation `--model`이 best-effort(설치 빌드의 print 모드가 무시할 수 있음 —
교차검증 안 함), claude는 model만 있고 effort 노브가 없다.
프롬프트는 argv 위치인자가 아니라 stdin으로 넣는다(세 CLI 모두 위치인자가 없으면
stdin을 읽음 — 검증) — 큰 transcript(설계 상한 ≈120k 토큰)가 단일 argv 한도
(`MAX_ARG_STRLEN`≈128KB, 초과 시 execve E2BIG)를 넘겨 죽지 않게.
평가는 stdout으로 나오고 셸이 파일로 캡처 — 평가자는 write 권한 0이어도 된다.
결과는 사람이 raw로 먼저 보고, main agent 반응은 *나중에 별도로* 덧붙는다
(출력단에서도 평가 대상이 자기 성적표를 필터링하지 못하게).

**셋 다 vs 부분집합.** 기본 `all`(codex·agy·claude). `both`는 교차 패밀리 쌍
(codex·agy)만. 평가들을 머지하지 않고 따로 제시 — 머지하려면 main agent가 개입해
편향이 되돌아오고, 평가 간 불일치 자체가 신호다.

## 대상·모드 (target seam)

원래는 transcript가 유일한 대상이었다 — seam 부재(스크립트는 임의 파일을 받지 못했고
페르소나는 코드 리뷰를 명시 금지). 이제 호출 시점에 임의 대상을 지정한다. 세 모드:

- **transcript-only**(기본) — 세션 궤적 + 내용 타당성. 출력 6섹션. 현행 동작.
- **additive**(`--target X`) — transcript + 핀으로 박은 타깃. 타깃은 *보조 증거*:
  축소(A4·S1~S6)가 버린 실제 파일을 복원해 축3(내용 타당성) 판단을 지면에 박는다.
  평가 단위는 여전히 궤적 — 타깃은 sharpen, 출력은 transcript 6섹션 그대로.
- **target-only**(`--target X --only`) — transcript 제외, 산출물 자체만. 출력 4섹션
  (평결·핵심 결함·놓친 대안·멈췄을 지점). 궤적 통계(rewind·active-path) 없음.

요지: **additive = sharpen, only = replace.**

**타깃 전달 = 복사.** 타깃은 `.tpr/targets/`로 스냅샷 복사하고 manifest로 참조한다.
in-place 참조(평가자가 원본 직독)나 프롬프트 본문 주입 대신 복사를 택한 이유:
(1) transcript도 이미 `.tpr/transcript.md`로 materialize됨 — "평가자가 보는 모든 건
`.tpr/`에 있다"가 일관, (2) 셋(codex read-only sandbox / claude / agy)이 모두 cwd 밑을
읽으므로 절대경로·`--add-dir`의 CLI별 미검증 quirk를 피함, (3) prepare 시점 스냅샷이라
결정론·감사가능, (4) transcript 축소 파이프라인 무손상(타깃은 복사만) → 기본 모드 산출물
불변. 타깃은 통째 복사(축소 안 함) — 인간이 의도적으로 핀으로 박은 소수 파일이므로
"read in full". 비정상적으로 크면 `warnings`로 경고(차단 아님). 매 실행 `.tpr/targets/`를
새로 만들고 이전 타깃은 지운다(stale pin 차단); only 모드는 stale `transcript.md`도 지운다.

**자연어 파싱 + echo 가드.** 대상·모드·평가자는 main agent가 자연어 `$ARGUMENTS`에서
뽑아 클린 플래그로 옮긴다(정규식 아님 — 슬래시 인자든 평범한 요청이든 동일). 퍼지 층의
오파싱을 비싼 멀티모델 실행 *전에* 잡으려고, 비-기본 호출은 해소한 계획(모드+실제 경로+
reviewers)을 한 줄 echo 후 진행(진짜 모호할 때만 질문 1개). 순수 기본은 frictionless 유지.

**프롬프트 구조 = 공통 + body.** 페르소나는 `evaluator-common.md`(target-agnostic posture)
+ 모드별 body(`evaluator-transcript.md` / `evaluator-target.md`)로 분할. body가 `{{COMMON}}`을
포함하고 스크립트가 치환(새 include 엔진 없이 기존 `.replace()` 결). 공통을 한 곳에 둔 건
`reduction_config.py`(스크립트·프롬프트 공유 단일소스)와 같은 원칙.

**백워드 호환(staged 골든).** 분할 시 공통 문장이 원본에서 transcript 표현으로 interleave돼
있어 reword가 불가피 → prompt.txt byte-identical은 성립 불가(축3도 어차피 prompt를 바꿈).
그래서 골든을 *의미 있는 곳*에 둔다: ① **`transcript.md`(축소 산출물)는 byte-identical** —
reduction 함수(load_events/S1~S6/render)를 무수정 유지, 고정 입력(`--jsonl`)으로 old/new diff가
비어야 함(검증함). ② prompt.txt는 reword·축3가 *의도된* 변경이라 의미 diff 사람 리뷰 + e2e로
검증 — "원 지시 100% 보존 + 의도분만 추가".
