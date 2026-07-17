"""YouTube parsing: metadata, caption-track selection, transcript flattening. No network."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from app.services import youtube

_INFO: dict[str, Any] = {
    "id": "abc123",
    "title": "One-Pot Chicken and Rice",
    "description": "Full recipe below:\n2 cups rice\n1 lb chicken\nSimmer 30 minutes.",
    "uploader": "Test Kitchen",
    "channel": "Test Kitchen Channel",
    "thumbnail": "https://i.ytimg.com/vi/abc123/hq.jpg",
    "duration": 615,
    "automatic_captions": {
        "en": [
            {"ext": "vtt", "url": "https://x.test/auto.vtt"},
            {"ext": "json3", "url": "https://x.test/auto.json3"},
        ]
    },
    "subtitles": {"en": [{"ext": "json3", "url": "https://x.test/manual.json3"}]},
}


def test_parse_info_reads_metadata() -> None:
    data = youtube.parse_info(_INFO)
    assert data.video_id == "abc123"
    assert data.title == "One-Pot Chicken and Rice"
    assert "2 cups rice" in data.description
    assert data.uploader == "Test Kitchen"
    assert data.thumbnail_url == "https://i.ytimg.com/vi/abc123/hq.jpg"
    assert data.duration_seconds == 615
    assert data.captions is None


def test_parse_info_requires_title() -> None:
    with pytest.raises(youtube.YoutubeError):
        youtube.parse_info({"id": "x", "title": ""})


def test_pick_caption_url_prefers_manual_json3() -> None:
    # Manual subtitles win over automatic captions for the same language.
    assert youtube.pick_caption_url(_INFO) == "https://x.test/manual.json3"


def test_pick_caption_url_none_when_absent() -> None:
    assert youtube.pick_caption_url({"id": "x", "title": "t"}) is None


def test_json3_to_text_flattens_events() -> None:
    payload = {
        "events": [
            {"segs": [{"utf8": "Add the "}, {"utf8": "rice."}]},
            {"segs": [{"utf8": "\n"}]},  # whitespace-only event is dropped
            {"segs": [{"utf8": "Then simmer."}]},
        ]
    }
    assert youtube.json3_to_text(payload) == "Add the rice. Then simmer."


def test_prompt_text_includes_description_and_transcript() -> None:
    data = replace(youtube.parse_info(_INFO), captions="Add the rice. Then simmer.")
    text = data.prompt_text()
    assert "One-Pot Chicken and Rice" in text
    assert "2 cups rice" in text
    assert "Add the rice." in text
