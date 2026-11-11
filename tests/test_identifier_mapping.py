"""新旧 OAI 标识对照与换系统补采的行为测试。"""
from __future__ import annotations

OLD_1 = "oai:sample.lib:rec-001"
OLD_2 = "oai:sample.lib:rec-002"
OLD_13 = "oai:sample.lib:rec-013"
NEW_1 = "oai:sample2.lib:rec-001"
NEW_2 = "oai:sample2.lib:rec-002"


def _harvest(client, payload):
    resp = client.post("/api/v1/harvest", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _register(api, pairs):
    return api.post(
        "/api/v1/identifier-mappings",
        json={"mappings": [
            {"old_identifier": old, "new_identifier": new}
            for old, new in pairs
        ]},
    )


def _seed_old_system(client):
    """采入 10 月旧号：26 条在架 + rec-013 撤回。"""
    api, db_mod, _oai = client
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-31"})
    assert api.get("/api/v1/progress").json()["last_until"] == "2026-10-31"
    return api, db_mod


def _conflict_identifiers(resp):
    detail = resp.json()["detail"]
    return {item["identifier"] for item in detail["conflicts"]}


def test_register_batch_does_not_touch_bib_or_progress(client):
    api, _db = _seed_old_system(client)
    before_record = api.get(f"/api/v1/catalog/{OLD_1}").json()
    before_progress = api.get("/api/v1/progress").json()

    resp = _register(api, [(OLD_1, NEW_1), (OLD_2, NEW_2)])
    assert resp.status_code == 200, resp.text
    assert resp.json()["registered"] == 2

    after_record = api.get(f"/api/v1/catalog/{OLD_1}").json()
    assert after_record == before_record
    assert api.get("/api/v1/progress").json() == before_progress
    assert len(api.get("/api/v1/identifier-mappings").json()) == 2

    # 详情可用新号查询，仍返回旧号这一条
    alias = api.get(f"/api/v1/catalog/{NEW_1}").json()
    assert alias["identifier"] == OLD_1
    assert alias["status"] == "active"


def test_conflicts_identify_identifier_and_register_nothing(client):
    api, _db = _seed_old_system(client)
    # rec-013 已撤下，不能作为旧号
    resp = _register(api, [(OLD_13, NEW_1)])
    assert resp.status_code == 409
    assert OLD_13 in _conflict_identifiers(resp)
    assert api.get("/api/v1/identifier-mappings").json() == []

    # 新号必须未入库，rec-013 已是库中墓碑
    resp = _register(api, [(OLD_1, OLD_13)])
    assert resp.status_code == 409
    assert OLD_13 in _conflict_identifiers(resp)

    # 旧号不存在
    resp = _register(api, [("oai:missing:rec", NEW_1)])
    assert resp.status_code == 409
    assert "oai:missing:rec" in _conflict_identifiers(resp)

    # 本批内同一标识重复参与（旧号重复 / 新号重复 / 交叉）
    for pairs in (
        [(OLD_1, NEW_1), (OLD_1, NEW_2)],
        [(OLD_1, NEW_1), (OLD_2, NEW_1)],
        [(OLD_1, NEW_1), (NEW_1, NEW_2)],
    ):
        resp = _register(api, pairs)
        assert resp.status_code == 409
        assert api.get("/api/v1/identifier-mappings").json() == []

    # 已登记对照中的标识再次参与换号
    assert _register(api, [(OLD_1, NEW_1)]).status_code == 200
    for pairs in (
        [(OLD_1, NEW_2)],       # 旧号已登记
        [(OLD_2, NEW_1)],       # 新号已登记
        [(NEW_1, NEW_2)],       # 已登记新号又想当旧号
        [(OLD_2, OLD_1)],       # 已登记旧号又想当新号
    ):
        resp = _register(api, pairs)
        assert resp.status_code == 409
        assert _conflict_identifiers(resp)
    assert len(api.get("/api/v1/identifier-mappings").json()) == 1


def test_conflict_in_one_row_rolls_back_whole_batch(client):
    api, _db = _seed_old_system(client)
    resp = _register(api, [(OLD_2, NEW_2), (OLD_13, "oai:bad:rec")])
    assert resp.status_code == 409
    assert OLD_13 in _conflict_identifiers(resp)
    # 合法的 OLD_2 也未登记
    assert api.get("/api/v1/identifier-mappings").json() == []
    assert _register(api, [(OLD_2, NEW_2)]).status_code == 200


def test_old_number_retirement_ignored_new_update_lands_on_old(client):
    api, _db = _seed_old_system(client)
    assert _register(api, [(OLD_1, NEW_1), (OLD_2, NEW_2)]).status_code == 200

    result = _harvest(api, {"from": "2026-10-31", "until": "2026-11-10"})
    # 4 条：两条旧号删除标记（忽略）+ 两条新号更新
    assert result["fetched"] == 4
    assert result["ignored"] == 2
    assert result["updated"] == 2
    assert result["withdrawn"] == 0

    # 旧号停用（删除标记）不得撤下书目，新号更新落到原书目
    record = api.get(f"/api/v1/catalog/{OLD_1}").json()
    assert record["identifier"] == OLD_1
    assert record["status"] == "active"
    assert "新系统" in record["title"]

    # 目录仍用旧号且每个逻辑书目只有一条；新号不单独成行
    catalog = api.get("/api/v1/catalog?status=all").json()
    assert len(catalog) == 27
    assert sum(r["identifier"] == OLD_1 for r in catalog) == 1
    assert not any(r["identifier"] == NEW_1 for r in catalog)
    # 任一标识均可查详情
    assert api.get(f"/api/v1/catalog/{NEW_1}").json()["identifier"] == OLD_1

    progress = api.get("/api/v1/progress").json()
    assert progress["last_until"] == "2026-11-10"


def test_new_number_deletion_withdraws_original_bib(client):
    api, _db = _seed_old_system(client)
    assert _register(api, [(OLD_1, NEW_1), (OLD_2, NEW_2)]).status_code == 200
    _harvest(api, {"from": "2026-10-31", "until": "2026-11-10"})
    assert api.get(f"/api/v1/catalog/{OLD_1}").json()["status"] == "active"

    result = _harvest(api, {"from": "2026-11-10", "until": "2026-11-30"})
    assert result["fetched"] == 2
    assert result["withdrawn"] == 2

    record = api.get(f"/api/v1/catalog/{OLD_1}").json()
    assert record["status"] == "withdrawn"
    # 新号详情回落到同一旧书目，状态一致
    alias = api.get(f"/api/v1/catalog/{NEW_1}").json()
    assert alias["identifier"] == OLD_1
    assert alias["status"] == "withdrawn"
    # 默认目录只列在架，旧号 rec-001/002 已撤下
    active = api.get("/api/v1/catalog").json()
    assert not any(r["identifier"] in {OLD_1, OLD_2} for r in active)


def test_mapping_registered_during_paging_applies_at_write(client, monkeypatch):
    """翻页早于登记开始：写库事务内读取对照，仍按已登记结果处理。"""
    api, db_mod = _seed_old_system(client)

    from app import main as main_mod
    from app import mappings as mappings_mod
    from app.oai_client import OAIClient as RealOAIClient

    class RegisterDuringPaging(RealOAIClient):
        def list_records_first(self, *, from_date, until_date):
            page = super().list_records_first(
                from_date=from_date, until_date=until_date
            )
            # 首页已经取到后才开始登记，模拟“翻页早于登记开始”
            conn = db_mod.get_connection()
            try:
                with db_mod.transaction(conn):
                    mappings_mod.register_mappings(
                        conn, [(OLD_1, NEW_1), (OLD_2, NEW_2)]
                    )
                    conn.commit()
            finally:
                conn.close()
            return page

    monkeypatch.setattr(main_mod, "OAIClient", RegisterDuringPaging)
    result = _harvest(api, {"from": "2026-10-31", "until": "2026-11-10"})

    assert result["ignored"] == 2
    assert result["updated"] == 2
    record = api.get(f"/api/v1/catalog/{OLD_1}").json()
    assert record["status"] == "active"
    assert "新系统" in record["title"]
    assert api.get(f"/api/v1/catalog/{NEW_1}").json()["identifier"] == OLD_1


def test_bare_list_request_body_also_works(client):
    api, _db = _seed_old_system(client)
    resp = api.post(
        "/api/v1/identifier-mappings",
        json=[{"old": OLD_1, "new": NEW_1}],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["registered"] == 1
    assert api.get(f"/api/v1/catalog/{NEW_1}").json()["identifier"] == OLD_1
