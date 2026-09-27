"""
book-epub 스킬의 "고급 옵션" 빌드 스크립트 템플릿.

이 파일은 그대로 실행하는 게 아니라, 새 책 프로젝트에 맞게 복사해서
아래 CONFIG 섹션만 고쳐 쓰는 용도다. 전처리 함수들(demote_headings,
wrap_box_sections, strip_source_heading, split_fenced_tables,
build_combined_css)은 대부분 프로젝트에 그대로 재사용 가능하다.

독자생존 프로젝트(AI와 함께 홀로서기)에서 실전 검증됨 — SKILL.md의
"고급 옵션" 절 1~9번이 이 스크립트가 실제로 하는 일이다.
"""

import re
import glob
import os
import shutil
import subprocess

# ============================== CONFIG ==============================
# 아래 값들을 프로젝트에 맞게 고친다.

BASE = r"C:\path\to\project"          # 책 프로젝트 루트
SRC_DIR = os.path.join(BASE, "본문")   # 챕터 .md가 있는 폴더
REF_DIR = os.path.join(BASE, "Reference")  # 표지/부 표지 이미지, 서지 등이 있는 폴더
CONTENT_IMG_DIR = os.path.join(SRC_DIR, "이미지")  # 본문 삽입용 콘텐츠 이미지 폴더
OUT_EPUB = os.path.join(BASE, "출판", "책제목.epub")

BUILD_DIR = os.path.dirname(os.path.abspath(__file__))
MD_DIR = os.path.join(BUILD_DIR, "md")
IMG_DIR = os.path.join(MD_DIR, "images")  # 표지/부표지 이미지를 여기로 복사해 사용

BOOK_TITLE = "책 제목"
BOOK_AUTHOR = "저자명"
BOOK_LANGUAGE = "ko-KR"                # 넘기지 않으면 pandoc이 en-US로 박는다

# GROUPS: 목차 구조. "part"가 있으면 그 부의 장들은 ## (level 2)로,
# 없으면(프롤로그/에필로그/부록처럼 부에 속하지 않는 챕터) # (level 1)로 주입된다.
# part 항목은 (부 제목, 부 표지 이미지 파일명) 튜플, 없으면 None.
# chapters 항목은 (장 번호, 장 제목, 표지 이미지 파일명 또는 None) 튜플.
#   표지 이미지가 있으면: 본문 시작 전에 "이미지만 있는 페이지"를 별도로 삽입한다
#   (SKILL.md "고급 옵션" 2번 참고). 이건 부 표지 이미지와는 별개다.
GROUPS = [
    {"part": None, "chapters": [("00", "프롤로그 제목", "프롤로그.png")]},
    {"part": ("1부 · 부 제목", "1부.png"), "chapters": [
        ("01", "1장 제목", None),
        ("02", "2장 제목", None),
    ]},
    # ... 나머지 부/장
    {"part": None, "chapters": [("99", "에필로그 제목", "에필로그.png")]},
]

# 20/21장처럼 원본 안에 이미 ##/### 같은 헤딩이 있어서(예: 다른 문서를 인용) 그대로
# 합치면 목차를 오염시키는 챕터가 있다면, 장 번호 -> 강등할 레벨 수를 적는다.
DEMOTE = {}

# 콜아웃 박스 마커 (SKILL.md 고급 옵션 3번). 프로젝트에 이런 관습이 없으면 None으로 둔다.
BOX_MARKER = "박스"  # 예: "#### 박스 · 제목" / "**박스 · 제목**"

# 각주/출처 라벨 헤딩을 통째로 지우고 싶다면 정규식을 넣는다(고급 옵션 4번).
# 프로젝트에 해당 관습이 없으면 None으로 둔다.
SOURCE_HEADING_PATTERN = None  # 예: r"^#{1,6}\s*출처\(참고문헌, 영상, Web\)\s*$"

# 코드펜스 안 표를 실제 표로 빼낼지(고급 옵션 8번). 필요 없으면 False.
EXTRACT_FENCED_TABLES = True

