"""Bounded document reads inside the existing read_file/model loop."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import stat
import tempfile
from pathlib import Path, PurePosixPath
from zipfile import ZipFile

from analyst_runtime.agent.tools.pdf import read_pdf_page

OFFICE_SUFFIXES = {
    ".doc",
    ".docx",
    ".xls",
    ".xlsx",
    ".ppt",
    ".pptx",
    ".odt",
    ".ods",
    ".odp",
    ".rtf",
}
DOCUMENT_SUFFIXES = OFFICE_SUFFIXES | {".zip", ".html", ".htm", ".png", ".jpg", ".jpeg"}
TEXT_SUFFIXES = {".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".log"}
MAX_BYTES = 100 * 1024 * 1024


def archive_entries(content: bytes):
    archive = ZipFile(io.BytesIO(content))
    entries = archive.infolist()
    if len(entries) > 200 or sum(e.file_size for e in entries) > 400 * 1024 * 1024:
        archive.close()
        raise ValueError("Archive exceeds 200 entries or 400 MB expanded size")
    seen = set()
    for entry in entries:
        name = entry.filename
        path = PurePosixPath(name)
        mode = entry.external_attr >> 16
        if (
            path.is_absolute()
            or ".." in path.parts
            or "\\" in name
            or ":" in name
            or any(ord(c) < 32 for c in name)
            or len(name) > 500
            or name in seen
            or stat.S_ISLNK(mode)
        ):
            archive.close()
            raise ValueError("Unsafe or duplicate archive path")
        seen.add(name)
        if (
            entry.flag_bits & 1
            or entry.file_size > MAX_BYTES
            or entry.file_size > max(1, entry.compress_size) * 100
        ):
            archive.close()
            raise ValueError("Encrypted, oversized or excessive-compression archive entry")
    return archive, entries


async def office_pdf(content: bytes, suffix: str) -> bytes:
    from analyst_runtime.agent.tools.document_isolation import isolate_office

    # A private profile disables macros and external document updates; no credentials inherited.
    with tempfile.TemporaryDirectory(prefix="analyst-office-") as directory:
        root = Path(directory)
        profile = root / "profile"
        (profile / "user").mkdir(parents=True)
        (profile / "user/registrymodifications.xcu").write_text(
            """<?xml version="1.0"?><oor:items xmlns:oor="http://openoffice.org/2001/registry"><item oor:path="/org.openoffice.Office.Common/Security/Scripting"><prop oor:name="MacroSecurityLevel" oor:op="fuse"><value>3</value></prop></item><item oor:path="/org.openoffice.Office.Writer/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>0</value></prop></item><item oor:path="/org.openoffice.Office.Calc/Content/Update"><prop oor:name="Link" oor:op="fuse"><value>0</value></prop></item></oor:items>"""
        )
        env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(root),
            "LANG": "C.UTF-8",
            "TMPDIR": str(root),
            "SAL_USE_VCLPLUGIN": "svp",
        }
        # Initialize only our empty profile before exposing untrusted input. A first-run
        # LibreOffice restart needs to replace its IPC socket outside the file sandbox.
        initialize = await asyncio.create_subprocess_exec(
            "soffice",
            "-env:UserInstallation=" + profile.as_uri(),
            "--headless",
            "--terminate_after_init",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            start_new_session=True,
        )
        try:
            await asyncio.wait_for(initialize.wait(), 45)
        except BaseException:
            if initialize.returncode is None:
                import signal

                os.killpg(initialize.pid, signal.SIGKILL)
            await initialize.wait()
            raise
        if initialize.returncode:
            raise ValueError("Cannot initialize the document parser")
        source = root / ("source" + suffix)
        source.write_bytes(content)
        process = await asyncio.create_subprocess_exec(
            "soffice",
            "-env:UserInstallation=" + profile.as_uri(),
            "--headless",
            "--nologo",
            "--nodefault",
            "--norestore",
            "--convert-to",
            "pdf",
            "--outdir",
            str(root),
            str(source),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            start_new_session=True,
            preexec_fn=(lambda: isolate_office(directory)) if os.sys.platform == "linux" else None,
        )
        try:
            await asyncio.wait_for(process.wait(), 45)
        except BaseException:
            if process.returncode is None:
                import signal

                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()
            raise
        # Remove only this random profile's exact IPC socket (the child cannot unlink /tmp).
        if os.sys.platform == "linux":
            digest = hashlib.md5(
                profile.as_uri().encode("utf-16-le"), usedforsecurity=False
            ).digest()
            pipe = Path("/tmp") / (
                f"OSL_PIPE_{os.getuid()}_SingleOfficeIPC_" + "".join(format(b, "x") for b in digest)
            )
            try:
                if pipe.is_socket():
                    pipe.unlink()
            except FileNotFoundError:
                pass
        pdf = root / "source.pdf"
        if process.returncode or not pdf.is_file() or pdf.stat().st_size > MAX_BYTES:
            raise ValueError(
                "Document cannot be converted; it may be encrypted, damaged or too large"
            )
        return pdf.read_bytes()


