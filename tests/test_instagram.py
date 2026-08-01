"""Instagram ingestion: URL canonicalization, embed-page caption parsing, and the pipeline
branch. Fully offline - the parser is pure and the pipeline path is driven with supplied HTML or
a monkeypatched fetch (CONVENTIONS 15)."""

from __future__ import annotations

import json
import sqlite3

import pytest

from app import config
from app.extraction import ExtractedIngredient, ExtractedRecipe, ExtractedStep
from app.services import ingest, instagram, pipeline, recipes

_AI_RECIPE = ExtractedRecipe(
    title="Reel Pasta",
    ingredients=[ExtractedIngredient(original_text="200 g spaghetti")],
    steps=[ExtractedStep(instruction="Boil it.")],
)


@pytest.fixture(autouse=True)
def _no_image_download(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.services.images.store_image_from_url", lambda recipe_id, url: None)


class _FakeExtractor:
    provider = "anthropic"
    model = "claude-sonnet-5"

    def __init__(self, recipe: ExtractedRecipe | None = None) -> None:
        self._recipe = recipe or _AI_RECIPE
        self.seen: list[str] = []

    def extract(self, content: str, *, source_url: str):  # type: ignore[no-untyped-def]
        from app.ai.base import AIExtraction

        self.seen.append(content)
        return AIExtraction(
            recipe=self._recipe, provider=self.provider, model=self.model,
            input_tokens=500, output_tokens=100, cost_micros=900,
        )


# --------------------------------------------------------------------------------------
# URL recognition & canonicalization
# --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://www.instagram.com/reel/ABC123xyz/", "ABC123xyz"),
        ("https://www.instagram.com/reels/ABC123xyz/", "ABC123xyz"),
        ("https://instagram.com/p/ABC123xyz", "ABC123xyz"),
        ("https://www.instagram.com/tv/ABC123xyz/", "ABC123xyz"),
        ("https://m.instagram.com/reel/ABC123xyz/?igshid=abc", "ABC123xyz"),
        # A profile, the site root, and a non-Instagram host are not posts.
        ("https://www.instagram.com/somechef/", None),
        ("https://www.instagram.com/", None),
        ("https://notinstagram.com/reel/ABC123xyz/", None),
        ("https://www.instagram.com/reel/!!/", None),
    ],
)
def test_instagram_shortcode(url: str, expected: str | None) -> None:
    assert ingest.instagram_shortcode(url) == expected


def test_reel_and_post_urls_collapse_to_one_key() -> None:
    """The IG app shares /reel/, the website shares /p/ - the same post must not ingest twice."""
    from_app = ingest.normalize_url("https://www.instagram.com/reel/ABC123xyz/?igshid=zzz")
    from_web = ingest.normalize_url("https://instagram.com/p/ABC123xyz/")
    assert from_app == from_web == "https://www.instagram.com/reel/ABC123xyz"


def test_duplicate_reel_share_reuses_the_job(migrated_db: sqlite3.Connection) -> None:
    first, created = ingest.enqueue_job(migrated_db, "https://www.instagram.com/reel/ABC123xyz/")
    assert created
    second, created_again = ingest.enqueue_job(migrated_db, "https://instagram.com/p/ABC123xyz")
    assert not created_again
    assert second.id == first.id


def test_profile_url_is_not_routed_to_instagram() -> None:
    """A profile link must fall through to the normal web path, not the caption extractor."""
    normalized = ingest.normalize_url("https://instagram.com/somechef")
    assert ingest.instagram_shortcode(normalized) is None


# --------------------------------------------------------------------------------------
# Embed-page parsing
# --------------------------------------------------------------------------------------

_CAPTION = "Weeknight pasta\n200g spaghetti\n2 cloves garlic\nBoil, toss, serve.\n#pasta #easy"


