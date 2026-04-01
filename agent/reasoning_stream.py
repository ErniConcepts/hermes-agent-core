from __future__ import annotations

import re
from typing import Any

THINK_OPEN_TAGS = ("<REASONING_SCRATCHPAD>", "<think>", "<reasoning>", "<THINKING>", "<thinking>")
THINK_CLOSE_TAGS = ("</REASONING_SCRATCHPAD>", "</think>", "</reasoning>", "</THINKING>", "</thinking>")


def strip_reasoning_blocks(content: str, *, trim: bool = True) -> str:
    """Remove inline reasoning blocks, returning only user-visible text."""
    if not content:
        return ""
    stripped = content
    stripped = re.sub(r"<think>.*?</think>", "", stripped, flags=re.DOTALL | re.IGNORECASE)
    stripped = re.sub(r"<reasoning>.*?</reasoning>", "", stripped, flags=re.DOTALL | re.IGNORECASE)
    stripped = re.sub(
        r"<REASONING_SCRATCHPAD>.*?</REASONING_SCRATCHPAD>",
        "",
        stripped,
        flags=re.DOTALL,
    )
    stripped = re.sub(r"</?(?:think|thinking|reasoning)>", "", stripped, flags=re.IGNORECASE)
    stripped = re.sub(r"</REASONING_SCRATCHPAD>", "", stripped)
    if "</think>" in content.lower() and "<think>" not in content.lower():
        trailing = re.split(r"</think>", stripped, flags=re.IGNORECASE)[-1]
        if trailing.strip():
            stripped = trailing
    return stripped.strip() if trim else stripped


class ReasoningStreamMux:
    """Split a mixed text stream into reasoning and visible-answer channels."""

    def __init__(self, *, on_answer: Any, on_reasoning: Any) -> None:
        self._on_answer = on_answer
        self._on_reasoning = on_reasoning
        self._buffer = ""
        self._in_reasoning = False
        self._has_visible_answer = False

    def feed(self, text: str | None) -> None:
        if not text:
            return
        self._buffer += str(text)
        while self._buffer:
            if self._in_reasoning:
                close_idx, close_tag = self._find_first_tag(self._buffer, THINK_CLOSE_TAGS)
                if close_tag is None:
                    self._emit_safe_reasoning_tail()
                    return
                reasoning = self._buffer[:close_idx]
                if reasoning:
                    self._on_reasoning(reasoning)
                self._buffer = self._buffer[close_idx + len(close_tag) :]
                self._in_reasoning = False
                continue

            open_idx, open_tag = self._find_first_tag(self._buffer, THINK_OPEN_TAGS)
            close_idx, close_tag = self._find_first_tag(self._buffer, THINK_CLOSE_TAGS)
            if close_tag is not None and (open_tag is None or close_idx < open_idx) and not self._has_visible_answer:
                reasoning = self._buffer[:close_idx]
                if reasoning:
                    self._on_reasoning(reasoning)
                self._buffer = self._buffer[close_idx + len(close_tag) :]
                continue
            if open_tag is not None:
                before = self._buffer[:open_idx]
                if before:
                    self._emit_answer(before)
                self._buffer = self._buffer[open_idx + len(open_tag) :]
                self._in_reasoning = True
                continue
            self._emit_safe_answer_tail()
            return

    def flush(self) -> None:
        if not self._buffer:
            return
        if self._in_reasoning:
            self._on_reasoning(self._buffer)
        else:
            self._emit_answer(self._buffer)
        self._buffer = ""
        self._in_reasoning = False

    def _emit_answer(self, text: str) -> None:
        visible = strip_reasoning_blocks(text, trim=False)
        if visible:
            if visible.strip():
                self._has_visible_answer = True
            self._on_answer(visible)

    def _emit_safe_answer_tail(self) -> None:
        safe = self._buffer
        for tag in THINK_OPEN_TAGS:
            for i in range(1, len(tag)):
                if self._buffer.endswith(tag[:i]):
                    safe = self._buffer[:-i]
                    break
        if safe:
            self._emit_answer(safe)
            self._buffer = self._buffer[len(safe) :]

    def _emit_safe_reasoning_tail(self) -> None:
        max_tag_len = max(len(tag) for tag in THINK_CLOSE_TAGS)
        if len(self._buffer) <= max_tag_len:
            return
        safe_reasoning = self._buffer[:-max_tag_len]
        if safe_reasoning:
            self._on_reasoning(safe_reasoning)
        self._buffer = self._buffer[-max_tag_len:]

    @staticmethod
    def _find_first_tag(buffer: str, tags: tuple[str, ...]) -> tuple[int, str | None]:
        first_idx = -1
        first_tag = None
        lowered = buffer.lower()
        for tag in tags:
            idx = lowered.find(tag.lower())
            if idx != -1 and (first_idx == -1 or idx < first_idx):
                first_idx = idx
                first_tag = tag
        return first_idx, first_tag
