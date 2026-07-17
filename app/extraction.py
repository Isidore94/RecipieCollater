"""The single shape every extractor produces (recipe-scrapers today, AI and YouTube next).

An ``ExtractedRecipe`` is provider-neutral and pre-normalization: raw ingredient/step text
plus whatever structured hints a source offered. The ingestion pipeline maps it onto a
:class:`app.services.recipes.RecipeInput`; ingredient normalization (P2-5) refines it further.
Keeping this schema in one place means the AI adapter's strict structured-output target and the
schema.org fast-path agree on a single contract (docs/04-ingestion-pipeline.md 5).
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Bumped when the extraction shape changes; recorded on every extraction_run for provenance.
SCHEMA_VERSION = "1"


class ExtractedIngredient(BaseModel):
    model_config = ConfigDict(extra="forbid")

    original_text: str
    section: str | None = None
    quantity_text: str | None = None
    unit: str | None = None
    food: str | None = None
    note: str | None = None


class ExtractedStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    instruction: str
    section: str | None = None
    minutes: int | None = None


class ExtractedRecipe(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    description: str | None = None
    servings_text: str | None = None
    prep_minutes: int | None = None
    cook_minutes: int | None = None
    total_minutes: int | None = None
    image_url: str | None = None
    source_name: str | None = None
    ingredients: list[ExtractedIngredient] = Field(default_factory=list)
    steps: list[ExtractedStep] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)

    @field_validator("title")
    @classmethod
    def _title_not_blank(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("extracted recipe needs a title")
        return cleaned

    def is_complete(self) -> bool:
        """A recipe worth saving has both ingredients and steps (docs/04 6)."""
        return bool(self.ingredients and self.steps)
