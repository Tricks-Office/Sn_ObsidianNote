---
name: mindmap-json
description: obsimap(표시 이름 "Simple Mindmap") 플러그인이 쓰는 `.mindmap` JSON 파일을 직접 생성/수정하는 스킬. 좌표(x/y)는 계산하지 않고 트리 구조(id/text/children/parent/side)만 작성하면 obsimap이 열 때마다 자동으로 배치한다. "마인드맵 만들어줘", "마인드맵에 노드 추가/수정해줘", "이 내용 마인드맵으로 정리해줘" 같은 요청에 사용한다.
---

## 언제 사용하나

사용자가 `7. MindMap/` 폴더의 `.mindmap` 파일(obsimap 플러그인용 JSON 마인드맵)을 새로 만들거나 기존 파일의 노드를 추가/수정/삭제하고 싶을 때 사용한다.

이 스킬은 `.mindmap`(JSON, obsimap) 형식만 다룬다. `7. MindMap/`에는 `mindmap-editor` 플러그인이 쓰는 들여쓰기 불릿 `.md` 아웃라인 노트(`Template/Mindmap.md` 형식)도 섞여 있을 수 있는데, 그건 이 스킬의 대상이 아니다 — 확장자로 구분한다.

## 배경: 왜 좌표를 계산하지 않아도 되는가

obsimap(`.obsidian/plugins/obsimap/main.js`)은 파일을 열거나 다시 그릴 때마다 `render() → layoutMindMap()`을 호출해서 모든 노드의 x/y를 트리 구조(부모-자식, `side`, 텍스트 길이)로부터 매번 새로 계산한다. 저장돼 있는 x/y는 참고용 캐시일 뿐이고 렌더링 시 항상 덮어써진다. `side`도 root의 직계 자식에서 생략하면 `ensureRootChildSides()`가 좌/우 노드 수를 세서 자동 배분한다(적은 쪽 우선, 동률이면 오른쪽).

→ 따라서 이 스킬로 파일을 쓸 때 **x, y, collapsed 필드는 아예 넣지 않는다.** 넣어도 무시되거나(x/y) 의도치 않게 접힌 채로 열릴 수 있다(collapsed).

이 결론은 `0. Temp/`에서 x/y 없이 만든 샘플 파일을 실제로 Obsidian에서 열어 정상 렌더링/자동배치되는 것을 확인해서 검증했다 (2026-08-22).

## 파일 형식

frontmatter는 Zettelkasten 노트와 동일한 다섯 필드(`write date`/`edit date`/`tags`/`type`/`links`)를 쓴다. 다만 본문 프로즈 대신 JSON 코드블록이 노트 내용 전체를 담는다는 점만 다르다.

```
---
write date: YYYY-MM-DD HH:mm
edit date: YYYY-MM-DD HH:mm
tags: []
type: mindmap
links: []
---

[```]json
{
  "root": {
    "id": "root",
    "text": "<중심 주제>",
    "children": [ <노드, 노드, ...> ]
  }
}
[```]
```

(위에서 `[```]`는 실제 파일에서는 대괄호 없이 그냥 ` ```json ` / ` ``` `로 쓴다 — 이 문서 안에서 코드펜스 중첩 표기를 위한 임시 표시일 뿐이다. 정확한 형식은 `templates/example.mindmap`을 그대로 참고할 것.)

### 노드 스키마

| 필드 | 필수 | 설명 |
|---|---|---|
| `id` | O | 파일 내에서 유일한 문자열. `root`는 예약어이므로 다른 노드에 쓰지 않는다. 새 노드는 `n1`, `n1-1`처럼 짧고 읽기 쉬운 id를 붙인다(obsimap 자체는 `타임스탬프-인덱스-랜덤5자` 형식을 쓰지만, 유일하기만 하면 되므로 그 형식을 흉내낼 필요 없음). |
| `text` | O | 노드 내용. |
| `children` | O | 항상 배열로 존재. 자식이 없으면 `[]`. |
| `parent` | root 제외 O | 부모 노드의 id. root의 직계 자식은 `"parent": "root"`. |
| `side` | X | root의 직계 자식에서만 의미 있음(하위 노드는 조상을 따라가므로 불필요). `"left"` 또는 `"right"`. 생략하면 자동 배분됨 — 배치를 고정하고 싶을 때만 명시. |
| `x`, `y`, `collapsed` | 쓰지 않음 | 위 "배경" 참고. |

## 새 마인드맵 만들기

1. 저장 위치는 `7. MindMap/`. 파일명 = 노트 제목, 확장자는 `.mindmap`.
2. 대화에서 주제와 하위 구조를 계층으로 정리한다. 구조가 애매하면 만들기 전에 사용자에게 원하는 큰 가지를 확인한다.
3. `templates/example.mindmap`의 형식을 그대로 따라 JSON을 작성해 저장한다. frontmatter의 `write date`/`edit date`는 생성 시각으로 동일하게 채운다.
4. 저장 후 실제 렌더링 확인은 Obsidian을 여는 사용자에게 맡긴다(Claude Code에서 직접 시각적 확인 불가).

## 기존 마인드맵 수정하기

1. 대상 `.mindmap` 파일을 읽고 JSON을 파싱해 현재 구조를 파악한다.
2. 기존 노드의 `id`는 그대로 보존한다.
3. 새로 추가하는 노드에만 새 id를 부여한다. 노드를 삭제할 때는 그 노드 + 하위 트리 전체를 함께 제거한다(부모의 `children` 배열에서도 빠져야 함).
4. 배치를 일부러 바꾸려는 게 아니면 root 직계 자식의 기존 `side` 값을 그대로 유지한다.
5. 노드 내용을 실제로 바꾸는 수정이라면 frontmatter의 `edit date`를 현재 시각으로 갱신한다(`write date`는 그대로 둔다).

## 저장 전 검증 체크리스트

- [ ] JSON이 유효하게 parse되는가
- [ ] `root.id === "root"`
- [ ] 모든 비-root 노드에 `children`(배열)과 `parent`가 있는가
- [ ] 파일 전체에서 `id`가 중복되지 않는가
- [ ] frontmatter가 `write date`/`edit date`/`tags`/`type: mindmap`/`links` 다섯 필드를 갖췄는가
- [ ] x/y/collapsed 필드를 넣지 않았는가

## 주의사항

- x/y 좌표를 손으로 계산하려 하지 않는다 — 시간 낭비이며 어차피 렌더링 시 무시된다.
- `templates/example.mindmap`은 참고용 원본이므로 덮어쓰거나 수정하지 않는다.
