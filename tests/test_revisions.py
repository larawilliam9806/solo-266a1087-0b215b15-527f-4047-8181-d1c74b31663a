"""End-to-end tests for the totals-revision workflow."""

from __future__ import annotations

import itertools

Q = "2024Q1"

DISTRICTS = ["东城区", "西城区", "南城区", "北城区"]
INDUSTRIES = ["制造业", "建筑业", "批发零售"]

VALUES = [
    [30, 50, 40],
    [25, 35, 35],
    [10, 20, 30],
    [45, 3, 2],
]
ROW_TOTALS = [sum(r) for r in VALUES]
COL_TOTALS = [sum(VALUES[i][j] for i in range(len(VALUES)))
              for j in range(len(VALUES[0]))]

REVISION_PATH = f"/api/v1/editor/quarters/{Q}/totals/revisions"


def totals_body(districts, industries):
    return {
        "districts": [{"name": n, "total": t}
                      for n, t in districts],
        "industries": [{"name": n, "total": t}
                       for n, t in industries],
    }


def register(client, districts=DISTRICTS, industries=INDUSTRIES,
             row_totals=ROW_TOTALS, col_totals=COL_TOTALS):
    return client.put(
        f"/api/v1/editor/quarters/{Q}/totals",
        json=totals_body(list(zip(districts, row_totals)),
                         list(zip(industries, col_totals))),
    )


def submit(client, values=VALUES, must_public=(), **extra):
    body = {"values": values,
            "must_public": [list(c) for c in must_public]}
    body.update(extra)
    return client.post(f"/api/v1/editor/quarters/{Q}/detail", json=body)


def mask(client, cells, **extra):
    body = {"cells": [list(c) for c in cells]}
    body.update(extra)
    return client.put(f"/api/v1/editor/quarters/{Q}/mask", json=body)


def revise(client, districts, industries, row_totals, col_totals, **extra):
    body = totals_body(list(zip(districts, row_totals)),
                       list(zip(industries, col_totals)))
    body.update(extra)
    return client.post(REVISION_PATH, json=body)


def publish(client, **extra):
    return client.post(f"/api/v1/editor/quarters/{Q}/publish",
                       json=extra or {})


def advised_mask(client):
    advice = client.post(
        f"/api/v1/editor/quarters/{Q}/suppression-advice", json={}
    ).json()
    assert advice["valid"], advice
    return [(c["row"], c["col"]) for c in advice["masked"]]


# ------------------------------------------------------------- happy paths

def test_reorder_revision_carries_detail_must_public_and_mask(client):
    register(client)
    submit(client, must_public=[(0, 0)])
    cells = advised_mask(client)
    assert mask(client, cells).status_code == 200

    new_districts = list(reversed(DISTRICTS))
    new_industries = list(reversed(INDUSTRIES))
    new_rows = [ROW_TOTALS[DISTRICTS.index(d)] for d in new_districts]
    new_cols = [COL_TOTALS[INDUSTRIES.index(i)] for i in new_industries]
    response = revise(client, new_districts, new_industries, new_rows, new_cols)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["registration_version"] == 2
    assert body["previous_registration_version"] == 1
    assert body["change_kind"] == "reorder"
    assert body["carried_detail_version"] == 2
    assert body["carried_mask_version"] == 1

    state = client.get(f"/api/v1/editor/quarters/{Q}").json()
    assert state["registration_version"] == 2
    assert state["districts"] == new_districts
    assert state["industries"] == new_industries
    assert state["row_totals"] == new_rows
    # Only detail versions of the current registration are usable.
    assert [v["version"] for v in state["detail_versions"]] == [2]
    assert all(v["registration_version"] == 2
               for v in state["detail_versions"])
    assert state["current_detail_version"] == 2

    # The base table was re-arranged by name, not re-entered.
    # Old (0,0)=30 (东城区/制造业) now sits at new positions (3,2).
    carried = client.post(
        f"/api/v1/editor/quarters/{Q}/suppression-advice", json={}
    )
    assert carried.status_code == 200
    # The carried mask must be exactly the old one moved by name.
    row_map = [new_districts.index(d) for d in DISTRICTS]
    col_map = [new_industries.index(i) for i in INDUSTRIES]
    expected_cells = sorted((row_map[r], col_map[c]) for r, c in cells)
    assert sorted((c["row"], c["col"]) for c in state["masked_cells"]) == \
        expected_cells
    # Must-public travelled with the cell content.
    assert state["analysis"]["valid"] is True
    assert (row_map[0], col_map[0]) == (3, 2)


