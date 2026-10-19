from __future__ import annotations

import asyncio
import hashlib
import os
import sys
from io import BytesIO

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from app.knowledge import ingestion


def pdf_bytes(page_count=1, text=None, encrypted=False):
    writer = PdfWriter()
    for _ in range(page_count):
        page = writer.add_blank_page(width=300, height=300)
        if text is not None:
            font = DictionaryObject(
                {
                    NameObject("/Type"): NameObject("/Font"),
                    NameObject("/Subtype"): NameObject("/Type1"),
                    NameObject("/BaseFont"): NameObject("/Helvetica"),
                }
            )
            page[NameObject("/Resources")] = DictionaryObject(
                {
                    NameObject("/Font"): DictionaryObject(
                        {NameObject("/F1"): writer._add_object(font)}
                    ),
                }
            )
            stream = DecodedStreamObject()
            stream.set_data(b"BT /F1 12 Tf 20 200 Td (" + text + b") Tj ET")
            page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt("secret")
    result = BytesIO()
    writer.write(result)
    return result.getvalue()


@pytest.mark.asyncio
async def test_bounded_interface_and_original_hash():
    data = b"  hello\nworld  "
    result = await ingestion.extract_file_bounded("notes.MD", data)
    assert result.media_type == "text/markdown"
    assert result.text == "hello\nworld"
    assert result.pages == [ingestion.ExtractedPage("hello\nworld")]
    assert result.source_sha256 == hashlib.sha256(data).hexdigest()
    assert result.extraction_version


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("filename", "data", "match"),
    [
        ("x.txt", b"x" * (5 * 1024 * 1024 + 1), "5 MiB"),
        ("x.txt", b"x" * 100001, "character"),
        ("x.txt", b"\xff", "UTF-8"),
        ("x.md", b" \n ", "empty"),
        ("x.doc", b"anything", "Only PDF"),
        ("x.pdf", b"not a PDF private content", "could not be read"),
        ("x.pdf", pdf_bytes(51), "50 pages"),
        ("x.pdf", pdf_bytes(), "extractable text"),
        ("x.pdf", pdf_bytes(text=b"private", encrypted=True), "encrypted"),
    ],
    ids=[
        "bytes",
        "characters",
        "utf8",
        "empty",
        "unsupported",
        "malformed",
        "pages",
        "unextractable",
        "encrypted",
    ],
)
async def test_rejections_are_safe(filename, data, match):
    with pytest.raises(ingestion.UnsupportedSourceError, match=match) as exc:
        await ingestion.extract_file_bounded(filename, data)
    assert "private" not in str(exc.value)


@pytest.mark.asyncio
async def test_real_worker_pdf_success_and_separators():
    data = pdf_bytes(2, b"hello")
    result = await ingestion.extract_file_bounded("x.pdf", data, max_characters=12)
    assert result.text == "hello\n\nhello"
    assert [page.page_number for page in result.pages] == [1, 2]
    assert result.extraction_version.startswith("pypdf:")
    assert result.source_sha256 == hashlib.sha256(data).hexdigest()
    with pytest.raises(ingestion.UnsupportedSourceError, match="character"):
        await ingestion.extract_file_bounded("x.pdf", data, max_characters=11)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_bytes": 5 * 1024 * 1024 + 1},
        {"max_pages": 51},
        {"max_characters": 100001},
        {"timeout_seconds": 11},
        {"max_pages": 0},
        {"timeout_seconds": float("nan")},
    ],
)
async def test_cannot_raise_hard_limits(kwargs):
    with pytest.raises(ValueError):
        await ingestion.extract_file_bounded("x.txt", b"ok", **kwargs)


@pytest.fixture
def slow_worker(monkeypatch, tmp_path):
    script = tmp_path / "slow.py"
    script.write_text("import time\ntime.sleep(60)\n")
    processes = []
    actual_spawn = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        assert kwargs["env"] == {}
        assert args[0] == sys.executable
        assert "-I" in args
        process = await actual_spawn(args[0], "-I", str(script), **kwargs)
        processes.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    return processes