# 맨 마지막 페이지에 넣을 판권/서지 파일(고급 옵션 7번). 없으면 None.
COLOPHON_SRC = None  # 예: os.path.join(REF_DIR, "서지.md")
COLOPHON_UNLISTED = False  # 목차에서 감추고 싶으면 True

# 부/장 목차 계층을 켤지(고급 옵션 5번). GROUPS에 "part"가 하나라도 있으면 보통 True.
USE_TOC_HIERARCHY = True

# ============================ 전처리 함수 ============================
# 프로젝트마다 크게 달라지지 않는 부분 — 보통 그대로 재사용한다.

BOX_HEADING_RE = re.compile(rf"^(#{{1,4}})\s+{re.escape(BOX_MARKER)}\s*·\s*(.+?)\s*$") if BOX_MARKER else None
BOX_BOLD_RE = re.compile(rf"^\*\*{re.escape(BOX_MARKER)}\s*·\s*(.+?)\*\*\s*$") if BOX_MARKER else None
ANY_HEADING_RE = re.compile(r"^#{1,4}\s")
SOURCE_HEADING_RE = re.compile(SOURCE_HEADING_PATTERN) if SOURCE_HEADING_PATTERN else None
TABLE_ROW_RE = re.compile(r"^\|.*\|\s*$")
TABLE_SEP_RE = re.compile(r"^\|(\s*:?-{1,}:?\s*\|)+\s*$")


def find_file(num):
    matches = glob.glob(os.path.join(SRC_DIR, f"*_{num}장_*.md")) or \
              glob.glob(os.path.join(SRC_DIR, f"*{num}*.md"))
    assert len(matches) == 1, (num, matches)
    return matches[0]


def split_frontmatter(text):
    m = re.match(r"^---\n.*?\n---\n", text, re.S)
    if not m:
        return "", text
    return m.group(0), text[m.end():]


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


def strip_source_heading(body):
    if not SOURCE_HEADING_RE:
        return body
    lines = body.split("\n")
    return "\n".join(line for line in lines if not SOURCE_HEADING_RE.match(line))


def wrap_box_sections(body):
    if not BOX_HEADING_RE:
        return body
    lines = body.split("\n")
    out = []
    i = 0
    n = len(lines)
    while i < n:
        hm = BOX_HEADING_RE.match(lines[i])
        bm = None if hm else BOX_BOLD_RE.match(lines[i])
        if not hm and not bm:
            out.append(lines[i])
            i += 1
            continue
        first_line = f"{hm.group(1)} {hm.group(2)}" if hm else f"**{bm.group(1)}**"
        j = i + 1
        content = []
        while j < n and not ANY_HEADING_RE.match(lines[j]):
            content.append(lines[j])
            j += 1
        out += ["", "::: {.box}", first_line, *content, ":::", ""]
        i = j
    return "\n".join(out)


def segment_fence(inner_lines):
    """코드펜스 내부를 code/table 세그먼트로 번갈아 쪼갠다."""
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


def split_fenced_tables(body):
    if not EXTRACT_FENCED_TABLES:
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
            segments = segment_fence(inner)
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


def process_chapter_body(num, body):
    body = demote_headings(body, DEMOTE.get(num, 0))
    body = strip_source_heading(body)
    body = wrap_box_sections(body)
    body = split_fenced_tables(body)
    return body


def write(path, content):
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)


