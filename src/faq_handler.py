"""FAQ engine: reads the clinic's Markdown file and hands its content to the LLM.

How it works
------------
1. The `.md` file is split into sections (one per `#` / `##` heading).
2. For small files (the normal case) the WHOLE file is returned and placed in
   the system prompt, so the LLM can answer any FAQ question from it.
3. For large files we rank sections by keyword overlap with the user's
   question and only return the best matches, which keeps the prompt small.
4. The file is re-read automatically when it changes on disk, so you can edit
   `clinic_info.md` while the bot is running.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

DEFAULT_FAQ_PATH = Path(__file__).resolve().parent.parent / "data" / "clinic_info.md"

# Words that carry no meaning for keyword matching.
_STOPWORDS = frozenset(
    "a an and are as at be but by can do does for from has have how i in is it "
    "me my of on or please the there to u we what when where which who will with "
    "you your tell about".split()
)


@dataclass(frozen=True)
class Section:
    """One heading + its body text from the Markdown file."""

    title: str
    body: str

    def as_text(self) -> str:
        return f"## {self.title}\n{self.body}".strip()


def _tokenize(text: str) -> set[str]:
    """Lowercase words without stopwords, e.g. 'What are your hours?' -> {'hours'}."""
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in _STOPWORDS and len(w) > 1}


class FAQHandler:
    """Loads a Markdown knowledge base and returns context for the LLM."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else DEFAULT_FAQ_PATH
        self._mtime: float = 0.0
        self._full_text: str = ""
        self._sections: list[Section] = []
        self._reload_if_changed()

    # ------------------------------------------------------------------ loading
    def _reload_if_changed(self) -> None:
        """Re-read the file only when it was modified (cheap `stat` check)."""
        if not self.path.exists():
            raise FileNotFoundError(f"FAQ file not found: {self.path}")
        mtime = self.path.stat().st_mtime
        if mtime == self._mtime:
            return
        self._full_text = self.path.read_text(encoding="utf-8").strip()
        self._sections = self._split_sections(self._full_text)
        self._mtime = mtime

    @staticmethod
    def _split_sections(text: str) -> list[Section]:
        """Split Markdown into sections at every heading line."""
        sections: list[Section] = []
        title, lines = "Overview", []
        for line in text.splitlines():
            heading = re.match(r"^#{1,3}\s+(.*)$", line)
            if heading:
                if "".join(lines).strip():
                    sections.append(Section(title, "\n".join(lines).strip()))
                title, lines = heading.group(1).strip(), []
            else:
                lines.append(line)
        if "".join(lines).strip():
            sections.append(Section(title, "\n".join(lines).strip()))
        return sections

    # ------------------------------------------------------------------ public API
    @property
    def sections(self) -> list[Section]:
        self._reload_if_changed()
        return list(self._sections)

    def get_context(self, query: str | None = None, max_chars: int = 8000, top_k: int = 4) -> str:
        """Return the text the LLM should use as its knowledge base.

        - Small file (or no query)  -> the full file.
        - Big file + a query        -> the `top_k` most relevant sections.
        """
        self._reload_if_changed()
        if not query or len(self._full_text) <= max_chars:
            return self._full_text

        query_words = _tokenize(query)
        scored = [
            (len(query_words & _tokenize(f"{s.title} {s.body}")) + 2 * len(query_words & _tokenize(s.title)), s)
            for s in self._sections
        ]
        scored.sort(key=lambda pair: pair[0], reverse=True)
        best = [s for score, s in scored[:top_k] if score > 0] or [s for _, s in scored[:top_k]]
        return "\n\n".join(s.as_text() for s in best)
