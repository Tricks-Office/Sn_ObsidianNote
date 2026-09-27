"""book-epub 스킬의 설정 파일 기반 CLI 빌드 스크립트.

tools/book_builder/docs/SRS.md (v1.0)의 FR-1~FR-73(공통 및 build_epub.py
전용 부분)을 구현한다. 비대화형 CLI로, 사용자 확인이 필요한 지점은
SKILL.md의 지시에 따라 Claude가 처리하고 이 스크립트에는 확정된 설정만
전달된다.
"""

import argparse
import glob
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid
import zipfile

MIN_PANDOC_VERSION = (3, 6, 4)
PANDOC_TIMEOUT_SECONDS = 120

EXIT_CONFIG_NOT_FOUND = 2
EXIT_CONFIG_UNREADABLE = 3
EXIT_SCHEMA_INVALID = 4
EXIT_PATH_NOT_FOUND = 5
EXIT_STRUCTURE_INVALID = 6
EXIT_OPTION_CONFLICT = 7
EXIT_PANDOC_UNAVAILABLE = 8
EXIT_PANDOC_FAILED = 9
EXIT_OUTPUT_NOT_WRITABLE = 10
EXIT_IO_ERROR = 11
EXIT_REPLACE_FAILED = 12
EXIT_TIMEOUT = 13

TOP_LEVEL_KEYS = {
    "schema_version", "book_title", "book_author", "language",
    "src_dir", "output_path",
    "groups", "demote", "source_heading_pattern", "extract_fenced_tables",
    "content_image_dir", "box_marker",
    "ref_dir", "cover_image", "use_toc_hierarchy", "colophon_src",
    "colophon_unlisted", "extra_css", "identifier",
}
# 이 스킬은 한국어 원고를 전제로 하므로 언어 기본값도 한국어다. pandoc에
# lang을 넘기지 않으면 en-US가 박혀, 리더의 줄바꿈·글꼴 선택과 일부 플랫폼
# 검수에서 걸린다.
DEFAULT_LANGUAGE = "ko-KR"

GROUP_KEYS = {"part", "chapters"}
PART_KEYS = {"title", "image"}
CHAPTER_KEYS = {"num", "title", "image"}

# FR-56: 빈 문자열을 금지하는 필드(extra_css는 자유 텍스트라 예외)
EMPTY_STRING_FORBIDDEN_FIELDS = (
    "src_dir", "output_path", "ref_dir", "content_image_dir", "colophon_src",
    "box_marker", "source_heading_pattern", "cover_image", "identifier",
    "language",
)

DEFAULT_EXTRA_CSS = """
h1.hidden-heading, h2.hidden-heading, h3.hidden-heading,
h4.hidden-heading, h5.hidden-heading, h6.hidden-heading {
  display: none;
}
figure > figcaption {
  display: none;
}
section.hidden-heading figure {
  text-align: center;
  margin: 2em 0;
}
section.box, div.box {
  margin: 1.5em 0.2em;
  padding: 0.1em 1.2em 1em 1.2em;
  border: 1px solid currentColor;
  border-radius: 6px;
}
h2 {
  page-break-before: always;
}

/* 줄간격 — pandoc 기본값(html 1.2)은 한글 본문을 전자책 단말기에서 읽기에
   좁다. 아래 값은 이전 책에서 Sigil로 직접 넓혀 단말기로 확인한 수치다.
   pandoc 기본 CSS 뒤에 붙으므로 같은 특이도에서는 이쪽이 이긴다. 여백
   규칙(ol/ul/blockquote/table/h1~h6의 margin)은 건드리지 않으려고
   line-height만 다시 지정한다. */
html, body, div, span, applet, object, iframe, h1, h2, h3, h4, h5, h6, p,
blockquote, pre, a, abbr, acronym, address, big, cite, code, del, dfn,
em, img, ins, kbd, q, s, samp, small, strike, strong, sub, sup, tt, var,
b, u, i, center, fieldset, form, label, legend, table, caption, tbody,
tfoot, thead, tr, th, td, article, aside, canvas, details, embed, figure,
figcaption, footer, header, hgroup, menu, nav, output, ruby, section,
summary, time, mark, audio, video, ol, ul, li, dl, dt, dd {
  line-height: 150%;
}
li, dt, dd, figure, figcaption {
  margin: 0.3em 0;
}
li > ol, li > ul {
  margin: 1em 0;
  line-height: 150%;
}
h1 {
  line-height: 170%;
}
h2 {
  line-height: 160%;
}
"""


class BuildError(Exception):
    def __init__(self, exit_code, message):
        super().__init__(message)
        self.exit_code = exit_code


def fail(exit_code, message):
    raise BuildError(exit_code, message)


# ============================ 설정 로드 (FR-2, FR-3) ============================

