"""Isolated PDF parser: bounded JSON on stdout; never emits document errors."""

from __future__ import annotations

import json
import sys
from io import BytesIO


def main() -> None:
    if sys.platform == "linux":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
        resource.setrlimit(resource.RLIMIT_CPU, (10, 11))
    try:
        import pypdf

        max_pages, max_characters = int(sys.argv[1]), int(sys.argv[2])
        data = sys.stdin.buffer.read(5 * 1024 * 1024 + 1)
        reader = pypdf.PdfReader(BytesIO(data), strict=True)
        if reader.is_encrypted:
            result = {"error": "encrypted"}
        elif len(reader.pages) > max_pages:
            result = {"error": "pages"}
        else:
            pages = []
            characters = 0
            for number, page in enumerate(reader.pages, 1):
                text = (page.extract_text() or "").strip()
                if not text:
                    continue
                characters += len(text) + (2 if pages else 0)
                if characters > max_characters:
                    result = {"error": "characters"}
                    break
                pages.append({"text": text, "page_number": number})
            else:
                result = (
                    {"pages": pages, "version": f"pypdf:{pypdf.__version__}:v1"}
                    if pages
                    else {"error": "unextractable"}
                )
    except Exception:
        result = {"error": "unreadable"}
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8"))


if __name__ == "__main__":
    main()
