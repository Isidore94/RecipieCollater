-- Migration 006 - Phase 1: recipe full-text search (FTS5).
--
-- A standalone FTS5 index over each recipe's title, TLDR, description, aggregated ingredient
-- text, and tags, keyed by recipe id (rowid = recipes.id). Triggers keep it in sync when the
-- recipe, its ingredients, or its tag links change, so search never drifts from the data.
-- SQLite has no shared trigger procedure, so the "rebuild this recipe's row" body (delete the
-- row, re-insert the aggregated document) is inlined per trigger. The migration runner's
-- trigger-aware splitter (CONVENTIONS 13) applies the BEGIN...END bodies atomically.
--
-- The runner owns transactions: no BEGIN/COMMIT/VACUUM here.

CREATE VIRTUAL TABLE recipe_fts USING fts5(
    title, tldr, description, ingredients, tags,
    tokenize = 'unicode61 remove_diacritics 2'
);

CREATE TRIGGER recipe_fts_recipe_ai AFTER INSERT ON recipes BEGIN
    INSERT INTO recipe_fts(rowid, title, tldr, description, ingredients, tags)
    VALUES (NEW.id, NEW.title, COALESCE(NEW.tldr, ''), COALESCE(NEW.description, ''), '', '');
END;

CREATE TRIGGER recipe_fts_recipe_ad AFTER DELETE ON recipes BEGIN
    DELETE FROM recipe_fts WHERE rowid = OLD.id;
END;

CREATE TRIGGER recipe_fts_recipe_au AFTER UPDATE ON recipes BEGIN
    DELETE FROM recipe_fts WHERE rowid = NEW.id;
    INSERT INTO recipe_fts(rowid, title, tldr, description, ingredients, tags)
    SELECT r.id, r.title, COALESCE(r.tldr, ''), COALESCE(r.description, ''),
           COALESCE((SELECT group_concat(original_text, ' ')
                     FROM recipe_ingredients WHERE recipe_id = r.id), ''),
           COALESCE((SELECT group_concat(t.name, ' ')
                     FROM recipe_tags rt JOIN tags t ON t.id = rt.tag_id
                     WHERE rt.recipe_id = r.id), '')
    FROM recipes r WHERE r.id = NEW.id;
END;

CREATE TRIGGER recipe_fts_ingredient_ai AFTER INSERT ON recipe_ingredients BEGIN
    DELETE FROM recipe_fts WHERE rowid = NEW.recipe_id;
    INSERT INTO recipe_fts(rowid, title, tldr, description, ingredients, tags)
    SELECT r.id, r.title, COALESCE(r.tldr, ''), COALESCE(r.description, ''),
           COALESCE((SELECT group_concat(original_text, ' ')
                     FROM recipe_ingredients WHERE recipe_id = r.id), ''),
           COALESCE((SELECT group_concat(t.name, ' ')
                     FROM recipe_tags rt JOIN tags t ON t.id = rt.tag_id
                     WHERE rt.recipe_id = r.id), '')
    FROM recipes r WHERE r.id = NEW.recipe_id;
END;

CREATE TRIGGER recipe_fts_ingredient_au AFTER UPDATE ON recipe_ingredients BEGIN
    DELETE FROM recipe_fts WHERE rowid = NEW.recipe_id;
    INSERT INTO recipe_fts(rowid, title, tldr, description, ingredients, tags)
    SELECT r.id, r.title, COALESCE(r.tldr, ''), COALESCE(r.description, ''),
           COALESCE((SELECT group_concat(original_text, ' ')
                     FROM recipe_ingredients WHERE recipe_id = r.id), ''),
           COALESCE((SELECT group_concat(t.name, ' ')
                     FROM recipe_tags rt JOIN tags t ON t.id = rt.tag_id
                     WHERE rt.recipe_id = r.id), '')
    FROM recipes r WHERE r.id = NEW.recipe_id;
END;

CREATE TRIGGER recipe_fts_ingredient_ad AFTER DELETE ON recipe_ingredients BEGIN
    DELETE FROM recipe_fts WHERE rowid = OLD.recipe_id;
    INSERT INTO recipe_fts(rowid, title, tldr, description, ingredients, tags)
    SELECT r.id, r.title, COALESCE(r.tldr, ''), COALESCE(r.description, ''),
           COALESCE((SELECT group_concat(original_text, ' ')
                     FROM recipe_ingredients WHERE recipe_id = r.id), ''),
           COALESCE((SELECT group_concat(t.name, ' ')
                     FROM recipe_tags rt JOIN tags t ON t.id = rt.tag_id
                     WHERE rt.recipe_id = r.id), '')
    FROM recipes r WHERE r.id = OLD.recipe_id;
END;

CREATE TRIGGER recipe_fts_tag_ai AFTER INSERT ON recipe_tags BEGIN
    DELETE FROM recipe_fts WHERE rowid = NEW.recipe_id;
    INSERT INTO recipe_fts(rowid, title, tldr, description, ingredients, tags)
    SELECT r.id, r.title, COALESCE(r.tldr, ''), COALESCE(r.description, ''),
           COALESCE((SELECT group_concat(original_text, ' ')
                     FROM recipe_ingredients WHERE recipe_id = r.id), ''),
           COALESCE((SELECT group_concat(t.name, ' ')
                     FROM recipe_tags rt JOIN tags t ON t.id = rt.tag_id
                     WHERE rt.recipe_id = r.id), '')
    FROM recipes r WHERE r.id = NEW.recipe_id;
END;

CREATE TRIGGER recipe_fts_tag_ad AFTER DELETE ON recipe_tags BEGIN
    DELETE FROM recipe_fts WHERE rowid = OLD.recipe_id;
    INSERT INTO recipe_fts(rowid, title, tldr, description, ingredients, tags)
    SELECT r.id, r.title, COALESCE(r.tldr, ''), COALESCE(r.description, ''),
           COALESCE((SELECT group_concat(original_text, ' ')
                     FROM recipe_ingredients WHERE recipe_id = r.id), ''),
           COALESCE((SELECT group_concat(t.name, ' ')
                     FROM recipe_tags rt JOIN tags t ON t.id = rt.tag_id
                     WHERE rt.recipe_id = r.id), '')
    FROM recipes r WHERE r.id = OLD.recipe_id;
END;