def load_config(config_path):
    if not os.path.isfile(config_path):
        fail(EXIT_CONFIG_NOT_FOUND, f"--config 경로가 존재하지 않습니다: {config_path}")
    try:
        with open(config_path, "r", encoding="utf-8", errors="strict") as f:
            text = f.read()
    except UnicodeDecodeError as e:
        fail(EXIT_CONFIG_UNREADABLE, f"설정 파일이 UTF-8 텍스트가 아닙니다: {config_path} ({e})")
    except OSError as e:
        fail(EXIT_CONFIG_UNREADABLE, f"설정 파일을 읽을 수 없습니다: {config_path} ({e})")
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        fail(EXIT_CONFIG_UNREADABLE, f"설정 파일이 유효한 JSON이 아닙니다: {config_path} ({e})")


# ============================ 원문 스키마 검증 (FR-9~20, 55~58, 71~72) ============================

def _check_unknown_keys(obj, allowed, where):
    if not isinstance(obj, dict):
        fail(EXIT_SCHEMA_INVALID, f"{where}는 객체여야 합니다")
    unknown = set(obj.keys()) - allowed
    if unknown:
        fail(EXIT_SCHEMA_INVALID, f"{where}에 정의되지 않은 키가 있습니다: {sorted(unknown)}")


def _require(obj, key, types, where):
    if key not in obj:
        fail(EXIT_SCHEMA_INVALID, f"{where}에 필수 필드 '{key}'가 없습니다")
    value = obj[key]
    if not isinstance(value, types):
        fail(EXIT_SCHEMA_INVALID, f"{where}의 필드 '{key}' 타입이 올바르지 않습니다")
    return value


def _optional(obj, key, types, default, where, nullable=True):
    if key not in obj:
        return default
    value = obj[key]
    if value is None:
        if nullable:
            return default
        fail(EXIT_SCHEMA_INVALID, f"{where}의 필드 '{key}'는 null을 허용하지 않습니다(생략하면 기본값 사용)")
    if not isinstance(value, types):
        fail(EXIT_SCHEMA_INVALID, f"{where}의 필드 '{key}' 타입이 올바르지 않습니다")
    return value


def _check_empty_string(obj, key, where):
    if key in obj and obj.get(key) == "":
        fail(EXIT_SCHEMA_INVALID, f"{where}의 필드 '{key}'는 빈 문자열일 수 없습니다")


def _is_safe_ref_filename(name):
    """FR-71: ref_dir 바로 아래의 단일 파일명인지 검사한다."""
    if not isinstance(name, str) or name == "":
        return False
    if "/" in name or "\\" in name or ":" in name:
        return False
    if name in (".", ".."):
        return False
    return True


