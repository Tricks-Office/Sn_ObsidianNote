"""build_docx.py의 회귀 테스트 스위트.

tools/book_builder/docs/SRS.md §9의 TC-ID를 테스트 이름에 그대로 붙여
추적 가능하게 한다. Implementation Plan §3.3의 TC-Phase-포맷 매트릭스에서
"공통(양쪽 실행)"으로 표시된 TC는 Phase 1(build_epub.py)에서 이미 epub
경로로 확인했더라도 여기서 docx 경로로 다시 실행한다(코드 공유가 없으므로
재검증이 필수, PRD §7). TC-08/12/24~27/30은 EPUB 전용이라 여기 없다.
TC-23은 실제 Claude 스킬 호출이 필요해 자동화 대상이 아니다.

실행: `python test_build_docx.py` 또는 `python -m unittest test_build_docx`
(같은 폴더의 build_docx.py를 직접 import하므로 이 파일과 같은 위치에서
실행해야 한다).
"""

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest
import unittest.mock
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import build_docx as bd

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
)

PASSTHROUGH_VERSION_SHIM = """
import sys
if "--version" in sys.argv:
    print("pandoc 3.6.4")
    sys.exit(0)
"""


class BuildDocxTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="test_build_docx_")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)
        self._orig_pandoc_env = os.environ.get("BOOK_BUILDER_PANDOC")
        self._orig_timeout = bd.PANDOC_TIMEOUT_SECONDS
        self.addCleanup(self._restore_env)
        self._write("본문/00_00장_프롤로그.md", "---\n소속 부/장: 프롤로그\n상태: 완료\n---\n\n프롤로그 본문.\n")
        self._write("본문/01_01장_시작.md", "---\n소속 부/장: 1부/1장\n---\n\n### 원래 소제목\n\n1장 본문.\n")
        self._write("본문/02_02장_다음.md", "---\n소속 부/장: 1부/2장\n---\n\n2장 본문.\n")

    def _restore_env(self):
        if self._orig_pandoc_env is None:
            os.environ.pop("BOOK_BUILDER_PANDOC", None)
        else:
            os.environ["BOOK_BUILDER_PANDOC"] = self._orig_pandoc_env
        bd.PANDOC_TIMEOUT_SECONDS = self._orig_timeout

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
            "output_path": "out/test.docx",
            "groups": [
                {"part": None, "chapters": [{"num": "00", "title": "프롤로그"}]},
                {"part": {"title": "1부 · 테스트"},
                 "chapters": [
                     {"num": "01", "title": "1장 · 시작"},
                     {"num": "02", "title": "2장 · 다음"},
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
        config_path = self.write_config(cfg, name)
        return bd.main(["--config", config_path])

    def out_path(self):
        return self._path("out/test.docx")

    def unzip_output(self):
        dest = self._path("unz")
        with zipfile.ZipFile(self.out_path()) as zf:
            zf.extractall(dest)
        return dest

    def read_document_xml(self):
        with open(os.path.join(self.unzip_output(), "word", "document.xml"), encoding="utf-8") as f:
            return f.read()

    def read_core_xml(self):
        with open(os.path.join(self.unzip_output(), "docProps", "core.xml"), encoding="utf-8") as f:
            return f.read()

    def read_footnotes_xml(self):
        with open(os.path.join(self.unzip_output(), "word", "footnotes.xml"), encoding="utf-8") as f:
            return f.read()

    def paragraph_style_for_text(self, xml, text_fragment):
        """text_fragment를 포함하는 <w:p>...</w:p> 문단을 찾아 그 w:pStyle 값을
        반환한다(P2-03 — 문단 텍스트와 스타일을 같은 문단 단위로 묶어 검증하기
        위함이며, "문서 어딘가에 이 텍스트가 있다"와 "문서 어딘가에 이 스타일이
        있다"를 각각 따로 확인하는 것보다 엄격하다). 찾지 못하면 None."""
        for m in re.finditer(r"<w:p\b.*?</w:p>", xml, re.S):
            para = m.group(0)
            if text_fragment in para:
                style_m = re.search(r'<w:pStyle w:val="([^"]*)"', para)
                if not style_m:
                    return None
                # XML 속성 값의 &quot;/&amp;/&lt;/&gt;/&apos; 엔티티를 원래 문자로 되돌린다
                value = style_m.group(1)
                for entity, char in (("&quot;", '"'), ("&apos;", "'"), ("&lt;", "<"), ("&gt;", ">"), ("&amp;", "&")):
                    value = value.replace(entity, char)
                return value
        return None

    def make_reference_doc(self, rel="ref.docx"):
        """pandoc 기본 참조 문서를 뽑아 reference_doc으로 쓸 파일을 만든다."""
        p = self._path(rel)
        os.makedirs(os.path.dirname(p), exist_ok=True) if os.path.dirname(p) else None
        result = subprocess.run(
            ["pandoc", "--print-default-data-file=reference.docx"],
            capture_output=True, timeout=30, check=True,
        )
        with open(p, "wb") as f:
            f.write(result.stdout)
        return p

    def install_pandoc_shim(self, after_version_body="", pass_version=True):
        parts = []
        if pass_version:
            parts.append(PASSTHROUGH_VERSION_SHIM)
        if after_version_body:
            parts.append(textwrap.dedent(after_version_body))
        shim_path = self._write("shim.py", "\n".join(parts))
        os.environ["BOOK_BUILDER_PANDOC"] = f'"{sys.executable}" "{shim_path}"'


class TestSchemaAndStructure(BuildDocxTestCase):
    def test_tc01_minimal_build_success(self):
        code = self.run_build(self.base_config())
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(self.out_path()))

    def test_tc02_missing_required_field(self):
        cfg = self.base_config()
        del cfg["book_title"]
        self.assertEqual(self.run_build(cfg), bd.EXIT_SCHEMA_INVALID)

    def test_tc03_unknown_nested_key(self):
        cfg = self.base_config()
        cfg["groups"][1]["chapters"][0]["bogus"] = "x"
        self.assertEqual(self.run_build(cfg), bd.EXIT_SCHEMA_INVALID)

    def test_tc04_duplicate_chapter_num(self):
        cfg = self.base_config()
        cfg["groups"][0]["chapters"][0]["num"] = "01"
        self.assertEqual(self.run_build(cfg), bd.EXIT_STRUCTURE_INVALID)

    def test_tc05_chapter_file_not_found(self):
        cfg = self.base_config()
        cfg["groups"][0]["chapters"][0]["num"] = "99"
        self.assertEqual(self.run_build(cfg), bd.EXIT_PATH_NOT_FOUND)

    def test_tc06_ambiguous_chapter_match(self):
        self._write("dup/x_00_a.md", "내용 A\n")
        self._write("dup/x_00_b.md", "내용 B\n")
        cfg = self.base_config(
            src_dir="dup",
            groups=[{"part": None, "chapters": [{"num": "00", "title": "프롤로그"}]}],
        )
        self.assertEqual(self.run_build(cfg), bd.EXIT_PATH_NOT_FOUND)

    def test_tc09_box_option_mismatch(self):
        cfg = self.base_config(box_marker="박스")  # box_style_name/reference_doc 없음
        self.assertEqual(self.run_build(cfg), bd.EXIT_OPTION_CONFLICT)

    def test_tc31_cross_format_key_rejected(self):
        """DOCX에 없는 EPUB 전용 키는 거부한다(표지·간지 도입 뒤에도 남는 것들)."""
        cfg = self.base_config()
        cfg["use_toc_hierarchy"] = True
        self.assertEqual(self.run_build(cfg), bd.EXIT_SCHEMA_INVALID)

    def test_ref_dir_without_images_is_allowed(self):
        """ref_dir만 있고 참조하는 이미지가 없으면 그냥 쓰이지 않을 뿐 오류가 아니다."""
        os.makedirs(self._path("ref"), exist_ok=True)
        cfg = self.base_config(ref_dir="ref")
        self.assertEqual(self.run_build(cfg), 0)

    def test_image_without_ref_dir_rejected(self):
        """표지·간지 이미지를 쓰면서 ref_dir을 빠뜨리면 거부한다."""
        cfg = self.base_config(cover_image="cover.png")
        self.assertEqual(self.run_build(cfg), bd.EXIT_SCHEMA_INVALID)

    def test_missing_ref_image_file_rejected(self):
        """ref_dir에 실제 파일이 없으면 빌드 전에 잡는다."""
        os.makedirs(self._path("ref"), exist_ok=True)
        cfg = self.base_config(ref_dir="ref", cover_image="없는파일.png")
        self.assertEqual(self.run_build(cfg), bd.EXIT_PATH_NOT_FOUND)

    def test_epub_only_keys_all_rejected(self):
        for key, value in (
            ("use_toc_hierarchy", True),
            ("colophon_src", "x.md"), ("colophon_unlisted", True),
            ("extra_css", "a{}"), ("identifier", "urn:x"),
        ):
            with self.subTest(key=key):
                cfg = self.base_config()
                cfg[key] = value
                self.assertEqual(self.run_build(cfg, name=f"cfg_{key}.json"), bd.EXIT_SCHEMA_INVALID)

    def test_cover_and_divider_images_embedded(self):
        """표지·부 간지·장 표지 이미지가 실제로 docx에 들어간다."""
        self._png("ref/cover.png")
        self._png("ref/divider.png")
        self._png("ref/chapcover.png")
        cfg = self.base_config(ref_dir="ref", cover_image="cover.png")
        cfg["groups"][1]["part"]["image"] = "divider.png"
        cfg["groups"][1]["chapters"][0]["image"] = "chapcover.png"
        self.assertEqual(self.run_build(cfg), 0)

        media = os.path.join(self.unzip_output(), "word", "media")
        self.assertTrue(os.path.isdir(media), "word/media 폴더가 없다 — 이미지가 안 들어갔다")
        self.assertEqual(len(os.listdir(media)), 3)

        xml = self.read_document_xml()
        # 부 제목은 남아 있어야 한다 — 워드의 자동 목차가 이 스타일을 읽는다.
        self.assertIn("1부", xml)
        # 이미지마다 쪽 나누기가 따라붙는다.
        self.assertGreaterEqual(xml.count('w:br w:type="page"'), 3)

    def test_cover_adds_no_heading(self):
        """표지는 헤딩 없이 이미지만 — 워드 자동 목차에 "표지"가 끼면 안 된다."""
        self._png("ref/cover.png")
        cfg = self.base_config(ref_dir="ref", cover_image="cover.png")
        self.assertEqual(self.run_build(cfg), 0)
        xml = self.read_document_xml()
        self.assertNotIn("표지", xml)

    def test_tc32_empty_string_path_field(self):
        cfg = self.base_config(src_dir="")
        self.assertEqual(self.run_build(cfg), bd.EXIT_SCHEMA_INVALID)

    def test_non_nullable_fields_reject_null(self):
        for field in ("demote", "extract_fenced_tables"):
            with self.subTest(field=field):
                cfg = self.base_config(**{field: None})
                self.assertEqual(self.run_build(cfg, name=f"cfg_{field}.json"), bd.EXIT_SCHEMA_INVALID)

    def test_nullable_fields_still_accept_null(self):
        cfg = self.base_config(box_marker=None, content_image_dir=None)
        self.assertEqual(self.run_build(cfg), 0)

    def test_tc37_output_equals_config_alias(self):
        cfg = self.base_config(output_path="config.docx")
        self.assertEqual(self.run_build(cfg, name="config.docx"), bd.EXIT_SCHEMA_INVALID)

    def test_tc36_output_equals_reference_doc(self):
        ref = self.make_reference_doc("shared.docx")
        cfg = self.base_config(reference_doc="shared.docx", box_marker="박스", box_style_name="박스스타일",
                                output_path="shared.docx")
        code = self.run_build(cfg)
        self.assertEqual(code, bd.EXIT_SCHEMA_INVALID)
        # 원본 reference_doc이 그대로 보존됐어야 한다
        self.assertTrue(os.path.isfile(ref))

    def test_output_equals_src_dir(self):
        os.makedirs(self._path("src.docx"), exist_ok=True)
        shutil.copy(self._path("본문/00_00장_프롤로그.md"), self._path("src.docx/00_00장_프롤로그.md"))
        cfg = self.base_config(
            src_dir="src.docx",
            output_path="src.docx",
            groups=[{"part": None, "chapters": [{"num": "00", "title": "프롤로그"}]}],
        )
        self.assertEqual(self.run_build(cfg), bd.EXIT_SCHEMA_INVALID)
        self.assertTrue(os.path.isdir(self._path("src.docx")))


class TestContentPipeline(BuildDocxTestCase):
    def test_tc07_heading_demotion(self):
        cfg = self.base_config(demote={"01": 1})
        self.assertEqual(self.run_build(cfg), 0)
        xml = self.read_document_xml()
        # setUp의 01장 본문에 있는 "### 원래 소제목"(h3)이 +1 강등되어 h4가 돼야 한다
        self.assertEqual(self.paragraph_style_for_text(xml, "원래 소제목"), "Heading4")
        # 스크립트가 주입하는 장 제목 자체(## 1장 · 시작)는 demote 대상이 아니라 그대로 Heading2
        self.assertEqual(self.paragraph_style_for_text(xml, "1장 · 시작"), "Heading2")

    def test_tc10_content_image(self):
        self._png("본문/이미지/01_01.png")
        self._write(
            "본문/01_01장_시작.md",
            "---\n소속 부/장: 1부/1장\n---\n\n본문.\n\n![그림](이미지/01_01.png)\n",
        )
        cfg = self.base_config(content_image_dir="본문/이미지")
        self.assertEqual(self.run_build(cfg), 0)
        media_dir = os.path.join(self.unzip_output(), "word", "media")
        self.assertTrue(os.path.isdir(media_dir) and os.listdir(media_dir))

    def test_indented_box_inside_list(self):
        """번호 목록 항목 안에 들여쓴 박스도 변환되고 들여쓰기가 유지된다."""
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
        out = bd.wrap_box_sections(body, "박스", "박스스타일")
        self.assertIn('   ::: {custom-style="박스스타일"}', out)
        self.assertIn("   :::", out)
        self.assertNotIn("```박스", out)
        self.assertNotIn('\n::: {custom-style=', out)

    def test_tc13_docx_custom_style_mapping(self):
        self._write(
            "본문/00_00장_프롤로그.md",
            "---\n소속 부/장: 프롤로그\n---\n\n일반 문단.\n\n```박스\n박스 내용입니다.\n```\n",
        )
        self.make_reference_doc("ref.docx")
        cfg = self.base_config(box_marker="박스", box_style_name="박스스타일", reference_doc="ref.docx")
        self.assertEqual(self.run_build(cfg), 0)
        xml = self.read_document_xml()
        self.assertIn('w:pStyle w:val="박스스타일"', xml)

    def test_tc14_prologue_epilogue_heading_level(self):
        """FR-45: part가 null인 그룹의 챕터도 항상 Heading2(부 제목만 Heading1)."""
        cfg = self.base_config()
        self.assertEqual(self.run_build(cfg), 0)
        xml = self.read_document_xml()
        # "프롤로그" 문단 자체의 스타일이 정확히 Heading2인지 확인(다른 곳에 있는
        # Heading2와 프롤로그 텍스트를 따로 확인하면 실제로는 어긋나 있어도 통과할 수 있다)
        self.assertEqual(self.paragraph_style_for_text(xml, "프롤로그"), "Heading2")
        # 부 제목("1부 · 테스트")은 Heading1
        self.assertEqual(self.paragraph_style_for_text(xml, "1부 · 테스트"), "Heading1")

    def test_tc15_metadata(self):
        cfg = self.base_config()
        self.assertEqual(self.run_build(cfg), 0)
        core = self.read_core_xml()
        self.assertIn("테스트 책", core)
        self.assertIn("테스트 저자", core)

    def test_tc21_idempotency(self):
        cfg = self.base_config()
        self.assertEqual(self.run_build(cfg), 0)
        shutil.copy(self.out_path(), self._path("run1.docx"))
        self.assertEqual(self.run_build(cfg), 0)
        shutil.copy(self.out_path(), self._path("run2.docx"))
        with zipfile.ZipFile(self._path("run1.docx")) as z1, zipfile.ZipFile(self._path("run2.docx")) as z2:
            names1, names2 = sorted(z1.namelist()), sorted(z2.namelist())
            self.assertEqual(names1, names2)
            for name in names1:
                if name == "docProps/core.xml":
                    continue  # 수정 시각 등 비결정적 필드가 있어 제외(SRS §4)
                self.assertEqual(z1.read(name), z2.read(name), f"{name} 내용이 재실행마다 달라짐")

    def test_tc28_source_heading_removed_footnote_kept(self):
        self._write(
            "본문/00_00장_프롤로그.md",
            "---\n소속 부/장: 프롤로그\n---\n\n본문[^1] 각주.\n\n"
            "### 출처(참고문헌, 영상, Web)\n\n[^1]: 각주 내용입니다.\n",
        )
        cfg = self.base_config(source_heading_pattern=r"^#{1,6}\s*출처\(참고문헌, 영상, Web\)\s*$")
        self.assertEqual(self.run_build(cfg), 0)
        xml = self.read_document_xml()
        self.assertNotIn("출처(참고문헌", xml)
        # 각주 본문은 document.xml이 아니라 word/footnotes.xml에 별도로 들어간다(docx 고유 구조)
        self.assertIn("각주 내용입니다", self.read_footnotes_xml())

    def test_p201_multi_chapter_footnote_numbers_do_not_collide(self):
        """P2-01 회귀: 서로 다른 챕터가 각자 [^1]부터 다시 매기는 각주를 쓰면
        --file-scope 없이는 뒤 챕터의 정의가 앞 챕터의 정의를 덮어써 내용이
        사라진다(실측으로 확인된 결함, 이제 FR-47에 따라 --file-scope로 방지)."""
        self._write("본문/00_00장_프롤로그.md", "본문A[^1]\n\n[^1]: FIRST_NOTE_SENTINEL\n")
        self._write("본문/01_01장_시작.md", "본문B[^1]\n\n[^1]: SECOND_NOTE_SENTINEL\n")
        cfg = self.base_config(
            groups=[{"part": None, "chapters": [{"num": "00", "title": "A"}, {"num": "01", "title": "B"}]}],
        )
        self.assertEqual(self.run_build(cfg), 0)
        footnotes = self.read_footnotes_xml()
        self.assertIn("FIRST_NOTE_SENTINEL", footnotes)
        self.assertIn("SECOND_NOTE_SENTINEL", footnotes)

    def test_p202_box_style_name_with_embedded_quote(self):
        """P2-02 회귀: box_style_name에 큰따옴표가 포함돼도 fenced div 구문이
        깨지지 않고 지정한 스타일 이름 그대로 적용돼야 한다."""
        self._write(
            "본문/00_00장_프롤로그.md",
            "---\n소속 부/장: 프롤로그\n---\n\n```박스\nBOX_SENTINEL\n```\n",
        )
        self.make_reference_doc("ref.docx")
        cfg = self.base_config(box_marker="박스", box_style_name='Note "Special"', reference_doc="ref.docx")
        self.assertEqual(self.run_build(cfg), 0)
        xml = self.read_document_xml()
        self.assertNotIn(":::", xml)
        self.assertNotIn("custom-style", xml)
        self.assertEqual(self.paragraph_style_for_text(xml, "BOX_SENTINEL"), 'Note"Special"')

    def test_tc29_fenced_table_extraction(self):
        self._write(
            "본문/02_02장_다음.md",
            "---\n소속 부/장: 1부/2장\n---\n\n본문.\n\n```\n표:\n| 이름 | 값 |\n|---|---|\n| a | 1 |\n"
            "그 뒤 코드.\n```\n",
        )
        cfg = self.base_config(extract_fenced_tables=True)
        self.assertEqual(self.run_build(cfg), 0)
        xml = self.read_document_xml()
        self.assertIn("<w:tbl>", xml)

    def test_tc34_frontmatter_removed(self):
        cfg = self.base_config()
        self.assertEqual(self.run_build(cfg), 0)
        xml = self.read_document_xml()
        core = self.read_core_xml()
        self.assertNotIn("소속 부/장", xml)
        self.assertNotIn("상태: 완료", xml)
        self.assertNotIn("소속 부/장", core)


class TestFailureHandling(BuildDocxTestCase):
    def test_tc11_relative_path_from_different_cwd(self):
        cfg = self.base_config()
        config_path = self.write_config(cfg)
        old_cwd = os.getcwd()
        other_dir = tempfile.mkdtemp(prefix="test_build_docx_cwd_")
        self.addCleanup(shutil.rmtree, other_dir, ignore_errors=True)
        try:
            os.chdir(other_dir)
            code = bd.main(["--config", config_path])
        finally:
            os.chdir(old_cwd)
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(self.out_path()))

    def test_tc17_pandoc_missing(self):
        os.environ["BOOK_BUILDER_PANDOC"] = os.path.join(self.root, "does_not_exist_pandoc.exe")
        self.assertEqual(self.run_build(self.base_config()), bd.EXIT_PANDOC_UNAVAILABLE)

    def test_tc17_pandoc_version_too_low(self):
        self.install_pandoc_shim(pass_version=False, after_version_body="""
            import sys
            if "--version" in sys.argv:
                print("pandoc 3.6.3")
            sys.exit(0)
        """)
        self.assertEqual(self.run_build(self.base_config()), bd.EXIT_PANDOC_UNAVAILABLE)

    def test_tc18_pandoc_failure(self):
        self.install_pandoc_shim(after_version_body="""
        import sys
        sys.stderr.write("shim: simulated pandoc failure\\n")
        sys.exit(1)
        """)
        self.assertEqual(self.run_build(self.base_config()), bd.EXIT_PANDOC_FAILED)

    def test_tc19_pandoc_timeout(self):
        bd.PANDOC_TIMEOUT_SECONDS = 1
        self.install_pandoc_shim(after_version_body="""
        import time
        time.sleep(30)
        """)
        self.assertEqual(self.run_build(self.base_config()), bd.EXIT_TIMEOUT)

    def test_tc16_existing_output_preserved_on_pandoc_failure(self):
        os.makedirs(self._path("out"), exist_ok=True)
        self._write("out/test.docx", "PRE-EXISTING CONTENT")
        self.install_pandoc_shim(after_version_body="""
        import sys
        sys.exit(1)
        """)
        self.assertEqual(self.run_build(self.base_config()), bd.EXIT_PANDOC_FAILED)
        with open(self.out_path(), encoding="utf-8") as f:
            self.assertEqual(f.read(), "PRE-EXISTING CONTENT")

    def test_tc20_io_error_non_utf8_chapter(self):
        self._write_bytes("본문/00_00장_프롤로그.md", "정상 텍스트\n".encode("utf-8") + bytes([0xFF, 0xFE, 0x80]))
        self.assertEqual(self.run_build(self.base_config()), bd.EXIT_IO_ERROR)

    def test_tc22_build_dir_cleanup(self):
        before = set(os.listdir(tempfile.gettempdir()))
        self.assertEqual(self.run_build(self.base_config()), 0)
        after = set(os.listdir(tempfile.gettempdir()))
        leaked = [d for d in (after - before) if d.startswith("book_builder_docx_")]
        self.assertEqual(leaked, [], f"빌드 디렉터리가 정리되지 않고 남음: {leaked}")

    def test_tc35_keep_build_dir(self):
        config_path = self.write_config(self.base_config())
        code = bd.main(["--config", config_path, "--keep-build-dir"])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(self.out_path()))

    def test_tc33_build_dir_cleanup_failure_is_nonblocking(self):
        """빌드 디렉터리 정리(shutil.rmtree)가 실패해도 이미 성공한 빌드 결과
        (종료 코드 0, output_path 생성)는 그대로 유지되어야 한다(FR-50, SRS-14)."""
        config_path = self.write_config(self.base_config())
        with unittest.mock.patch.object(bd.shutil, "rmtree", side_effect=PermissionError("simulated")):
            code = bd.main(["--config", config_path])
        self.assertEqual(code, 0)
        self.assertTrue(os.path.isfile(self.out_path()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