def test_reorder_preserves_cell_values_by_name(client):
    register(client)
    submit(client)

    new_districts = [DISTRICTS[2], DISTRICTS[0], DISTRICTS[3], DISTRICTS[1]]
    new_industries = [INDUSTRIES[1], INDUSTRIES[0], INDUSTRIES[2]]
    new_rows = [ROW_TOTALS[DISTRICTS.index(d)] for d in new_districts]
    new_cols = [COL_TOTALS[INDUSTRIES.index(i)] for i in new_industries]
    response = revise(client, new_districts, new_industries, new_rows, new_cols)
    assert response.status_code == 200, response.text

    # A fresh detail matching the re-arranged base table must validate.
    # Re-derive each new cell from the (district, industry) names, which is
    # independent of the service's own index bookkeeping.
    old_by_name = {
        (d, i): VALUES[r][c]
        for r, d in enumerate(DISTRICTS)
        for c, i in enumerate(INDUSTRIES)
    }
    expected = [
        [old_by_name[(d, i)] for i in new_industries]
        for d in new_districts
    ]
    # Publish path re-runs validation on the carried table: build a mask and
    # publish without resubmitting anything.
    cells = advised_mask(client)
    assert mask(client, cells,
                expected_registration_version=2).status_code == 200
    published = publish(client,
                        expected_registration_version=2).json()
    for i, j in itertools.product(range(4), range(3)):
        if published["cells"][i][j] is not None:
            assert published["cells"][i][j] == expected[i][j]
    assert published["districts"] == [
        {"name": d, "total": new_rows[i]}
        for i, d in enumerate(new_districts)
    ]


def test_breaking_reversion_requires_fresh_detail(client):
    register(client)
    submit(client)
    advised_mask(client)
    mask(client, advised_mask(client))

    # Same names, but per-name household counts change: one household moves
    # from district 3 to district 0 and from industry 1 to industry 0.
    new_rows = ROW_TOTALS[:]
    new_rows[0] += 1
    new_rows[3] -= 1
    new_cols = COL_TOTALS[:]
    new_cols[0] += 1
    new_cols[1] -= 1
    response = revise(client, DISTRICTS, INDUSTRIES, new_rows, new_cols)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["change_kind"] == "breaking"
    assert body["registration_version"] == 2

    state = client.get(f"/api/v1/editor/quarters/{Q}").json()
    # No usable detail under the new registration.
    assert state["detail_versions"] == []
    assert state["current_detail_version"] is None
    assert state["mask_version"] is None
    assert state["masked_cells"] == []

    # Masking against the archived table is impossible now.
    refused = mask(client, [(0, 0)],
                   expected_registration_version=2)
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "detail_required"

    # A fresh detail against the new margins starts a new working set.
    # Move one household from cell (3,1) to (0,0): row 0/col 0 each gain 1,
    # row 3/col 1 each lose 1, grand total unchanged.
    new_values = [row[:] for row in VALUES]
    new_values[3][1] -= 1
    new_values[0][0] += 1
    assert [sum(r) for r in new_values] == new_rows
    assert [sum(new_values[r][c] for r in range(4)) for c in range(3)] == \
        new_cols
    ok = submit(client, new_values, expected_registration_version=2)
    assert ok.status_code == 200, ok.text
    assert ok.json()["current_detail_version"] == 2
    assert ok.json()["registration_version"] == 2


