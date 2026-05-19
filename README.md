# keel

> native Claude Code 위에 얹는, **엔지니어링 규율(discipline) harness**.

`keel`은 배의 용골 — 흐름에 떠밀리지 않게 침로를 잡아주는 구조물이다. 이
저장소는 코딩 에이전트가 *추측으로 떠내려가지* 않도록 잡아주는 장치들의
모음이다. 바이브 코딩이 아니라 agentic engineering을 위한 harness.

keel은 한 개인의 `~/.claude` 설정 중 **직접 저작한 부분만** 큐레이션해 담은
미러다. 제3자 스킬·플러그인은 담지 않고 [`docs/DEPENDENCIES.md`](docs/DEPENDENCIES.md)에서
참조만 한다 ([ADR-0002](docs/adr/0002-authored-content-only.md)).

---

## 철학

native Claude Code는 유능하지만, 그대로 두면 LLM 특유의 실수를 한다 — 확인 없이
가정하고, 테스트보다 구현을 먼저 쏟아내고, 시키지 않은 범위를 건드린다.
keel은 그 실수를 *구조적으로* 막는다.

- **[`CLAUDE.md`](CLAUDE.md)** — 행동 규범. 코딩 전 가정 명시, 최소 구현,
  외과적 변경, 목표 기반 실행.
- **TDD 강제 훅** — 규범을 글로만 두지 않고 훅으로 강제한다. 테스트보다
  구현을 먼저 쓰면 차단된다.
- **STATUS 보드** — 프로젝트 상태를 매 세션 자동 갱신해, "어디까지 했지"를
  추측이 아니라 파일에서 읽게 한다.

규범 → 강제 → 가시성. 이 세 층이 keel의 골격이다.

---

## 무엇이 들어있나

| 종류 | 항목 |
|---|---|
| Skills | `ai-readiness-cartography`, `audit-and-write-readme`, `daily-dev-log`, `session-dev-log`, `setup-status-harness`, `status`, `tech-blog` |
| Hooks | `tdd_keyword` · `tdd_guard` · `tdd_mark` · `tdd_verify`, `session_devlog` |
| Scripts | `status.py`, `sound_complete.sh`, `sound_permission.sh` |
| Agent | `korean-context-writer` |
| Config | `CLAUDE.md`, `statusline.sh`, `settings.json` |

컴포넌트들은 독립적이지 않다. 아래 세 묶음은 **함께 있어야 동작**한다 — 하나만
설치하면 반쪽짜리가 된다.

### 1. TDD 강제 파이프라인 (hooks)

4단계 훅이 `~/.claude/hooks/.tdd-state/`의 세션 파일로 연결된 파이프라인:

1. `tdd_keyword.py` — `UserPromptSubmit`. 프롬프트에서 TDD 키워드를 감지해
   sticky한 **TDD MODE**를 켠다.
2. `tdd_guard.py` — `PreToolUse(Edit|Write)`. TDD MODE 중 테스트 파일을 아직
   한 번도 건드리지 않았다면, 새 구현 파일 생성을 **hard block**한다.
3. `tdd_mark.py` — `PostToolUse(Edit|Write)`. 테스트 파일이 편집됐음을 기록.
4. `tdd_verify.py` — `Stop`. 세션 종료 시 테스트 스위트가 GREEN인지 검증.

`test_tdd_hooks.py`가 이 파이프라인을 테스트한다.

### 2. STATUS 보드

프로젝트 루트의 `STATUS.md`를 매 세션 자동 갱신하는 묶음:

- `scripts/status.py` — `.scratch/`의 이슈 파일에서 `STATUS.md`의 이슈
  테이블을 생성한다. **canonical 버전.**
- `status` 스킬 — `STATUS.md`를 갱신한다(`/status`). `status.py`를 호출하고
  손으로 쓰는 narrative 섹션을 갱신.
- `setup-status-harness` 스킬 — STATUS 하버스가 없는 프로젝트에 설치한다.
  자체적으로 `status.py` 사본을 번들한다 — 이 사본은 `sync.sh`가 canonical과
  항상 일치시킨다 ([ADR-0001](docs/adr/0001-keel-is-a-sync-mirror.md)).
- `settings.json`의 `SessionStart`/`Stop` 훅 배선이 `status.py`를 돌린다.

