"""HTTP API 集成测试：展柜移交放行（移交单 handover order）。

覆盖：
- 以非空移交单号 + 非空、不重复窗口标识列表申请放行；逐窗口存在且最新签发
  证书未撤销、结论合格时一次登记，固化各窗口证书标识/版本/结论，返回单号、
  明细与有效状态；
- 任一窗口不存在 -> 404；缺当前可用证书（无证书/最新版撤销）或结论非合格
  （fail/pending）-> 409，整单不留记录；空列表、重复标识、非正整数、空
  单号 -> 422；
- 同一单号按窗口集合（不计顺序）重放返回原单及当前状态（effect=idempotent，
  明细顺序与登记一致），即使原单已失效；集合不同 -> 409；
- 按单号查询：不存在 404；明细保持登记内容；任一引用证书撤销或窗口出现
  更新签发版本 -> 整单失效并锁存；后续再签发也不恢复；撤销旧版本不影响
  （本来就因有新版本而失效），撤销非最新版本不影响仍引用最新版的其它单；
- 登记/查询与证书签发、撤销并发：结果对应一致提交状态（BEGIN IMMEDIATE
  串行）。

运行：PYTHONPATH=. python -m pytest tests/test_handover_api.py -q
"""
from __future__ import annotations

import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.config import settings


