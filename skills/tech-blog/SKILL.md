---
name: tech-blog
disable-model-invocation: true
description: Writes plain, honest Korean technical blog posts that contain only verifiable information — no speculation, no clickbait, no exaggeration. Every factual claim must trace to the user's code/project, a cited external source, or user-confirmed input; otherwise it is asked about or omitted. Use when the user asks to write a Korean technical blog post, mentions 기술 블로그 글/블로그 포스트 작성, or wants to turn a topic or code into a blog article.
---

# 기술 블로그 작성

한국어 기술 블로그 글을 쓴다. 원칙은 하나다 — 담백하게, 기술만, 검증된 사실만.

## 핵심 원칙 (예외 없음)

1. **검증된 사실만 쓴다.** 모든 기술적 주장은 다음 중 하나에 근거해야 한다.
   - 사용자가 제공한 코드/프로젝트 (직접 읽어서 확인)
   - 인용 가능한 외부 출처 (공식 문서, 릴리스 노트, 표준 명세)
   - 사용자가 확인해 준 정보

   셋 중 어디에도 근거가 없으면 → **추측해서 쓰지 말고**, 사용자에게 묻거나 그 내용을 뺀다.

2. **추측 금지.** "아마", "~일 것이다", "보통 ~한다" 같은 표현으로 빈틈을 메우지 않는다. 모르면 생략한다.

3. **어그로·과장 금지.** "충격", "이것만 알면", "완벽 정리", "혁명적", "반드시 알아야 할" 류의 표현은 제목에도 본문에도 쓰지 않는다. 내용을 있는 그대로 적는다.

4. **주관 최소화.** 의견을 쓸 때는 의견임을 밝히고("개인적으로는") 근거를 함께 댄다. 평가·추천은 사실 나열로 대체할 수 있으면 그렇게 한다.

5. **정보 밀도.** 인사말·맺음말을 제외한 모든 문장은 독자에게 새 정보를 줘야 한다. 그렇지 않은 문장은 삭제한다.

## 워크플로우

1. **주제와 재료 파악** — 무엇에 대한 글인지, 근거 자료(코드/문서/링크)가 무엇인지 확인한다. 재료가 없으면 먼저 요청한다.
2. **빈틈 메우기** — 사실이 불확실한 지점을 목록으로 정리한다. 코드/프로젝트 사실은 직접 읽거나 사용자에게 묻고, 외부 기술 사실은 WebSearch/WebFetch로 1차 출처를 찾는다. 양쪽 다 안 되면 그 내용은 글에 넣지 않는다.
3. **초안 작성** — [REFERENCE.md](REFERENCE.md)의 구조 템플릿과 톤을 따른다.
4. **자가 검수** — [REFERENCE.md](REFERENCE.md)의 체크리스트로 점검한다. 근거 없는 문장과 과장 표현을 제거한다.
5. **저장** — `/mnt/c/Users/shine/Documents/test/<제목>.md` 로 저장한다. 제목은 글 제목 그대로(과장 없이), 파일명에 쓸 수 없는 문자는 `-`로 바꾼다.

## 톤

예시 글(`goddaehee.tistory.com/581`) 스타일을 따른다. 가벼운 인사말로 시작하되 본문은 기술만 담담하게 다룬다. 친근하지만 과장하지 않는다. 자세한 구조·표현 규칙·체크리스트는 [REFERENCE.md](REFERENCE.md)에 있다.
