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


class ExtractedReceiptItem(BaseModel):
    """One purchased grocery item off a receipt photo or a pasted online order."""

    model_config = ConfigDict(extra="forbid")

    original_text: str
    name: str | None = None  # the product, expanded ('Kirkland organic chicken breast')
    food: str | None = None  # short generic kitchen name a recipe would use ('chicken breast')
    quantity_text: str | None = None  # units bought ('2')
    size_text: str | None = None  # one unit's pack size when shown ('15 oz', '2 kg')


class ExtractedReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ExtractedReceiptItem] = Field(default_factory=list)


class ProposedPlanEntry(BaseModel):
    """One meal the assistant proposes for the week board."""

    model_config = ConfigDict(extra="forbid")

    day_index: int = Field(ge=0, le=6)  # 0=Mon .. 6=Sun, relative to the target week
    slot: str = "dinner"
    recipe_id: int | None = None  # must be one of the candidate ids the context supplied
    note: str | None = None       # a note entry ('leftovers', 'takeout') when recipe_id is null
    servings_text: str | None = None


class ProposedPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entries: list[ProposedPlanEntry] = Field(default_factory=list)


class ProposedPantryChange(BaseModel):
    """One conversational pantry update ('2 cans of tomatoes into the downstairs pantry')."""

    model_config = ConfigDict(extra="forbid")

    food: str
    action: str = "have"  # 'have' | 'out' | 'add'
    quantity_text: str | None = None
    unit: str | None = None
    location: str | None = None


class ProposedPantryUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    changes: list[ProposedPantryChange] = Field(default_factory=list)


class AssistantResponse(BaseModel):
    """The assistant's structured turn: presentation text plus optional pending proposals.

    The model NEVER mutates data - a meal_plan/pantry_update here is a proposal the household
    accepts, edits, or dismisses (docs/05 section 3). Authoritative quantities and writes are
    computed by deterministic services on acceptance.
    """

    model_config = ConfigDict(extra="forbid")

    message: str
    meal_plan: ProposedPlan | None = None
    pantry_update: ProposedPantryUpdate | None = None


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
        """A recipe worth saving has both ingredients and steps (docs/04 section 5).

        The YouTube path deliberately relaxes this to ingredients-only (the video carries the
        method) - see pipeline._ai_extract_and_apply(require_steps=False).
        """
        return bool(self.ingredients and self.steps)