def validate_schema(cfg):
    """스키마 타입·필수·미정의 키·빈 문자열 검사를 수행하고, 기본값이
    채워진 정규화 딕셔너리를 반환한다. 이 단계에서 경로 값은 아직
    config 디렉터리 기준으로 정규화되지 않은 원문 문자열이다."""
    _check_unknown_keys(cfg, TOP_LEVEL_KEYS, "설정 최상위 객체")

    schema_version = _require(cfg, "schema_version", str, "설정")
    if schema_version != "1":
        fail(EXIT_SCHEMA_INVALID, f"schema_version은 정확히 '1'이어야 합니다(실제: {schema_version!r})")

    for key in EMPTY_STRING_FORBIDDEN_FIELDS:
        _check_empty_string(cfg, key, "설정")

    book_title = _require(cfg, "book_title", str, "설정")
    if book_title == "":
        fail(EXIT_SCHEMA_INVALID, "book_title은 비어있을 수 없습니다")
    book_author = _require(cfg, "book_author", str, "설정")
    if book_author == "":
        fail(EXIT_SCHEMA_INVALID, "book_author는 비어있을 수 없습니다")
    language = _optional(cfg, "language", str, DEFAULT_LANGUAGE, "설정")

    src_dir = _require(cfg, "src_dir", str, "설정")
    output_path = _require(cfg, "output_path", str, "설정")
    if not output_path.lower().endswith(".epub"):
        fail(EXIT_SCHEMA_INVALID, f"output_path는 .epub 확장자여야 합니다: {output_path}")

    groups_raw = _require(cfg, "groups", list, "설정")
    if len(groups_raw) == 0:
        fail(EXIT_STRUCTURE_INVALID, "groups는 최소 1개 이상의 원소가 있어야 합니다")

    demote = _optional(cfg, "demote", dict, {}, "설정", nullable=False)
    for k, v in demote.items():
        if not isinstance(k, str):
            fail(EXIT_SCHEMA_INVALID, "demote의 키는 문자열이어야 합니다")
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            fail(EXIT_SCHEMA_INVALID, f"demote['{k}']는 0 이상의 정수여야 합니다")

    source_heading_pattern = _optional(cfg, "source_heading_pattern", str, None, "설정")
    source_heading_re = None
    if source_heading_pattern is not None:
        try:
            source_heading_re = re.compile(source_heading_pattern)
        except re.error as e:
            fail(EXIT_SCHEMA_INVALID, f"source_heading_pattern이 유효한 정규식이 아닙니다: {e}")

    extract_fenced_tables = _optional(cfg, "extract_fenced_tables", bool, False, "설정", nullable=False)
    content_image_dir = _optional(cfg, "content_image_dir", str, None, "설정")
    box_marker = _optional(cfg, "box_marker", str, None, "설정")

    ref_dir = _optional(cfg, "ref_dir", str, None, "설정")
    cover_image = _optional(cfg, "cover_image", str, None, "설정")
    use_toc_hierarchy = _optional(cfg, "use_toc_hierarchy", bool, None, "설정")
    colophon_src = _optional(cfg, "colophon_src", str, None, "설정")
    colophon_unlisted = _optional(cfg, "colophon_unlisted", bool, False, "설정", nullable=False)
    extra_css = _optional(cfg, "extra_css", str, "", "설정", nullable=False)
    identifier = _optional(cfg, "identifier", str, None, "설정")

    if cover_image is not None and not _is_safe_ref_filename(cover_image):
        fail(EXIT_SCHEMA_INVALID, f"cover_image은 ref_dir 바로 아래의 단일 파일명이어야 합니다: {cover_image!r}")

    groups = []
    any_part = False
    for gi, g in enumerate(groups_raw):
        where = f"groups[{gi}]"
        _check_unknown_keys(g, GROUP_KEYS, where)
        if "part" not in g:
            fail(EXIT_SCHEMA_INVALID, f"{where}에 'part' 필드가 없습니다(null이어도 명시해야 함)")
        part_raw = g["part"]
        part = None
        if part_raw is not None:
            any_part = True
            _check_unknown_keys(part_raw, PART_KEYS, f"{where}.part")
            title = _require(part_raw, "title", str, f"{where}.part")
            if title == "":
                fail(EXIT_SCHEMA_INVALID, f"{where}.part.title은 비어있을 수 없습니다")
            _check_empty_string(part_raw, "image", f"{where}.part")
            image = part_raw.get("image")
            if image is not None and not _is_safe_ref_filename(image):
                fail(EXIT_SCHEMA_INVALID, f"{where}.part.image는 ref_dir 바로 아래의 단일 파일명이어야 합니다: {image!r}")
            part = {"title": title, "image": image}

        chapters_raw = _require(g, "chapters", list, where)
        if len(chapters_raw) == 0:
            fail(EXIT_STRUCTURE_INVALID, f"{where}.chapters는 최소 1개 이상이어야 합니다")
        chapters = []
        for ci, c in enumerate(chapters_raw):
            cwhere = f"{where}.chapters[{ci}]"
            _check_unknown_keys(c, CHAPTER_KEYS, cwhere)
            num = _require(c, "num", str, cwhere)
            if num == "":
                fail(EXIT_SCHEMA_INVALID, f"{cwhere}.num은 비어있을 수 없습니다")
            title = _require(c, "title", str, cwhere)
            if title == "":
                fail(EXIT_SCHEMA_INVALID, f"{cwhere}.title은 비어있을 수 없습니다")
            _check_empty_string(c, "image", cwhere)
            image = c.get("image")
            if image is not None and not _is_safe_ref_filename(image):
                fail(EXIT_SCHEMA_INVALID, f"{cwhere}.image는 ref_dir 바로 아래의 단일 파일명이어야 합니다: {image!r}")
            chapters.append({"num": num, "title": title, "image": image})
        groups.append({"part": part, "chapters": chapters})

    if use_toc_hierarchy is None:
        use_toc_hierarchy = any_part

    needs_ref_dir = cover_image is not None or any(
        (g["part"] and g["part"]["image"]) or any(c["image"] for c in g["chapters"])
        for g in groups
    )
    if needs_ref_dir and ref_dir is None:
        fail(EXIT_SCHEMA_INVALID, "표지/부 표지/챕터 표지 이미지를 쓰려면 ref_dir이 필요합니다")

    return {
        "book_title": book_title,
        "book_author": book_author,
        "language": language,
        "src_dir": src_dir,
        "output_path": output_path,
        "groups": groups,
        "demote": demote,
        "source_heading_re": source_heading_re,
        "extract_fenced_tables": extract_fenced_tables,
        "content_image_dir": content_image_dir,
        "box_marker": box_marker,
        "ref_dir": ref_dir,
        "cover_image": cover_image,
        "use_toc_hierarchy": use_toc_hierarchy,
        "colophon_src": colophon_src,
        "colophon_unlisted": colophon_unlisted,
        "extra_css": extra_css,
        "identifier": identifier,
    }


# ============================ 경로 정규화 (FR-55) ============================

def resolve_paths(cfg, config_dir):
    for key in ("src_dir", "output_path", "ref_dir", "content_image_dir", "colophon_src"):
        value = cfg.get(key)
        if value is None:
            continue
        if not os.path.isabs(value):
            value = os.path.join(config_dir, value)
        cfg[key] = os.path.normpath(os.path.abspath(value))
    return cfg


# ============================ 구조·존재 검증 (FR-12~31, 71) ============================

