---
name: session-dev-log
description: Summarizes the single Claude Code session it is invoked from into a 티키타카-style Korean markdown log — the user's prompts kept verbatim, the AI's replies and work summarized — and saves it to the user's Obsidian vault. Use when asked "이번 세션 정리", "이 세션 요약", "지금까지 한 거 정리해줘", "세션 로그 작성", "session dev log", or "/session-dev-log". Differs from daily-dev-log (which summarizes a whole day across all sessions). Reads the live session JSONL and saves to /mnt/c/Users/shine/Documents/Obsidian/0. Daily/Dev log/YYYY/YYYY-MM/YYYY-MM-DD/Sessions/HH-MM 한줄제목.md.
---

# Session Dev Log

지금 실행 중인 *이 세션 하나*의 대화를 티키타카 형식으로 정리한다. **사용자(Human)의 말은 원문 그대로**, **AI의 응답·작업은 요약**해서 사람이 읽기 좋은 한국어 마크다운으로 만든다.

> 하루 전체(여러 세션)를 정리하는 건 `daily-dev-log`. 이 스킬은 단일 세션 전용.

## Quick start

```bash
python3 ~/.claude/skills/session-dev-log/scripts/collect_session.py
# 특정 세션을 지정할 때
python3 ~/.claude/skills/session-dev-log/scripts/collect_session.py --session <session-id>
```

스크립트는 cwd의 프로젝트 폴더에서 **가장 최근 활동한 JSONL = 지금 이 세션**을 골라(스크립트 실행 자체가 현재 세션 파일을 갱신하므로 신뢰 가능) `turns` 배열을 JSON으로 출력한다.

## Workflow

1. **수집**: 위 명령 실행 → JSON. `turns`는 시간순 대화. `role`이 `human`(원문 보존 대상) / `ai`(요약 대상).
2. **검토**: 각 `human` 턴의 `text`(사용자 원문), `ai` 턴의 `text`(AI 응답)·`tools`(도구 호출)를 읽고 흐름 파악.
3. **작성**: 아래 [출력 구조](#출력-구조)대로 티키타카 마크다운 생성.
4. **제목 만들기**: 세션 전체를 대표하는 한 줄 제목(한국어, 15자 안팎)을 짓는다. 파일명에 쓰므로 `\ / : * ? " < > |` 는 제외.
5. **저장 경로 계산** (`first_ts_local` 기준, KST):
   ```
   /mnt/c/Users/shine/Documents/Obsidian/0. Daily/Dev log/<YYYY>/<YYYY-MM>/<YYYY-MM-DD>/Sessions/<HH-MM> <제목>.md
   ```
   - `<HH-MM>`는 세션 시작 시각 (예: `12-54`).
   - 디렉토리 없으면 `mkdir -p`. 같은 이름 파일이 있으면 사용자에게 덮어쓸지 물어본다.
6. **마무리**: 저장한 절대 경로 한 줄만 출력. 하루 요약본(`YYYY-MM-DD.md`)은 건드리지 않는다 — 그건 `daily-dev-log` 몫.

## 출력 구조

```markdown
#dev-log #session-log

# HH:MM 세션 — 한줄제목

> 프로젝트 `별명` (`/full/path`) · 브랜치 `branch` · HH:MM ~ HH:MM (KST) · 사용자 N턴

## 세션 요약

2~4줄. 이 세션에서 무엇을, 왜 했고 어떻게 끝났는지.

---

### 🙋 사용자
> 사용자 프롬프트 원문 그대로 (여러 줄이면 모든 줄 앞에 `> `)

### 🤖 작업
- AI가 한 일·답한 내용을 불릿으로 요약
- **수정 파일**: `relative/path` (3개 초과면 처음 3개 + `… +N`)
- **도구**: skill/agent 호출이 의미 있을 때만

### 🙋 사용자
> …

### 🤖 작업
- …
```

## 작성 가이드

- **맨 위 첫 줄은 `#dev-log #session-log` 태그** (제목 위, 빈 줄과 함께). Obsidian 그래프/검색 연결용.
- **사용자 말은 절대 요약·수정 금지**. `human` 턴 `text`를 그대로 인용 블록(`>`)에 넣는다. 오타도 그대로. `text_truncated`가 true면 끝에 `… (이하 생략)` 표기.
- **AI 말은 요약**. `ai` 턴의 장황한 `text`를 불릿 2~5개로 압축. 무엇을 했는지가 드러나게 (도구 이름 나열이 아니라).
- **자명한 명령은 묶거나 생략**. `kind: "command"`이고 `/clear` 처럼 의미 없는 건 본문에서 빼거나 "세션 시작/리셋" 한 줄로. 단 `/write-a-skill ...`처럼 인자가 실제 요청인 명령은 사용자 턴으로 원문 인용.
- **시간은 KST**. `first_ts_local`/`last_ts_local` 그대로 사용 (UTC인 `ts` 필드 변환 금지 — 헤더 시각은 `*_local` 값).
- **프로젝트 별명**: `/home/shine` → `home`, `.../autocolor_for_calendar` → `autocolor` 식으로 짧게. 헤더에 풀 경로 함께 표기.
- **파일 경로는 프로젝트 루트 기준 상대 경로**로 줄여 표시.
- **마지막 턴이 이 스킬 호출(`/session-dev-log` 등) 자체**라면 "(이 세션 정리 스킬 실행)" 한 줄로만 적거나 생략.

## 검증 체크리스트

- [ ] 사용자 말이 한 글자도 안 바뀌고 원문 그대로 인용되었나
- [ ] AI 턴은 요약되었나 (원문 통째 복붙 아님)
- [ ] 티키타카 순서(사용자 → 작업 → 사용자 → …)가 시간순으로 유지되나
- [ ] 헤더 시각이 KST인가
- [ ] 파일이 `<YYYY>/<YYYY-MM>/<YYYY-MM-DD>/Sessions/<HH-MM> <제목>.md` 경로에 저장되었나
