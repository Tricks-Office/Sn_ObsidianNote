#!/usr/bin/env python3
"""Non-interactive extract/pack helper for the epub_editor skill.

See tools/epub_editor/docs/SRS.md for the full functional contract
(exit codes, path-safety rules, backup/restore/promote state machine).
All destructive choices (folder conflicts, overwrite) are decided by
the caller (Claude, via SKILL.md) and passed in as CLI flags -- this
script never prompts.
"""
import argparse
import os
import re
import secrets
import shutil
import stat
import sys
import time
import zipfile
from pathlib import Path

EXIT_OK = 0
EXIT_BAD_EPUB_PATH = 2
EXIT_BAD_ZIP = 3
EXIT_UNSAFE_ENTRY = 4
EXIT_ENCRYPTED_ENTRY = 5
EXIT_BAD_DEST_NAME = 6
EXIT_DEST_EXISTS = 7
EXIT_EXTRACT_ERROR = 8
EXIT_BACKUP_FAILED = 9
EXIT_DEST_NOT_PLAIN_DIR = 10
EXIT_RESTORE_FAILED = 11
EXIT_PROMOTE_FAILED = 12
EXIT_PACK_PRECONDITION = 13
EXIT_SELF_CONTAINED = 14
EXIT_OUTPUT_EXISTS = 15
EXIT_PACK_WRITE_ERROR = 16

MIMETYPE_BYTES = b"application/epub+zip"

WINDOWS_RESERVED_NAMES = (
    {"CON", "PRN", "AUX", "NUL"}
    | {f"COM{i}" for i in range(1, 10)}
    | {f"LPT{i}" for i in range(1, 10)}
)


class ToolError(Exception):
    def __init__(self, exit_code: int, message: str):
        super().__init__(message)
        self.exit_code = exit_code
        self.message = message


def fail(exit_code: int, message: str) -> "ToolError":
    return ToolError(exit_code, message)


def validate_dest_name(name: str) -> None:
    if not name:
        raise fail(EXIT_BAD_DEST_NAME, "목적지 폴더명이 비어 있습니다")
    if "/" in name or "\\" in name:
        raise fail(
            EXIT_BAD_DEST_NAME,
            f"목적지 폴더명에 경로 구분자를 포함할 수 없습니다: {name!r}",
        )
    if name in (".", ".."):
        raise fail(
            EXIT_BAD_DEST_NAME, f"목적지 폴더명으로 '.'/'..'를 사용할 수 없습니다: {name!r}"
        )
    if re.match(r"^[A-Za-z]:", name):
        raise fail(
            EXIT_BAD_DEST_NAME, f"목적지 폴더명에 드라이브 문자를 사용할 수 없습니다: {name!r}"
        )
    base = name.split(".", 1)[0].upper()
    if base in WINDOWS_RESERVED_NAMES:
        raise fail(
            EXIT_BAD_DEST_NAME,
            f"목적지 폴더명으로 Windows 예약 이름을 사용할 수 없습니다: {name!r}",
        )
    if name[-1] in (".", " "):
        raise fail(
            EXIT_BAD_DEST_NAME, f"목적지 폴더명은 '.' 또는 공백으로 끝날 수 없습니다: {name!r}"
        )


def is_reparse_point(path: Path) -> bool:
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    reparse_attr = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", None)
    file_attrs = getattr(st, "st_file_attributes", None)
    if reparse_attr is not None and file_attrs is not None:
        return bool(file_attrs & reparse_attr)
    return os.path.islink(path)


def is_within(child: str, root: str) -> bool:
    root_n = os.path.normcase(os.path.normpath(root))
    child_n = os.path.normcase(os.path.normpath(child))
    return child_n == root_n or child_n.startswith(root_n + os.sep)


def safe_entry_parts(name: str):
    """Return (parts, is_dir) for a zip entry, or None if the entry is unsafe.

    Rejects absolute/drive/UNC paths and any '.'/'..'/empty segment instead
    of trying to mathematically resolve them away (SRS §2 "금지된 경로
    세그먼트"): ambiguous interpretations of ".." are the root cause of
    zip-slip, so entries containing them are refused outright rather than
    "safely" normalized.
    """
    if "\x00" in name:
        return None
    if name.startswith("/"):
        return None
    if "\\" in name:
        return None
    if re.match(r"^[A-Za-z]:", name):
        return None
    is_dir = name.endswith("/")
    core = name[:-1] if is_dir else name
    if core == "":
        return None
    parts = core.split("/")
    if any(p in ("", ".", "..") for p in parts):
        return None
    return parts, is_dir


