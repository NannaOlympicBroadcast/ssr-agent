from __future__ import annotations

from xiaomi_speaker_sdk.speaker import chunk_for_tts, strip_markdown_for_tts


def test_emphasis_and_headings_removed():
    assert strip_markdown_for_tts("### Hello **world** and *italic* and `code`") == "Hello world and italic and code"


def test_links_and_images_collapse_to_text():
    assert strip_markdown_for_tts("see [the doc](https://example.com/x) here") == "see the doc here"
    assert strip_markdown_for_tts("![a diagram](pic.png)") == "a diagram"


def test_plain_text_is_unchanged():
    assert strip_markdown_for_tts("就是一句普通的话，没有任何标记。") == "就是一句普通的话，没有任何标记。"


def test_empty_input():
    assert strip_markdown_for_tts("") == ""
    assert strip_markdown_for_tts(None) == ""  # type: ignore[arg-type]


def test_chunk_short_text_is_single_chunk():
    assert chunk_for_tts("hello") == ["hello"]


def test_chunk_long_text_splits_on_sentences():
    text = "。".join(["句子" + str(i) * 20 for i in range(20)])
    chunks = chunk_for_tts(text, limit=50)
    assert len(chunks) > 1
    assert all(len(c) <= 60 for c in chunks)  # small slack for trailing space/period