def test_reorder_without_detail_creates_no_versions(client):
    register(client)
    new_districts = list(reversed(DISTRICTS))
    new_rows = [ROW_TOTALS[DISTRICTS.index(d)] for d in new_districts]
    response = revise(client, new_districts, INDUSTRIES, new_rows, COL_TOTALS)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["change_kind"] == "reorder"
    assert body["carried_detail_version"] is None
    state = client.get(f"/api/v1/editor/quarters/{Q}").json()
    assert state["current_detail_version"] is None
    assert state["detail_versions"] == []


def test_second_revision_bumps_to_version_3_and_keeps_history(client):
    register(client)
    submit(client)

    reversed_districts = list(reversed(DISTRICTS))
    rows2 = [ROW_TOTALS[DISTRICTS.index(d)] for d in reversed_districts]
    first = revise(client, reversed_districts, INDUSTRIES, rows2, COL_TOTALS)
    assert first.status_code == 200
    assert first.json()["registration_version"] == 2

    rows3 = [rows2[reversed_districts.index(d)] for d in DISTRICTS]
    second = revise(client, DISTRICTS, INDUSTRIES, rows3, COL_TOTALS,
                    expected_registration_version=2)
    assert second.status_code == 200
    assert second.json()["registration_version"] == 3

    state = client.get(f"/api/v1/editor/quarters/{Q}").json()
    history = state["registration_history"]
    assert [h["version"] for h in history] == [1, 2, 3]
    assert [h["change_kind"] for h in history] == \
        ["initial", "reorder", "reorder"]
    # Two carried details (v2, v3) exist; only v3 belongs to the current
    # registration, but all versions are still on file.
    assert [v["version"] for v in state["detail_versions"]] == [3]


# ------------------------------------------------------------- guards

