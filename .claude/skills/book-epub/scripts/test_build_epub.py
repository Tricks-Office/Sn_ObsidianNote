"""build_epub.py의 회귀 테스트 스위트.

tools/book_builder/docs/SRS.md §9의 TC-ID를 테스트 이름에 그대로 붙여
추적 가능하게 한다(TC-13/14/09/36은 build_docx.py 전용이라 여기 없음,
TC-23은 실제 Claude 스킬 호출이 필요해 자동화 대상이 아니다 — 별도
수동 walkthrough 기록 참고).

실행: `python test_build_epub.py` 또는 `python -m unittest test_build_epub`
(같은 폴더의 build_epub.py를 직접 import하므로 이 파일과 같은 위치에서
실행해야 한다).
"""

import base64
import json
import os
import re
import shutil
import sys
import tempfile
import textwrap
import unittest
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_epub as be

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class BuildEpubTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="test_build_epub_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self._orig_pandoc_env = os.environ.get("BOOK_BUILDER_PANDOC")
        self._orig_timeout = be.PANDOC_TIMEOUT_SECONDS
        self.addCleanup(self._restore_env)
        self._write("본문/00_00장_프롤로그.md", "---\n소속 부/장: 프롤로그\n상태: 완료\n---\n\n프롤로그 본문.\n")
        self._write("본문/01_01장_시작.md", "---\n소속 부/장: 1부/1장\n---\n\n## 원래 소제목\n\n1장 본문.\n")
        self._write("본문/02_02장_다음.md", "---\n소속 부/장: 1부/2장\n---\n\n2장 본문.\n")

    def _restore_env(self):
        if self._orig_pandoc_env is None:
            os.environ.pop("BOOK_BUILDER_PANDOC", None)
        else:
            os.environ["BOOK_BUILDER_PANDOC"] = self._orig_pandoc_env
        be.PANDOC_TIMEOUT_SECONDS = self._orig_timeout

    def _path(self, rel):
        return os.path.join(self.root, rel)

    def _write(self, rel, content):
        p = self._path(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(content)
        return p

    def _write_bytes(self, rel, data):
        p = self._path(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def _png(self, rel):
        return self._write_bytes(rel, TINY_PNG)

    def base_config(self, **overrides):
        cfg = {
            "schema_version": "1",
            "book_title": "테스트 책",
            "book_author": "테스트 저자",
            "src_dir": "본문",
            "output_path": "out/test.epub",
            "groups": [
                {"part": None, "chapters": [{"num": "00", "title": "프롤로그", "image": None}]},
                {"part": {"title": "1부 · 테스트", "image": None},
                 "chapters": [
                     {"num": "01", "title": "1장 · 시작", "image": None},
                     {"num": "02", "title": "2장 · 다음", "image": None},
                 ]},
            ],
        }
        cfg.update(overrides)
        return cfg

    def write_config(self, cfg, name="config.json"):
        p = self._path(name)
        with open(p, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False)
        return p

    def run_build(self, cfg, name="config.json"):
        """설정을 파일로 쓰고 build_epub.main()을 직접 호출해 종료 코드를 반환한다."""
        config_path = self.write_config(cfg, name)
        return be.main(["--config", config_path])

    def out_path(self):
        return self._path("out/test.epub")

    def unzip_output(self):
        dest = self._path("unz")
        with zipfile.ZipFile(self.out_path()) as zf:
            zf.extractall(dest)
        return dest

    def read_output_text(self, *rel_parts):
        with open(os.path.join(self.unzip_output(), "EPUB", *rel_parts), encoding="utf-8") as f:
            return f.read()

    def install_pandoc_shim(self, after_version_body="", pass_version=True):
        """BOOK_BUILDER_PANDOC을 지정한 파이썬 스크립트로 임시 교체한다.
        각 토큰을 큰따옴표로 감싸 공백이 있는 경로도 안전하게 다룬다
        (pandoc_argv_prefix()가 감싼 큰따옴표를 벗겨낸다).

        `pass_version`이 True이면 `--version` 호출은 항상 성공시키고,
        그 외 호출에서만 `after_version_body`(들여쓰기 없는 별도
        텍스트로 각자 dedent됨 — 문자열을 그냥 이어붙이면 원본 들여쓰기가
        섞여 문법 오류가 나므로 조각마다 따로 dedent한다)를 실행한다."""
        parts = []
        if pass_version:
            parts.append(PASSTHROUGH_VERSION_SHIM)
        if after_version_body:
            parts.append(textwrap.dedent(after_version_body))
        shim_path = self._write("shim.py", "\n".join(parts))
        os.environ["BOOK_BUILDER_PANDOC"] = f'"{sys.executable}" "{shim_path}"'


PASSTHROUGH_VERSION_SHIM = """
import sys
if "--version" in sys.argv:
    print("pandoc 3.6.4")
    sys.exit(0)
"""


class TestSchemaAndStructure(BuildEpubTestCase):
    def test_tc01_minimal_build_success(self):
        code = self.run_build(self.base_config())
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(self.out_path()))

    def test_tc02_missing_required_field(self):
        cfg = self.base_config()
        del cfg["book_title"]
        self.assertEqual(self.run_build(cfg), be.EXIT_SCHEMA_INVALID)

    def test_tc03_unknown_nested_key(self):
        cfg = self.base_config()
        cfg["groups"][1]["chapters"][0]["bogus"] = "x"
        self.assertEqual(self.run_build(cfg), be.EXIT_SCHEMA_INVALID)

    def test_tc04_duplicate_chapter_num(self):
        cfg = self.base_config()
        cfg["groups"][0]["chapters"][0]["num"] = "01"
        self.assertEqual(self.run_build(cfg), be.EXIT_STRUCTURE_INVALID)

    def test_tc05_chapter_file_not_found(self):
        cfg = self.base_config()
        cfg["groups"][0]["chapters"][0]["num"] = "99"
        self.assertEqual(self.run_build(cfg), be.EXIT_PATH_NOT_FOUND)

    def test_tc06_ambiguous_chapter_match(self):
        self._write("dup/x_00_a.md", "내용 A\n")
        self._write("dup/x_00_b.md", "내용 B\n")
        cfg = self.base_config(
            src_dir="dup",
            groups=[{"part": None, "chapters": [{"num": "00", "title": "프롤로그", "image": None}]}],
        )
        self.assertEqual(self.run_build(cfg), be.EXIT_PATH_NOT_FOUND)

    def test_tc30_ref_dir_traversal_rejected(self):
        os.makedirs(self._path("ref"), exist_ok=True)
        cfg = self.base_config(ref_dir="ref", cover_image="../secret.png")
        self.assertEqual(self.run_build(cfg), be.EXIT_SCHEMA_INVALID)

    def test_tc31_cross_format_key_rejected(self):
        cfg = self.base_config()
        cfg["reference_doc"] = "x.docx"  # docx-only key
        self.assertEqual(self.run_build(cfg), be.EXIT_SCHEMA_INVALID)

    def test_tc32_empty_string_path_field(self):
        cfg = self.base_config(src_dir="")
        self.assertEqual(self.run_build(cfg), be.EXIT_SCHEMA_INVALID)

    def test_p105_non_nullable_fields_reject_null(self):
        for field in ("demote", "extract_fenced_tables", "extra_css", "colophon_unlisted"):
            with self.subTest(field=field):
                cfg = self.base_config(**{field: None})
                self.assertEqual(self.run_build(cfg), be.EXIT_SCHEMA_INVALID)

    def test_nullable_fields_still_accept_null(self):
        cfg = self.base_config(box_marker=None, content_image_dir=None, identifier=None)
        self.assertEqual(self.run_build(cfg), 0)

    def test_tc37_output_equals_config_alias(self):
        cfg = self.base_config(output_path="config.epub")
        # config 파일 자체를 .epub 확장자로 저장해 output_path와 동일한 파일을 가리키게 한다
        self.assertEqual(self.run_build(cfg, name="config.epub"), be.EXIT_SCHEMA_INVALID)

    def test_p103_output_equals_src_dir(self):
        os.makedirs(self._path("src.epub"), exist_ok=True)
        shutil.copy(self._path("본문/00_00장_프롤로그.md"), self._path("src.epub/00_00장_프롤로그.md"))
        cfg = self.base_config(
            src_dir="src.epub",
            output_path="src.epub",
            groups=[{"part": None, "chapters": [{"num": "00", "title": "프롤로그", "image": None}]}],
        )
        self.assertEqual(self.run_build(cfg), be.EXIT_SCHEMA_INVALID)
        # 원본 디렉터리가 그대로 있어야 한다(디렉터리로 교체되지 않음)
        self.assertTrue(os.path.isdir(self._path("src.epub")))


class TestContentPipeline(BuildEpubTestCase):
    def test_tc07_heading_demotion(self):
        cfg = self.base_config(demote={"01": 1})
        self.assertEqual(self.run_build(cfg), 0)
        html = self.read_output_text("text", "ch004.xhtml") if os.path.exists(
            os.path.join(self.unzip_output(), "EPUB", "text", "ch004.xhtml")
        ) else None
        # 강등된 소제목(### 원래 소제목)이 산출물 어딘가에 존재하는지 전체 검색으로 확인
        found = False
        unz = self.unzip_output()
        for fn in os.listdir(os.path.join(unz, "EPUB", "text")):
            with open(os.path.join(unz, "EPUB", "text", fn), encoding="utf-8") as f:
                content = f.read()
            if "<h3>원래 소제목</h3>" in content:
                found = True
        self.assertTrue(found, "demote로 강등된 h3 헤딩을 찾지 못함")

    def test_tc08_box_codefence(self):
        self._write(
            "본문/00_00장_프롤로그.md",
            "---\n소속 부/장: 프롤로그\n---\n\n일반 본문.\n\n```박스\n박스 내용입니다.\n```\n",
        )
        cfg = self.base_config(box_marker="박스")
        self.assertEqual(self.run_build(cfg), 0)
        unz = self.unzip_output()
        found = any(
            'class="box"' in open(os.path.join(unz, "EPUB", "text", fn), encoding="utf-8").read()
            for fn in os.listdir(os.path.join(unz, "EPUB", "text"))
        )
        self.assertTrue(found)

    def test_tc10_content_image(self):
        self._png("본문/이미지/01_01.png")
        self._write(
            "본문/01_01장_시작.md",
            "---\n소속 부/장: 1부/1장\n---\n\n본문.\n\n![그림](이미지/01_01.png)\n",
        )
        cfg = self.base_config(content_image_dir="본문/이미지")
        self.assertEqual(self.run_build(cfg), 0)

    def test_indented_box_inside_list(self):
        """번호 목록 항목 안에 들여쓴 박스도 변환되고 목록 번호가 살아 있다."""
        body = (
            "1. 첫 단계다.\n"
            "2. 이렇게 시킨다.\n"
            "\n"
            "   ```박스\n"
            '   "복사해줘."\n'
            "   ```\n"
            "\n"
            "3. 셋째 단계다.\n"
        )
        out = be.wrap_box_sections(body, "박스")
        self.assertIn("   ::: {.box}", out)
        self.assertIn("   :::", out)
        self.assertNotIn("```박스", out)
        # 들여쓰기를 잃으면 pandoc이 목록 밖 박스로 읽어 번호가 끊긴다.
        self.assertNotIn("\n::: {.box}", out)

    def test_tc12_css_combination(self):
        cfg = self.base_config(extra_css=".tc12-sentinel { color: red; }")
        self.assertEqual(self.run_build(cfg), 0)
        css_files = [f for f in os.listdir(os.path.join(self.unzip_output(), "EPUB", "styles"))]
        combined = ""
        for fn in css_files:
            combined += open(os.path.join(self.unzip_output(), "EPUB", "styles", fn), encoding="utf-8").read()
        self.assertIn("hidden-heading", combined)
        self.assertIn(".tc12-sentinel", combined)

    def test_tc12b_line_height_defaults(self):
        """pandoc 기본 줄간격(1.2)은 단말기에서 좁아, 기본 CSS 뒤에서 넓힌다."""
        cfg = self.base_config()
        self.assertEqual(self.run_build(cfg), 0)
        styles = os.path.join(self.unzip_output(), "EPUB", "styles")
        combined = ""
        for fn in os.listdir(styles):
            combined += open(os.path.join(styles, fn), encoding="utf-8").read()
        # 본문 150% 규칙이 pandoc 기본 h1/h2 규칙보다 뒤에 와야 이긴다.
        body_rule = combined.rindex("line-height: 150%")
        h1_rule = combined.rindex("line-height: 170%")
        h2_rule = combined.rindex("line-height: 160%")
        self.assertLess(combined.index("h1 {"), h1_rule)
        self.assertLess(combined.index("h2 {"), h2_rule)
        self.assertGreater(body_rule, combined.index("line-height: 1.2"))

    def test_tc15_metadata(self):
        cfg = self.base_config(identifier="urn:uuid:fixed-test-id")
        self.assertEqual(self.run_build(cfg), 0)
        opf_dir = os.path.join(self.unzip_output(), "EPUB")
        opf = next(f for f in os.listdir(opf_dir) if f.endswith(".opf"))
        content = open(os.path.join(opf_dir, opf), encoding="utf-8").read()
        self.assertIn("테스트 책", content)
        self.assertIn("테스트 저자", content)
        self.assertIn("urn:uuid:fixed-test-id", content)
        # language를 생략하면 ko-KR이 박힌다(넘기지 않으면 pandoc이 en-US로 쓴다)
        self.assertIn("ko-KR", content)
        self.assertNotIn("en-US", content)

    def test_language_override(self):
        cfg = self.base_config(language="en-US")
        self.assertEqual(self.run_build(cfg), 0)
        opf_dir = os.path.join(self.unzip_output(), "EPUB")
        opf = next(f for f in os.listdir(opf_dir) if f.endswith(".opf"))
        content = open(os.path.join(opf_dir, opf), encoding="utf-8").read()
        self.assertIn("en-US", content)
        self.assertNotIn("ko-KR", content)

    def test_language_empty_string_rejected(self):
        self.assertEqual(self.run_build(self.base_config(language="")), be.EXIT_SCHEMA_INVALID)

    def test_language_wrong_type_rejected(self):
        self.assertEqual(self.run_build(self.base_config(language=42)), be.EXIT_SCHEMA_INVALID)

    def test_tc21_idempotency(self):
        cfg = self.base_config(identifier="urn:uuid:fixed-test-id")
        self.assertEqual(self.run_build(cfg), 0)
        shutil.copy(self.out_path(), self._path("run1.epub"))
        self.assertEqual(self.run_build(cfg), 0)
        shutil.copy(self.out_path(), self._path("run2.epub"))
        with zipfile.ZipFile(self._path("run1.epub")) as z1, zipfile.ZipFile(self._path("run2.epub")) as z2:
            names1, names2 = sorted(z1.namelist()), sorted(z2.namelist())
            self.assertEqual(names1, names2)
            for name in names1:
                if name.lower().endswith(".opf"):
                    continue  # dcterms:modified/date는 항상 다름(SRS §4 신뢰성 정의에서 제외)
                self.assertEqual(z1.read(name), z2.read(name), f"{name} 내용이 재실행마다 달라짐")

    def test_tc24_cover_image_opf_property(self):
        self._png("ref/cover.png")
        cfg = self.base_config(ref_dir="ref", cover_image="cover.png")
        self.assertEqual(self.run_build(cfg), 0)
        opf_dir = os.path.join(self.unzip_output(), "EPUB")
        opf = next(f for f in os.listdir(opf_dir) if f.endswith(".opf"))
        content = open(os.path.join(opf_dir, opf), encoding="utf-8").read()
        self.assertIn('properties="cover-image"', content)
        # EPUB3 속성만으로는 표지를 못 찾는 리더/서점이 있어 구형 메타도 넣는다.
        self.assertIn('<meta name="cover"', content)
        item_id = re.search(r'<item[^>]*properties="cover-image"[^>]*>', content).group(0)
        cover_id = re.search(r'id="([^"]+)"', item_id).group(1)
        self.assertIn('<meta name="cover" content="%s"' % cover_id, content)

    def test_tc24b_no_cover_no_legacy_meta(self):
        """표지를 지정하지 않으면 구형 메타도 넣지 않는다."""
        cfg = self.base_config()
        self.assertEqual(self.run_build(cfg), 0)
        opf_dir = os.path.join(self.unzip_output(), "EPUB")
        opf = next(f for f in os.listdir(opf_dir) if f.endswith(".opf"))
        content = open(os.path.join(opf_dir, opf), encoding="utf-8").read()
        self.assertNotIn('<meta name="cover"', content)

    def test_tc25_chapter_cover_toc_bugfix(self):
        self._png("ref/ch01.png")
        cfg = self.base_config(ref_dir="ref")
        cfg["groups"][1]["chapters"][0]["image"] = "ch01.png"
        self.assertEqual(self.run_build(cfg), 0)
        nav = self.read_output_text("nav.xhtml")
        self.assertIn("1장 · 시작", nav)
        self.assertNotIn("표지", nav)

    def test_tc26_toc_hierarchy(self):
        cfg = self.base_config(use_toc_hierarchy=True)
        self.assertEqual(self.run_build(cfg), 0)
        nav = self.read_output_text("nav.xhtml")
        self.assertIn("1부 · 테스트", nav)
        # 부 항목 안에 중첩된 <ol>이 있어야 장이 들여쓰기된 것
        part_idx = nav.index("1부 · 테스트")
        self.assertIn('<ol class="toc">', nav[part_idx:part_idx + 400])

    def test_tc27_colophon_unlisted(self):
        self._write("ref/서지.md", "서지 정보입니다.\n")
        cfg = self.base_config(colophon_src="ref/서지.md", colophon_unlisted=True)
        self.assertEqual(self.run_build(cfg), 0)
        nav = self.read_output_text("nav.xhtml")
        self.assertNotIn("판권", nav)
        unz = self.unzip_output()
        found = any(
            "서지 정보입니다" in open(os.path.join(unz, "EPUB", "text", fn), encoding="utf-8").read()
            for fn in os.listdir(os.path.join(unz, "EPUB", "text"))
        )
        self.assertTrue(found)

    def test_tc28_source_heading_removed_footnote_kept(self):
        self._write(
            "본문/00_00장_프롤로그.md",
            "---\n소속 부/장: 프롤로그\n---\n\n본문[^1] 각주.\n\n"
            "### 출처(참고문헌, 영상, Web)\n\n[^1]: 각주 내용입니다.\n",
        )
        cfg = self.base_config(source_heading_pattern=r"^#{1,6}\s*출처\(참고문헌, 영상, Web\)\s*$")
        self.assertEqual(self.run_build(cfg), 0)
        unz = self.unzip_output()
        combined = "".join(
            open(os.path.join(unz, "EPUB", "text", fn), encoding="utf-8").read()
            for fn in os.listdir(os.path.join(unz, "EPUB", "text"))
        )
        self.assertNotIn("출처(참고문헌", combined)
        self.assertIn("각주 내용입니다", combined)

    def test_tc29_fenced_table_extraction(self):
        self._write(
            "본문/02_02장_다음.md",
            "---\n소속 부/장: 1부/2장\n---\n\n본문.\n\n```\n표:\n| 이름 | 값 |\n|---|---|\n| a | 1 |\n"
            "그 뒤 코드.\n```\n",
        )
        cfg = self.base_config(extract_fenced_tables=True)
        self.assertEqual(self.run_build(cfg), 0)
        unz = self.unzip_output()
        combined = "".join(
            open(os.path.join(unz, "EPUB", "text", fn), encoding="utf-8").read()
            for fn in os.listdir(os.path.join(unz, "EPUB", "text"))
        )
        self.assertIn("<table>", combined)
        self.assertIn("<pre><code>그 뒤 코드.</code></pre>", combined)

    def test_tc34_frontmatter_removed(self):
        cfg = self.base_config()
        self.assertEqual(self.run_build(cfg), 0)
        opf_dir = os.path.join(self.unzip_output(), "EPUB")
        opf = next(f for f in os.listdir(opf_dir) if f.endswith(".opf"))
        opf_content = open(os.path.join(opf_dir, opf), encoding="utf-8").read()
        self.assertNotIn("소속 부/장", opf_content)
        unz_text_dir = os.path.join(opf_dir, "text")
        combined = "".join(
            open(os.path.join(unz_text_dir, fn), encoding="utf-8").read() for fn in os.listdir(unz_text_dir)
        )
        self.assertNotIn("소속 부/장", combined)
        self.assertNotIn("상태: 완료", combined)


class TestFailureHandling(BuildEpubTestCase):
    def test_tc11_relative_path_from_different_cwd(self):
        cfg = self.base_config()
        config_path = self.write_config(cfg)
        old_cwd = os.getcwd()
        other_dir = tempfile.mkdtemp(prefix="test_build_epub_cwd_")
        self.addCleanup(shutil.rmtree, other_dir, ignore_errors=True)
        try:
            os.chdir(other_dir)
            code = be.main(["--config", config_path])
        finally:
            os.chdir(old_cwd)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(self.out_path()))

    def test_tc17_pandoc_missing(self):
        os.environ["BOOK_BUILDER_PANDOC"] = os.path.join(self.root, "does_not_exist_pandoc.exe")
        self.assertEqual(self.run_build(self.base_config()), be.EXIT_PANDOC_UNAVAILABLE)

    def test_tc17_pandoc_version_too_low(self):
        self.install_pandoc_shim(pass_version=False, after_version_body="""
            import sys
            if "--version" in sys.argv:
                print("pandoc 3.6.3")
            sys.exit(0)
        """)
        self.assertEqual(self.run_build(self.base_config()), be.EXIT_PANDOC_UNAVAILABLE)

    def test_pandoc_argv_prefix_handles_quoted_path_with_spaces(self):
        """BOOK_BUILDER_PANDOC이 공백 있는 경로를 큰따옴표로 감싸 지정해도
        pandoc_argv_prefix()가 따옴표를 벗겨내고 올바르게 실행해야 한다
        (Phase 1 코드 리뷰에서 관찰된 "인용부호 포함 시 PermissionError"
        문제의 회귀 테스트). shim이 버전 확인·CSS 추출·본 빌드(-o 경로에
        더미 파일 쓰기)를 모두 성공시키므로 전체 파이프라인이 종료 코드
        0으로 끝나는 것까지 확인한다(중간에 다른 이유로 실패해 이 회귀를
        가리는 일이 없도록)."""
        spaced_dir = self._path("dir with spaces")
        os.makedirs(spaced_dir, exist_ok=True)
        shim_path = os.path.join(spaced_dir, "shim.py")
        with open(shim_path, "w", encoding="utf-8") as f:
            f.write(textwrap.dedent("""
                import sys
                if "--version" in sys.argv:
                    print("pandoc 3.6.4")
                    sys.exit(0)
                if any(a.startswith("--print-default-data-file") for a in sys.argv):
                    print("/* default css */")
                    sys.exit(0)
                out = sys.argv[sys.argv.index("-o") + 1]
                with open(out, "w", encoding="utf-8") as f:
                    f.write("dummy epub content")
                sys.exit(0)
            """))
        os.environ["BOOK_BUILDER_PANDOC"] = f'"{sys.executable}" "{shim_path}"'
        code = self.run_build(self.base_config())
        self.assertEqual(code, 0)
        with open(self.out_path(), encoding="utf-8") as f:
            self.assertEqual(f.read(), "dummy epub content")

    def test_tc18_pandoc_failure(self):
        self.install_pandoc_shim(after_version_body="""
        import sys
        sys.stderr.write("shim: simulated pandoc failure\\n")
        sys.exit(1)
        """)
        self.assertEqual(self.run_build(self.base_config()), be.EXIT_PANDOC_FAILED)

    def test_tc19_pandoc_timeout(self):
        be.PANDOC_TIMEOUT_SECONDS = 1
        self.install_pandoc_shim(after_version_body="""
        import time
        time.sleep(30)
        """)
        self.assertEqual(self.run_build(self.base_config()), be.EXIT_TIMEOUT)

    def test_tc16_existing_output_preserved_on_pandoc_failure(self):
        os.makedirs(self._path("out"), exist_ok=True)
        self._write("out/test.epub", "PRE-EXISTING CONTENT")
        self.install_pandoc_shim(after_version_body="""
        import sys
        sys.exit(1)
        """)
        self.assertEqual(self.run_build(self.base_config()), be.EXIT_PANDOC_FAILED)
        with open(self.out_path(), encoding="utf-8") as f:
            self.assertEqual(f.read(), "PRE-EXISTING CONTENT")

    def test_tc20_io_error_non_utf8_chapter(self):
        self._write_bytes("본문/00_00장_프롤로그.md", "정상 텍스트\n".encode("utf-8") + bytes([0xFF, 0xFE, 0x80]))
        self.assertEqual(self.run_build(self.base_config()), be.EXIT_IO_ERROR)

    def test_tc22_build_dir_cleanup(self):
        before = set(os.listdir(tempfile.gettempdir()))
        self.assertEqual(self.run_build(self.base_config()), 0)
        after = set(os.listdir(tempfile.gettempdir()))
        leaked = [d for d in (after - before) if d.startswith("book_builder_epub_")]
        self.assertEqual(leaked, [], f"빌드 디렉터리가 정리되지 않고 남음: {leaked}")

    def test_tc35_keep_build_dir(self):
        config_path = self.write_config(self.base_config())
        code = be.main(["--config", config_path, "--keep-build-dir"])
        self.assertEqual(code, 0)
        # stderr 캡처가 없으므로(직접 main 호출), 최소한 결과물 생성만 확인하고
        # 실제 플래그 동작(보존 경고)은 CLI 서브프로세스 레벨에서 이미 별도로 확인함.
        self.assertTrue(os.path.isfile(self.out_path()))


def suite_summary():
    loader = unittest.TestLoader()
    return loader.loadTestsFromModule(sys.modules[__name__])


if __name__ == "__main__":
    unittest.main(verbosity=2)