def cmd_extract(args: argparse.Namespace) -> int:
    epub_path = Path(args.epub_path).resolve()
    if epub_path.suffix.lower() != ".epub" or not epub_path.is_file():
        raise fail(
            EXIT_BAD_EPUB_PATH,
            f"epub 파일이 아니거나 존재하지 않습니다: {epub_path}",
        )

    try:
        zf = zipfile.ZipFile(epub_path)
    except (zipfile.BadZipFile, OSError) as exc:
        raise fail(EXIT_BAD_ZIP, f"유효한 zip이 아닙니다: {epub_path} ({exc})")

    with zf:
        dest_name = args.dest_name if args.dest_name else epub_path.stem
        validate_dest_name(dest_name)

        parent = epub_path.parent
        dest_root = parent / dest_name

        seen: dict[str, str] = {}
        entries = []  # (info, parts, is_dir)
        for info in zf.infolist():
            parsed = safe_entry_parts(info.filename)
            if parsed is None:
                raise fail(
                    EXIT_UNSAFE_ENTRY,
                    f"안전하지 않은 압축 항목입니다: {info.filename!r}",
                )
            parts, is_dir = parsed
            key = os.path.normcase(os.path.join(*parts))
            if key in seen:
                raise fail(
                    EXIT_UNSAFE_ENTRY,
                    "정규화 충돌: "
                    f"{seen[key]!r}와 {info.filename!r}이(가) 같은 목적지 경로로 충돌합니다",
                )
            seen[key] = info.filename
            if info.flag_bits & 0x1:
                raise fail(
                    EXIT_ENCRYPTED_ENTRY,
                    f"암호화된 항목이 포함되어 있습니다(DRM/암호화 epub 미지원): {info.filename!r}",
                )
            entries.append((info, parts, is_dir))

        backup_path = None
        if os.path.lexists(dest_root):
            # Reject anything that isn't a plain directory (regular file,
            # symlink, junction) before even looking at --force: none of
            # those are the "existing extraction folder" this conflict flow
            # is designed for, and treating a file as a folder further down
            # (iterdir/rename-as-backup) would corrupt unrelated data.
            if dest_root.is_symlink() or is_reparse_point(dest_root) or not dest_root.is_dir():
                raise fail(
                    EXIT_DEST_NOT_PLAIN_DIR,
                    "목적지가 일반 폴더가 아닙니다(파일 또는 symlink/정션으로 추정), "
                    f"수동 확인 필요: {dest_root}",
                )
            if not args.force:
                nonempty = any(dest_root.iterdir())
                raise fail(
                    EXIT_DEST_EXISTS,
                    f"목적지 폴더가 이미 존재합니다: {dest_root} "
                    f"(비어있지 않음: {nonempty})",
                )
            backup_path = parent / f"{dest_name}.bak-{int(time.time())}-{secrets.token_hex(4)}"
            try:
                os.rename(dest_root, backup_path)
            except OSError as exc:
                raise fail(
                    EXIT_BACKUP_FAILED,
                    f"기존 폴더 백업(rename)에 실패했습니다, 기존 폴더는 그대로입니다: {dest_root} ({exc})",
                )

        temp_dir = None
        try:
            for _ in range(5):
                candidate = parent / f"{dest_name}.extracting-{secrets.token_hex(4)}"
                try:
                    os.mkdir(candidate)
                except FileExistsError:
                    continue
                temp_dir = candidate
                break
            if temp_dir is None:
                raise OSError("임시 작업 폴더 이름을 생성하지 못했습니다(반복된 이름 충돌)")
            for info, parts, is_dir in entries:
                target = temp_dir.joinpath(*parts)
                if is_dir:
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with zf.open(info) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
        except Exception as exc:  # noqa: BLE001 - convert any failure into FR-17 handling
            restore_failed = False
            if backup_path is not None:
                try:
                    os.rename(backup_path, dest_root)
                except OSError:
                    restore_failed = True
            cleanup_failed = False
            # Only clean up temp_dir if we actually created it ourselves
            # (exclusive os.mkdir above) -- never rmtree a path we merely
            # guessed the name of.
            if temp_dir is not None:
                try:
                    shutil.rmtree(temp_dir, ignore_errors=False)
                except OSError:
                    cleanup_failed = True

            if restore_failed:
                extra = f", 정리도 실패해 임시 폴더가 남아있습니다: {temp_dir}" if cleanup_failed else ""
                raise fail(
                    EXIT_RESTORE_FAILED,
                    "압축 해제 실패 후 백업 복원도 실패했습니다. "
                    f"수동으로 확인해 주세요. 백업 폴더: {backup_path}{extra} (원인: {exc})",
                )
            if cleanup_failed:
                raise fail(
                    EXIT_EXTRACT_ERROR,
                    f"압축 해제 도중 오류가 발생했습니다({exc}). 목적지는 이전 상태로 복원됐지만, "
                    f"임시 폴더 정리에 실패해 수동 삭제가 필요합니다: {temp_dir}",
                )
            raise fail(
                EXIT_EXTRACT_ERROR,
                f"압축 해제 도중 오류가 발생했습니다({exc}). 목적지는 이전 상태로 복원됐습니다.",
            )

        try:
            os.rename(temp_dir, dest_root)
        except OSError as exc:
            restore_note = ""
            if backup_path is not None:
                try:
                    os.rename(backup_path, dest_root)
                    restore_note = f", 기존 폴더는 복원됐습니다: {dest_root}"
                except OSError:
                    restore_note = f", 백업 폴더가 남아있습니다: {backup_path}"
            raise fail(
                EXIT_PROMOTE_FAILED,
                f"결과 승격(임시 폴더 이름 변경)에 실패했습니다({exc}). "
                f"압축 해제 결과는 다음 경로에 그대로 남아있습니다: {temp_dir}{restore_note}",
            )

        if backup_path is not None:
            try:
                shutil.rmtree(backup_path)
            except OSError as exc:
                print(
                    f"경고: 백업 폴더 삭제에 실패했습니다, 수동으로 삭제해 주세요: {backup_path} ({exc})",
                    file=sys.stderr,
                )

    print(str(dest_root))
    return EXIT_OK


