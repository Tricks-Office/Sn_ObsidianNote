"""book-docx 스킬의 설정 파일 기반 CLI 빌드 스크립트.

tools/book_builder/docs/SRS.md (v1.1)의 FR-1~FR-73 중 공통 계약과
build_docx.py 전용 요구사항(§3.4, §3.7)을 구현한다. build_epub.py와
코드를 공유하지 않는다(PRD §7 — 스킬 폴더가 서로 독립적으로 이식
가능해야 한다는 의도적 설계 결정). 비대화형 CLI로, 사용자 확인이
필요한 지점은 SKILL.md의 지시에 따라 Claude가 처리하고 이 스크립트에는
확정된 설정만 전달된다.
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
    "schema_version", "book_title", "book_author", "src_dir", "output_path",
    "groups", "demote", "source_heading_pattern", "extract_fenced_tables",
    "content_image_dir", "box_marker",
    "box_style_name", "reference_doc",
    "ref_dir", "cover_image",
}
GROUP_KEYS = {"part", "chapters"}
PART_KEYS = {"title", "image"}
CHAPTER_KEYS = {"num", "title", "image"}

# FR-56: 빈 문자열을 금지하는 필드(extra_css에 해당하는 필드가 docx에는
# 없으므로 이 목록에는 예외가 없다)
EMPTY_STRING_FORBIDDEN_FIELDS = (
    "src_dir", "output_path", "content_image_dir",
    "box_marker", "source_heading_pattern", "box_style_name", "reference_doc",
    "ref_dir", "cover_image",
)

# 워드에는 epub의 '표지 전용 페이지' 같은 개념이 없으므로, 표지·간지 이미지
# 뒤에 쪽 나누기를 직접 넣어 다음 내용이 같은 쪽에 붙지 않게 한다. pandoc이
# 그대로 통과시키는 raw OpenXML 블록이다.
PAGE_BREAK = (
    "```{=openxml}\n"
    '<w:p><w:r><w:br w:type="page"/></w:r></w:p>\n'
    "```\n"
)


def _is_safe_ref_filename(name):
    """ref_dir 바로 아래의 단일 파일명인지 검사한다(book-epub과 같은 규칙)."""
    if not isinstance(name, str) or name == "":
        return False
    if "/" in name or "\\" in name or ":" in name:
        return False
    if name in (".", ".."):
        return False
    return True


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


# ============================ 원문 스키마 검증 (FR-9~20, 28~31, 55~58, 72) ============================

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

    src_dir = _require(cfg, "src_dir", str, "설정")
    output_path = _require(cfg, "output_path", str, "설정")
    if not output_path.lower().endswith(".docx"):
        fail(EXIT_SCHEMA_INVALID, f"output_path는 .docx 확장자여야 합니다: {output_path}")

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
    box_style_name = _optional(cfg, "box_style_name", str, None, "설정")
    reference_doc = _optional(cfg, "reference_doc", str, None, "설정")
    ref_dir = _optional(cfg, "ref_dir", str, None, "설정")
    cover_image = _optional(cfg, "cover_image", str, None, "설정")
    if cover_image is not None and not _is_safe_ref_filename(cover_image):
        fail(EXIT_SCHEMA_INVALID, f"cover_image은 ref_dir 바로 아래의 단일 파일명이어야 합니다: {cover_image!r}")

    # FR-31: box_marker/box_style_name/reference_doc는 모두 설정되거나 모두 null
    box_fields = {
        "box_marker": box_marker,
        "box_style_name": box_style_name,
        "reference_doc": reference_doc,
    }
    set_fields = [k for k, v in box_fields.items() if v is not None]
    if set_fields and len(set_fields) != 3:
        missing = [k for k, v in box_fields.items() if v is None]
        fail(
            EXIT_OPTION_CONFLICT,
            f"box_marker/box_style_name/reference_doc는 모두 설정되거나 모두 null이어야 합니다"
            f"(누락: {missing})",
        )

    groups = []
    for gi, g in enumerate(groups_raw):
        where = f"groups[{gi}]"
        _check_unknown_keys(g, GROUP_KEYS, where)
        if "part" not in g:
            fail(EXIT_SCHEMA_INVALID, f"{where}에 'part' 필드가 없습니다(null이어도 명시해야 함)")
        part_raw = g["part"]
        part = None
        if part_raw is not None:
            _check_unknown_keys(part_raw, PART_KEYS, f"{where}.part")
            title = _require(part_raw, "title", str, f"{where}.part")
            if title == "":
                fail(EXIT_SCHEMA_INVALID, f"{where}.part.title은 비어있을 수 없습니다")
            _check_empty_string(part_raw, "image", f"{where}.part")
            part_image = part_raw.get("image")
            if part_image is not None and not _is_safe_ref_filename(part_image):
                fail(EXIT_SCHEMA_INVALID, f"{where}.part.image는 ref_dir 바로 아래의 단일 파일명이어야 합니다: {part_image!r}")
            part = {"title": title, "image": part_image}

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
            chapter_image = c.get("image")
            if chapter_image is not None and not _is_safe_ref_filename(chapter_image):
                fail(EXIT_SCHEMA_INVALID, f"{cwhere}.image는 ref_dir 바로 아래의 단일 파일명이어야 합니다: {chapter_image!r}")
            chapters.append({"num": num, "title": title, "image": chapter_image})
        groups.append({"part": part, "chapters": chapters})

    needs_ref_dir = cover_image is not None or any(
        (g["part"] and g["part"]["image"]) or any(c["image"] for c in g["chapters"])
        for g in groups
    )
    if needs_ref_dir and ref_dir is None:
        fail(EXIT_SCHEMA_INVALID, "표지/부 간지/장 표지 이미지를 쓰려면 ref_dir이 필요합니다")

    return {
        "book_title": book_title,
        "book_author": book_author,
        "src_dir": src_dir,
        "output_path": output_path,
        "groups": groups,
        "demote": demote,
        "source_heading_re": source_heading_re,
        "extract_fenced_tables": extract_fenced_tables,
        "content_image_dir": content_image_dir,
        "box_marker": box_marker,
        "box_style_name": box_style_name,
        "reference_doc": reference_doc,
        "ref_dir": ref_dir,
        "cover_image": cover_image,
    }


# ============================ 경로 정규화 (FR-55) ============================

def resolve_paths(cfg, config_dir):
    for key in ("src_dir", "output_path", "content_image_dir", "reference_doc", "ref_dir"):
        value = cfg.get(key)
        if value is None:
            continue
        if not os.path.isabs(value):
            value = os.path.join(config_dir, value)
        cfg[key] = os.path.normpath(os.path.abspath(value))
    return cfg


# ============================ 구조·존재 검증 (FR-12~16, 30) ============================

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

    all_nums = [c["num"] for g in cfg["groups"] for c in g["chapters"]]
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

    chapter_files = {n: find_chapter_file(cfg["src_dir"], n) for n in all_nums}
    cfg["chapter_files"] = chapter_files

    if cfg["content_image_dir"] is not None and not os.path.isdir(cfg["content_image_dir"]):
        fail(EXIT_PATH_NOT_FOUND, f"content_image_dir이 존재하는 디렉터리가 아닙니다: {cfg['content_image_dir']}")

    if cfg["reference_doc"] is not None and not os.path.isfile(cfg["reference_doc"]):
        fail(EXIT_PATH_NOT_FOUND, f"reference_doc 파일이 없습니다: {cfg['reference_doc']}")

    if cfg["ref_dir"] is not None and not os.path.isdir(cfg["ref_dir"]):
        fail(EXIT_PATH_NOT_FOUND, f"ref_dir이 존재하는 디렉터리가 아닙니다: {cfg['ref_dir']}")

    def resolve_ref_image(name, where):
        if name is None:
            return None
        p = os.path.join(cfg["ref_dir"], name)
        if not os.path.isfile(p):
            fail(EXIT_PATH_NOT_FOUND, f"{where}에서 참조한 이미지가 ref_dir에 없습니다: {name}")
        return p

    resolve_ref_image(cfg["cover_image"], "cover_image")
    for gi, g in enumerate(cfg["groups"]):
        if g["part"]:
            resolve_ref_image(g["part"]["image"], f"groups[{gi}].part.image")
        for ci, c in enumerate(g["chapters"]):
            resolve_ref_image(c["image"], f"groups[{gi}].chapters[{ci}].image")

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
    if cfg["reference_doc"]:
        candidates.append(("reference_doc", cfg["reference_doc"]))

    for label, candidate in candidates:
        if _samefile_or_equal(output_path, candidate):
            fail(EXIT_SCHEMA_INVALID, f"output_path가 입력 파일({label}: {candidate})과 같은 파일을 가리킵니다")

    for label, directory in (
        ("src_dir", cfg["src_dir"]),
        ("content_image_dir", cfg["content_image_dir"]),
        ("ref_dir", cfg["ref_dir"]),
    ):
        if directory and _is_within(output_path, directory):
            fail(EXIT_SCHEMA_INVALID, f"output_path가 {label} 하위 경로를 가리킵니다: {output_path}")


# ============================ 전처리 파이프라인 (FR-32~36, 44~45) ============================

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


def _escape_pandoc_attribute_value(value):
    """fenced div 속성 값(큰따옴표로 감싼 문자열) 안에 그대로 들어갈 수
    있도록 역슬래시와 큰따옴표를 이스케이프한다. pandoc의 속성 파서는
    이 역슬래시 이스케이프를 인식해 원래 문자(따옴표 포함)로 되돌린다
    (실측 확인됨) — 그렇지 않으면 이름에 큰따옴표가 있을 때 fenced div
    구문 자체가 깨진다(P2-02)."""
    return value.replace("\\", "\\\\").replace('"', '\\"')


def wrap_box_sections(body, box_marker, box_style_name):
    """FR-44: 코드펜스 박스를 custom-style fenced div로 치환한다."""
    if not box_marker:
        return body
    escaped_style_name = _escape_pandoc_attribute_value(box_style_name)
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
        out += [
            "",
            indent + f'::: {{custom-style="{escaped_style_name}"}}',
            *content,
            indent + ":::",
            "",
        ]
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


def process_chapter_body(body, demote_n, source_heading_re, box_marker, box_style_name, extract_fenced_tables):
    body = demote_headings(body, demote_n)
    body = strip_source_heading(body, source_heading_re)
    body = wrap_box_sections(body, box_marker, box_style_name)
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


# ============================ 이미지 처리 (FR-48) ============================

def copy_content_images(cfg, md_dir):
    if cfg["content_image_dir"] is None:
        return
    dest = os.path.join(md_dir, "이미지")
    try:
        shutil.copytree(cfg["content_image_dir"], dest, dirs_exist_ok=True)
    except OSError as e:
        fail(EXIT_IO_ERROR, f"콘텐츠 이미지 복사에 실패했습니다: {e}")


def copy_ref_images(cfg, md_dir):
    """표지·부 간지·장 표지 이미지를 빌드 폴더의 images/로 모은다."""
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
        fail(EXIT_IO_ERROR, f"표지/간지 이미지 복사에 실패했습니다: {e}")


# ============================ 챕터 조립 (FR-45) ============================

def build_chapter_files(cfg, md_dir):
    files = []
    seq = 0

    # 표지는 어떤 제목보다 앞이므로 헤딩 없이 이미지만 두고 쪽을 나눈다.
    # 헤딩을 붙이면 워드의 "참조 > 목차"에 "표지"가 한 줄 끼어든다.
    if cfg["cover_image"]:
        seq += 1
        p = os.path.join(md_dir, f"{seq:03d}_cover.md")
        write_text(p, f"![](images/{cfg['cover_image']})\n\n{PAGE_BREAK}")
        files.append(p)

    for g in cfg["groups"]:
        if g["part"]:
            seq += 1
            piece = f"# {g['part']['title']}\n"
            if g["part"]["image"]:
                # 부 제목("제목 1")은 남긴다 — 워드의 "참조 > 목차"가 이 스타일을
                # 읽어 목차를 만들기 때문이다. 간지 이미지는 그 아래에 놓고 쪽을
                # 나눠, 다음 장이 간지와 같은 쪽에 붙지 않게 한다.
                piece += f"\n![](images/{g['part']['image']})\n\n{PAGE_BREAK}"
            p = os.path.join(md_dir, f"{seq:03d}_part.md")
            write_text(p, piece)
            files.append(p)

        for c in g["chapters"]:
            seq += 1
            text = read_chapter_text(cfg["chapter_files"][c["num"]])
            body = split_frontmatter(text)
            body = process_chapter_body(
                body,
                cfg["demote"].get(c["num"], 0),
                cfg["source_heading_re"],
                cfg["box_marker"],
                cfg["box_style_name"],
                cfg["extract_fenced_tables"],
            )
            # FR-45: 챕터 제목은 항상 ##(부 소속 여부와 무관)
            pieces = []
            if c["image"]:
                # 장 표지는 장 제목 앞에 오고 그 뒤에서 쪽을 나눈다.
                pieces.append(f"![](images/{c['image']})\n\n{PAGE_BREAK}\n")
            pieces.append(f"## {c['title']}\n\n{body}")
            content = "".join(pieces)
            outp = os.path.join(md_dir, f"{seq:03d}_{c['num']}.md")
            write_text(outp, content)
            files.append(outp)

    return files


# ============================ pandoc 실행 (FR-46~47, 50, 52~54, 59, 61~64) ============================

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
            build_dir = tempfile.mkdtemp(prefix="book_builder_docx_")
        except OSError as e:
            fail(EXIT_IO_ERROR, f"빌드 디렉터리를 만들 수 없습니다: {e}")

        def do_build(staging_path):
            copy_content_images(cfg, build_dir)
            copy_ref_images(cfg, build_dir)
            files = build_chapter_files(cfg, build_dir)

            pandoc_args = [
                *[os.path.basename(f) for f in files],
                "-o", staging_path,
                "--metadata", f"title={cfg['book_title']}",
                "--metadata", f"author={cfg['book_author']}",
                "--standalone",
                # 여러 챕터가 각자 [^1]부터 다시 매기는 각주를 쓰면(장마다 번호가
                # 초기화되는 경우), --file-scope 없이는 pandoc이 전체 입력을 한
                # 덩어리로 파싱해 같은 번호의 각주 정의가 뒤 챕터 것으로 덮어써진다
                # (실측 확인됨). --file-scope는 파일별로 따로 파싱해 각주를 자동으로
                # 재번호한다 — book-epub와 동일한 이유(SRS FR-47).
                "--file-scope",
            ]
            if cfg["reference_doc"]:
                pandoc_args += ["--reference-doc", cfg["reference_doc"]]

            run_pandoc(pandoc_args, cwd=build_dir, stage_name="본 빌드")

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
