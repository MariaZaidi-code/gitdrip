from __future__ import annotations

from pathlib import Path

from gitdrip.config import GitdripError

MAX_DOC_CHARS = 60_000

TEXT_EXTENSIONS = {
    ".md", ".txt", ".rst", ".csv", ".tsv", ".json", ".yaml", ".yml", ".toml",
    ".ini", ".cfg", ".py", ".js", ".ts", ".tsx", ".jsx", ".java", ".go", ".rs",
    ".c", ".h", ".cpp", ".cs", ".rb", ".php", ".sh", ".sql", ".html", ".css",
}


def extract_text(path: Path) -> str:
    if not path.is_file():
        raise GitdripError(f"document not found: {path}")
    return extract_bytes(path.name, path.read_bytes())


def extract_bytes(name: str, data: bytes) -> str:
    suffix = Path(name).suffix.lower()
    if suffix == ".pdf":
        return _extract_pdf(data, name)
    if suffix in TEXT_EXTENSIONS or not suffix:
        return _read_bytes(name, data)
    raise GitdripError(
        f"unsupported document type '{suffix}' (use pdf, md, txt, csv, json, code files...)"
    )


def _read_bytes(name: str, data: bytes) -> str:
    for encoding in ("utf-8", "utf-8-sig", "latin-1"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise GitdripError(f"cannot decode {name}")


def _extract_pdf(data: bytes, name: str) -> str:
    import io

    try:
        from pypdf import PdfReader
    except ImportError:
        raise GitdripError("pdf support needs pypdf: pip install pypdf")
    reader = PdfReader(io.BytesIO(data))
    pages = [(page.extract_text() or "") for page in reader.pages]
    return "\n\n".join(pages)


def truncate(text: str, limit: int = MAX_DOC_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n\n[... document truncated at {limit} characters ...]"
