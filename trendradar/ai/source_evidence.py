"""Deterministic weekly module assignment from source and publication evidence."""

from dataclasses import dataclass
import re
from typing import Mapping


CURRENT_EVENTS = "current_events"
RESEARCH = "research"


@dataclass(frozen=True)
class ModuleEvidence:
    module_type: str
    reason: str


_DISTINCT_JOURNAL_NAMES = re.compile(
    r"(?:"
    r"Rice Science|The Crop Journal|Molecular Plant|Plant Communications|"
    r"Nature Plants|Nature Genetics|Nature Biotechnology|"
    r"Science Advances|PNAS|Cell|bioRxiv"
    r")",
    re.IGNORECASE,
)
_GENERIC_JOURNAL_PUBLICATION = re.compile(
    r"(?:发表于|刊发于|刊载于|published\s+in|appeared\s+in)\s*"
    r"(?:the\s+)?(?:journal\s+)?(?:Nature|Science)\b",
    re.IGNORECASE,
)
_CHINESE_PAPER_TITLE = re.compile(
    r"(?:"
    r"(?:论文|文章|研究)\s*(?:题为|名为|《)|"
    r"(?:发表|发布|刊发)\s*(?:了\s*)?(?:题为|名为)"
    r")\s*"
    r"(?:《[^》]{8,200}》|[“\"][^”\"]{8,200}[”\"])",
)
_ENGLISH_PAPER_TITLE = re.compile(
    r"\b(?:paper|article|study)\s+(?:entitled|titled)\s+"
    r"[\"“][^\"”]{12,300}[\"”]",
    re.IGNORECASE,
)


def classify_source_evidence(
    item: Mapping[str, object],
    source_categories: Mapping[str, str],
) -> ModuleEvidence:
    """Return a module without allowing an AI response to choose it."""
    source_id = str(
        item.get("source_id") or item.get("feed_id") or ""
    ).strip()
    if source_categories.get(source_id) == "scholarly":
        return ModuleEvidence(RESEARCH, "scholarly_source")

    content = " ".join(
        str(item.get(field) or "")
        for field in ("content", "summary")
    ).strip()
    if _DISTINCT_JOURNAL_NAMES.search(content) or (
        _GENERIC_JOURNAL_PUBLICATION.search(content)
    ):
        return ModuleEvidence(RESEARCH, "journal_name")
    if _CHINESE_PAPER_TITLE.search(content) or _ENGLISH_PAPER_TITLE.search(
        content
    ):
        return ModuleEvidence(RESEARCH, "paper_title")
    return ModuleEvidence(CURRENT_EVENTS, "no_publication_evidence")
