# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

This is an Obsidian vault used as a personal Zettelkasten (제텔카스텐) knowledge base. It is not a software project — there is no build, lint, or test process for the vault content itself. The only actual code in this repository is the `mcp-obsidian/` subfolder (see below). Everything else is Korean-language Markdown notes plus Obsidian configuration.

The owner's note-taking philosophy (see `2. 메모/메모/*.md` meta-notes) centers on two ideas:
- **압축 (compression)**: rewrite an idea in your own words down to one note = one idea.
- **연결 (connection)**: link compressed notes to each other via `[[wikilinks]]` rather than relying on folder hierarchy alone.

Notes are also classified by lifecycle, not just topic:
- 임시 메모 (temporary/fleeting note) → triage within ~2 days into a permanent note or delete
- 영구 메모 (permanent note) → lives in the topic folders under `2. 메모/`
- PJT 메모 (project note) → lives under `1. Project/`, folded into a permanent note or dropped once the project ends

## Folder structure

| Folder | Purpose |
|---|---|
| `0. Temp/` | Inbox for fleeting/unsorted notes. Should be triaged quickly, not left to accumulate. |
| `1. Project/` | Active project notes, one subfolder per project. Book-writing projects live under `1. Project/집필/<책 이름>/`, each its own independent local-only git repo — see the 책 쓰기 entry under Note format and Git workflow below. Pre-project book concepts (소재모음·기획안 before a book becomes a full project) live under `1. Project/집필컨셉/<책 이름>/`, which is gitignored and kept local-only (no nested repo). |
| `2. 메모/` | The core Zettelkasten — permanent notes, organized into topic subfolders (`DX n AI`, `경제경영`, `공부`, `메모` [meta notes about note-taking itself], `성공`, `신체`, `아이디어`, `인간관계`, `자아성찰`, `재미`, `집필` [writing-craft notes — the craft/process of writing itself, distinct from the actual book manuscripts under `1. Project/집필/`]). New permanent notes belong here, filed under the closest matching topic. |
| `3. 완성/` | Finished/polished output derived from permanent notes (currently empty). |
| `4. Archive/` | Notes retired from active use but kept for reference. |
| `6. 외부자료/` | External reference material not authored by the vault owner. |
| `7. MindMap/` | Mind map notes, not folder-hierarchy Zettelkasten notes. Two formats/plugins coexist here, distinguished by extension: `.mindmap` (JSON tree, `obsimap` / "Simple Mindmap" plugin — see the `mindmap-json` skill for AI-driven editing) and `.md` (indented bullet outline, `mindmap-editor` / "Mind map editor" plugin, see `Template/Mindmap.md`). |
| `Template/` | Note templates. `Zettelkasten.md`, `Mindmap.md`, and `회의록.md` are the standard templates for new notes — see Note format below. |
| `Zotero/` | Zotero reference-manager data backing the `obsidian-citation-plugin`. Treat as generated/external data, not hand-edited content. |
| `mcp-obsidian/` | A cloned third-party MCP server (Python, `uv`-managed) that exposes the Obsidian Local REST API as MCP tools (list/read/search/patch/append/delete files in the vault). Not authored here; only touch it if the user is working on the MCP integration itself. |
| `.claude/skills/` | Project-local Claude Code skills for this vault. `mindmap-json/` — creating/editing `.mindmap` JSON files (see Note format below and the skill's own `SKILL.md`). `book-interview/` — conversational interview that draws out memories/material for a book-writing project's `0. 소재모음.md` in lieu of the author writing it directly (see the skill's own `SKILL.md`). `book-epub/` — assembles a book-writing project's chapter `.md` files (in table-of-contents order) into an actual `.epub` file via pandoc (see the skill's own `SKILL.md`). `book-docx/` — merges a project's chapter `.md` files (part titles as "Heading 1", chapter titles as "Heading 2") into a single manuscript `.docx`, for platforms that auto-convert an uploaded Word file to an ebook (e.g. 작가와); cover/bibliographic-info/table-of-contents insertion is intentionally left to be done by hand in Word (see the skill's own `SKILL.md`). `meeting-minutes/` — turns raw meeting notes/transcripts into a `회의록.md`-shaped note (일시/참석자/안건/논의 내용/결정 사항/액션 아이템/제약 사항) filed under the relevant project folder (see the skill's own `SKILL.md`). |

## Note format

**Always start a new note from the matching template under `Template/`** (or, for `.mindmap` JSON files, the `mindmap-json` skill's own template) rather than writing frontmatter/structure from scratch. Copy the template's frontmatter and structure as-is, then fill it in — this is what keeps `type:` values and frontmatter shape (see Frontmatter rules below) consistent across the vault as new notes are added.

This vault has six note formats. Each one's exact structure and conventions live in its template file (or skill) — follow that directly rather than duplicating its structure here:
- **제텔카스텐 (Zettelkasten)** — permanent notes under `2. 메모/`: `Template/Zettelkasten.md`
- **Mindmap outline** — bullet-outline mind map notes under `7. MindMap/`, authored/viewed via the `mindmap-editor` ("Mind map editor") plugin: `Template/Mindmap.md`
- **Mindmap JSON** — `.mindmap` files under `7. MindMap/`, authored/viewed via the `obsimap` ("Simple Mindmap") plugin. When creating or editing these, use the `mindmap-json` skill (`.claude/skills/mindmap-json/`) rather than hand-computing node coordinates — the plugin recalculates layout on every render, so stored x/y are irrelevant.
- **책 쓰기 (book writing)** — book projects live under `1. Project/집필/<책 이름>/`, started by copying `Template/책쓰기_표준/` wholesale as the new project's root (that folder's own `CLAUDE.md`/`AGENTS.md` carry the full workflow, folder layout, and reviewer role-split — read those directly rather than duplicating them here). Six templates cover the sequence: `0. 소재모음.md` (`type: book-material-collection` — 기획 이전·진행 중의 사건·기억·대화·자료를 축적) → `1. 책 기획안.md` (`type: book-proposal` — thesis, target reader, differentiation, submission tracker) → `2. 책 목차.md` (`type: book-outline` — 부/장 structure) → `3. 챕터 초고.md` (`type: book-chapter`, one per chapter) → `4. 리뷰 레포트.md` (`type: book-review-report`, one per review pass) → `5. 편집메모.md` (`type: book-edit-log` — a single consolidated revision log for the whole book, not per-chapter). This mirrors the vault owner's own PRD → SRS → implementation pattern applied to writing. `1. Project/집필/` itself is gitignored in this repo — each book project is its own independent local-only git repository instead (no remote); see Git workflow below.
- **AI 작곡 (AI songwriting)** — Suno-generated song records under `1. Project/AI작곡/노래/`, one file per song (`type: ai-song`), started from `Template/AI작곡.md`. Each file has one `## 언어` section per language actually produced (한국어/영어/중국어, occasionally 일본어), and each language section carries the same four subsections — `### 노래 제목` / `### 링크` / `### Style` / `### 가사` / `### 노래 설명` — in that order. Full workflow and prompting conventions (BPM-locking, per-language cultural-adaptation rules, Suno prompt shape) live in `1. Project/AI작곡/지침/AI운동음악지침.md`, not here.
- **회의록 (meeting minutes)** — meeting notes filed under the relevant `1. Project/<프로젝트명>/회의록/` folder (`type: meeting-minutes`), started from `Template/회의록.md`. Sections in fixed order: 일시 / 참석자 / 안건 / 논의 내용 / 결정 사항 / 액션 아이템 (담당자·할 일·기한 표) / 제약 사항. Use the `meeting-minutes` skill (`.claude/skills/meeting-minutes/`) to turn raw meeting notes/transcripts into this shape rather than freehand formatting.

## Frontmatter rules

All four note formats — Zettelkasten, Mindmap outline, Mindmap JSON (`.mindmap`), and 회의록 — share the same five-field frontmatter shape, in this order:

```yaml
---
write date: YYYY-MM-DD HH:mm
edit date: YYYY-MM-DD HH:mm
tags: []
type: <format-name>
links: []
---
```

- `write date` — when the note was first created. Never changes afterward.
- `edit date` — when the note's content was last substantively edited.
  - On a brand-new note, set both to the same timestamp (creation time).
  - When editing an existing note's content (not just retroactive/bulk metadata fixes), update `edit date` to the current date/time while leaving `write date` untouched.
- `tags` — array, `[]` if none, otherwise `[tag1, tag2]` (no quotes, comma-separated, no `#` prefix).
- `type` — identifies which note format/template the note follows (e.g. `zettelkasten`, `mindmap`). This is the field that ties a note back to its template — see Note format above.
- `links` — array of `[[wikilink]]` connections to other notes, `[]` if none yet, otherwise one `"[[Note Name]]"` per line. For `.mindmap` JSON notes, this tracks links to *other notes*, separate from the tree structure inside the JSON code block.

Copy the exact shape from the matching template (`Template/Zettelkasten.md`, `Template/Mindmap.md`, or the `mindmap-json` skill's `templates/example.mindmap`) rather than writing it from scratch. **When adding a new template**, keep this same five-field shape and give it its own distinct `type:` value — existing notes and any tooling that filters/reads `type:` depend on this being consistent across the vault.

## Git workflow

This vault is a git repository, and the owner may edit notes from other devices (e.g. the Obsidian app directly) between Claude Code sessions.

- **Always `git pull` before starting any note-editing work.** Do this at the start of a session before reading or modifying files under the vault's note folders, so edits are never based on a stale local copy and never silently overwrite changes made elsewhere.
- If `pull` reports local changes that would be overwritten, stop and surface them to the user rather than discarding or force-pulling.
- Do not `commit` or `push` unless the user explicitly asks — leave staging/committing decisions to them.
- Book-writing project folders under `1. Project/집필/` are gitignored here and each keeps its own independent local-only git repository (no remote). `git status`/`add`/`commit` on files under there operate on that nested repo, not this one, unless the user explicitly asks otherwise.
- Book-concept folders under `1. Project/집필컨셉/` are also gitignored here, but have no nested repo — they are simply local-only and never committed or pushed. (Files there were tracked before 2026-09-27, so older commits in this repo's history still contain them; that history was intentionally left as-is.)

## Working in `mcp-obsidian/`

This is a standard `uv`-managed Python package (see its own `README.md` for full details):
- `uv sync` — install/update dependencies and the lockfile.
- Run/debug via the MCP Inspector: `npx @modelcontextprotocol/inspector uv --directory /path/to/mcp-obsidian run mcp-obsidian`.
- Requires the Obsidian **Local REST API** community plugin to be running in the vault, configured via `OBSIDIAN_API_KEY` / `OBSIDIAN_HOST` / `OBSIDIAN_PORT` (server config or a `.env` file).
