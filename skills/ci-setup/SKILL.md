---
name: ci-setup
description: Designs and writes GitHub Actions CI workflows for the current repo. Detects languages, delegates modern/stable toolchain research to the ci-researcher sub-agent, then walks the user through five gates (detection → toolchain → matrix → triggers → final review) before writing any YAML. Use when the user invokes /ci-setup, asks to set up or refresh CI for the current repo, or asks for GitHub Actions workflows for a project that has none or needs revision.
---

# /ci-setup

언어별로 한 파일씩(`.github/workflows/ci-<language>.yml`) GitHub Actions CI 워크플로를 생성한다. 도구 선택은 매 실행마다 [`ci-researcher`](../../agents/ci-researcher.md) sub-agent가 web 리서치로 도출한 **modern / stable** 두 안을 사용자에게 제시해 결정한다.

**범위**: CI만 (test/lint/typecheck/build). CD는 다루지 않는다. GitHub Actions 전용.

## 5-단계 흐름

사용자가 어느 단계에서든 자연어로 수정을 요청하면 **해당 gate만 재제안**하고 다음으로 넘어간다. G1으로 강제 복귀하지 않는다.

### G1. 감지 → 확인
1. 한 번에 본다:
   - Marker: `pyproject.toml`, `package.json`, `Cargo.toml`, `go.mod`, `pom.xml`, `build.gradle`, `Gemfile`, `mix.exs`, `composer.json`, `pubspec.yaml` 등
   - `README.*` 첫 페이지에서 프로젝트 성격 단서 (library / application / cli / monorepo)
   - 소스 트리 파일 확장자 통계 — 어떤 언어가 주축인지
   - 기존 `.github/workflows/` 파일 (있으면 읽어둠. G5의 diff용)
2. 결과를 정리해서 보여준다: primary/secondary 언어, 프로젝트 성격, 기존 워크플로 유무.
3. 언어가 하나도 안 잡히면 (docs-only / 빈 repo) **여기서 종료.** "CI를 셋업할 대상 코드가 보이지 않습니다." 워크플로 파일은 만들지 않는다.

### G2. 언어별 toolchain
확인된 언어마다 `ci-researcher` sub-agent를 호출. 언어가 여러 개면 **하나의 메시지에서 병렬로** Agent 호출.

각 호출에 넘기는 brief 예:
```
language: python
project_kind: web application
extra_signals: Django + Celery; primarily backend
```

Sub-agent는 `modern` / `stable` 두 안과 `sources`를 담은 YAML만 돌려준다 (상세 계약은 `~/.claude/agents/ci-researcher.md`).

사용자에게 두 안을 나란히 보여주고 선택을 받는다 — `modern` / `stable` / 자유 수정 ("modern인데 type checker는 pyright로"). 자유 수정이면 그 자리에서 조합을 반영해 재제안.

### G3. Matrix (OS × 버전)
Default 제안:
- Library 성격: `ubuntu-latest` × 메이저 버전 2개
- Application / CLI: `ubuntu-latest` × 최신 단일 버전
- macOS / Windows는 사용자가 명시 요청할 때만 추가.

### G4. Triggers
Default 제안:
- `push`: default branch만
- `pull_request`: 모든 branch
- `schedule` / `workflow_dispatch`는 요청 시에만 추가.

### G5. YAML 미리보기 → 쓰기
1. G2~G4 결정에 기반해 `ci-<language>.yml`들을 메모리상 조립.
2. **모든 워크플로에 무조건 포함**:
   - `concurrency: { group: ${{ github.workflow }}-${{ github.ref }}, cancel-in-progress: true }`
   - `permissions: { contents: read }` (도구가 더 필요로 할 때만 상향)
   - 의존성 캐싱 (`setup-python` / `setup-node` 등의 `cache:` 옵션)
3. 기존 `.github/workflows/*.yml`이 있으면 **diff를 보여주고** 덮어쓸지 묻는다.
4. 승인 시 실제로 파일 작성. 거절 시 자연어 피드백 → 해당 부분만 재조립 후 다시 미리보기.

## 파일·액션 규칙

- 파일명: `.github/workflows/ci-<lang>.yml` (lowercase). 예: `ci-python.yml`, `ci-node.yml`, `ci-go.yml`.
- Polyglot은 파일 분리 + `paths:` 필터로 monorepo 가드. 예: `ci-python.yml`은 `paths: ['**.py', 'pyproject.toml', '<py-srcdir>/**']`.
- Workflow `name:`은 사람이 읽기 좋은 형태: `CI (Python)`, `CI (Node)`.
- Action 버전은 **메이저 핀** (`actions/checkout@v4`). SHA 핀은 사용자가 명시 요청 시에만.

## 금지 사항

- CD/배포 step 추가 (publish / deploy / release).
- Coverage / Codecov 통합 — 사용자가 명시 요청 시에만.
- 사용자 확인 없이 기존 워크플로 덮어쓰기.
- Sub-agent 출력 없이 toolchain 임의 결정.