@pytest.mark.asyncio
async def test_timeout_kills_reaps_and_eventloop_stays_responsive(slow_worker):
    beats = 0

    async def heartbeat():
        nonlocal beats
        for _ in range(30):
            beats += 1
            await asyncio.sleep(0.01)

    with pytest.raises(ingestion.UnsupportedSourceError, match="timed out"):
        await asyncio.gather(
            ingestion.extract_file_bounded("x.pdf", b"%PDF-", timeout_seconds=0.2),
            heartbeat(),
        )
    assert beats >= 5
    assert len(slow_worker) == 1
    assert slow_worker[0].returncode is not None
    with pytest.raises(ProcessLookupError):
        os.kill(slow_worker[0].pid, 0)


@pytest.mark.asyncio
async def test_cancellation_kills_reaps_and_queue_has_a_deadline(slow_worker):
    task = asyncio.create_task(ingestion.extract_file_bounded("x.pdf", b"%PDF-"))
    for _ in range(100):
        if slow_worker:
            break
        await asyncio.sleep(0.01)
    assert len(slow_worker) == 1
    with pytest.raises(ingestion.UnsupportedSourceError, match="timed out"):
        await ingestion.extract_file_bounded("x.pdf", b"%PDF-", timeout_seconds=0.05)
    assert len(slow_worker) == 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert slow_worker[0].returncode is not None
    with pytest.raises(ProcessLookupError):
        os.kill(slow_worker[0].pid, 0)


@pytest.mark.asyncio
async def test_worker_output_is_bounded(monkeypatch, tmp_path):
    script = tmp_path / "noisy.py"
    script.write_text('import sys\nsys.stdout.buffer.write(b"x" * 2000000)\n')
    actual_spawn = asyncio.create_subprocess_exec

    async def spawn(*args, **kwargs):
        return await actual_spawn(sys.executable, "-I", str(script), **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    with pytest.raises(ingestion.UnsupportedSourceError, match="could not be read"):
        await ingestion.extract_file_bounded("x.pdf", b"%PDF-")


@pytest.mark.asyncio
async def test_limit_and_timeout_have_distinct_error_types(slow_worker):
    with pytest.raises(ingestion.ExtractionLimitError):
        await ingestion.extract_file_bounded("x.txt", b"ab", max_characters=1)
    with pytest.raises(ingestion.ExtractionTimeoutError):
        await ingestion.extract_file_bounded("x.pdf", b"%PDF-", timeout_seconds=0.05)


def test_chunk_count_is_capped_before_allocation():
    with pytest.raises(ingestion.ExtractionLimitError, match="chunk"):
        ingestion.chunk_pages(
            [ingestion.ExtractedPage("abcdef")],
            chunk_characters=2,
            overlap=1,
            max_chunks=2,
        )
    assert (
        len(
            ingestion.chunk_pages(
                [ingestion.ExtractedPage("abc")],
                chunk_characters=2,
                overlap=1,
                max_chunks=2,
            )
        )
        == 2
    )


def test_worker_json_fits_output_cap_for_valid_astral_text(monkeypatch):
    from io import TextIOWrapper
    from types import SimpleNamespace

    import pypdf

    from app.knowledge import extraction_worker

    output = BytesIO()
    stdout = TextIOWrapper(output, encoding="utf-8")
    monkeypatch.setattr(
        extraction_worker,
        "sys",
        SimpleNamespace(
            platform="test",
            argv=["worker", "50", "100000"],
            stdin=SimpleNamespace(buffer=BytesIO(b"fixture")),
            stdout=stdout,
        ),
    )
    monkeypatch.setattr(
        pypdf,
        "PdfReader",
        lambda *args, **kwargs: SimpleNamespace(
            is_encrypted=False, pages=[SimpleNamespace(extract_text=lambda: "😀" * 100000)]
        ),
    )
    extraction_worker.main()
    stdout.flush()
    assert len(output.getvalue()) <= ingestion._MAX_WORKER_OUTPUT
