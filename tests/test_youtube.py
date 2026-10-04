"""YouTube parsing: metadata, caption-track selection, transcript flattening. No network."""

from __future__ import annotations

import json
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


_CH = youtube.Chapter


def test_parse_chapters_sorts_dedupes_and_skips_junk() -> None:
    raw = [
        {"start_time": 90.4, "end_time": 120, "title": " Simmer "},
        {"start_time": 0, "end_time": 90, "title": "Intro"},
        {"start_time": 90, "title": "Duplicate start"},
        {"start_time": "x", "title": "Bad"},
        {"start_time": 5, "title": ""},
        "nope",
    ]
    assert youtube.parse_chapters(raw) == (_CH(0, "Intro"), _CH(90, "Simmer"))
    assert youtube.parse_chapters(None) == ()


def test_parse_description_chapters_reads_timestamp_lines() -> None:
    desc = "Recipe below\n0:00 Intro\n[0:45] - Sear the chicken\n1:02:10 Serve\nEnjoy!"
    assert youtube.parse_description_chapters(desc) == (
        _CH(0, "Intro"),
        _CH(45, "Sear the chicken"),
        _CH(3730, "Serve"),
    )


def test_parse_description_chapters_needs_ascending_pair() -> None:
    assert youtube.parse_description_chapters("Ready in\n2:30 hours total") == ()
    assert youtube.parse_description_chapters("1:00 B\n0:30 A") == ()
    assert youtube.parse_description_chapters("0:75 nope\n1:00 x") == ()


def test_parse_info_uses_ytdlp_chapters_then_description() -> None:
    info = dict(_INFO, chapters=[{"start_time": 0, "title": "Prep"}])
    assert youtube.parse_info(info).chapters == (_CH(0, "Prep"),)
    info = dict(_INFO, description="0:00 Prep\n0:30 Cook")
    assert youtube.parse_info(info).chapters == (_CH(0, "Prep"), _CH(30, "Cook"))
    assert youtube.parse_info(_INFO).chapters == ()


def test_prompt_text_and_json_carry_chapters() -> None:
    data = replace(youtube.parse_info(_INFO), chapters=(_CH(0, "Intro"), _CH(3725, "Serve")))
    text = data.prompt_text()
    assert "Chapters:\n0:00 Intro\n1:02:05 Serve" in text
    payload = json.loads(data.to_json())
    assert payload["chapters"][1] == {"start_seconds": 3725, "title": "Serve"}
    assert payload["comments_used"] == []


def test_is_thin() -> None:
    assert youtube.is_thin("", None)
    assert youtube.is_thin("Link in comments!", "short")
    assert not youtube.is_thin("x" * 700, None)
    assert not youtube.is_thin("tiny", "y" * 700)


def test_select_comments_prefers_pinned_then_author_then_likes() -> None:
    raw = [
        {"text": "lol", "like_count": 500},
        {"text": "Mid", "like_count": 40},
        {"text": "Owner reply", "author_is_uploader": True, "like_count": 1},
        {"text": "PINNED RECIPE", "is_pinned": True, "author": "Chef", "like_count": 2},
        {"text": "Low", "like_count": 3},
        {"text": "Also low", "like_count": 2},
        {"text": "  "},
    ]
    picked = youtube.select_comments(raw)
    assert [c.text for c in picked] == ["PINNED RECIPE", "Owner reply", "lol", "Mid", "Low"]
    assert picked[0].pinned and picked[0].author == "Chef"
    assert picked[1].by_uploader


def test_select_comments_caps_total_characters() -> None:
    raw = [
        {"text": "a" * 80, "is_pinned": True},
        {"text": "b" * 80, "like_count": 9},
        {"text": "c" * 20, "like_count": 8},
    ]
    picked = youtube.select_comments(raw, max_total_chars=110)
    assert [c.text[0] for c in picked] == ["a", "c"]
    assert youtube.select_comments("junk") == ()


def test_prompt_text_labels_comments() -> None:
    comments = youtube.select_comments([{"text": "2 cups rice", "is_pinned": True}])
    data = replace(youtube.parse_info(_INFO), comments=comments)
    assert "Pinned/top comments" in data.prompt_text()
    assert "[pinned] 2 cups rice" in data.prompt_text()
    assert json.loads(data.to_json())["comments_used"][0]["pinned"] is True


_STEPS = ["Prep the veg.", "Sear the chicken.", "Simmer."]
_CHAPS = (_CH(0, "Prep"), _CH(60, "Sear"), _CH(200, "Simmer"))


def test_assign_step_seconds_keeps_model_timestamps_and_drops_bad() -> None:
    out = youtube.assign_step_seconds(_STEPS, [10, 9999, None], _CHAPS, duration_seconds=300)
    assert out == [10, None, None]  # the model spoke: no chapter guessing on top


def test_assign_step_seconds_matches_chapter_title_prefix() -> None:
    steps = ["Sear the chicken until golden.", "Stir.", "Serve."]
    assert youtube.assign_step_seconds(steps, [None] * 3, _CHAPS) == [60, None, None]


def test_assign_step_seconds_maps_by_order() -> None:
    chapters = (_CH(0, "Part one"), _CH(60, "Part two"), _CH(200, "Part three"))
    assert youtube.assign_step_seconds(_STEPS, [None] * 3, chapters) == [0, 60, 200]
    # an intro chapter is set aside so the counts line up
    with_intro = (_CH(0, "Intro"), _CH(30, "A"), _CH(60, "B"), _CH(200, "C"))
    assert youtube.assign_step_seconds(_STEPS, [], with_intro) == [30, 60, 200]


def test_assign_step_seconds_gives_up_when_counts_differ() -> None:
    many = tuple(_CH(i * 10, f"Part {i}") for i in range(8))
    assert youtube.assign_step_seconds(_STEPS, [None] * 3, many) == [None] * 3
    assert youtube.assign_step_seconds(_STEPS, [None] * 3, ()) == [None] * 3
    assert youtube.assign_step_seconds([], [], _CHAPS) == []
