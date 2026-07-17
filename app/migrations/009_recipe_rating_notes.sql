-- Migration 009 - per-recipe rating (1-5 stars) and a free-text notes field the cook keeps for
-- next time ("double the garlic", "needed 10 more minutes"). Kept on the recipes row for a simple
-- first version; a fuller per-cook log is a later phase. The runner owns transactions.

ALTER TABLE recipes ADD COLUMN rating INTEGER;
ALTER TABLE recipes ADD COLUMN notes TEXT;