### 3. Dev 로그

세션이 끝나면 그 세션을 한국어로 정리해 Obsidian 보관함에 남기는 묶음:

`session_devlog.py`(`SessionEnd` 훅) → detached headless `claude` 실행 →
`korean-context-writer` 에이전트 + `session-dev-log` 스킬 → Obsidian.
`daily-dev-log`는 하루치 세션을 프로젝트별로 묶어 같은 폴더에 정리한다.

> ⚠️ 이 묶음은 Obsidian 보관함 경로가 하드코딩돼 있다. "공개 전 체크리스트" 참조.

### 독립 컴포넌트

- `ai-readiness-cartography` — repo를 AI-Ready 루브릭(100점·7범주)으로 감사해
  HTML 대시보드와 ROI 정렬 액션 목록을 만든다. `scripts/score.py` 번들.
- `audit-and-write-readme` — 프로젝트를 깊게 감사해 검증된 영문/한국어 README를
  쓴다. 모든 주장은 실제 파일 대조 게이트를 통과해야 한다.
- `tech-blog` — 담백한 한국어 기술 블로그 글. 검증된 사실만, 과장 없이.
- `statusline.sh` — 커스텀 상태줄.
- `sound_complete.sh` / `sound_permission.sh` — 완료·권한요청 알림음.
  **WSL 전용** (`powershell.exe`로 Windows 사운드를 재생).

---

## settings.json — harness vs 개인 취향

`settings.json`은 두 가지가 섞여 있다. 가져다 쓸 때 구분이 필요하다.

**harness 핵심** (keel이 동작하려면 필요):

- `hooks` — 위 세 묶음의 모든 훅 배선.
- `statusLine` — `statusline.sh` 연결.

**개인 취향** (harness와 무관, 복사 불필요):

- `permissions` — 개인 deny 규칙.
- `effortLevel`, `autoCompactEnabled`, `remoteControlAtStartup`,
  `skipAutoPermissionPrompt`, `skipDangerousModePermissionPrompt`.
- `enabledPlugins`, `extraKnownMarketplaces` — 플러그인 선택.

---

## 설치

> 아직 설치 스크립트는 없다. keel은 작게 시작해 하나씩 덧붙이는 중이다.

수동으로:

1. `skills/`, `hooks/`, `scripts/`, `agents/`의 파일을 `~/.claude`의 대응
   위치에 복사한다.
2. `settings.json`의 **harness 핵심** 블록(`hooks`, `statusLine`)을 자신의
   `~/.claude/settings.json`에 병합한다.
3. 훅 명령의 경로는 현재 `/home/shine/...`로 하드코딩돼 있다 — 자신의 홈
   경로로 바꾼다.

---

## 유지보수 — 작업 모델

keel은 `~/.claude`의 **다운스트림 미러**다. `~/.claude`가 source of truth고,
keel을 직접 편집하지 않는다 ([ADR-0001](docs/adr/0001-keel-is-a-sync-mirror.md)).

```
~/.claude 에서 수정  →  ./sync.sh  →  keel 에서 commit / push
```

`sync.sh`는 [`.allowlist`](.allowlist)에 적힌 저작물만 `~/.claude`에서
가져오고, 런타임 쓰레기와 제3자 콘텐츠는 제외한다.

---

## 공개 전 체크리스트

keel은 지금 private다. 공개 전 처리할 것:

- [ ] Obsidian 경로 하드코딩 제거 — `daily-dev-log`, `session-dev-log`,
      `tech-blog`, `korean-context-writer`의 메모리 경로를 설정값으로 분리.
- [ ] 절대경로 제너릭화 — `/home/shine` → `$HOME`.
- [ ] `LICENSE` 추가.
- [ ] README 영문 동반본 (`audit-and-write-readme` 스킬 활용).
- [ ] `settings.json`을 harness 블록과 개인 취향으로 분리.
- [ ] `sound_*.sh` 크로스플랫폼화 (선택).

---

## 문서

- [`CONTEXT.md`](CONTEXT.md) — 용어 정의.
- [`docs/DEPENDENCIES.md`](docs/DEPENDENCIES.md) — keel에 담지 않은 제3자 의존성.
- [`docs/adr/`](docs/adr/) — 설계 결정 기록.
