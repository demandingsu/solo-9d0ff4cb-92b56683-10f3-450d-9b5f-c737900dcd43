"""端到端测试：场站号归并（旧号 → 正式号）。

覆盖：
- 两类历史原始记录并入正式号（同键同值去重、互补行迁入、旧号行清零）
- 同键异值整次归并冲突回滚，不留半份更改
- 归并后两种 CSV 的旧号导入按正式号判重，且不能写入旧号新记录
- 新数据集按正式号取数；既有数据集原参数重试/查询仍读原快照
- 重复归并幂等、不得改绑；正式号不能是已归并旧号；禁止链式
- 两个连接交错导入/归并/生成时的串行化与快照完整性
- 提交失败时整体回滚
"""
import json
import os
import sqlite3
import sys
import threading

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, logic  # noqa: E402
from app.main import app  # noqa: E402

CSV_HEADERS = {"Content-Type": "text/csv"}
OLD = "ST01-OLD"
CANON = "ST01"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = str(tmp_path / "test.db")
    monkeypatch.setenv("APP_DB_PATH", db_file)
    logic.SNAPSHOT_TEST_HOOK = None
    logic.MERGE_TEST_HOOK = None
    with TestClient(app) as client:
        yield client, db_file
    logic.SNAPSHOT_TEST_HOOK = None
    logic.MERGE_TEST_HOOK = None


def query(db_file, sql, params=()):
    conn = db.connect(db_file)
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()


def station_counts(db_file):
    out = {}
    for table in ("forecasts", "actuals"):
        rows = query(db_file, f"SELECT station, COUNT(*) AS n FROM {table} GROUP BY station")
        out[table] = {r["station"]: r["n"] for r in rows}
    return out


def merge(client, old=OLD, canon=CANON):
    return client.post("/stations/merge", json={"old_station": old, "canonical_station": canon})


# ---------------------------------------------------------------- 基本合并语义

