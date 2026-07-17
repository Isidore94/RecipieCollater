"""The migration runner's SQL splitter must be trigger- and comment-aware so future
FTS5 trigger migrations apply atomically (CONVENTIONS §13)."""

from __future__ import annotations

from app.db import split_sql_statements


def test_splits_simple_statements() -> None:
    sql = "CREATE TABLE a (id INTEGER);\nCREATE TABLE b (id INTEGER);"
    assert split_sql_statements(sql) == [
        "CREATE TABLE a (id INTEGER)",
        "CREATE TABLE b (id INTEGER)",
    ]


def test_ignores_line_and_block_comments() -> None:
    sql = """
    -- a comment; with a semicolon
    CREATE TABLE a (id INTEGER); /* block ; comment */
    CREATE TABLE b (id INTEGER);
    """
    stmts = split_sql_statements(sql)
    assert len(stmts) == 2
    assert stmts[0].startswith("CREATE TABLE a")


def test_semicolon_inside_string_literal_does_not_split() -> None:
    sql = "INSERT INTO t (v) VALUES ('a; b'); INSERT INTO t (v) VALUES ('c');"
    stmts = split_sql_statements(sql)
    assert len(stmts) == 2
    assert "'a; b'" in stmts[0]


def test_escaped_quote_in_string() -> None:
    sql = "INSERT INTO t (v) VALUES ('it''s; fine');"
    stmts = split_sql_statements(sql)
    assert len(stmts) == 1
    assert "it''s; fine" in stmts[0]


def test_trigger_body_semicolons_do_not_split() -> None:
    sql = """
    CREATE TRIGGER trg AFTER INSERT ON t BEGIN
        UPDATE t SET n = n + 1 WHERE id = NEW.id;
        DELETE FROM u WHERE id = NEW.id;
    END;
    CREATE TABLE after (id INTEGER);
    """
    stmts = split_sql_statements(sql)
    assert len(stmts) == 2
    assert stmts[0].upper().startswith("CREATE TRIGGER")
    assert "END" in stmts[0]
    assert stmts[1].startswith("CREATE TABLE after")


def test_top_level_case_end_still_splits() -> None:
    # A CASE...END at statement level (no BEGIN) must not swallow the terminator.
    sql = "SELECT CASE WHEN 1 THEN 'a' ELSE 'b' END; SELECT 2;"
    stmts = split_sql_statements(sql)
    assert len(stmts) == 2


def test_trailing_statement_without_semicolon() -> None:
    assert split_sql_statements("SELECT 1") == ["SELECT 1"]


def test_empty_input() -> None:
    assert split_sql_statements("   \n -- just a comment\n") == []
