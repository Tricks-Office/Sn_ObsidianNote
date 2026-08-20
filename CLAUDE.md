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
| `1. Project/` | Active project notes, one subfolder per project (e.g. `Next 출판` — a publishing project). |
| `2. 메모/` | The core Zettelkasten — permanent notes, organized into topic subfolders (`DX n AI`, `경제경영`, `공부`, `메모` [meta notes about note-taking itself], `성공`, `신체`, `아이디어`, `인간관계`, `자아성찰`, `재미`, `회사`). New permanent notes belong here, filed under the closest matching topic. |
| `3. 완성/` | Finished/polished output derived from permanent notes (currently empty). |
| `4. Archive/` | Notes retired from active use but kept for reference. |
| `5. GPTers/` | Material tied to the GPTers community/course, including a multi-chapter sub-project (`잔소리_노래`). |
| `6. 외부자료/` | External reference material not authored by the vault owner. |
| `7. MindMap/` | Mind map notes. Authored and viewed in Obsidian via a Mind Map editor community plugin, not folder-hierarchy Zettelkasten notes. |
| `Template/` | Note templates. `Zettelkasten.md` and `Mindmap.md` are the standard templates for new notes — see Note format below. |
| `Zotero/` | Zotero reference-manager data backing the `obsidian-citation-plugin`. Treat as generated/external data, not hand-edited content. |
| `mcp-obsidian/` | A cloned third-party MCP server (Python, `uv`-managed) that exposes the Obsidian Local REST API as MCP tools (list/read/search/patch/append/delete files in the vault). Not authored here; only touch it if the user is working on the MCP integration itself. |

## Note format

This vault has two note formats. Each one's exact structure and conventions live in its template file under `Template/` — follow that file directly rather than duplicating its structure here:
- **제텔카스텐 (Zettelkasten)** — permanent notes under `2. 메모/`: `Template/Zettelkasten.md`
- **Mindmap** — mind map notes under `7. MindMap/`, authored/viewed via the Obsidian Mind Map editor plugin: `Template/Mindmap.md`

## Git workflow

This vault is a git repository, and the owner may edit notes from other devices (e.g. the Obsidian app directly) between Claude Code sessions.

- **Always `git pull` before starting any note-editing work.** Do this at the start of a session before reading or modifying files under the vault's note folders, so edits are never based on a stale local copy and never silently overwrite changes made elsewhere.
- If `pull` reports local changes that would be overwritten, stop and surface them to the user rather than discarding or force-pulling.
- Do not `commit` or `push` unless the user explicitly asks — leave staging/committing decisions to them.

## Working in `mcp-obsidian/`

This is a standard `uv`-managed Python package (see its own `README.md` for full details):
- `uv sync` — install/update dependencies and the lockfile.
- Run/debug via the MCP Inspector: `npx @modelcontextprotocol/inspector uv --directory /path/to/mcp-obsidian run mcp-obsidian`.
- Requires the Obsidian **Local REST API** community plugin to be running in the vault, configured via `OBSIDIAN_API_KEY` / `OBSIDIAN_HOST` / `OBSIDIAN_PORT` (server config or a `.env` file).