@pytest.fixture()
def client(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(settings, "db_path", str(db_file))
    monkeypatch.setattr(settings, "db_timeout", 5)
    # 延迟导入 app，确保 connect() 读取被覆盖后的 settings
    from app.main import app

    with TestClient(app) as c:
        yield c


WINDOW_BASE = "2026-10-01T00:00:00Z"


def make_window(client, wid=None, minutes=60, tmin=18.0, tmax=22.0,
                max_exceed=300.0, max_gap=600.0, name=None):
    body = {
        "start_at": WINDOW_BASE,
        "end_at": (datetime.fromisoformat(WINDOW_BASE.replace("Z", "+00:00"))
                   + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z"),
        "allowed_temp_min": tmin,
        "allowed_temp_max": tmax,
        "max_continuous_exceed_seconds": max_exceed,
        "max_sample_interval_seconds": max_gap,
    }
    if name:
        body["name"] = name
    r = client.post("/api/v1/windows", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def reading(rid, rev, minute, temp):
    ts = datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc) + timedelta(minutes=minute)
    return {
        "reading_id": rid, "revision": rev,
        "measured_at": ts.isoformat().replace("+00:00", "Z"),
        "temperature": temp,
    }


def fill_pass(client, wid):
    """每分钟一条 20°C 读数 + 终点覆盖采样：间隔 <= 600s 且温度在区间内 -> pass。"""
    for m in range(61):
        ts = (datetime(2026, 10, 1, 0, 0, tzinfo=timezone.utc)
              + timedelta(minutes=m)).isoformat().replace("+00:00", "Z")
        r = client.post(f"/api/v1/windows/{wid}/readings",
                        json={"reading_id": f"r{m:02d}", "revision": 1,
                              "measured_at": ts, "temperature": 20.0})
        assert r.status_code == 201, r.text


def issue(client, wid, note=None):
    return client.post(f"/api/v1/windows/{wid}/certificates/issue",
                       json={"note": note} if note is not None else {})


def register(client, order_no, window_ids):
    return client.post("/api/v1/handover-orders",
                       json={"order_no": order_no, "window_ids": window_ids})


def get_order(client, order_no):
    return client.get(f"/api/v1/handover-orders/{order_no}")


# ---------------------------------------------------------------- 登记成功

def test_register_freezes_certificate_for_each_window(client):
    w1 = make_window(client, name="w1")
    w2 = make_window(client, name="w2")
    fill_pass(client, w1["id"])
    fill_pass(client, w2["id"])
    c1 = issue(client, w1["id"], note="w1-v1").json()
    c2 = issue(client, w2["id"], note="w2-v1").json()

    r = register(client, "HO-1", [w2["id"], w1["id"]])
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["effect"] == "created"
    assert body["order_no"] == "HO-1"
    assert body["valid"] is True
    assert body["window_count"] == 2
    # 明细顺序与登记请求一致，内容固化为登记时的当前可用证书
    assert [it["window_id"] for it in body["items"]] == [w2["id"], w1["id"]]
    assert body["items"][0] == {
        "window_id": w2["id"], "certificate_id": c2["id"],
        "certificate_version": 1, "verdict": "pass", "verdict_label_zh": "合格"}
    assert body["items"][1]["certificate_id"] == c1["id"]
    assert body["registered_at"]

    # 按单号查询：不存在 -> 404；存在则明细一致、仍有效
    assert get_order(client, "missing").status_code == 404
    g = get_order(client, "HO-1")
    assert g.status_code == 200
    got = g.json()
    assert got["valid"] is True
    assert [it["certificate_id"] for it in got["items"]] == [c2["id"], c1["id"]]


# ---------------------------------------------------------------- 422

def test_empty_or_duplicate_or_invalid_window_list_422(client):
    make_window(client)
    fill_pass(client, 1)
    issue(client, 1)

    r = register(client, "HO-E", [])
    assert r.status_code == 422
    r = register(client, "HO-E", [1, 1])
    assert r.status_code == 422 and "重复" in str(r.json())
    # 非正整数
    assert register(client, "HO-E", [0]).status_code == 422
    assert register(client, "HO-E", [-1]).status_code == 422
    # 空/空白单号或缺字段
    assert register(client, "   ", [1]).status_code == 422
    r = client.post("/api/v1/handover-orders", json={"window_ids": [1]})
    assert r.status_code == 422
    r = client.post("/api/v1/handover-orders", json={"order_no": "HO-E"})
    assert r.status_code == 422


# ---------------------------------------------------------------- 404

def test_any_window_missing_404(client):
    make_window(client)
    fill_pass(client, 1)
    issue(client, 1)

    r = register(client, "HO-404", [1, 999])
    assert r.status_code == 404
    assert "999" in r.json()["detail"]
    # 即使缺失窗口排在首位
    assert register(client, "HO-404b", [888, 1]).status_code == 404
    # 整单不留记录
    assert get_order(client, "HO-404").status_code == 404


# ---------------------------------------------------------------- 409：证书不可用/非合格

def test_missing_current_certificate_409_and_no_record(client):
    make_window(client)
    fill_pass(client, 1)
    # 尚未签发
    r = register(client, "HO-NC", [1])
    assert r.status_code == 409 and "当前可用证书" in r.json()["detail"]
    assert get_order(client, "HO-NC").status_code == 404  # 整单不留记录

    # 签发后合格 -> 可登记（证明此前失败未占用单号）
    issue(client, 1, note="v1")
    r = register(client, "HO-NC", [1])
    assert r.status_code == 201 and r.json()["effect"] == "created"


def test_revoked_latest_no_current_certificate_409(client):
    make_window(client)
    fill_pass(client, 1)
    c1 = issue(client, 1).json()
    client.post(f"/api/v1/certificates/{c1['id']}/revocation",
                json={"reason": "依据有误"})
    # 撤销最新版后不自动退回旧版 -> 无当前可用证书 -> 409
    r = register(client, "HO-RV", [1])
    assert r.status_code == 409 and "当前可用证书" in r.json()["detail"]
    assert get_order(client, "HO-RV").status_code == 404


def test_non_pass_verdict_409(client):
    # fail：max_exceed=60s，前 40 分钟 30°C（超限 2400s）-> fail
    make_window(client, max_exceed=60.0, name="fail-win")
    for m in range(7):
        r = client.post("/api/v1/windows/1/readings",
                        json=reading(f"r{m}", 1, m * 10, 30.0 if m < 4 else 20.0))
        assert r.status_code == 201
    cert = issue(client, 1).json()
    assert cert["verdict"] == "fail"
    r = register(client, "HO-F", [1])
    assert r.status_code == 409 and "非合格" in r.json()["detail"]
    assert get_order(client, "HO-F").status_code == 404

    # pending：空窗口签发 -> pending
    make_window(client, name="pending-win")
    pcert = issue(client, 2).json()
    assert pcert["verdict"] == "pending"
    r = register(client, "HO-P", [2])
    assert r.status_code == 409 and "非合格" in r.json()["detail"]
    assert get_order(client, "HO-P").status_code == 404


# ---------------------------------------------------------------- 重放

def test_replay_same_window_set_order_independent(client):
    w1 = make_window(client, name="w1")
    w2 = make_window(client, name="w2")
    fill_pass(client, 1)
    fill_pass(client, 2)
    issue(client, 1)
    issue(client, 2)

    r1 = register(client, "HO-RP", [1, 2])
    assert r1.status_code == 201 and r1.json()["effect"] == "created"
    # 同单同集合、顺序不同 -> 幂等，返回原单（明细顺序仍为登记顺序）
    r2 = register(client, "HO-RP", [2, 1])
    assert r2.status_code == 201
    body = r2.json()
    assert body["effect"] == "idempotent"
    assert [it["window_id"] for it in body["items"]] == [1, 2]
    assert body["registered_at"] == r1.json()["registered_at"]
    # 再以原顺序重放仍幂等
    r3 = register(client, "HO-RP", [1, 2])
    assert r3.json()["effect"] == "idempotent"


def test_replay_same_order_different_window_set_409(client):
    w1 = make_window(client, name="w1")
    w2 = make_window(client, name="w2")
    fill_pass(client, 1)
    fill_pass(client, 2)
    issue(client, 1)
    issue(client, 2)

    assert register(client, "HO-C", [1]).status_code == 201
    # 集合不同（多一个窗口）-> 409
    r = register(client, "HO-C", [1, 2])
    assert r.status_code == 409 and "窗口集合不同" in r.json()["detail"]
    # 少窗口 / 换窗口同样 409
    assert register(client, "HO-C", [2]).status_code == 409

    # 原单仍可按原集合重放且内容未变
    again = register(client, "HO-C", [1])
    assert again.status_code == 201 and again.json()["effect"] == "idempotent"
    assert [it["window_id"] for it in again.json()["items"]] == [1]


def test_replay_returns_original_even_when_invalid(client):
    make_window(client)
    fill_pass(client, 1)
    issue(client, 1)
    register(client, "HO-INV", [1])
    # 再签发 -> 原单失效
    issue(client, 1, note="v2")
    g = get_order(client, "HO-INV")
    assert g.json()["valid"] is False
    # 重放仍返回原单（effect=idempotent），且 valid=false 不恢复
    r = register(client, "HO-INV", [1])
    assert r.status_code == 201
    body = r.json()
    assert body["effect"] == "idempotent"
    assert body["valid"] is False
    assert body["items"][0]["certificate_version"] == 1  # 明细仍固化 v1


# ---------------------------------------------------------------- 失效锁存

def test_revocation_of_referenced_certificate_invalidates_order(client):
    make_window(client)
    fill_pass(client, 1)
    c1 = issue(client, 1, note="v1").json()
    register(client, "HO-V", [1])
    assert get_order(client, "HO-V").json()["valid"] is True

    r = client.post(f"/api/v1/certificates/{c1['id']}/revocation",
                    json={"reason": "依据有误"})
    assert r.status_code == 201
    g = get_order(client, "HO-V").json()
    assert g["valid"] is False
    # 明细仍为登记时内容（证书标识/版本/结论）
    it = g["items"][0]
    assert it["certificate_id"] == c1["id"] and it["certificate_version"] == 1
    assert it["verdict"] == "pass"

    # 后续再签发新版本也不恢复原单
    c2 = issue(client, 1, note="v2").json()
    assert c2["version"] == 2
    assert get_order(client, "HO-V").json()["valid"] is False


def test_new_issued_version_invalidates_order_without_revocation(client):
    w1 = make_window(client, name="w1")
    w2 = make_window(client, name="w2")
    fill_pass(client, 1)
    fill_pass(client, 2)
    c1 = issue(client, 1).json()
    c2 = issue(client, 2).json()
    register(client, "HO-N", [1, 2])

    # 窗口 2 签发新版本（v1 未撤销）-> 引用其 v1 的移交单失效
    c2b = issue(client, 2, note="v2").json()
    assert c2b["version"] == 2 and c2b["revoked"] is False
    g = get_order(client, "HO-N").json()
    assert g["valid"] is False
    assert [it["certificate_id"] for it in g["items"]] == [c1["id"], c2["id"]]

    # 即使随后撤销新版本 v2（v1 未撤销），原单也不恢复：
    # “已有更新版本”这一事实不会消失
    client.post(f"/api/v1/certificates/{c2b['id']}/revocation",
                json={"reason": "新版误签"})
    assert get_order(client, "HO-N").json()["valid"] is False


def test_invalidation_is_latched_per_order(client):
    """一张新证书导致旧单失效；引用新证书的另一张单保持有效。"""
    make_window(client)
    fill_pass(client, 1)
    issue(client, 1, note="v1")
    register(client, "OLD", [1])
    issue(client, 1, note="v2")
    assert get_order(client, "OLD").json()["valid"] is False
    # 新单登记时固化 v2，有效
    r = register(client, "NEW", [1])
    assert r.status_code == 201 and r.json()["valid"] is True
    assert r.json()["items"][0]["certificate_version"] == 2
    assert get_order(client, "NEW").json()["valid"] is True
    # 旧单依旧失效
    assert get_order(client, "OLD").json()["valid"] is False


# ---------------------------------------------------------------- 并发

def test_concurrent_register_and_issue_follow_commit_order(client):
    """登记与签发并发：BEGIN IMMEDIATE 串行，任一提交顺序下结果自洽。

    - 签发先提交：登记固化新版本 v2，移交单有效；
    - 登记先提交：固化 v1 且有效；随后查询因已有 v2 而失效。
    两种顺序都必须对应“同一已提交状态”，不出现撕裂。
    """
    make_window(client)
    fill_pass(client, 1)
    issue(client, 1, note="v1")

    outcome = {}

    def do_register(barrier):
        c = TestClient(client.app)
        barrier.wait()
        r = c.post("/api/v1/handover-orders",
                   json={"order_no": "HO-C1", "window_ids": [1]})
        outcome["reg_code"] = r.status_code
        outcome["reg_body"] = r.json() if r.status_code == 201 else None

    def do_issue(barrier):
        c = TestClient(client.app)
        barrier.wait()
        r = c.post("/api/v1/windows/1/certificates/issue", json={"note": "并发 v2"})
        outcome["issue_code"] = r.status_code
        outcome["issue_version"] = r.json()["version"]

    barrier = threading.Barrier(2)
    t1 = threading.Thread(target=do_register, args=(barrier,))
    t2 = threading.Thread(target=do_issue, args=(barrier,))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert outcome["reg_code"] == 201 and outcome["issue_code"] == 201, outcome
    assert outcome["issue_version"] == 2
    frozen_version = outcome["reg_body"]["items"][0]["certificate_version"]
    assert frozen_version in (1, 2)

    g = get_order(client, "HO-C1").json()
    # 固化版本不随后续查询改变；当前有效状态严格自洽
    assert g["items"][0]["certificate_version"] == frozen_version
    if frozen_version == 1:
        # 登记先提交：v1 已被 v2 取代 -> 失效
        assert g["valid"] is False
    else:
        # 签发先提交：登记固化 v2，仍是最新未撤销 -> 有效
        assert g["valid"] is True


def test_concurrent_register_and_revoke_follow_commit_order(client):
    """登记与撤销并发：

    - 撤销先提交：窗口最新版已撤销、无当前可用证书 -> 登记 409，不留记录；
    - 登记先提交：登记成功且固化该证书；随后查询因证书已撤销而失效。
    """
    make_window(client)
    fill_pass(client, 1)
    c1 = issue(client, 1, note="v1").json()

    outcome = {}

    def do_register(barrier):
        c = TestClient(client.app)
        barrier.wait()
        r = c.post("/api/v1/handover-orders",
                   json={"order_no": "HO-C2", "window_ids": [1]})
        outcome["reg_code"] = r.status_code

    def do_revoke(barrier):
        c = TestClient(client.app)
        barrier.wait()
        r = c.post(f"/api/v1/certificates/{c1['id']}/revocation",
                   json={"reason": "并发撤销"})
        outcome["rev_code"] = r.status_code

    barrier = threading.Barrier(2)
    t1 = threading.Thread(target=do_register, args=(barrier,))
    t2 = threading.Thread(target=do_revoke, args=(barrier,))
    t1.start(); t2.start(); t1.join(); t2.join()

    assert outcome["rev_code"] == 201, outcome
    if outcome["reg_code"] == 409:
        # 撤销先提交：整单不留记录
        assert get_order(client, "HO-C2").status_code == 404
    else:
        # 登记先提交：随后查询必为失效（引用证书已撤销）
        assert outcome["reg_code"] == 201
        g = get_order(client, "HO-C2")
        assert g.status_code == 200 and g.json()["valid"] is False


def test_concurrent_same_order_registration_serializes_to_one_record(client):
    """两个并发请求以同一单号、同一窗口集合登记：至多产生一次 created，
    另一个因唯一约束转为 idempotent 重放，双方响应自洽。"""
    make_window(client)
    fill_pass(client, 1)
    issue(client, 1)

    outcome = {}

    def do_register(barrier, key):
        c = TestClient(client.app)
        barrier.wait()
        r = c.post("/api/v1/handover-orders",
                   json={"order_no": "HO-DUP", "window_ids": [1]})
        outcome[key] = (r.status_code, r.json().get("effect") if r.status_code == 201 else None)

    barrier = threading.Barrier(2)
    t1 = threading.Thread(target=do_register, args=(barrier, "a"))
    t2 = threading.Thread(target=do_register, args=(barrier, "b"))
    t1.start(); t2.start(); t1.join(); t2.join()

    effects = sorted(outcome[k][1] for k in ("a", "b"))
    assert all(code == 201 for code, _ in outcome.values()), outcome
    assert effects == ["created", "idempotent"], outcome
    g = get_order(client, "HO-DUP").json()
    assert g["window_count"] == 1 and g["valid"] is True
