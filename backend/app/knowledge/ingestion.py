from __future__ import annotations

import asyncio
import hashlib
import json
import math
import sys
import threading
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path


class UnsupportedSourceError(ValueError):
    pass


class ExtractionLimitError(UnsupportedSourceError):
    pass


class ExtractionTimeoutError(UnsupportedSourceError):
    pass


@dataclass(frozen=True)
class ExtractedPage:
    text: str
    page_number: int | None = None


@dataclass(frozen=True)
class ChunkDraft:
    content: str
    page_number: int | None


@dataclass(frozen=True)
class ExtractedSource:
    media_type: str
    pages: list[ExtractedPage]
    source_sha256: str
    extraction_version: str

    @property
    def text(self) -> str:
        return "\n\n".join(page.text for page in self.pages)


_PDF_SLOT = threading.Lock()
_MAX_WORKER_OUTPUT = 1024 * 1024


async def _extract_pdf(data: bytes, max_pages: int, max_characters: int) -> dict:
    # A process-wide lock also works when separate requests use separate event loops.
    while not _PDF_SLOT.acquire(blocking=False):  # noqa: ASYNC110 - lock spans event loops
        await asyncio.sleep(0.01)
    process = None
    spawn = None
    tasks = []
    try:
        spawn = asyncio.create_task(
            asyncio.create_subprocess_exec(
                sys.executable,
                "-I",
                str(Path(__file__).with_name("extraction_worker.py")),
                str(max_pages),
                str(max_characters),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env={},
                limit=65536,
            )
        )
        process = await asyncio.shield(spawn)

        async def write_input() -> None:
            try:
                process.stdin.write(data)
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                process.stdin.close()

        async def read_output() -> bytes:
            output = bytearray()
            while part := await process.stdout.read(65536):
                output.extend(part)
                if len(output) > _MAX_WORKER_OUTPUT:
                    raise UnsupportedSourceError("The PDF could not be read")
            return bytes(output)

        tasks = [asyncio.create_task(write_input()), asyncio.create_task(read_output())]
        _, output = await asyncio.gather(*tasks)
        if await process.wait() != 0:
            raise UnsupportedSourceError("The PDF could not be read")
        return json.loads(output)
    finally:
        try:
            # Shield launch so cancellation cannot lose a child between spawn and assignment.
            if process is None and spawn is not None:
                process = await spawn
            if process is not None:
                if process.returncode is None:
                    with suppress(ProcessLookupError):
                        process.kill()
                for task in tasks:
                    task.cancel()
                if tasks:
                    await asyncio.gather(*tasks, return_exceptions=True)
                # Drain after killing: a full pipe must not prevent transport cleanup/reaping.
                while await process.stdout.read(65536):
                    pass
                await process.wait()
        finally:
            _PDF_SLOT.release()


async def extract_file_bounded(
    filename: str,
    data: bytes,
    *,
    max_bytes: int = 5 * 1024 * 1024,
    max_pages: int = 50,
    max_characters: int = 100000,
    timeout_seconds: float = 10,
) -> ExtractedSource:
    for limit, ceiling in ((max_bytes, 5 * 1024 * 1024), (max_pages, 50), (max_characters, 100000)):
        if type(limit) is not int or not 0 < limit <= ceiling:
            raise ValueError("Extraction limits must be positive and within the hard ceilings")
    if not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 10:
        raise ValueError("Extraction timeout must be positive and at most 10 seconds")
    if len(data) > max_bytes:
        raise ExtractionLimitError("Source exceeds the byte limit (maximum 5 MiB)")
    if not data:
        raise UnsupportedSourceError("The source is empty")
    suffix = Path(filename).suffix.lower()
    if suffix in {".txt", ".md"}:
        try:
            text = data.decode("utf-8").strip()
        except UnicodeDecodeError:
            raise UnsupportedSourceError("Text files must contain valid UTF-8") from None
        if not text:
            raise UnsupportedSourceError("The source is empty")
        if len(text) > max_characters:
            raise ExtractionLimitError("Source exceeds the character limit")
        media_type = "text/markdown" if suffix == ".md" else "text/plain"
        pages = [ExtractedPage(text)]
        version = "utf8:v1"
    elif suffix == ".pdf":
        try:
            async with asyncio.timeout(timeout_seconds):
                result = await _extract_pdf(data, max_pages, max_characters)
            errors = {
                "encrypted": "The PDF is encrypted",
                "pages": "Source exceeds the page limit (maximum 50 pages)",
                "characters": "Source exceeds the character limit",
                "unextractable": "The PDF has no extractable text; OCR is not supported",
                "unreadable": "The PDF could not be read",
            }
            if "error" in result:
                error_type = (
                    ExtractionLimitError
                    if result["error"] in {"pages", "characters"}
                    else UnsupportedSourceError
                )
                raise error_type(errors.get(result["error"], errors["unreadable"]))
            pages = [ExtractedPage(**page) for page in result["pages"]]
            version = result["version"]
            media_type = "application/pdf"
        except TimeoutError:
            raise ExtractionTimeoutError("PDF extraction timed out") from None
        except UnsupportedSourceError:
            raise
        except Exception:
            raise UnsupportedSourceError("The PDF could not be read") from None
    else:
        raise UnsupportedSourceError("Only PDF, TXT, and Markdown files are supported")
    return ExtractedSource(media_type, pages, hashlib.sha256(data).hexdigest(), version)


def chunk_pages(
    pages: list[ExtractedPage],
    *,
    chunk_characters: int,
    overlap: int,
    max_chunks: int | None = None,
) -> list[ChunkDraft]:
    if max_chunks is not None and (type(max_chunks) is not int or max_chunks < 1):
        raise ValueError("Maximum chunk count must be positive")
    if overlap >= chunk_characters:
        raise ValueError("Chunk overlap must be smaller than chunk size")
    chunks: list[ChunkDraft] = []
    step = chunk_characters - overlap
    for page in pages:
        normalized = " ".join(page.text.split())
        for start in range(0, len(normalized), step):
            content = normalized[start : start + chunk_characters].strip()
            if content:
                if max_chunks is not None and len(chunks) >= max_chunks:
                    raise ExtractionLimitError("Source exceeds the chunk limit")
                chunks.append(ChunkDraft(content=content, page_number=page.page_number))
            if start + chunk_characters >= len(normalized):
                break
    return chunks