def test_revision_requires_known_quarter(client):
    response = client.post(
        "/api/v1/editor/quarters/2024Q2/totals/revisions",
        json=totals_body([("A", 1)], [("X", 1)]),
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "quarter_not_found"


def test_revision_validates_totals_without_touching_registration(client):
    register(client)
    submit(client)
    # Inconsistent grand totals: validation happens before any write.
    bad = client.post(REVISION_PATH, json={
        "districts": [{"name": "A", "total": 10},
                      {"name": "B", "total": 5}],
        "industries": [{"name": "X", "total": 10},
                       {"name": "Y", "total": 4}],
    })
    assert bad.status_code == 422
    assert bad.json()["error"]["code"] == "totals_inconsistent"

    state = client.get(f"/api/v1/editor/quarters/{Q}").json()
    assert state["registration_version"] == 1
    assert state["districts"] == DISTRICTS
    assert state["current_detail_version"] == 1


def test_published_quarter_refuses_revision(client):
    register(client)
    submit(client)
    mask(client, advised_mask(client))
    assert publish(client).status_code == 200

    response = revise(client, list(reversed(DISTRICTS)), INDUSTRIES,
                      list(reversed(ROW_TOTALS)), COL_TOTALS)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "bulletin_already_published"

    state = client.get(f"/api/v1/editor/quarters/{Q}").json()
    assert state["registration_version"] == 1
    # The public bulletin is untouched.
    assert client.get(f"/api/v1/bulletins/{Q}").status_code == 200


def test_stale_seen_registration_version_blocks_revision(client):
    register(client)
    submit(client)
    reversed_districts = list(reversed(DISTRICTS))
    rows2 = [ROW_TOTALS[DISTRICTS.index(d)] for d in reversed_districts]
    assert revise(client, reversed_districts, INDUSTRIES, rows2,
                  COL_TOTALS).status_code == 200

    stale = revise(client, DISTRICTS, INDUSTRIES, ROW_TOTALS, COL_TOTALS,
                   expected_registration_version=1)
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_registration_version"


def test_mutating_calls_require_version_after_revision(client):
    register(client)
    submit(client)
    reversed_districts = list(reversed(DISTRICTS))
    rows2 = [ROW_TOTALS[DISTRICTS.index(d)] for d in reversed_districts]
    assert revise(client, reversed_districts, INDUSTRIES, rows2,
                  COL_TOTALS).status_code == 200

    # Omitting the version is only legal on the never-revised registration.
    for response in (
        submit(client, VALUES),
        mask(client, [(0, 0)]),
        publish(client),
    ):
        assert response.status_code == 409
        assert response.json()["error"]["code"] == \
            "registration_version_required", response.text

    # Naming an outdated version is rejected distinctly.
    for response in (
        submit(client, VALUES, expected_registration_version=1),
        mask(client, [(0, 0)], expected_registration_version=1),
        publish(client, expected_registration_version=1),
    ):
        assert response.status_code == 409
        assert response.json()["error"]["code"] == \
            "stale_registration_version", response.text


def test_version_may_be_omitted_before_any_revision(client):
    register(client)
    # No expected_registration_version anywhere: version 1 is implicit.
    assert submit(client).status_code == 200
    assert mask(client, advised_mask(client)).status_code == 200
    assert publish(client).status_code == 200


def test_interleaved_operations_each_consume_one_complete_version(client):
    register(client)
    submit(client)
    cells = advised_mask(client)
    assert mask(client, cells).status_code == 200

    # Revision 2 (reorder) carries everything.
    reversed_districts = list(reversed(DISTRICTS))
    rows2 = [ROW_TOTALS[DISTRICTS.index(d)] for d in reversed_districts]
    rev2 = revise(client, reversed_districts, INDUSTRIES, rows2, COL_TOTALS)
    assert rev2.status_code == 200
    carried = rev2.json()["carried_detail_version"]

    # A detail submitted against v2 succeeds; its mask must target v2's detail.
    row_map = [reversed_districts.index(d) for d in DISTRICTS]
    reordered = [[VALUES[r][c] for c in range(3)] for r in row_map]
    detail = submit(client, reordered,
                    expected_registration_version=2)
    assert detail.status_code == 200, detail.text
    new_detail_version = detail.json()["current_detail_version"]
    assert new_detail_version == carried + 1

    # Old carried mask is now superseded: publishing without re-masking names
    # the stale plan, and publishing with a wrong reg version is blocked first.
    blocked = publish(client, expected_registration_version=2)
    assert blocked.status_code == 409
    assert blocked.json()["error"]["code"] == "mask_plan_superseded"

    new_cells = advised_mask(client)
    saved = mask(client, new_cells, expected_registration_version=2,
                 detail_version=new_detail_version)
    assert saved.status_code == 200
    ok = publish(client, expected_registration_version=2)
    assert ok.status_code == 200, ok.text

    # After publication, another revision is refused.
    refused = revise(client, DISTRICTS, INDUSTRIES, ROW_TOTALS, COL_TOTALS,
                     expected_registration_version=2)
    assert refused.status_code == 409
    assert refused.json()["error"]["code"] == "bulletin_already_published"


def test_failed_revision_keeps_detail_and_mask_valid(client):
    register(client)
    submit(client, must_public=[(0, 0)])
    cells = advised_mask(client)
    assert mask(client, cells).status_code == 200

    # Inconsistent grand total -> the whole revision rolls back.
    failed = client.post(REVISION_PATH, json={
        "districts": [{"name": n, "total": t}
                      for n, t in zip(DISTRICTS, ROW_TOTALS)],
        "industries": [{"name": n, "total": t}
                       for n, t in zip(INDUSTRIES, COL_TOTALS[:-1] + [0])],
    })
    assert failed.status_code == 422
    assert failed.json()["error"]["code"] == "totals_inconsistent"

    state = client.get(f"/api/v1/editor/quarters/{Q}").json()
    assert state["registration_version"] == 1
    assert state["current_detail_version"] == 1
    assert state["mask_version"] == 1
    assert sorted((c["row"], c["col"]) for c in state["masked_cells"]) == \
        sorted(cells)
    # The unchanged setup still publishes.
    assert publish(client).status_code == 200


def test_suppression_advice_only_uses_current_registration(client):
    register(client)
    submit(client)
    mask(client, advised_mask(client))

    # A reorder revision carries the detail to version 2; asking advice about
    # the archived version 1 is refused even though the row still exists.
    reversed_districts = list(reversed(DISTRICTS))
    rows2 = [ROW_TOTALS[DISTRICTS.index(d)] for d in reversed_districts]
    assert revise(client, reversed_districts, INDUSTRIES, rows2,
                  COL_TOTALS).status_code == 200

    current = client.post(
        f"/api/v1/editor/quarters/{Q}/suppression-advice", json={})
    assert current.status_code == 200
    assert current.json()["detail_version"] == 2

    stale = client.post(
        f"/api/v1/editor/quarters/{Q}/suppression-advice",
        json={"detail_version": 1})
    assert stale.status_code == 409
    assert stale.json()["error"]["code"] == "stale_detail_version"

    # A subsequent breaking revision leaves no usable detail at all.
    new_rows = ROW_TOTALS[:]
    new_rows[0] += 1
    new_rows[3] -= 1
    new_cols = COL_TOTALS[:]
    new_cols[0] += 1
    new_cols[1] -= 1
    assert revise(client, DISTRICTS, INDUSTRIES, new_rows, new_cols,
                  expected_registration_version=2).status_code == 200
    none = client.post(
        f"/api/v1/editor/quarters/{Q}/suppression-advice", json={})
    assert none.status_code == 409
    assert none.json()["error"]["code"] == "detail_required"


def test_dimension_change_is_breaking_and_archived(client):
    register(client)
    submit(client)

    # Drop one district: names changed -> breaking, old detail archived.
    fewer_districts = DISTRICTS[:3]
    fewer_rows = ROW_TOTALS[:3]
    # The removed district's households leave the grand total on both axes.
    removed = ROW_TOTALS[3]
    fewer_cols = COL_TOTALS[:]
    fewer_cols[0] -= removed
    response = revise(client, fewer_districts, INDUSTRIES, fewer_rows,
                      fewer_cols)
    assert response.status_code == 200, response.text
    assert response.json()["change_kind"] == "breaking"
    state = client.get(f"/api/v1/editor/quarters/{Q}").json()
    assert state["districts"] == fewer_districts
    assert state["current_detail_version"] is None
    # History retains the v1 registration.
    assert [h["version"] for h in state["registration_history"]] == [1, 2]


def test_reorder_carried_mask_still_publishes_under_old_rules(client):
    register(client)
    submit(client)
    cells = advised_mask(client)
    assert mask(client, cells).status_code == 200

    reversed_districts = list(reversed(DISTRICTS))
    rows2 = [ROW_TOTALS[DISTRICTS.index(d)] for d in reversed_districts]
    rev = revise(client, reversed_districts, INDUSTRIES, rows2, COL_TOTALS)
    assert rev.status_code == 200
    assert rev.json()["carried_mask_version"] == 1

    # Publication re-runs the original rules against the re-arranged base
    # table and carried mask, no re-masking needed.
    response = publish(client, expected_registration_version=2)
    assert response.status_code == 200, response.text
    assert response.json()["districts"][0]["name"] == reversed_districts[0]


def test_revision_endpoint_enforces_strict_integer_inputs(client):
    register(client)
    bad_body = totals_body(list(zip(DISTRICTS, ROW_TOTALS)),
                           list(zip(INDUSTRIES, COL_TOTALS)))
    bad_body["districts"][0]["total"] = 120.5
    response = client.post(REVISION_PATH, json=bad_body)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"
    state = client.get(f"/api/v1/editor/quarters/{Q}").json()
    assert state["registration_version"] == 1


def test_revision_requires_editor_token(client):
    from fastapi.testclient import TestClient

    register(client)
    anon = TestClient(client.app)
    response = anon.post(REVISION_PATH, json=totals_body(
        list(zip(DISTRICTS, ROW_TOTALS)), list(zip(INDUSTRIES, COL_TOTALS))))
    assert response.status_code == 401
    anon.headers.update({"Authorization": "Bearer nope"})
    assert anon.post(REVISION_PATH, json=totals_body(
        list(zip(DISTRICTS, ROW_TOTALS)),
        list(zip(INDUSTRIES, COL_TOTALS)))).status_code == 403