def _reraise(exc: OSError):
    # os.walk's default onerror=None silently skips a directory it can't
    # scandir(), which would let cmd_pack report success (code 0) on an
    # epub that's quietly missing some of the folder's content.
    raise exc


def _iter_folder_files(folder: Path):
    """Yield packable files under folder, sorted by relative path.

    Uses os.walk(followlinks=False, onerror=_reraise) and explicitly
    rejects any symlinked or reparse-point (e.g. Windows junction) file
    or directory instead of silently skipping or following it: either
    could otherwise pull bytes from outside folder_path into the produced
    epub, or quietly drop content the user expects to be packaged.
    followlinks=False alone is not enough on Windows -- junctions are not
    reported as symlinks by Path.is_symlink(), so is_reparse_point() is
    checked as well.
    """
    collected = []
    for root, dirs, files in os.walk(folder, onerror=_reraise, followlinks=False):
        root_path = Path(root)
        for d in dirs:
            dir_path = root_path / d
            if dir_path.is_symlink() or is_reparse_point(dir_path):
                raise ValueError(
                    "폴더 안에 심볼릭 링크·정션(디렉터리)이 포함되어 있어 재압축할 수 없습니다: "
                    f"{dir_path}"
                )
        for name in files:
            path = root_path / name
            if path.is_symlink() or is_reparse_point(path):
                raise ValueError(
                    f"폴더 안에 심볼릭 링크·정션이 포함되어 있어 재압축할 수 없습니다: {path}"
                )
            if root_path == folder and name == "mimetype":
                continue
            collected.append(path)
    collected.sort(key=lambda p: p.relative_to(folder).as_posix())
    yield from collected


