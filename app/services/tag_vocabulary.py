"""The controlled tag vocabulary, as data.

`app.ai.base.TAG_GUIDE` states this vocabulary in prose because that is what goes into the
extraction prompts. The same words are needed as data - to offer them on the recipe form, and
to point out on the tag screen which tags have drifted outside them - and retyping a list in
three places is how the three quietly stop agreeing. A test asserts every word here appears in
TAG_GUIDE, so the prompt stays the prose and this stays the list.

Deliberately dependency-free: the recipe form and the tag screen import it in the web process,
which must not pull in the AI package (CONVENTIONS section 4).
"""

from __future__ import annotations

# Cuisines are open-ended by design - the guide says "the cuisine as one lowercase word" and a
# household should not need a code change to cook Georgian - so they are not listed here. Only
# the closed groups are, which is why off_vocabulary() treats them as advisory.
VOCABULARY: dict[str, tuple[str, ...]] = {
    "meal": ("breakfast", "lunch", "dinner", "side", "dessert", "snack", "drink"),
    "protein": ("chicken", "beef", "pork", "seafood", "lamb", "turkey", "vegetarian"),
    "method": (
        "baked", "grilled", "stovetop", "slow-cooked", "instant-pot", "air-fryer", "no-cook",
    ),
    "effort": ("weeknight", "project"),
}

# Cuisines the guide names as examples: enough to seed the form's suggestions without
# pretending the list is closed.
EXAMPLE_CUISINES: tuple[str, ...] = ("italian", "mexican", "thai")

ALL_WORDS: frozenset[str] = frozenset(
    word for group in VOCABULARY.values() for word in group
)


def _singular(word: str) -> str:
    return word[:-1] if len(word) > 3 and word.endswith("s") else word


def drifted(names: list[str]) -> list[str]:
    """Which of these tags look like drift rather than a deliberate choice.

    Deliberately NOT "everything outside the vocabulary": the guide asks for the cuisine as a
    free word, so italian, thai and whatever else this family cooks are legitimately off-list.
    A banner that flagged those would list half the tags and teach everyone to ignore it.

    What it does flag is what cannot be intentional:

    * a tag that is not all lower case - the vocabulary is lower case throughout, so "Soup"
      beside a cookbook of lower-case tags is a stray capital, and it renders as one odd chip;
    * two tags that differ only by a plural - "soup" and "soups" split one shelf in two.

    Advisory in both cases. The screen shows them; a person decides.
    """
    flagged: list[str] = []
    by_stem: dict[str, list[str]] = {}
    for name in names:
        by_stem.setdefault(_singular(name.strip().lower()), []).append(name)
    for name in names:
        clean = name.strip()
        if clean != clean.lower() or len(by_stem[_singular(clean.lower())]) > 1:
            flagged.append(name)
    return flagged
