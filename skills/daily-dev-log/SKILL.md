---
name: daily-dev-log
disable-model-invocation: true
description: Reviews all Claude Code sessions active on a given date (default today, local timezone) and writes a Korean per-project markdown dev log into the user's Obsidian vault. Use when asked to "오늘 작업 정리", "오늘 한 일 정리해줘", "dev log 작성", "daily dev log", "오늘 세션 요약", or "summarize today's claude sessions". Pulls from ~/.claude/projects/*/<sessionId>.jsonl and saves to /mnt/c/Users/shine/Documents/Obsidian/0. Daily/Dev log/YYYY/YYYY-MM/YYYY-MM-DD/YYYY-MM-DD-dev.md (the day folder also holds per-session logs under Sessions/, written by the session-dev-log skill).
---

# Daily Dev Log

오늘(또는 지정한 날짜)에 활동이 있었던 모든 Claude Code 세션을 훑어 프로젝트별로 정리한 한국어 마크다운을 Obsidian Dev log 보관함에 저장한다.

> **"하루"의 정의**: 05:00 ~ 다음날 04:59 (로컬 시각). 즉 0~5시 사이의 새벽 작업은 *전날*의 연장으로 본다. 19일 02:00에 `/daily-dev-log`를 실행하면 기본 대상은 18일이다. `--date 2026-05-18`은 `2026-05-18 05:00 ~ 2026-05-19 04:59` 범위의 활동을 모은다.

## Quick start

```bash
python3 ~/.claude/skills/daily-dev-log/scripts/collect_today.py
# 특정 날짜
python3 ~/.claude/skills/daily-dev-log/scripts/collect_today.py --date 2026-05-13
```

스크립트는 JSON을 stdout으로 출력한다. 이 JSON을 읽어 마크다운을 작성하고 지정 경로에 저장하면 끝.

## Workflow

1. **수집**: 위 Quick start 명령으로 JSON 추출.
2. **검토**: `sessions[].prompts`(사용자 요청), `files_edited`, `bash_descriptions`, `agents_used`, `skills_used`를 보고 각 세션에서 *무엇을* 했는지 파악. 프로젝트(`cwd`)별로 묶는다.
3. **작성**: 아래 [출력 구조](#출력-구조) 그대로 한국어 마크다운 생성.
4. **저장 경로 계산**:
   ```
   /mnt/c/Users/shine/Documents/Obsidian/0. Daily/Dev log/<YYYY>/<YYYY-MM>/<YYYY-MM-DD>/<YYYY-MM-DD>-dev.md
   ```
   - `<YYYY-MM-DD>` 폴더가 그날의 디렉토리. 그 안의 `Sessions/`에는 단일 세션 로그(`session-dev-log` 스킬 작성)가 들어가고, 이 전체 요약본은 폴더 바로 아래 `<YYYY-MM-DD>-dev.md`로 둔다. (`<YYYY-MM-DD>.md`는 Obsidian Calendar 플러그인이 생성하는 데일리 노트가 차지하므로, 덮어쓰지 않도록 `-dev` 접미사를 붙인다.)
   - 디렉토리 없으면 `mkdir -p`.
   - 기존 파일이 있으면 사용자에게 덮어쓸지 물어본다.
5. **마무리**: 저장한 절대 경로 한 줄만 출력. 다른 날짜 제안 같은 후속 영업 금지.

## 출력 구조

```markdown
#dev-log

# YYYY-MM-DD 개발 로그

> Claude Code 세션 N개 · 프로젝트 M개 · HH:MM ~ HH:MM (KST)

## 오늘의 요약

2~4줄. 어느 프로젝트에서 무엇을 했는지 자연스럽게.

## 프로젝트별 작업

### `프로젝트 별명` — `/full/path`
- **세션** N개 · 브랜치 `branch-name` · HH:MM ~ HH:MM
- **주요 작업**
  - 핵심 요청 한 줄 요약
  - …
- **수정 파일** (K개): `relative/path1`, `relative/path2` …
- **사용 도구**: skill/agent 호출이 의미 있을 때만

### (다음 프로젝트)
…
```

## 작성 가이드

- **첫 줄은 `#dev-log` 태그**. Obsidian에서 데일리 노트 그래프/검색에 묶이도록 본문 맨 위(제목 위)에 빈 줄과 함께 둔다.
- **사용자 프롬프트는 한 줄로 요약**. 원문을 그대로 붙여 넣지 말 것 — 길고 노이즈가 많다. 단, 의미가 압축되지 않는 짧은 프롬프트는 그대로 인용해도 OK.
- **시간은 KST**. JSON의 `first_ts_local`/`last_ts_local`이 이미 로컬 타임존 변환된 값이라 그대로 사용. UTC인 `*_utc` 필드 쓰지 말 것.
- **프로젝트 별명**: `/home/shine/projects/my-long-project` → `myproj`, `/home/shine` → `home`, `.claude/skills` → `skills` 식으로 짧게. 첫 등장 시 풀 경로 함께 표기.
- **파일 경로는 프로젝트 루트 기준 상대 경로**로 줄여 표시 (`/home/shine/projects/my-long-project/src/foo.ts` → `src/foo.ts`).
- **파일이 3개 초과**면 처음 3개 + `… +N more` 형태로.
- **의미 있는 prompt가 없는 세션** (예: `/clear`만 누르고 끝) 은 "탐색/리셋만" 한 줄로 묶거나 생략.
- **노이즈 제외**: `<system-reminder>`, `<command-name>` 같은 메타는 스크립트가 이미 걸러냄. 그래도 prompts에 명령어성 텍스트(`/foo`)가 남아 있으면 묶어서 처리.
- **세션이 어제부터 이어진 경우**: JSON의 `first_ts_local`이 어제 날짜더라도 today에 활동이 있어서 포함된 것 — "어제부터 이어진 세션" 표기 한 줄.
- **새벽까지 이어진 작업**: `first_ts_local`이 *오늘*인데 `last_ts_local`이 다음날 새벽(< 05:00)이라면 정상이다. 시각은 벽시계 그대로 (`23:00 ~ 02:30 (KST)`).

## 검증 체크리스트

- [ ] 모든 세션의 `cwd`가 어느 프로젝트 섹션에든 포함되었나
- [ ] 시간이 KST인가 (UTC 아닌가)
- [ ] 파일 경로가 너무 길어 한 줄을 잡아먹지 않는가
- [ ] 사용자가 실제로 *요청한* 일이 한 줄 요약에 드러나는가 (도구 호출 나열에 그치지 않게)
- [ ] 파일이 의도한 경로(`<YYYY>/<YYYY-MM>/<YYYY-MM-DD>/<YYYY-MM-DD>-dev.md`)에 저장되었나
