"""The local paper collection: PDF (or XML), extracted text and a metadata sidecar per paper."""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .models import Paper

SIDECAR_VERSION = 1

TRANSLIT = str.maketrans(
    {"ł": "l", "Ł": "L", "ø": "o", "Ø": "O", "ß": "ss", "æ": "ae", "Æ": "AE", "đ": "d", "Đ": "D",
     "ı": "i", "œ": "oe", "Œ": "OE", "þ": "th", "ð": "d"}
)
STOPWORDS = {"a", "an", "the", "of", "on", "in", "for", "and", "to", "with", "from", "by", "at", "is",
             "are", "via", "towards", "toward", "do", "does", "what", "how", "we", "can", "using"}


def slug(text: str) -> str:
    text = unicodedata.normalize("NFKD", text.translate(TRANSLIT)).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def surname(name: str) -> str:
    name = name.strip()
    if "," in name:
        return name.split(",")[0]
    return name.split()[-1] if name.split() else ""


def base_key(paper: Paper) -> str:
    """Human-friendly file name stem, e.g. 'vaswani2017-attention-all-you-need'."""
    words = [w for w in slug(paper.title or "").split("-") if w and w not in STOPWORDS][:4]
    who = slug(surname(paper.authors[0])) if paper.authors else ""
    if words and (who or paper.year):
        return f"{who}{paper.year or ''}-{'-'.join(words)}"
    for kind_value in paper.ids():
        return slug(kind_value)[:80]
    return "paper"


@dataclass
class Record:
    key: str
    paper: Paper
    source: dict
    files: dict[str, Path]  # "pdf" / "xml" / "text" -> path

    @property
    def text_path(self) -> Path:
        return self.files["text"]

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            **self.paper.to_dict(),
            "source": self.source,
            "files": {k: str(v) for k, v in self.files.items()},
        }


def _atomic_write(path: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except BaseException:
        os.unlink(tmp)
        raise


def header(paper: Paper, source: dict) -> str:
    lines = [f"# {paper.title or 'Untitled'}", ""]
    if paper.authors:
        lines.append("Authors: " + ", ".join(paper.authors))
    facts = [f"Year: {paper.year}" if paper.year else "", f"Venue: {paper.venue}" if paper.venue else ""]
    if any(facts):
        lines.append(" · ".join(f for f in facts if f))
    ids = [f"{k}: {getattr(paper, k)}" for k in ("doi", "arxiv", "pmcid", "acl") if getattr(paper, k)]
    if ids:
        lines.append(" · ".join(ids))
    version = f", {source['version']}" if source.get("version") else ""
    lines.append(f"Source: {source.get('url')} (via {source.get('source')}{version})")
    return "\n".join(lines) + "\n\n---\n\n"


class Library:
    def __init__(self, root: Path):
        self.root = root

    def _sidecars(self):
        if not self.root.exists():
            return
        for path in sorted(self.root.glob("*.json")):
            try:
                data = json.loads(path.read_text())
            except (OSError, ValueError):
                continue
            if isinstance(data, dict) and data.get("paperreader") == SIDECAR_VERSION:
                yield path, data

    def _record(self, path: Path, data: dict) -> Record:
        files = {k: self.root / v for k, v in data.get("files", {}).items()}
        return Record(path.stem, Paper.from_dict(data["paper"]), data.get("source", {}), files)

    def all(self) -> list[Record]:
        return [self._record(p, d) for p, d in self._sidecars()]

    def _lookup(self, paper: Paper) -> Record | None:
        wanted = set(paper.ids())
        if not wanted:
            return None
        for path, data in self._sidecars():
            if wanted & set(Paper.from_dict(data["paper"]).ids()):
                return self._record(path, data)
        return None

    def find(self, paper: Paper) -> Record | None:
        """The stored copy of this paper, matched on any shared identifier."""
        rec = self._lookup(paper)
        if rec and all(p.exists() for p in rec.files.values()):
            return rec
        return None

    def _free_key(self, paper: Paper) -> str:
        base = base_key(paper)
        key, n = base, 1
        while (self.root / f"{key}.json").exists():
            n += 1
            key = f"{base}-{n}"
        return key

    def save(self, paper: Paper, content: bytes, fmt: str, text: str, source: dict) -> Record:
        """Store the document (fmt "pdf" or "xml") and its extracted text."""
        self.root.mkdir(parents=True, exist_ok=True)
        existing = self._lookup(paper)
        key = existing.key if existing else self._free_key(paper)
        source = {**source, "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        files = {fmt: f"{key}.{fmt}", "text": f"{key}.md"}
        _atomic_write(self.root / files[fmt], content)
        _atomic_write(self.root / files["text"], (header(paper, source) + text + "\n").encode())
        sidecar = {"paperreader": SIDECAR_VERSION, "paper": paper.to_dict(), "source": source, "files": files}
        _atomic_write(self.root / f"{key}.json", json.dumps(sidecar, indent=2, ensure_ascii=False).encode())
        return self._record(self.root / f"{key}.json", sidecar)
