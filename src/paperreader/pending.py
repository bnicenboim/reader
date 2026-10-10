"""Papers that need a person: a human check, a sign-in, or a download by hand.

An agent without a display (a cloud container, say) cannot do these. `paper get` notes them
here, and `paper pending` lists them for when the person is at their own machine.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from .library import _atomic_write
from .models import Paper


class Pending:
    def __init__(self, path: Path):
        self.path = path

    def all(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    def _write(self, entries: list[dict]) -> None:
        if not entries and not self.path.exists():
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(self.path, json.dumps(entries, indent=2, ensure_ascii=False).encode())

    @staticmethod
    def _same(entry: dict, paper: Paper) -> bool:
        return bool(set(entry.get("ids") or []) & set(paper.ids()))

    def add(self, paper: Paper, ident: str, reason: str, open_url: str | None, hints: list[str],
            key: str | None = None) -> None:
        """Note the paper, replacing an earlier entry for it; that entry's key stays unless `key` is given."""
        entries, earlier = [], []
        for e in self.all():
            (earlier if self._same(e, paper) else entries).append(e)
        key = key or next((e["key"] for e in earlier if e.get("key")), None)
        if ident.startswith("url:"):  # a link is a weaker identifier than the DOI or other id given before
            ident = next((e["id"] for e in earlier if not e["id"].startswith("url:")), ident)
        entries.append({
            "id": ident,
            "key": key,
            "title": paper.title,
            "ids": paper.ids(),
            "reason": reason,
            "open": open_url,
            "hints": hints,
            "added": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        })
        self._write(entries)

    def remove(self, paper: Paper) -> None:
        entries = self.all()
        kept = [e for e in entries if not self._same(e, paper)]
        if len(kept) != len(entries):
            self._write(kept)

    def clear(self) -> None:
        self._write([])