def find_chapter_file(src_dir, num):
    matches = glob.glob(os.path.join(src_dir, f"*_{num}장_*.md"))
    if not matches:
        matches = glob.glob(os.path.join(src_dir, f"*{num}*.md"))
    if len(matches) != 1:
        fail(
            EXIT_PATH_NOT_FOUND,
            f"장 번호 '{num}'에 대응하는 챕터 파일을 정확히 1개 찾지 못했습니다"
            f"(src_dir={src_dir}, 매치={matches})",
        )
    return matches[0]


def validate_structure_and_paths(cfg):
    if not os.path.isdir(cfg["src_dir"]):
        fail(EXIT_PATH_NOT_FOUND, f"src_dir이 존재하는 디렉터리가 아닙니다: {cfg['src_dir']}")

    all_nums = []
    for g in cfg["groups"]:
        for c in g["chapters"]:
            all_nums.append(c["num"])
    seen = set()
    dup = set()
    for n in all_nums:
        if n in seen:
            dup.add(n)
        seen.add(n)
    if dup:
        fail(EXIT_STRUCTURE_INVALID, f"장 번호가 중복됩니다: {sorted(dup)}")

    for k in cfg["demote"]:
        if k not in seen:
            fail(EXIT_STRUCTURE_INVALID, f"demote의 키 '{k}'에 대응하는 챕터가 groups에 없습니다")

    chapter_files = {}
    for n in all_nums:
        chapter_files[n] = find_chapter_file(cfg["src_dir"], n)
    cfg["chapter_files"] = chapter_files

    if cfg["content_image_dir"] is not None and not os.path.isdir(cfg["content_image_dir"]):
        fail(EXIT_PATH_NOT_FOUND, f"content_image_dir이 존재하는 디렉터리가 아닙니다: {cfg['content_image_dir']}")

    if cfg["ref_dir"] is not None and not os.path.isdir(cfg["ref_dir"]):
        fail(EXIT_PATH_NOT_FOUND, f"ref_dir이 존재하는 디렉터리가 아닙니다: {cfg['ref_dir']}")

    def resolve_ref_image(name, where):
        if name is None:
            return None
        p = os.path.join(cfg["ref_dir"], name)
        if not os.path.isfile(p):
            fail(EXIT_PATH_NOT_FOUND, f"{where}에서 참조한 이미지가 ref_dir에 없습니다: {name}")
        return p

    cfg["cover_image_path"] = resolve_ref_image(cfg["cover_image"], "cover_image")
    for g in cfg["groups"]:
        if g["part"]:
            g["part"]["image_path"] = resolve_ref_image(g["part"]["image"], "groups[].part.image")
        for c in g["chapters"]:
            c["image_path"] = resolve_ref_image(c["image"], f"chapters[{c['num']}].image")

    if cfg["colophon_src"] is not None and not os.path.isfile(cfg["colophon_src"]):
        fail(EXIT_PATH_NOT_FOUND, f"colophon_src 파일이 없습니다: {cfg['colophon_src']}")

    return cfg


# ============================ 출력 경로 별칭 검사 (FR-73) ============================

def _samefile_or_equal(a, b):
    if os.path.exists(a) and os.path.exists(b):
        try:
            return os.path.samefile(a, b)
        except OSError:
            return False
    return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def _is_within(path, directory):
    """path가 directory 자신이거나 그 하위 경로인지(FR-73)."""
    if directory is None:
        return False
    path_n = os.path.normcase(os.path.abspath(path))
    dir_abs = os.path.normcase(os.path.abspath(directory))
    return path_n == dir_abs or path_n.startswith(dir_abs + os.sep)


def check_output_alias(cfg, config_path):
    output_path = cfg["output_path"]

    candidates = [("--config 파일", config_path)]
    for num, path in cfg["chapter_files"].items():
        candidates.append((f"챕터 파일(num={num})", path))
    if cfg["colophon_src"]:
        candidates.append(("colophon_src", cfg["colophon_src"]))
    if cfg["cover_image_path"]:
        candidates.append(("cover_image", cfg["cover_image_path"]))
    for g in cfg["groups"]:
        if g["part"] and g["part"].get("image_path"):
            candidates.append(("groups[].part.image", g["part"]["image_path"]))
        for c in g["chapters"]:
            if c.get("image_path"):
                candidates.append((f"chapters[{c['num']}].image", c["image_path"]))

    for label, candidate in candidates:
        if _samefile_or_equal(output_path, candidate):
            fail(EXIT_SCHEMA_INVALID, f"output_path가 입력 파일({label}: {candidate})과 같은 파일을 가리킵니다")

    for label, directory in (
        ("src_dir", cfg["src_dir"]),
        ("ref_dir", cfg["ref_dir"]),
        ("content_image_dir", cfg["content_image_dir"]),
    ):
        if directory and _is_within(output_path, directory):
            fail(EXIT_SCHEMA_INVALID, f"output_path가 {label} 하위 경로를 가리킵니다: {output_path}")