def test_merge_moves_both_kinds_and_dedups_same_key_same_value(env):
    client, db_file = env
    # 旧号：两条预报（一条与正式号同键同值）、两条实测（一条同键同值）
    client.post("/imports/forecasts", headers=CSV_HEADERS, content=(
        "station,target_hour,issued_at,irradiance\n"
        f"{OLD},2026-03-01T00:00:00Z,2026-02-27T08:00:00Z,500.0\n"   # 与正式号同键同值
        f"{OLD},2026-03-01T01:00:00Z,2026-02-28T00:30:00Z,510.0\n"   # 仅旧号有
    ))
    client.post("/imports/actuals", headers=CSV_HEADERS, content=(
        "station,target_hour,energy\n"
        f"{OLD},2026-03-01T00:00:00Z,48.2\n"                          # 与正式号同键同值
        f"{OLD},2026-03-01T01:00:00Z,49.1\n"                          # 仅旧号有
    ))
    # 正式号：各有一条独有记录
    client.post("/imports/forecasts", headers=CSV_HEADERS, content=(
        "station,target_hour,issued_at,irradiance\n"
        f"{CANON},2026-03-01T00:00:00Z,2026-02-27T08:00:00Z,500.0\n"
        f"{CANON},2026-03-01T03:00:00Z,2026-02-27T09:00:00Z,520.0\n"
    ))
    client.post("/imports/actuals", headers=CSV_HEADERS, content=(
        "station,target_hour,energy\n"
        f"{CANON},2026-03-01T00:00:00Z,48.2\n"
        f"{CANON},2026-03-01T02:00:00Z,47.5\n"
    ))

    r = merge(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["idempotent"] is False
    assert body["forecast_merged"] == 1 and body["forecast_deduplicated"] == 1
    assert body["actual_merged"] == 1 and body["actual_deduplicated"] == 1

    counts = station_counts(db_file)
    # 旧号名下不再有任何原始记录；正式号预报 3 条、实测 3 条（去重后）
    assert OLD not in counts["forecasts"] and OLD not in counts["actuals"]
    assert counts["forecasts"] == {CANON: 3}
    assert counts["actuals"] == {CANON: 3}

    aliases = query(db_file, "SELECT old_station, canonical_station FROM station_aliases")
    assert aliases == [{"old_station": OLD, "canonical_station": CANON}]


def test_merge_works_when_only_one_side_has_data(env):
    client, db_file = env
    # 只有旧号有数据
    client.post("/imports/actuals", headers=CSV_HEADERS, content=(
        "station,target_hour,energy\n"
        f"{OLD},2026-03-01T00:00:00Z,48.2\n"
    ))
    r = merge(client)
    assert r.status_code == 200
    assert r.json()["actual_merged"] == 1
    assert station_counts(db_file)["actuals"] == {CANON: 1}

    # 全新号对、两边都无数据：归并仍成功并登记别名
    r = client.post("/stations/merge", json={"old_station": "EMPTY-OLD", "canonical_station": "EMPTY-NEW"})
    assert r.status_code == 200
    aliases = {a["old_station"]: a["canonical_station"]
               for a in client.get("/stations/aliases").json()}
    assert aliases[OLD] == CANON and aliases["EMPTY-OLD"] == "EMPTY-NEW"


# ---------------------------------------------------------------- 冲突回滚

def test_merge_same_key_different_value_rolls_back_entirely(env):
    client, db_file = env
    # 预报同键异值
    client.post("/imports/forecasts", headers=CSV_HEADERS, content=(
        "station,target_hour,issued_at,irradiance\n"
        f"{OLD},2026-03-01T00:00:00Z,2026-02-27T08:00:00Z,500.0\n"
        f"{OLD},2026-03-01T01:00:00Z,2026-02-28T00:30:00Z,510.0\n"
    ))
    client.post("/imports/forecasts", headers=CSV_HEADERS, content=(
        "station,target_hour,issued_at,irradiance\n"
        f"{CANON},2026-03-01T00:00:00Z,2026-02-27T08:00:00Z,999.0\n"
    ))
    # 实测同键异值
    client.post("/imports/actuals", headers=CSV_HEADERS, content=(
        f"station,target_hour,energy\n{OLD},2026-03-01T00:00:00Z,48.2\n"
    ))
    client.post("/imports/actuals", headers=CSV_HEADERS, content=(
        f"station,target_hour,energy\n{CANON},2026-03-01T00:00:00Z,40.0\n"
    ))

    before = station_counts(db_file)
    r = merge(client)
    assert r.status_code == 409
    detail = r.json()["detail"]
    sources = sorted(c["source"] for c in detail["conflicts"])
    assert sources == ["actuals", "forecasts"]
    fc = next(c for c in detail["conflicts"] if c["source"] == "forecasts")
    assert fc["old_value"] == 500.0 and fc["canonical_value"] == 999.0

    # 整次归并回滚：数据与别名表都保持归并前状态
    assert station_counts(db_file) == before
    assert query(db_file, "SELECT COUNT(*) AS n FROM station_aliases")[0]["n"] == 0


def test_failed_merge_commit_leaves_no_partial_change(env, monkeypatch):
    """提交失败时整体回滚：归并的所有中间更改都不可见。"""
    client, db_file = env
    client.post("/imports/actuals", headers=CSV_HEADERS, content=(
        f"station,target_hour,energy\n{OLD},2026-03-01T00:00:00Z,48.2\n"
    ))

    import app.main as main_module

    real_commit = main_module._commit

    def flaky_commit(conn):
        raise sqlite3.OperationalError("simulated merge commit failure")

    monkeypatch.setattr(main_module, "_commit", flaky_commit)
    with pytest.raises(sqlite3.OperationalError, match="simulated merge commit failure"):
        merge(client)
    monkeypatch.setattr(main_module, "_commit", real_commit)

    # 旧号记录仍在、正式号无记录、别名未登记
    counts = station_counts(db_file)
    assert counts["actuals"] == {OLD: 1}
    assert query(db_file, "SELECT COUNT(*) AS n FROM station_aliases")[0]["n"] == 0

    # 数据库仍可用：重新归并成功
    r = merge(client)
    assert r.status_code == 200
    assert station_counts(db_file)["actuals"] == {CANON: 1}


# ---------------------------------------------------------------- 归并后的导入判重

def test_post_merge_old_station_imports_dedup_against_canonical(env):
    client, db_file = env
    client.post("/imports/forecasts", headers=CSV_HEADERS, content=(
        f"station,target_hour,issued_at,irradiance\n"
        f"{CANON},2026-03-01T00:00:00Z,2026-02-27T08:00:00Z,500.0\n"
    ))
    assert merge(client).status_code == 200

    # 用旧号导入同键同值：按正式号判重 → 全部 skipped，旧号名下无新记录
    r = client.post("/imports/forecasts", headers=CSV_HEADERS, content=(
        f"station,target_hour,issued_at,irradiance\n"
        f"{OLD},2026-03-01T00:00:00Z,2026-02-27T08:00:00Z,500.0\n"
    ))
    assert r.status_code == 200
    assert r.json() == {"inserted": 0, "skipped_duplicates": 1, "accepted_rows": 1}
    assert station_counts(db_file)["forecasts"] == {CANON: 1}

    # 用旧号导入同键异值：与正式号冲突，整批拒收
    r = client.post("/imports/forecasts", headers=CSV_HEADERS, content=(
        f"station,target_hour,issued_at,irradiance\n"
        f"{OLD},2026-03-01T00:00:00Z,2026-02-27T08:00:00Z,777.0\n"
        f"{OLD},2026-03-01T02:00:00Z,2026-02-27T08:00:00Z,123.0\n"
    ))
    assert r.status_code == 409
    assert r.json()["detail"]["conflicts"][0]["db_value"] == 500.0
    # 整批拒收：连新键的正常行也没有落在旧号名下
    assert station_counts(db_file)["forecasts"] == {CANON: 1}

    # 用旧号导入全新键：解析为正式号写入（不会写入旧号的新记录）
    r = client.post("/imports/actuals", headers=CSV_HEADERS, content=(
        f"station,target_hour,energy\n{OLD},2026-03-01T05:00:00Z,12.3\n"
    ))
    assert r.status_code == 200 and r.json()["inserted"] == 1
    assert station_counts(db_file)["actuals"] == {CANON: 1}


# ---------------------------------------------------------------- 归并后的取数与快照

def test_new_dataset_uses_canonical_and_existing_dataset_keeps_snapshot(env):
    client, db_file = env
    params = {
        "station": OLD,
        "start_hour": "2026-03-01T00:00:00Z",
        "end_hour": "2026-03-01T00:00:00Z",
        "lead_hours": 24,
    }
    # 归并前：旧号有预报、无实测 → no_actual
    client.post("/imports/forecasts", headers=CSV_HEADERS, content=(
        f"station,target_hour,issued_at,irradiance\n"
        f"{OLD},2026-03-01T00:00:00Z,2026-02-27T08:00:00Z,500.0\n"
    ))
    pre = client.post("/datasets/pre-merge/generate", json=params)
    assert pre.status_code == 200
    pre_row = pre.json()["rows"][0]
    assert pre_row["station"] == OLD
    assert pre_row["missing_reason"] == "no_actual"

    # 归并：把正式号的实测并进来
    client.post("/imports/actuals", headers=CSV_HEADERS, content=(
        f"station,target_hour,energy\n{CANON},2026-03-01T00:00:00Z,48.2\n"
    ))
    assert merge(client).status_code == 200

    # 既有数据集：原参数重试仍读原快照
    replay = client.post("/datasets/pre-merge/generate", json=params)
    assert replay.status_code == 200 and replay.json()["replayed"] is True
    assert replay.json()["rows"] == pre.json()["rows"]
    got = client.get("/datasets/pre-merge").json()
    assert got["rows"][0]["missing_reason"] == "no_actual"

    # 原参数换数据集号 → 冲突（参数不同，因为新数据集按正式号取数）
    r = client.post("/datasets/pre-merge/generate", json={**params, "station": CANON})
    assert r.status_code == 409

    # 新数据集（无论用旧号还是正式号请求）都按正式号取数 → 配对成功
    for req_station in (OLD, CANON):
        ds = client.post(f"/datasets/new-{req_station.replace('.', '-')}/generate",
                         json={**params, "station": req_station})
        assert ds.status_code == 200, ds.text
        body = ds.json()
        assert body["params"]["station"] == CANON
        row = body["rows"][0]
        assert row["station"] == CANON
        assert row["missing_reason"] is None
        assert row["irradiance"] == 500.0 and row["energy"] == 48.2

    # 同一新数据集号用旧号字面量重试：同一正式参数 → 读回原快照
    again = client.post("/datasets/new-ST01-OLD/generate", json=params)
    assert again.status_code == 200 and again.json()["replayed"] is True


# ---------------------------------------------------------------- 幂等与身份规则

def test_repeat_merge_idempotent_and_cannot_rebind(env):
    client, db_file = env
    client.post("/imports/actuals", headers=CSV_HEADERS, content=(
        f"station,target_hour,energy\n{OLD},2026-03-01T00:00:00Z,48.2\n"
    ))
    first = merge(client)
    assert first.status_code == 200 and first.json()["idempotent"] is False
    counts_after_first = station_counts(db_file)

    second = merge(client)
    assert second.status_code == 200
    assert second.json()["idempotent"] is True
    assert second.json()["actual_merged"] == 0
    assert station_counts(db_file) == counts_after_first
    assert query(db_file, "SELECT COUNT(*) AS n FROM station_aliases")[0]["n"] == 1

    # 改绑到其他正式号 → 409，映射不变
    r = client.post("/stations/merge", json={"old_station": OLD, "canonical_station": "ST99"})
    assert r.status_code == 409
    aliases = query(db_file, "SELECT * FROM station_aliases")
    assert aliases[0]["canonical_station"] == CANON


def test_canonical_cannot_be_merged_alias_and_no_chain(env):
    client, db_file = env
    assert merge(client).status_code == 200  # OLD → CANON

    # 正式号 CANON 已是……不，CANON 没被归并；但 CANON 不能作为旧号被归并到 ST99，
    # 因为它名下已经挂着旧号 OLD（是现役正式号）
    r = client.post("/stations/merge", json={"old_station": CANON, "canonical_station": "ST99"})
    assert r.status_code == 409
    assert "正式号" in r.json()["detail"]["message"]

    # 已归并的旧号 OLD 不能作为新的归并目标（正式号身份非法）
    r = client.post("/stations/merge", json={"old_station": "ST02", "canonical_station": OLD})
    assert r.status_code == 409
    assert "已归并的旧号" in r.json()["detail"]["message"]

    # 别名表仍只有一条
    assert query(db_file, "SELECT old_station, canonical_station FROM station_aliases") == [
        {"old_station": OLD, "canonical_station": CANON}
    ]


def test_merge_validation_errors(env):
    client, db_file = env
    # 旧号与正式号相同
    r = client.post("/stations/merge", json={"old_station": "SX", "canonical_station": "SX"})
    assert r.status_code == 422
    # 空值（Pydantic 层）
    r = client.post("/stations/merge", json={"old_station": "", "canonical_station": "SX"})
    assert r.status_code == 422
    assert query(db_file, "SELECT COUNT(*) AS n FROM station_aliases")[0]["n"] == 0


# ---------------------------------------------------------------- 交错并发

def test_interleaved_import_during_merge_serializes_to_canonical(env):
    """归并事务进行中，另一连接导入旧号数据：写锁串行化，提交后必然落在正式号。"""
    client, db_file = env
    client.post("/imports/actuals", headers=CSV_HEADERS, content=(
        f"station,target_hour,energy\n{OLD},2026-03-01T00:00:00Z,48.2\n"
    ))
    started = threading.Event()
    release = threading.Event()
    outcome = {}

    def concurrent_import():
        started.set()
        release.wait(timeout=10)
        other = db.connect(db_file)
        try:
            other.execute("BEGIN IMMEDIATE")
            # 直接走业务函数解析场站号，模拟另一实例的 CSV 导入
            station = logic.resolve_station(other, OLD)
            other.execute(
                "INSERT INTO actuals (station, target_hour, energy) VALUES (?, ?, ?)",
                (station, "2026-03-01T01:00:00Z", 49.1),
            )
            other.execute("COMMIT")
            outcome["station"] = station
        except sqlite3.OperationalError as exc:
            outcome["error"] = str(exc)
        finally:
            other.close()

    t = threading.Thread(target=concurrent_import)
    logic.MERGE_TEST_HOOK = lambda: (started.wait(10), release.set())
    t.start()
    r = merge(client)
    t.join(timeout=10)
    logic.MERGE_TEST_HOOK = None

    assert r.status_code == 200
    assert outcome.get("station") == CANON  # 归并先提交，导入解析到正式号
    assert station_counts(db_file)["actuals"] == {CANON: 2}


def test_generate_interleaved_with_merge_reads_one_complete_state(env):
    """生成事务期间另一连接尝试归并：被写锁挡住；新数据集只能来自归并前或归并后的完整状态。"""
    client, db_file = env
    # 旧号有预报无实测；正式号有实测
    client.post("/imports/forecasts", headers=CSV_HEADERS, content=(
        f"station,target_hour,issued_at,irradiance\n"
        f"{OLD},2026-03-01T00:00:00Z,2026-02-27T08:00:00Z,500.0\n"
    ))
    client.post("/imports/actuals", headers=CSV_HEADERS, content=(
        f"station,target_hour,energy\n{CANON},2026-03-01T00:00:00Z,48.2\n"
    ))
    params = {
        "station": OLD,
        "start_hour": "2026-03-01T00:00:00Z",
        "end_hour": "2026-03-01T00:00:00Z",
        "lead_hours": 24,
    }
    state = {}

    def hook():
        other = db.connect(db_file)
        other.execute("PRAGMA busy_timeout=300")
        try:
            other.execute("BEGIN IMMEDIATE")
            logic.merge_station(other, OLD, CANON)
            other.execute("COMMIT")
            state["committed"] = True
        except sqlite3.OperationalError as exc:
            state["error"] = str(exc)
            try:
                other.execute("ROLLBACK")
            except sqlite3.Error:
                pass
        finally:
            other.close()

    logic.SNAPSHOT_TEST_HOOK = hook
    body = client.post("/datasets/during/generate", json=params).json()
    logic.SNAPSHOT_TEST_HOOK = None

    # 交错归并被锁挡住，未提交；生成结果来自归并前完整状态（旧号名下无实测 → no_actual）
    assert state.get("committed") is not True
    assert "locked" in state.get("error", "").lower()
    assert body["rows"][0]["missing_reason"] == "no_actual"

    # 生成结束后归并成功；新数据集来自归并后完整状态（配对成功）
    assert merge(client).status_code == 200
    after = client.post("/datasets/after/generate", json=params).json()
    assert after["params"]["station"] == CANON
    assert after["rows"][0]["missing_reason"] is None
    assert after["rows"][0]["energy"] == 48.2

    # 归并前的数据集仍是原快照
    old = client.get("/datasets/during").json()
    assert old["rows"][0]["missing_reason"] == "no_actual"
