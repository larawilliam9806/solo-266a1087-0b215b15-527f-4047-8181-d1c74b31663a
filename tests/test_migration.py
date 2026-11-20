"""Migration from a pre-revision database schema must preserve all data."""

from __future__ import annotations

import json
import sqlite3

from fastapi.testclient import TestClient

from app.db import SCHEMA
from app.main import create_app

Q = "2024Q1"


def _legacy_schema() -> str:
    # The schema exactly as shipped before registration revisions existed.
    return """
    CREATE TABLE quarters(
        quarter TEXT PRIMARY KEY, district_labels TEXT NOT NULL,
        industry_labels TEXT NOT NULL, row_totals TEXT NOT NULL,
        col_totals TEXT NOT NULL, registered_at TEXT NOT NULL,
        registered_by INTEGER
    );
    CREATE TABLE detail_versions(
        id INTEGER PRIMARY KEY AUTOINCREMENT, quarter TEXT NOT NULL,
        version INTEGER NOT NULL, values_json TEXT NOT NULL,
        must_public TEXT NOT NULL, created_at TEXT NOT NULL,
        created_by INTEGER, UNIQUE(quarter, version)
    );
    CREATE TABLE mask_plans(
        quarter TEXT NOT NULL, detail_version INTEGER NOT NULL,
        mask_version INTEGER NOT NULL, cells_json TEXT NOT NULL,
        updated_at TEXT NOT NULL, updated_by INTEGER,
        PRIMARY KEY(quarter, detail_version)
    );
    CREATE TABLE bulletins(
        id INTEGER PRIMARY KEY AUTOINCREMENT, quarter TEXT NOT NULL UNIQUE,
        detail_version INTEGER NOT NULL, mask_version INTEGER,
        payload_json TEXT NOT NULL, published_at TEXT NOT NULL,
        published_by INTEGER
    );
    CREATE TABLE editor_tokens(
        id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT NOT NULL,
        token_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL,
        revoked_at TEXT
    );
    """


def test_legacy_database_migrates_and_keeps_working(tmp_path):
    db_path = tmp_path / "legacy.sqlite3"
    conn = sqlite3.connect(db_path)
    conn.executescript(_legacy_schema())
    conn.execute(
        "INSERT INTO quarters(quarter, district_labels, industry_labels, "
        "row_totals, col_totals, registered_at, registered_by) "
        "VALUES(?,?,?,?,?,?,?)",
        (Q, json.dumps(["D1", "D2"]), json.dumps(["I1", "I2"]),
         json.dumps([5, 7]), json.dumps([8, 4]), "2024-01-01T00:00:00+00:00",
         None),
    )
    conn.execute(
        "INSERT INTO detail_versions(quarter, version, values_json, "
        "must_public, created_at, created_by) VALUES(?,?,?,?,?,?)",
        (Q, 1, json.dumps([[2, 3], [6, 1]]), json.dumps([]),
         "2024-01-02T00:00:00+00:00", None),
    )
    conn.commit()
    conn.close()

    app = create_app(str(db_path))
    token = app.state.bootstrap_token
    # The migrated database had no tokens, so the usual bootstrap still mints
    # exactly one.
    assert token is not None

    with TestClient(app) as client:
        client.headers.update({"Authorization": f"Bearer {token}"})
        state = client.get(f"/api/v1/editor/quarters/{Q}").json()
        assert state["registration_version"] == 1
        assert state["districts"] == ["D1", "D2"]
        assert state["current_detail_version"] == 1
        assert state["detail_versions"][0]["registration_version"] == 1
        assert state["registration_history"] == [
            {"version": 1, "change_kind": "initial",
             "revised_at": "2024-01-01T00:00:00+00:00"},
        ]

        # The migrated quarter behaves like a never-revised one: the version
        # guard may be omitted, and a reorder revision works end to end.
        response = client.post(
            f"/api/v1/editor/quarters/{Q}/totals/revisions",
            json={
                "districts": [{"name": "D2", "total": 7},
                              {"name": "D1", "total": 5}],
                "industries": [{"name": "I1", "total": 8},
                               {"name": "I2", "total": 4}],
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["registration_version"] == 2
        assert body["change_kind"] == "reorder"
        assert body["carried_detail_version"] == 2

    # Re-open: the migration must be idempotent on already-current databases.
    app2 = create_app(str(db_path))
    with TestClient(app2) as client:
        client.headers.update({"Authorization": f"Bearer {token}"})
        state = client.get(f"/api/v1/editor/quarters/{Q}").json()
        assert state["registration_version"] == 2
        assert [h["version"] for h in state["registration_history"]] == [1, 2]


def test_fresh_database_has_current_schema(tmp_path):
    db_path = tmp_path / "fresh.sqlite3"
    app = create_app(str(db_path))
    token = app.state.bootstrap_token
    assert token is not None
    conn = sqlite3.connect(db_path)
    tables = {
        row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    }
    assert "totals_revisions" in tables
    quarter_cols = {row[1] for row in conn.execute(
        "PRAGMA table_info(quarters)").fetchall()}
    assert "registration_version" in quarter_cols
    conn.close()
    # SCHEMA is still self-contained for new deployments.
    assert "totals_revisions" in SCHEMA
