-- Migration 013 - Phase 4.7: receipt/order capture for pantry restocking.
--
-- A receipt photo (or a pasted Instacart/Costco order) is parsed by the AI provider into lines;
-- the parse is PERSISTED because it cost money and is not reproducible (review round-trips must
-- not re-call the model). The review screen edits/approves the lines; apply is the only writer
-- to the pantry (AI proposes, deterministic services mutate - CONVENTIONS 10).
--
-- Generalization ("KS ORG BLK BNS" -> "black beans") is learned, not re-guessed: on apply, each
-- confirmed line writes its original text into food_aliases, so the next receipt from the same
-- store resolves deterministically before the model gets a vote.
--
-- The runner owns transactions: no BEGIN/COMMIT/VACUUM here.

CREATE TABLE receipts (
    id         INTEGER PRIMARY KEY,
    source     TEXT NOT NULL CHECK (source IN ('photo','paste')),
    image_path TEXT,                    -- stored capture (photo source), relative to data/receipts
    raw_text   TEXT,                    -- pasted order text (paste source)
    status     TEXT NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','applied','discarded')),
    provider   TEXT,
    model      TEXT,
    created_by INTEGER REFERENCES users(id),
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    applied_at TEXT
);

CREATE TABLE receipt_lines (
    id            INTEGER PRIMARY KEY,
    receipt_id    INTEGER NOT NULL REFERENCES receipts(id) ON DELETE CASCADE,
    sort_order    INTEGER NOT NULL DEFAULT 0,
    original_text TEXT NOT NULL,        -- the line as printed, without the price
    product_name  TEXT,                 -- model's expansion ('Kirkland organic chicken breast')
    food_name     TEXT,                 -- model's generic kitchen name ('chicken breast')
    food_id       INTEGER REFERENCES foods(id),  -- deterministic match at parse time, if any
    quantity_text TEXT,                 -- units bought ('2')
    size_text     TEXT                  -- one unit's pack size ('15 oz')
);
CREATE INDEX idx_receipt_lines_receipt ON receipt_lines(receipt_id);
