"""知识库文档：读取带 front matter 的 Markdown，按标题分节，再按字符数分块。

分块保证 chunk.text == document.body[chunk.start:chunk.end]，便于引用时回到原文位置。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

TYPE_PREFIX = {"规范": "STD", "方法": "MTH", "复盘": "CASE"}
DOC_ID_PATTERN = re.compile(r"^(STD|MTH|CASE)-\d{3}$")
HEADING_PATTERN = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
SENTENCE_END = "。！？；"
DEFAULT_MAX_CHARS = 400


class DocumentError(ValueError):
    pass


@dataclass(frozen=True)
class Document:
    doc_id: str
    title: str
    doc_type: str
    path: str  # 相对文档根目录的 POSIX 路径
    body: str  # front matter 之后的正文
    sha256: str


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    doc_id: str
    seq: int
    section: str  # 标题路径，如「舆情上报流程 > 上报时限」
    text: str
    start: int
    end: int

    @property
    def embedding_input(self) -> str:
        return f"{self.section}\n{self.text}"


def parse_document(text: str, path: str) -> Document:
    text = text.replace("\r\n", "\n")
    if not text.startswith("---\n"):
        raise DocumentError(f"{path}：缺少 front matter（文件应以 --- 开头）")
    end = text.find("\n---\n", 4)
    if end < 0:
        raise DocumentError(f"{path}：front matter 没有结束标记 ---")
    meta: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if not line.strip():
            continue
        if ":" not in line:
            raise DocumentError(f"{path}：front matter 行格式应为 key: value，实际为 {line!r}")
        key, value = line.split(":", 1)
        meta[key.strip()] = value.strip()
    for key in ("doc_id", "title", "type"):
        if not meta.get(key):
            raise DocumentError(f"{path}：front matter 缺少 {key}")
    doc_id, doc_type = meta["doc_id"], meta["type"]
    if not DOC_ID_PATTERN.match(doc_id):
        raise DocumentError(f"{path}：doc_id {doc_id!r} 格式应为 STD-001 / MTH-001 / CASE-001")
    if doc_type not in TYPE_PREFIX:
        raise DocumentError(f"{path}：type 应为 {'、'.join(TYPE_PREFIX)} 之一，实际为 {doc_type!r}")
    if not doc_id.startswith(TYPE_PREFIX[doc_type] + "-"):
        raise DocumentError(f"{path}：type 为 {doc_type} 的文档编号应以 {TYPE_PREFIX[doc_type]}- 开头")
    body = text[end + len("\n---\n"):].lstrip("\n")
    return Document(
        doc_id=doc_id,
        title=meta["title"],
        doc_type=doc_type,
        path=path,
        body=body,
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
    )


def load_documents(docs_dir: Path) -> list[Document]:
    files = sorted(docs_dir.rglob("*.md"), key=lambda p: p.relative_to(docs_dir).as_posix())
    if not files:
        raise DocumentError(f"{docs_dir} 下没有 .md 文档")
    docs, seen = [], {}
    for file in files:
        rel = file.relative_to(docs_dir).as_posix()
        doc = parse_document(file.read_text(encoding="utf-8"), rel)
        if file.stem != doc.doc_id:
            raise DocumentError(f"{rel}：文件名应与 doc_id 一致（{doc.doc_id}.md）")
        if doc.doc_id in seen:
            raise DocumentError(f"{rel}：doc_id {doc.doc_id} 与 {seen[doc.doc_id]} 重复")
        seen[doc.doc_id] = rel
        docs.append(doc)
    return docs


# ---------- 分节 ----------


@dataclass(frozen=True)
class Section:
    path: str
    start: int
    end: int


def split_sections(body: str) -> list[Section]:
    """按 Markdown 标题切分；代码块内以 # 开头的行不当作标题。标题行本身不计入正文。"""
    sections: list[Section] = []
    stack: list[tuple[int, str]] = []
    current_path, current_start = "", 0
    in_fence = False
    pos = 0
    for line in body.splitlines(keepends=True):
        stripped = line.rstrip("\n")
        if stripped.lstrip().startswith("```"):
            in_fence = not in_fence
        match = None if in_fence else HEADING_PATTERN.match(stripped)
        if match:
            sections.append(Section(current_path, current_start, pos))
            level, title = len(match.group(1)), match.group(2)
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            current_path = " > ".join(t for _, t in stack)
            current_start = pos + len(line)
        pos += len(line)
    sections.append(Section(current_path, current_start, len(body)))
    return [s for s in sections if body[s.start:s.end].strip()]


# ---------- 分块 ----------


def _trim(body: str, start: int, end: int) -> tuple[int, int]:
    while start < end and body[start].isspace():
        start += 1
    while end > start and body[end - 1].isspace():
        end -= 1
    return start, end


def _paragraphs(body: str, start: int, end: int) -> list[tuple[int, int]]:
    """按空行切段；表格、列表等连续非空行算一段。"""
    spans, para_start, pos = [], None, start
    for line in body[start:end].splitlines(keepends=True):
        if line.strip():
            if para_start is None:
                para_start = pos
        elif para_start is not None:
            spans.append(_trim(body, para_start, pos))
            para_start = None
        pos += len(line)
    if para_start is not None:
        spans.append(_trim(body, para_start, end))
    return [s for s in spans if s[0] < s[1]]


def _split_long(body: str, start: int, end: int, max_chars: int) -> list[tuple[int, int]]:
    """超长段落依次按行、按句末标点、按字符数切开，每片不超过 max_chars。"""
    if end - start <= max_chars:
        return [(start, end)]
    for separators in ("\n", SENTENCE_END):
        cuts = [i + 1 for i in range(start, end - 1) if body[i] in separators]
        if cuts:
            result: list[tuple[int, int]] = []
            previous = start
            for cut in cuts + [end]:
                s, e = _trim(body, previous, cut)
                if s < e:
                    result.extend(_split_long(body, s, e, max_chars))
                previous = cut
            return result
    return [(i, min(i + max_chars, end)) for i in range(start, end, max_chars)]


def _pack(spans: list[tuple[int, int]], max_chars: int) -> list[tuple[int, int]]:
    """把相邻片段合并成不超过 max_chars 的块（长度按原文跨度计，含中间的空行）。"""
    packed: list[tuple[int, int]] = []
    for s, e in spans:
        if packed and e - packed[-1][0] <= max_chars:
            packed[-1] = (packed[-1][0], e)
        else:
            packed.append((s, e))
    return packed


def chunk_document(doc: Document, max_chars: int = DEFAULT_MAX_CHARS) -> list[Chunk]:
    chunks: list[Chunk] = []
    for section in split_sections(doc.body):
        spans: list[tuple[int, int]] = []
        for s, e in _paragraphs(doc.body, section.start, section.end):
            spans.extend(_split_long(doc.body, s, e, max_chars))
        for s, e in _pack(spans, max_chars):
            seq = len(chunks) + 1
            chunks.append(
                Chunk(
                    chunk_id=f"{doc.doc_id}#{seq:02d}",
                    doc_id=doc.doc_id,
                    seq=seq,
                    section=section.path or doc.title,
                    text=doc.body[s:e],
                    start=s,
                    end=e,
                )
            )
    return chunks


def corpus_sha256(docs: list[Document]) -> str:
    digest = hashlib.sha256()
    for doc in sorted(docs, key=lambda d: d.doc_id):
        digest.update(f"{doc.doc_id}:{doc.sha256}\n".encode("utf-8"))
    return digest.hexdigest()