# ============================ 전처리 파이프라인 (FR-32~43) ============================

def split_frontmatter(text):
    m = re.match(r"^---\n.*?\n---\n", text, re.S)
    if not m:
        return text
    return text[m.end():]


def demote_headings(body, n):
    if n == 0:
        return body
    prefix = "#" * n
    lines = body.split("\n")
    out = []
    in_fence = False
    for line in lines:
        if line.strip().startswith("```"):
            in_fence = not in_fence
            out.append(line)
            continue
        if not in_fence:
            m = re.match(r"^(#{1,6})(\s.*)$", line)
            if m:
                line = prefix + m.group(1) + m.group(2)
        out.append(line)
    return "\n".join(out)


def strip_source_heading(body, source_heading_re):
    if not source_heading_re:
        return body
    lines = body.split("\n")
    return "\n".join(line for line in lines if not source_heading_re.match(line))


def wrap_box_sections(body, box_marker):
    if not box_marker:
        return body
    # 여는 줄의 들여쓰기를 함께 잡는다 — 번호 목록 항목 안에 들여쓴 프롬프트도
    # 박스가 되어야 하고, 그때는 펜스드 디비전에도 같은 들여쓰기가 붙어야
    # pandoc이 그 항목 안의 박스로 읽는다.
    open_re = re.compile(rf"^([ \t]*)```\s*{re.escape(box_marker)}\s*$")
    close_re = re.compile(r"^[ \t]*```\s*$")
    lines = body.split("\n")
    out = []
    i = 0
    n = len(lines)
    while i < n:
        m = open_re.match(lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        indent = m.group(1)
        j = i + 1
        content = []
        while j < n and not close_re.match(lines[j]):
            content.append(lines[j])
            j += 1
        out += ["", indent + "::: {.box}", *content, indent + ":::", ""]
        i = j + 1
    return "\n".join(out)


TABLE_ROW_RE = re.compile(r"^\|.*\|\s*$")
TABLE_SEP_RE = re.compile(r"^\|(\s*:?-{1,}:?\s*\|)+\s*$")


def _segment_fence(inner_lines):
    segments = []
    cur = []
    k = 0
    n = len(inner_lines)
    while k < n:
        if TABLE_ROW_RE.match(inner_lines[k]) and k + 1 < n and TABLE_SEP_RE.match(inner_lines[k + 1]):
            if cur:
                segments.append(("code", cur))
                cur = []
            table_lines = [inner_lines[k], inner_lines[k + 1]]
            k += 2
            while k < n and TABLE_ROW_RE.match(inner_lines[k]):
                table_lines.append(inner_lines[k])
                k += 1
            segments.append(("table", table_lines))
        else:
            cur.append(inner_lines[k])
            k += 1
    if cur:
        segments.append(("code", cur))
    return segments


def split_fenced_tables(body, enabled):
    if not enabled:
        return body
    lines = body.split("\n")
    out = []
    i = 0
    n = len(lines)
    while i < n:
        if lines[i].strip().startswith("```"):
            j = i + 1
            inner = []
            while j < n and not lines[j].strip().startswith("```"):
                inner.append(lines[j])
                j += 1
            segments = _segment_fence(inner)
            if len(segments) == 1 and segments[0][0] == "code":
                out.append(lines[i])
                out.extend(inner)
                out.append(lines[j] if j < n else "```")
            else:
                for kind, seg_lines in segments:
                    if kind == "code":
                        out += ["```", *seg_lines, "```"]
                    else:
                        out += ["", *seg_lines, ""]
            i = j + 1
        else:
            out.append(lines[i])
            i += 1
    return "\n".join(out)


def process_chapter_body(body, demote_n, source_heading_re, box_marker, extract_fenced_tables):
    body = demote_headings(body, demote_n)
    body = strip_source_heading(body, source_heading_re)
    body = wrap_box_sections(body, box_marker)
    body = split_fenced_tables(body, extract_fenced_tables)
    return body


def read_chapter_text(path):
    try:
        with open(path, "r", encoding="utf-8", errors="strict") as f:
            return f.read()
    except UnicodeDecodeError as e:
        fail(EXIT_IO_ERROR, f"챕터 파일이 UTF-8 텍스트가 아닙니다: {path} ({e})")
    except OSError as e:
        fail(EXIT_IO_ERROR, f"챕터 파일을 읽을 수 없습니다: {path} ({e})")


def write_text(path, content):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except OSError as e:
        fail(EXIT_IO_ERROR, f"파일을 쓸 수 없습니다: {path} ({e})")


# ============================ 이미지 처리 (FR-48, 49) ============================

def copy_content_images(cfg, md_dir):
    if cfg["content_image_dir"] is None:
        return
    dest = os.path.join(md_dir, "이미지")
    try:
        shutil.copytree(cfg["content_image_dir"], dest, dirs_exist_ok=True)
    except OSError as e:
        fail(EXIT_IO_ERROR, f"콘텐츠 이미지 복사에 실패했습니다: {e}")


def copy_ref_images(cfg, md_dir):
    needed = set()
    if cfg["cover_image"]:
        needed.add(cfg["cover_image"])
    for g in cfg["groups"]:
        if g["part"] and g["part"]["image"]:
            needed.add(g["part"]["image"])
        for c in g["chapters"]:
            if c["image"]:
                needed.add(c["image"])
    if not needed:
        return
    img_dir = os.path.join(md_dir, "images")
    try:
        os.makedirs(img_dir, exist_ok=True)
        for name in needed:
            shutil.copy(os.path.join(cfg["ref_dir"], name), os.path.join(img_dir, name))
    except OSError as e:
        fail(EXIT_IO_ERROR, f"표지/부 표지 이미지 복사에 실패했습니다: {e}")


# ============================ 챕터 조립 (FR-38, 39, 41) ============================

def build_chapter_files(cfg, md_dir):
    files = []
    seq = 0
    for g in cfg["groups"]:
        if g["part"]:
            seq += 1
            title = g["part"]["title"]
            piece = f"# {title} {{.hidden-heading}}\n\n"
            if g["part"]["image"]:
                piece += f"![{title}](images/{g['part']['image']})\n"
            p = os.path.join(md_dir, f"{seq:03d}_part.md")
            write_text(p, piece)
            files.append(p)

        title_level = "##" if (g["part"] and cfg["use_toc_hierarchy"]) else "#"

        for c in g["chapters"]:
            seq += 1
            text = read_chapter_text(cfg["chapter_files"][c["num"]])
            body = split_frontmatter(text)
            body = process_chapter_body(
                body,
                cfg["demote"].get(c["num"], 0),
                cfg["source_heading_re"],
                cfg["box_marker"],
                cfg["extract_fenced_tables"],
            )
            pieces = []
            if c["image"]:
                # 실제 장 제목과 같은 헤딩 레벨을 써야 한다 — 표지 헤딩이 더 얕은 레벨(예: h1)이면
                # pandoc이 그 다음에 오는 실제 장 제목(h2)을 표지 헤딩의 하위 섹션으로 취급해서,
                # 표지가 .unlisted일 때 실제 장 제목까지 통째로 목차에서 사라진다(실측 확인됨).
                pieces.append(f"{title_level} {c['title']} 표지 {{.hidden-heading .unlisted}}\n\n![{c['title']}](images/{c['image']})\n\n")
            pieces.append(f"{title_level} {c['title']}\n\n{body}")
            content = "".join(pieces)
            outp = os.path.join(md_dir, f"{seq:03d}_{c['num']}.md")
            write_text(outp, content)
            files.append(outp)

    if cfg["colophon_src"]:
        seq += 1
        colophon_body = read_chapter_text(cfg["colophon_src"]).strip()
        heading = "# 판권" + (" {.hidden-heading .unlisted}" if cfg["colophon_unlisted"] else "")
        p = os.path.join(md_dir, f"{seq:03d}_colophon.md")
        write_text(p, f"{heading}\n\n{colophon_body}\n")
        files.append(p)

    return files


# ============================ pandoc 실행 (FR-50~54, 59, 61~64) ============================

def pandoc_argv_prefix():
    """BOOK_BUILDER_PANDOC(FR-63)이 설정되어 있으면 그 값을 pandoc 실행
    파일로 쓴다. 테스트 shim이 중간에 셸 래퍼(cmd.exe 등)를 거치면
    타임아웃으로 죽여도 손자 프로세스가 stdout/stderr 파이프를 계속
    쥐고 있어 지연될 수 있으므로, shlex로 나눠 shim을 셸 없이 직접
    자식 프로세스로 실행할 수 있게 한다(예: "python shim.py").

    ``shlex.split(..., posix=False)``는 Windows 경로의 역슬래시는 그대로
    보존하지만 감싼 큰따옴표는 벗겨내지 않고 토큰에 그대로 남긴다. 그래서
    공백이 포함된 경로(예: ``"C:\\Program Files\\Pandoc\\pandoc.exe"``)를
    지원하려면 토큰을 나눈 뒤 앞뒤에 남은 큰따옴표 한 쌍을 직접 벗겨낸다."""
    override = os.environ.get("BOOK_BUILDER_PANDOC")
    if not override:
        return ["pandoc"]
    tokens = shlex.split(override, posix=False)
    return [t[1:-1] if len(t) >= 2 and t[0] == '"' and t[-1] == '"' else t for t in tokens]


def run_pandoc(args, cwd, stage_name):
    try:
        result = subprocess.run(
            [*pandoc_argv_prefix(), *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=PANDOC_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        fail(EXIT_PANDOC_UNAVAILABLE, f"pandoc 실행 파일을 찾을 수 없습니다({pandoc_argv_prefix()})")
    except subprocess.TimeoutExpired:
        fail(EXIT_TIMEOUT, f"pandoc 호출({stage_name})이 {PANDOC_TIMEOUT_SECONDS}초 타임아웃을 초과했습니다")
    if result.returncode != 0:
        fail(EXIT_PANDOC_FAILED, f"pandoc 호출({stage_name})이 실패했습니다:\n{result.stderr}")
    return result.stdout


def check_pandoc_version():
    try:
        result = subprocess.run(
            [*pandoc_argv_prefix(), "--version"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=PANDOC_TIMEOUT_SECONDS,
        )
    except FileNotFoundError:
        fail(EXIT_PANDOC_UNAVAILABLE, f"pandoc 실행 파일을 찾을 수 없습니다({pandoc_argv_prefix()})")
    except subprocess.TimeoutExpired:
        fail(EXIT_TIMEOUT, f"pandoc --version 호출이 {PANDOC_TIMEOUT_SECONDS}초 타임아웃을 초과했습니다")
    if result.returncode != 0:
        fail(EXIT_PANDOC_UNAVAILABLE, f"pandoc --version 실행이 실패했습니다:\n{result.stderr}")
    first_line = (result.stdout or "").splitlines()[0] if result.stdout else ""
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", first_line)
    if not m:
        fail(
            EXIT_PANDOC_UNAVAILABLE,
            f"pandoc 버전을 확인할 수 없습니다(감지: 확인 불가, 최소 요구: {'.'.join(map(str, MIN_PANDOC_VERSION))})",
        )
    detected = tuple(int(x) for x in m.groups())
    if detected < MIN_PANDOC_VERSION:
        fail(
            EXIT_PANDOC_UNAVAILABLE,
            f"pandoc 버전이 최소 요구 버전보다 낮습니다(감지: {'.'.join(map(str, detected))}, "
            f"최소 요구: {'.'.join(map(str, MIN_PANDOC_VERSION))})",
        )


def build_combined_css(md_dir, extra_css):
    default_css = run_pandoc(["--print-default-data-file=epub.css"], cwd=md_dir, stage_name="기본 CSS 추출")
    css_path = os.path.join(md_dir, "extra.css")
    write_text(css_path, default_css.rstrip() + "\n\n" + DEFAULT_EXTRA_CSS.strip() + "\n\n" + extra_css.strip() + "\n")
    return "extra.css"


def prepare_output_location(output_path):
    parent = os.path.dirname(output_path) or "."
    try:
        os.makedirs(parent, exist_ok=True)
        probe = os.path.join(parent, f".book_builder_write_probe_{uuid.uuid4().hex}")
        with open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
    except OSError as e:
        fail(EXIT_OUTPUT_NOT_WRITABLE, f"output_path의 부모 디렉터리에 쓸 수 없습니다: {parent} ({e})")


COVER_ITEM_RE = re.compile(r'<item\b[^>]*\bproperties="[^"]*\bcover-image\b[^"]*"[^>]*>')
ITEM_ID_RE = re.compile(r'\bid="([^"]+)"')
LEGACY_COVER_RE = re.compile(r'<meta\b[^>]*\bname="cover"')
METADATA_CLOSE_RE = re.compile(r"</metadata>")


def _opf_path(zf):
    """container.xml이 가리키는 OPF 경로를 돌려준다. 못 찾으면 None."""
    try:
        container = zf.read("META-INF/container.xml").decode("utf-8")
    except KeyError:
        return None
    m = re.search(r'<rootfile\b[^>]*\bfull-path="([^"]+)"', container)
    return m.group(1) if m else None


def add_legacy_cover_meta(epub_path):
    """EPUB2 호환 <meta name="cover"/>를 OPF 메타데이터에 넣는다.

    pandoc은 EPUB3 방식(manifest 항목의 properties="cover-image")만 쓰는데,
    전자책 리더와 서점 중에는 이 구형 메타를 봐야 표지를 찾는 곳이 있다.
    둘 다 들어 있어야 어디서든 표지가 뜬다 — Sigil에서 이미지에 "Cover"
    의미를 지정하면 하는 일이 이것이다.

    표지가 없거나 이미 메타가 들어 있으면 아무것도 하지 않는다.
    """
    try:
        with zipfile.ZipFile(epub_path) as zf:
            opf_name = _opf_path(zf)
            if not opf_name:
                return False
            try:
                opf = zf.read(opf_name).decode("utf-8")
            except KeyError:
                return False
            item = COVER_ITEM_RE.search(opf)
            if not item or LEGACY_COVER_RE.search(opf):
                return False
            item_id = ITEM_ID_RE.search(item.group(0))
            close = METADATA_CLOSE_RE.search(opf)
            if not item_id or not close:
                return False
            meta = '    <meta name="cover" content="%s" />\n  ' % item_id.group(1)
            patched = opf[: close.start()] + meta + opf[close.start() :]
            entries = [(info, zf.read(info.filename)) for info in zf.infolist()]
    except (OSError, zipfile.BadZipFile) as e:
        fail(EXIT_IO_ERROR, f"표지 메타를 넣으려고 epub을 여는 데 실패했습니다: {e}")

    tmp_path = epub_path + ".covermeta"
    try:
        with zipfile.ZipFile(tmp_path, "w") as out:
            # mimetype은 반드시 첫 항목이고 무압축이어야 한다(EPUB OCF 규칙).
            ordered = sorted(entries, key=lambda e: e[0].filename != "mimetype")
            for info, data in ordered:
                if info.filename == opf_name:
                    data = patched.encode("utf-8")
                new = zipfile.ZipInfo(info.filename, date_time=info.date_time)
                new.external_attr = info.external_attr
                # 나머지 항목은 원본의 압축 방식을 그대로 둔다 — 이미 압축된
                # 이미지를 다시 DEFLATE하면 파일만 커진다.
                new.compress_type = (
                    zipfile.ZIP_STORED
                    if info.filename == "mimetype"
                    else info.compress_type
                )
                out.writestr(new, data)
        os.replace(tmp_path, epub_path)
    except OSError as e:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        fail(EXIT_IO_ERROR, f"표지 메타를 넣은 epub을 쓰지 못했습니다: {e}")
    return True


def stage_and_replace(output_path, build_fn):
    """build_fn(staging_path)를 호출해 스테이징 파일을 만들고, 성공하면
    원자적으로 output_path로 치환한다."""
    parent = os.path.dirname(output_path) or "."
    ext = os.path.splitext(output_path)[1]
    staging_name = f".{os.path.basename(output_path)}.tmp-{uuid.uuid4().hex[:8]}{ext}"
    staging_path = os.path.join(parent, staging_name)
    try:
        build_fn(staging_path)
    except BuildError:
        if os.path.exists(staging_path):
            try:
                os.remove(staging_path)
            except OSError as cleanup_err:
                sys.stderr.write(
                    f"경고: 실패한 스테이징 파일을 정리하지 못했습니다. 수동으로 삭제하세요: "
                    f"{staging_path} ({cleanup_err})\n"
                )
        raise
    try:
        os.replace(staging_path, output_path)
    except OSError as e:
        fail(
            EXIT_REPLACE_FAILED,
            f"pandoc은 성공했지만 결과물을 최종 경로로 옮기지 못했습니다. "
            f"스테이징 파일이 남아있습니다: {staging_path} ({e})",
        )


# ============================ main ============================

def cleanup_build_dir(build_dir, keep):
    if keep:
        sys.stderr.write(f"경고: --keep-build-dir 지정으로 빌드 디렉터리를 보존합니다: {build_dir}\n")
        return
    try:
        shutil.rmtree(build_dir)
    except OSError as e:
        sys.stderr.write(f"경고: 빌드 디렉터리 정리에 실패했습니다: {build_dir} ({e})\n")


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", required=True)
    parser.add_argument("--keep-build-dir", action="store_true")
    args = parser.parse_args(argv)

    config_path = os.path.abspath(args.config)
    build_dir = None
    try:
        raw_cfg = load_config(config_path)
        cfg = validate_schema(raw_cfg)
        cfg = resolve_paths(cfg, os.path.dirname(config_path))
        cfg = validate_structure_and_paths(cfg)
        check_output_alias(cfg, config_path)

        check_pandoc_version()
        prepare_output_location(cfg["output_path"])

        try:
            build_dir = tempfile.mkdtemp(prefix="book_builder_epub_")
        except OSError as e:
            fail(EXIT_IO_ERROR, f"빌드 디렉터리를 만들 수 없습니다: {e}")

        def do_build(staging_path):
            copy_content_images(cfg, build_dir)
            copy_ref_images(cfg, build_dir)
            files = build_chapter_files(cfg, build_dir)
            css_rel = build_combined_css(build_dir, cfg["extra_css"])

            pandoc_args = [
                *[os.path.basename(f) for f in files],
                "-o", staging_path,
                "--metadata", f"title={cfg['book_title']}",
                "--metadata", f"author={cfg['book_author']}",
                "--metadata", f"lang={cfg['language']}",
                "--toc",
                "--file-scope",
                "--css", css_rel,
            ]
            if cfg["identifier"]:
                pandoc_args += ["--metadata", f"identifier={cfg['identifier']}"]
            if cfg["use_toc_hierarchy"]:
                pandoc_args += ["--split-level=2"]
            if cfg["cover_image"]:
                pandoc_args += ["--epub-cover-image", os.path.join("images", cfg["cover_image"])]

            run_pandoc(pandoc_args, cwd=build_dir, stage_name="본 빌드")

            if cfg["cover_image"]:
                add_legacy_cover_meta(staging_path)

        stage_and_replace(cfg["output_path"], do_build)

        print(cfg["output_path"])
        return 0
    except BuildError as e:
        sys.stderr.write(str(e) + "\n")
        return e.exit_code
    finally:
        if build_dir and os.path.isdir(build_dir):
            cleanup_build_dir(build_dir, args.keep_build_dir)


if __name__ == "__main__":
    sys.exit(main())
