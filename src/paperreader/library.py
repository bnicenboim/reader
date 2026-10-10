"""The local paper collection: PDF (or XML), extracted text and a metadata sidecar per paper."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from .figures import FIGDIR
from .models import Paper

SIDECAR_VERSION = 1
KEY_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]*")

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

    def by_key(self, key: str) -> Record | None:
        path = self.root / f"{key}.json"
        if not KEY_RE.fullmatch(key) or not path.exists():
            return None
        return next((self._record(p, d) for p, d in self._sidecars() if p == path), None)

    def _check_key(self, key: str, paper: Paper) -> None:
        if not KEY_RE.fullmatch(key):
            raise ValueError(f"{key!r} is not a usable key (letters, digits, and . _ + - after the first)")
        taken = self.by_key(key)
        if taken and not set(taken.paper.ids()) & set(paper.ids()):
            raise ValueError(f"the key {key!r} is taken by another paper: {taken.paper.title}")

    def rename(self, rec: Record, key: str) -> Record:
        """Give a stored paper another key, e.g. its key in a bibliography."""
        if key == rec.key:
            return rec
        self._check_key(key, rec.paper)
        files = {}
        for kind, path in rec.files.items():
            files[kind] = f"{key}{path.suffix}"
            os.replace(path, self.root / files[kind])
        if "figures" in files:  # the text links to the images by the folder's name
            text_path = self.root / files["text"]
            text = text_path.read_text().replace(f"]({rec.key}.figures/", f"]({key}.figures/")
            _atomic_write(text_path, text.encode())
        sidecar = {"paperreader": SIDECAR_VERSION, "paper": rec.paper.to_dict(), "source": rec.source, "files": files}
        _atomic_write(self.root / f"{key}.json", json.dumps(sidecar, indent=2, ensure_ascii=False).encode())
        (self.root / f"{rec.key}.json").unlink()
        return self._record(self.root / f"{key}.json", sidecar)

    def update_paper(self, rec: Record, paper: Paper) -> Record:
        """Store richer metadata for a paper already in the library."""
        sidecar = {"paperreader": SIDECAR_VERSION, "paper": paper.to_dict(), "source": rec.source,
                   "files": {k: v.name for k, v in rec.files.items()}}
        _atomic_write(self.root / f"{rec.key}.json", json.dumps(sidecar, indent=2, ensure_ascii=False).encode())
        return self._record(self.root / f"{rec.key}.json", sidecar)

    def by_source_url(self, url: str) -> list[Record]:
        """The stored papers that were downloaded from `url`."""
        return [self._record(p, d) for p, d in self._sidecars() if d.get("source", {}).get("url") == url]

    def set_aside(self, rec: Record, folder: str = "mismatched") -> Path:
        """Move a paper's files out of the library, into a subfolder that lookups do not see."""
        dest = self.root / folder
        dest.mkdir(exist_ok=True)
        for path in [*rec.files.values(), self.root / f"{rec.key}.json"]:
            if path.exists():
                os.replace(path, dest / path.name)
        return dest

    def _free_key(self, paper: Paper) -> str:
        base = base_key(paper)
        key, n = base, 1
        while (self.root / f"{key}.json").exists():
            n += 1
            key = f"{base}-{n}"
        return key

    def key_for(self, paper: Paper, key: str | None = None) -> str:
        """The key `save` will store this paper under."""
        if key:
            self._check_key(key, paper)
            return key
        existing = self._lookup(paper)
        return existing.key if existing else self._free_key(paper)

    def save(self, paper: Paper, content: bytes, fmt: str, text: str, source: dict, key: str | None = None,
             figures: dict[str, bytes] | None = None, attachments: dict[str, bytes] | None = None) -> Record:
        """Store the document (fmt "pdf", "xml" or "html") and its extracted text, under `key` if given.

        `figures` are image files for the paper's figures folder; the text refers to them through FIGDIR.
        `attachments` are other formats of the document, e.g. the PDF next to arXiv's HTML.
        """
        self.root.mkdir(parents=True, exist_ok=True)
        existing = self._lookup(paper)
        key = self.key_for(paper, key)
        if existing and existing.key != key:
            for path in [*existing.files.values(), self.root / f"{existing.key}.json"]:
                shutil.rmtree(path) if path.is_dir() else path.unlink(missing_ok=True)
        source = {**source, "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        files = {fmt: f"{key}.{fmt}", "text": f"{key}.md"}
        for kind, data in (attachments or {}).items():
            files[kind] = f"{key}.{kind}"
            _atomic_write(self.root / files[kind], data)
        figdir = self.root / f"{key}.figures"
        if figdir.exists():
            shutil.rmtree(figdir)
        if figures:
            figdir.mkdir()
            for name, data in figures.items():
                (figdir / name).write_bytes(data)
            files["figures"] = figdir.name
        text = text.replace(FIGDIR, figdir.name)
        _atomic_write(self.root / files[fmt], content)
        _atomic_write(self.root / files["text"], (header(paper, source) + text + "\n").encode())
        sidecar = {"paperreader": SIDECAR_VERSION, "paper": paper.to_dict(), "source": source, "files": files}
        _atomic_write(self.root / f"{key}.json", json.dumps(sidecar, indent=2, ensure_ascii=False).encode())
        return self._record(self.root / f"{key}.json", sidecar)
