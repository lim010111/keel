---
name: daily-token-report
description: Generates a statistical token-usage report for one day of Claude Code activity as a self-contained HTML file, saved into the user's Obsidian Dev log folder. Aggregates token counts and estimated USD cost by project, model, session, hour, and per-task (one user request = one task, agent-summarised labels). Use when asked "오늘 토큰 사용량", "토큰 리포트 만들어줘", "하루 토큰 통계", "오늘 얼마 썼어", "어떤 작업이 토큰을 많이 썼어", "daily token report", or "토큰 소모량 정리". Scope matches the daily-dev-log skill (a "day" is 05:00~다음날 04:59 local). Pulls from ~/.claude/projects/*/<sessionId>.jsonl and saves to /mnt/c/Users/shine/Documents/Obsidian/0. Daily/Dev log/YYYY/YYYY-MM/YYYY-MM-DD/YYYY-MM-DD-tokens.html.
---

# Daily Token Report

지정한 날짜(기본: 오늘)에 활동이 있던 모든 Claude Code 세션의 토큰 사용량을 통계적으로 집계해 HTML 리포트로 저장한다.

> **"하루"의 정의**: 05:00 ~ 다음날 04:59 (로컬 시각) — daily-dev-log 스킬과 동일. 0~5시 새벽 작업은 *전날*에 귀속된다.
>
> **귀속 방식**: 세션 수집 범위는 daily-dev-log와 같지만, 토큰은 **메시지별 귀속** — 각 assistant 메시지의 타임스탬프가 속한 날에만 합산된다. 세션 하나가 이틀에 걸쳐 있어도 날짜별로 정확히 나뉜다.
>
> **작업 단위**: 사용자 요청 하나 = 작업 하나. 그 요청에 대한 어시스턴트 응답(서브에이전트 토큰 포함)을 다음 요청 전까지 합산한다.

`SCRIPT=~/.claude/skills/daily-token-report/scripts/daily_token_report.py`

## Workflow

작업별 라벨을 위해 **2단계**로 실행한다 (수집 → 요약 → 렌더).

1. **작업 목록 수집**: 라벨링 대상 작업 JSON을 받는다.
   ```bash
   python3 "$SCRIPT" --collect-tasks > /tmp/token-tasks.json   # 특정 날짜는 --date YYYY-MM-DD
   ```
   `tasks[]`의 각 항목: `id`, `prompt`(원문 일부), `files_edited`, `skills`, `agents`, `bash`, `tokens`, `cost`. `task_count_total`이 0이면 활동 없음 → 사용자에게 알리고 종료.
2. **작업 라벨 작성**: 각 task의 `prompt`(+ `files_edited`/`skills`/`agents`/`bash`)를 보고 *무엇을 한 작업인지* 한 줄 한국어로 요약. `{task_id: "요약", ...}` 형태로 `/tmp/token-labels.json`에 저장.
   - 명령어성 프롬프트(`/foo`)는 그 스킬이 한 일로 요약. 원문을 그대로 붙여넣지 말 것.
   - 요약은 25자 안팎으로 간결하게.
3. **렌더링**: 라벨을 입혀 HTML 생성·저장.
   ```bash
   python3 "$SCRIPT" --date <YYYY-MM-DD> --labels /tmp/token-labels.json
   ```
4. **덮어쓰기 확인**: 대상 파일이 이미 있으면 스크립트가 exit code 3으로 멈춘다. **사용자에게 물어보고**, 동의하면 `--force`를 붙여 다시 실행.
5. **마무리**: 스크립트가 출력한 저장 절대 경로와 핵심 수치(총 토큰·추정 비용)를 한두 줄로 전달. 후속 영업 금지.

> 라벨 없이 `python3 "$SCRIPT"` 한 번만 실행해도 동작한다 — 이때 작업 라벨은 프롬프트 첫 줄을 잘라 자동 생성한다. 빠른 확인용.

## 출력물

자체 완결형 HTML 파일 한 개. 외부 의존성 없이 브라우저로 바로 열린다. 구성:

- **헤더 + KPI 카드**: 총 토큰 / 추정 비용 / 캐시 재사용 비율 / 세션당 평균
  - 캐시 재사용 비율 = `cache_read / (cache_read + cache_creation)` — 캐시를 거친 토큰 중 읽기 비중. Anthropic 대시보드 "cache read ratio"와 같은 정의(캐시 안 된 새 입력은 분모 제외).
- **토큰 구성**: 입력·출력·캐시 생성·캐시 읽기의 비중 (스택 막대 + 표)
- **프로젝트별 사용량**: 3단계 접이식 트리(프로젝트 → 세션 → 작업). 행을 누르면 자식이 펼쳐지고, 접으면 펼쳐 둔 하위 행까지 같이 접힌다(기본 모두 접힘). 정렬은 각 단계 모두 토큰 총량순. 세션은 Claude Code가 기록한 제목(`ai-title`/`custom-title`, 없으면 세션 id 8자)으로 보여 주며, 워크트리(`<project>/.claude/worktrees/<name>`)에서 연 세션은 별도 프로젝트가 아니라 부모 프로젝트 밑에 `wt:<이름>` 배지로 들어간다.
- **모델별 사용량**: 토큰·비용 표, 내림차순 정렬.
- **시간대별 분포**: 시간별 토큰 막대 차트 (logical day 순서 05시→04시)

저장 경로:
```
/mnt/c/Users/shine/Documents/Obsidian/0. Daily/Dev log/<YYYY>/<YYYY-MM>/<YYYY-MM-DD>/<YYYY-MM-DD>-tokens.html
```
daily-dev-log의 그날 폴더와 같은 위치다. `--out <경로>`로 저장 위치를 바꿀 수 있다.

## 비용 추정

비용은 스크립트 상단 `PRICING` 표(모델 패밀리별 USD/100만 토큰)로 계산한 **추정치**다. Anthropic 가격이 바뀌면 그 표만 수정하면 된다. 모델 id에 매칭되는 패밀리(opus/sonnet/haiku)가 없으면 토큰만 집계하고 비용은 "가격 미상"으로 표시한다.

## 검증 체크리스트

- [ ] 대상 날짜가 의도한 날인가 (새벽 실행 시 전날로 귀속될 수 있음)
- [ ] `--collect-tasks`와 `--labels` 실행에 같은 `--date`를 썼는가
- [ ] 작업 라벨이 프롬프트 원문 복붙이 아니라 *한 일*의 요약인가
- [ ] 기존 파일을 덮어쓰기 전에 사용자에게 물었는가
- [ ] 저장 경로가 `<YYYY>/<YYYY-MM>/<YYYY-MM-DD>/<YYYY-MM-DD>-tokens.html` 인가