def _context_json_page(caption: str) -> str:
    """An embed page carrying the caption in the inline contextJSON blob (the primary shape)."""
    inner = json.dumps(
        {"shortcode_media": {"edge_media_to_caption": {"edges": [{"node": {"text": caption}}]}}}
    )
    return (
        '<html><head><meta property="og:image" content="https://cdn.test/thumb.jpg">'
        "</head><body>"
        f'<script>window.__additionalData = {{"contextJSON": {json.dumps(inner)}}};</script>'
        '<div class="Caption"><a class="CaptionUsername">chefspam</a>ignored</div>'
        "</body></html>"
    )


def _markup_page(caption: str) -> str:
    """An embed page with no inline JSON - only the rendered caption block."""
    html_caption = caption.replace("\n", "<br>")
    return (
        '<html><head><meta property="og:image" content="https://cdn.test/thumb.jpg">'
        "</head><body>"
        f'<div class="Caption"><a class="CaptionUsername">chefspam</a>{html_caption}'
        '<div class="CaptionComments">View all 42 comments</div></div>'
        "</body></html>"
    )


def test_parse_prefers_context_json() -> None:
    data = instagram.parse_embed_html(_context_json_page(_CAPTION), "ABC123xyz")
    assert "200g spaghetti" in data.caption
    assert data.shortcode == "ABC123xyz"
    assert data.author == "chefspam"
    assert data.thumbnail_url == "https://cdn.test/thumb.jpg"


def test_parse_falls_back_to_markup() -> None:
    data = instagram.parse_embed_html(_markup_page(_CAPTION), "ABC123xyz")
    assert "200g spaghetti" in data.caption
    # The username link and the comment tail are chrome, not recipe text.
    assert "View all 42 comments" not in data.caption
    assert not data.caption.startswith("chefspam")


def test_parse_falls_back_to_og_description() -> None:
    page = (
        '<html><head><meta property="og:description" '
        "content='12 likes, 3 comments - chefspam on July 4, 2026: \"200g spaghetti, boil it.\"'>"
        "</head><body></body></html>"
    )
    data = instagram.parse_embed_html(page, "ABC123xyz")
    assert data.caption == "200g spaghetti, boil it."


def test_trailing_hashtags_are_dropped() -> None:
    data = instagram.parse_embed_html(_context_json_page(_CAPTION), "ABC123xyz")
    assert "#pasta" not in data.caption
    assert "Boil, toss, serve." in data.caption


def test_hashtag_only_caption_is_kept() -> None:
    """Stripping must never empty a caption outright - an unusable caption is better than none."""
    data = instagram.parse_embed_html(_context_json_page("#dinner #pasta"), "ABC123xyz")
    assert "#dinner" in data.caption


def test_unavailable_post_raises_actionable_error() -> None:
    page = (
        "<html><body>The link to this photo or video may be broken, "
        "or the post may have been removed.</body></html>"
    )
    with pytest.raises(instagram.InstagramError, match="private or removed"):
        instagram.parse_embed_html(page, "ABC123xyz")


def test_caption_less_post_raises() -> None:
    with pytest.raises(instagram.InstagramError, match="No caption"):
        instagram.parse_embed_html("<html><body><div>nothing here</div></body></html>", "ABC")


def test_embed_url_uses_the_public_captioned_endpoint() -> None:
    assert instagram.embed_url("ABC") == "https://www.instagram.com/p/ABC/embed/captioned/"


# --------------------------------------------------------------------------------------
# Pipeline branch
# --------------------------------------------------------------------------------------