def build_combined_css(build_dir, extra_rules):
    """pandoc 기본 epub 스타일시트에 커스텀 규칙을 이어붙인다.
    --css를 그냥 쓰면 pandoc 기본 스타일시트(글꼴/여백/페이지나눔 등)가
    통째로 대체돼버리므로(SKILL.md 고급 옵션 9번), 반드시 이 함수로 합친다."""
    default_css = subprocess.run(
        ["pandoc", "--print-default-data-file=epub.css"],
        capture_output=True, text=True, encoding="utf-8", check=True,
    ).stdout
    css_path = os.path.join(build_dir, "extra.css")
    write(css_path, default_css.rstrip() + "\n\n" + extra_rules.strip() + "\n")
    return css_path


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
"""


# ================================ main ================================

def main():
    for d in (MD_DIR,):
        if os.path.exists(d):
            shutil.rmtree(d)
        os.makedirs(d)
    os.makedirs(IMG_DIR, exist_ok=True)

    # 표지/부 표지 이미지를 빌드 폴더로 복사 (경로에 공백 등이 있어도 안전하게)
    needed_images = set()
    for g in GROUPS:
        if g["part"]:
            needed_images.add(g["part"][1])
        for _, _, img in g["chapters"]:
            if img:
                needed_images.add(img)
    for fname in needed_images:
        shutil.copy(os.path.join(REF_DIR, fname), os.path.join(IMG_DIR, fname))

    # 본문 콘텐츠 이미지 폴더도 통째로 복사 (고급 옵션 6번)
    if os.path.isdir(CONTENT_IMG_DIR):
        shutil.copytree(CONTENT_IMG_DIR, os.path.join(MD_DIR, "이미지"), dirs_exist_ok=True)

    files = []
    seq = 0

    for g in GROUPS:
        if g["part"]:
            part_title, part_image = g["part"]
            seq += 1
            content = f"# {part_title} {{.hidden-heading}}\n\n![{part_title}](images/{part_image})\n"
            p = os.path.join(MD_DIR, f"{seq:03d}_part.md")
            write(p, content)
            files.append(p)

        title_level = "##" if (g["part"] and USE_TOC_HIERARCHY) else "#"

        for num, title, img in g["chapters"]:
            seq += 1
            src = find_file(num)
            text = open(src, encoding="utf-8").read()
            fm, body = split_frontmatter(text)
            body = process_chapter_body(num, body)

            pieces = [fm]
            if img:
                pieces.append(f"\n# {title} 표지 {{.hidden-heading .unlisted}}\n\n![{title}](images/{img})\n")
            # 본문 안의 콘텐츠 이미지 상대경로(이미지/파일.png)는 위에서 복사한
            # md/이미지/ 폴더를 그대로 가리키므로 별도 경로 치환이 필요 없다.
            pieces.append(f"\n{title_level} {title}\n\n{body}")
            content = "".join(pieces)

            outp = os.path.join(MD_DIR, f"{seq:03d}_{num}.md")
            write(outp, content)
            files.append(outp)

    if COLOPHON_SRC:
        seq += 1
        colophon_body = open(COLOPHON_SRC, encoding="utf-8").read().strip()
        heading = "# 판권" + (" {.hidden-heading .unlisted}" if COLOPHON_UNLISTED else "")
        write(os.path.join(MD_DIR, f"{seq:03d}_colophon.md"), f"{heading}\n\n{colophon_body}\n")
        files.append(os.path.join(MD_DIR, f"{seq:03d}_colophon.md"))

    css_path = build_combined_css(BUILD_DIR, DEFAULT_EXTRA_CSS)

    os.makedirs(os.path.dirname(OUT_EPUB), exist_ok=True)

    cmd = [
        "pandoc",
        *[os.path.basename(f) for f in files],
        "-o", OUT_EPUB,
        "--metadata", f"title={BOOK_TITLE}",
        "--metadata", f"author={BOOK_AUTHOR}",
        "--metadata", f"lang={BOOK_LANGUAGE}",
        "--toc",
        "--file-scope",
        "--css", css_path,
    ]
    if USE_TOC_HIERARCHY:
        cmd.append("--split-level=2")
    if os.path.exists(os.path.join(IMG_DIR, "CoverIMG.png")):
        cmd += ["--epub-cover-image", os.path.join(IMG_DIR, "CoverIMG.png")]

    result = subprocess.run(cmd, cwd=MD_DIR, capture_output=True, text=True, encoding="utf-8")
    print(result.stdout)
    print(result.stderr)
    if result.returncode != 0:
        raise SystemExit(result.returncode)
    print(f"OK -> {OUT_EPUB}")


if __name__ == "__main__":
    main()