def read_text(content: bytes, *, page: int, source: dict):
    text = content.decode("utf-8-sig")
    if "\0" in text:
        raise ValueError(
            "Binary format has no built-in reader; use a suitable workspace parser or ask for an exported document"
        )
    count = max(1, (len(text) + 39999) // 40000)
    if isinstance(page, bool) or not isinstance(page, int) or page < 1 or page > count:
        raise ValueError("Text page out of range")
    return json.dumps(
        {
            **source,
            "format": "text",
            "page": page,
            "page_count": count,
            "content": text[(page - 1) * 40000 : page * 40000],
            "notice": "Only this text segment was read; use page=N for remaining segments.",
        },
        ensure_ascii=False,
    )


async def read_document(
    content: bytes, *, suffix: str, page: int, member: str | None, vision: bool, source: dict, region: list[float] | None = None
):
    if not content or len(content) > MAX_BYTES:
        raise ValueError("Document exceeds 100 MB or is empty")
    if suffix == ".zip":
        with_archive, entries = archive_entries(content)
        with with_archive as archive:
            if member is None:
                if region is not None:
                    raise ValueError("Select an archive document member before requesting a visual region")
                return json.dumps(
                    {
                        **source,
                        "format": "zip",
                        "entries": [
                            {
                                "name": e.filename,
                                "bytes": e.file_size,
                                "readable": Path(e.filename).suffix.lower()
                                in DOCUMENT_SUFFIXES
                                | TEXT_SUFFIXES
                                | {".pdf", ".png", ".jpg", ".jpeg"},
                            }
                            for e in entries
                            if not e.is_dir()
                        ],
                        "notice": "Only the file list was read. Use read_file(path, member=exact_name, page=N). Nested ZIPs are not supported.",
                    },
                    ensure_ascii=False,
                )
            entry = next((e for e in entries if e.filename == member and not e.is_dir()), None)
            if entry is None:
                raise ValueError("Archive member not found")
            data = archive.read(entry)
        child_suffix = Path(member).suffix.lower()
        if child_suffix == ".zip":
            raise ValueError("Nested ZIPs are not supported; upload the inner archive separately")
        child = {
            **source,
            "archive_member": member,
            "member_sha256": hashlib.sha256(data).hexdigest(),
        }
        if child_suffix in DOCUMENT_SUFFIXES | {".pdf"}:
            return await read_document(
                data, suffix=child_suffix, page=page, member=None, vision=vision, source=child, region=region
            )
        if child_suffix not in {".png", ".jpg", ".jpeg"}:
            if region is not None:
                raise ValueError("Visual regions require PDF or converted document pages")
            return read_text(data, page=page, source=child)
        # Images use the same page-image pipeline as PDFs; no separate vision tool/loop.
        if child_suffix in {".png", ".jpg", ".jpeg"}:
            return await read_pdf_page(
                await office_pdf(data, child_suffix), page=page, vision=vision, source=child, region=region
            )
        raise ValueError("This archive member format is not supported")
    if member is not None:
        raise ValueError("member is only valid for ZIP files")
    if suffix == ".pdf":
        return await read_pdf_page(content, page=page, vision=vision, source=source, region=region)
    if suffix in OFFICE_SUFFIXES | {".html", ".htm", ".png", ".jpg", ".jpeg"}:
        if suffix in {".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"}:
            # Validate compressed input before handing it to the converter.
            archive, _ = archive_entries(content)
            archive.close()
        if suffix in {".html", ".htm"}:
            if region is not None:
                raise ValueError("Visual regions are unavailable for inert HTML text")
            # Read HTML as inert text; never run scripts, fetch URLs or load local resources.
            from html.parser import HTMLParser

            class Text(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.parts = []
                    self.hidden = 0

                def handle_starttag(self, tag, attrs):
                    if tag in {"script", "style", "template"}:
                        self.hidden += 1
                    if not self.hidden and tag in {
                        "p",
                        "div",
                        "br",
                        "tr",
                        "td",
                        "th",
                        "li",
                        "h1",
                        "h2",
                        "h3",
                    }:
                        self.parts.append("\n" if tag != "td" else "\t")

                def handle_endtag(self, tag):
                    if tag in {"script", "style", "template"}:
                        self.hidden = max(0, self.hidden - 1)

                def handle_data(self, data):
                    if not self.hidden:
                        self.parts.append(data)

            parser = Text()
            parser.feed(content.decode("utf-8-sig"))
            text = "".join(parser.parts).strip()
            chunks = [text[n : n + 40000] for n in range(0, len(text), 40000)] or [""]
            if isinstance(page, bool) or page < 1 or page > len(chunks):
                raise ValueError("HTML text page out of range")
            return json.dumps(
                {
                    **source,
                    "format": "html",
                    "page": page,
                    "page_count": len(chunks),
                    "content": chunks[page - 1],
                    "notice": "Inert HTML text/tables only. Scripts, images and external resources are not loaded. Read remaining text pages with page=N.",
                },
                ensure_ascii=False,
            )
        return await read_pdf_page(
            await office_pdf(content, suffix),
            page=page,
            vision=vision,
            source={**source, "original_format": suffix, "pagination": "converted_document_pages"},
            region=region,
        )
    raise ValueError("Unsupported document format")