def test_pipeline_ingests_public_reel_via_embed(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RC_ANTHROPIC_API_KEY", "test-key")
    config.reset_settings_cache()
    job, _ = ingest.enqueue_job(migrated_db, "https://www.instagram.com/reel/ABC123xyz/")
    data = instagram.InstagramData(
        shortcode="ABC123xyz", caption=_CAPTION, author="chefspam",
        thumbnail_url="https://cdn.test/thumb.jpg",
    )
    monkeypatch.setattr("app.services.instagram.fetch", lambda shortcode: data)
    monkeypatch.setattr("app.ai.get_provider", lambda settings: _FakeExtractor())

    pipeline.run_job(migrated_db, job)

    done = ingest.get_job(migrated_db, job.id)
    assert done is not None and done.status == "done"
    recipe = recipes.get_recipe(migrated_db, done.recipe_id or 0)
    assert recipe is not None
    # source_type stays 'web' (the schema CHECK has no 'instagram'); provenance lives on the run.
    assert recipe.source_type == "web"
    # video_id must stay NULL or cook mode builds a bogus youtube.com deep link.
    assert recipe.video_id is None
    run = migrated_db.execute(
        "SELECT extractor FROM extraction_runs WHERE recipe_id = ?", (done.recipe_id,)
    ).fetchone()
    assert run["extractor"] == "instagram"
    usage = migrated_db.execute(
        "SELECT operation FROM ai_usage_log WHERE job_id = ?", (job.id,)
    ).fetchone()
    assert usage["operation"] == "extract_instagram"


def test_pipeline_prefers_supplied_html_over_the_embed(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The Safari capture works for followers-only posts, so it must win and skip the network."""
    monkeypatch.setenv("RC_ANTHROPIC_API_KEY", "test-key")
    config.reset_settings_cache()

    def _boom(shortcode: str) -> instagram.InstagramData:
        raise AssertionError("supplied HTML must not trigger a network fetch")

    monkeypatch.setattr("app.services.instagram.fetch", _boom)
    extractor = _FakeExtractor()
    monkeypatch.setattr("app.ai.get_provider", lambda settings: extractor)

    job, _ = ingest.enqueue_job(
        migrated_db,
        "https://www.instagram.com/reel/ABC123xyz/",
        html="<html><body><p>200g spaghetti, boil it.</p></body></html>",
    )
    pipeline.run_job(migrated_db, job)

    done = ingest.get_job(migrated_db, job.id)
    assert done is not None and done.status == "done"
    assert "200g spaghetti" in extractor.seen[0]


def test_pipeline_reports_a_private_post_clearly(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("RC_ANTHROPIC_API_KEY", "test-key")
    config.reset_settings_cache()

    def _unavailable(shortcode: str) -> instagram.InstagramData:
        raise instagram.InstagramError("it may be private or removed. Open it in Safari...")

    monkeypatch.setattr("app.services.instagram.fetch", _unavailable)
    job, _ = ingest.enqueue_job(migrated_db, "https://www.instagram.com/reel/PRIV12345/")
    pipeline.run_job(migrated_db, job)

    done = ingest.get_job(migrated_db, job.id)
    assert done is not None and done.status == "failed"
    assert done.error_category == "instagram_unavailable"
    assert "Safari" in (done.error_message or "")


def test_pipeline_instagram_needs_ai_key(migrated_db: sqlite3.Connection) -> None:
    job, _ = ingest.enqueue_job(migrated_db, "https://www.instagram.com/reel/NOKEY12345/")
    pipeline.run_job(migrated_db, job)
    done = ingest.get_job(migrated_db, job.id)
    assert done is not None and done.status == "failed"
    assert done.error_category == "instagram_needs_ai"


def test_instagram_saves_ingredients_only_recipe(
    migrated_db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reel whose method is spoken aloud still yields a keeper (require_steps=False)."""
    monkeypatch.setenv("RC_ANTHROPIC_API_KEY", "test-key")
    config.reset_settings_cache()
    no_steps = ExtractedRecipe(
        title="Spoken Method Reel",
        ingredients=[ExtractedIngredient(original_text="200 g spaghetti")],
        steps=[],
    )
    data = instagram.InstagramData(
        shortcode="ABC123xyz", caption=_CAPTION, author="chefspam", thumbnail_url=None
    )
    monkeypatch.setattr("app.services.instagram.fetch", lambda shortcode: data)
    monkeypatch.setattr("app.ai.get_provider", lambda settings: _FakeExtractor(no_steps))

    job, _ = ingest.enqueue_job(migrated_db, "https://www.instagram.com/reel/ABC123xyz/")
    pipeline.run_job(migrated_db, job)

    done = ingest.get_job(migrated_db, job.id)
    assert done is not None and done.status == "done"
