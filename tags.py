"""Извлечение тегов Obsidian: frontmatter `tags`/`tag` и инлайновые `#теги`.

Теги нормализуются к нижнему регистру и используются только как первый,
дешёвый фильтр кандидатов для LLM-сравнения заметок.
"""
from __future__ import annotations

import re
import unicodedata

_FRONTMATTER = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
_FENCE = re.compile(r"(?ms)^[ \t]*(`{3,}|~{3,})[^\n]*\n.*?^[ \t]*\1[ \t]*$")
_INLINE_CODE = re.compile(r"`+[^`\n]*`+")
_MD_LINK_TARGET = re.compile(r"\]\([^)\n]*\)")
_WIKI_LINK = re.compile(r"!?\[\[[^\]\n]*\]\]")
_URL = re.compile(r"https?://\S+", re.I)
_TAG = re.compile(r"(?<![\w/#])#([\w/-]+)", re.UNICODE)
_TAG_VALID = re.compile(r"\A[\w][\w/-]*\Z", re.UNICODE)
_YAML_TAG_LINE = re.compile(r"\A(?:tags?)\s*:\s*(.*)\Z", re.I)


def normalize_tag(raw: str) -> str:
    """Канонический вид тега: без `#`, нижний регистр, NFC."""
    tag = unicodedata.normalize("NFC", str(raw).strip().strip("\"'").strip())
    tag = tag.lstrip("#").strip()
    tag = tag.lower()
    if not tag or tag.isdigit() or not _TAG_VALID.match(tag):
        return ""
    tag = tag.strip("/-")
    if not tag or tag.isdigit():
        return ""
    return tag[:100]


def _split_yaml_value(value: str) -> list[str]:
    value = value.strip()
    if value.startswith("[") and "]" in value:
        return re.split(r"[,\n]", value[value.index("[") + 1:value.rindex("]")])
    if not value.startswith(("\"", "'")):
        value = re.sub(r"\s+#.*$", "", value).strip()
    if "#" in value:  # tags: "#foo #bar"
        return re.findall(r"#[^\s,]+", value)
    return re.split(r"[,\n]", value)


def _frontmatter_tags(frontmatter: str) -> list[str]:
    """Минимальный разбор YAML без зависимости от PyYAML."""
    lines = frontmatter.splitlines()
    out: list[str] = []
    i = 0
    while i < len(lines):
        match = _YAML_TAG_LINE.match(lines[i])
        if not match:
            i += 1
            continue
        value = match.group(1).strip()
        i += 1
        if value:
            out.extend(_split_yaml_value(value))
            continue
        # Блочный список:
        # tags:
        #   - foo
        #   - bar
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()
            if not stripped:
                i += 1
                continue
            if stripped.startswith("-"):
                out.append(stripped[1:].strip())
                i += 1
                continue
            break
    return out


def _inline_tags(text: str) -> list[str]:
    text = _FENCE.sub("", text)
    text = _INLINE_CODE.sub("", text)
    text = _MD_LINK_TARGET.sub("]", text)
    text = _WIKI_LINK.sub("", text)
    text = _URL.sub("", text)
    return [match.group(1) for match in _TAG.finditer(text)]


def extract_tags(text: str) -> list[str]:
    """Вернуть отсортированный список уникальных тегов заметки."""
    text = text.lstrip("\ufeff")
    frontmatter = ""
    match = _FRONTMATTER.match(text)
    if match:
        frontmatter = match.group(1)
        text = text[match.end():]
    raw = _frontmatter_tags(frontmatter) + _inline_tags(text)
    tags = {tag for item in raw if (tag := normalize_tag(item))}
    return sorted(tags)