def cmd_pack(args: argparse.Namespace) -> int:
    folder_path = Path(args.folder_path).resolve()
    output_path = Path(args.output_epub_path).resolve()

    mimetype_path = folder_path / "mimetype"
    container_path = folder_path / "META-INF" / "container.xml"

    if (
        not mimetype_path.is_file()
        or mimetype_path.is_symlink()
        or is_reparse_point(mimetype_path)
        or not container_path.is_file()
        or container_path.is_symlink()
        or is_reparse_point(container_path)
    ):
        raise fail(
            EXIT_PACK_PRECONDITION,
            "재압축 대상 폴더에 필수 파일이 없거나 일반 파일이 아닙니다"
            "(mimetype/META-INF/container.xml, symlink·정션 불가): "
            f"{folder_path}",
        )
    try:
        mimetype_content = mimetype_path.read_bytes()
    except OSError as exc:
        raise fail(EXIT_PACK_PRECONDITION, f"mimetype 파일을 읽을 수 없습니다: {exc}")
    if mimetype_content != MIMETYPE_BYTES:
        raise fail(
            EXIT_PACK_PRECONDITION,
            "mimetype 내용이 정확한 'application/epub+zip'(20바이트)이 아닙니다: "
            f"{mimetype_path}",
        )

    folder_str = str(folder_path)
    if is_within(str(output_path.parent), folder_str) or is_within(str(output_path), folder_str):
        raise fail(
            EXIT_SELF_CONTAINED,
            "저장 대상이 재압축 대상 폴더 자신이거나 하위 경로입니다. "
            f"폴더({folder_path}) 밖의 경로를 지정해 주세요: {output_path}",
        )

    if output_path.exists() and not args.force:
        raise fail(
            EXIT_OUTPUT_EXISTS,
            f"저장 대상 경로에 이미 파일이 있습니다: {output_path}",
        )

    tmp_path = None
    try:
        tmp_fd = None
        for _ in range(5):
            candidate = output_path.parent / f".{output_path.name}.tmp-{secrets.token_hex(4)}"
            try:
                tmp_fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_RDWR)
            except FileExistsError:
                continue
            tmp_path = candidate
            break
        if tmp_path is None:
            raise OSError("임시 파일 이름을 생성하지 못했습니다(이름 충돌 또는 다른 쓰기 오류)")
        with os.fdopen(tmp_fd, "r+b") as fh:
            with zipfile.ZipFile(fh, "w") as zf:
                zf.writestr(
                    zipfile.ZipInfo("mimetype"),
                    mimetype_content,
                    compress_type=zipfile.ZIP_STORED,
                )
                for file_path in _iter_folder_files(folder_path):
                    arcname = file_path.relative_to(folder_path).as_posix()
                    zf.write(file_path, arcname=arcname, compress_type=zipfile.ZIP_DEFLATED)
        os.replace(tmp_path, output_path)
    except Exception as exc:  # noqa: BLE001 - convert any failure into FR-25 handling
        cleanup_failed = False
        # Only clean up tmp_path if we actually created it ourselves
        # (exclusive os.open above) -- never unlink a path we merely
        # guessed the name of.
        if tmp_path is not None:
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except OSError:
                cleanup_failed = True
        note = f" 임시 파일이 남아있습니다, 수동 삭제 필요: {tmp_path}" if cleanup_failed else ""
        raise fail(
            EXIT_PACK_WRITE_ERROR,
            f"재압축 중 오류가 발생했습니다({exc}). 기존 출력 파일은 보존됩니다.{note}",
        )

    print(str(output_path))
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="epub_tool.py")
    sub = parser.add_subparsers(dest="command", required=True)

    p_extract = sub.add_parser("extract", help="epub을 편집용 폴더로 압축 해제")
    p_extract.add_argument("epub_path")
    p_extract.add_argument("--force", action="store_true")
    p_extract.add_argument("--dest-name", default=None)
    p_extract.set_defaults(func=cmd_extract)

    p_pack = sub.add_parser("pack", help="편집용 폴더를 epub로 재압축")
    p_pack.add_argument("folder_path")
    p_pack.add_argument("output_epub_path")
    p_pack.add_argument("--force", action="store_true")
    p_pack.set_defaults(func=cmd_pack)

    return parser


def main(argv=None) -> int:
    # Force UTF-8 regardless of the console code page (e.g. cp949 on Korean
    # Windows), so callers capturing this process's output as UTF-8 don't
    # see mojibake in the Korean status/error messages.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")

    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ToolError as exc:
        print(exc.message, file=sys.stderr)
        return exc.exit_code
    except Exception as exc:  # pragma: no cover - defensive fallback
        print(f"예상하지 못한 오류: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
