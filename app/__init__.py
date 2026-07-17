"""RecipeCollater — self-hosted family recipe platform (LAN-only).

Phase 0 provides the deployable foundation: application factory, migration runner,
auth/onboarding, worker plumbing, and deploy/backup scaffolding. Recipe, ingestion,
pantry, shopping, meal-planning, and AI features arrive in later phases.
"""

from app.version import PACKAGE_VERSION

__version__ = PACKAGE_VERSION
