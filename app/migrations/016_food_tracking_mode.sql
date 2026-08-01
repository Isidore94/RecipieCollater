-- Migration 016 - Remember how each food is tracked.
--
-- The pantry offers three ways to hold an amount: an exact count, a full/half/low/out gauge, and
-- a plain have/out. Which one fits is a property of the food, not of the moment: you count
-- avocados and eggs, you eyeball the flour, and you either have cumin or you don't. The add form
-- defaulted everything to the gauge, so countable things arrived as "half an avocado", which is
-- not a thing anyone means.
--
-- app/services/quantity_mode.py can infer the right mode from a food's name and aisle, but an
-- inference is only a starting point - a household that buys rice in 1kg bags may well count
-- them. This column records the answer for a food once someone has actually chosen it, so the
-- choice sticks for every future item and every automatic creation (receipt import, restock)
-- without asking again. NULL means "nobody has said, infer it".
--
-- The runner owns transactions: no BEGIN/COMMIT/VACUUM here.

ALTER TABLE foods ADD COLUMN default_quantity_mode TEXT
    CHECK (default_quantity_mode IN ('exact', 'gauge', 'binary'));
