"""Tests for XiaoAI TTS text preparation — Markdown is flattened to plain
speech before the speaker reads it aloud (otherwise it voices ``*``/``#``/``` ` ```).
"""

from __future__ import annotations

from ssr.integrations.xiaomi import _strip_markdown_for_tts as strip


def test_emphasis_and_headings_removed():
    assert strip("### Hello **world** and *italic* and `code`") == "Hello world and italic and code"


def test_links_and_images_collapse_to_text():
    assert strip("see [the doc](https://example.com/x) here") == "see the doc here"
    assert strip("![a diagram](pic.png)") == "a diagram"
    assert strip("visit <https://mi.com> now") == "visit https://mi.com now"


def test_lists_and_blockquotes_lose_their_markers():
    assert "-" not in strip("- one\n- two\n+ three")
    assert strip("> quoted line").strip() == "quoted line"


def test_code_fence_keeps_inner_text_drops_backticks():
    out = strip("```python\nprint(1)\n```\ndone")
    assert "```" not in out and "print(1)" in out and "done" in out


def test_table_delimiter_row_is_dropped():
    out = strip("| a | b |\n|---|---|\n| 1 | 2 |")
    assert "---" not in out and "|" not in out


def test_horizontal_rule_removed():
    assert "---" not in strip("text\n---\nmore")


def test_plain_text_is_unchanged():
    assert strip("就是一句普通的话，没有任何标记。") == "就是一句普通的话，没有任何标记。"


def test_empty_input():
    assert strip("") == ""
    assert strip(None) == ""  # type: ignore[arg-type]
