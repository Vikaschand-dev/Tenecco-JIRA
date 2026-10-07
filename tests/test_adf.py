from app.adf import adf_to_text, text_to_adf


def test_round_trip_paragraphs_and_line_breaks():
    text = "First line\nsecond line\n\nNew paragraph"
    adf = text_to_adf(text)
    assert adf["type"] == "doc" and len(adf["content"]) == 2
    assert adf_to_text(adf) == text


def test_adf_to_text_handles_lists_and_mentions():
    adf = {
        "type": "doc",
        "version": 1,
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": "Hi "}, {"type": "mention", "attrs": {"text": "@Ana"}}]},
            {
                "type": "bulletList",
                "content": [
                    {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "one"}]}]},
                    {"type": "listItem", "content": [{"type": "paragraph", "content": [{"type": "text", "text": "two"}]}]},
                ],
            },
        ],
    }
    assert adf_to_text(adf) == "Hi @Ana\n\n- one\n- two"


def test_none_and_plain_strings():
    assert text_to_adf(None) is None
    assert adf_to_text(None) == ""
    assert adf_to_text("already text") == "already text"
