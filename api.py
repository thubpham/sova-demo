import json
import threading
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from langchain_core.messages import BaseMessage
from langgraph.types import Command, Interrupt
from pydantic import BaseModel

import erp
import memory
import usage
from graph import Router, build_graph, identities, thread_config
from run import ORDERS, request, reset, thread_status
from state import initial_state

ROOT = Path(__file__).parent
UI_DIR = ROOT / "ui"

SCENARIOS = {
    "run1": {"label": "Đơn chính", "expected": "Thiếu 300, PO 840tr > 50tr → chờ duyệt"},
    "run3": {"label": "Đơn dùng bộ nhớ", "expected": "Thiếu 30, PO ~36tr → tự duyệt"},
    "reject": {"label": "Từ chối công nợ", "expected": "41tr > 20tr hạn mức → bỏ qua các bước còn lại"},
    "instock": {"label": "Đủ hàng", "expected": "Tồn 2000 → bỏ qua Thu mua"},
    "compare": {"label": "So sánh", "expected": "Hai router phải đi cùng thứ tự bước"},
}


class StartRun(BaseModel):
    scenario: Literal["run1", "run3", "reject", "instock"]
    router: Router = "fixed"


class Decision(BaseModel):
    hanh_dong: Literal["approve", "reject"]
    nguoi_duyet: str = "truongphong.thumua@besgroup.vn"
    ghi_chu: str = "Đồng ý theo đề xuất của bộ phận thu mua"


class Channel:
    def __init__(self):
        self.events: list[dict] = []
        self.closed = False
        self.cond = threading.Condition()

    def emit(self, event: dict, close: bool = False) -> None:
        with self.cond:
            self.events.append(event)
            self.closed = close
            self.cond.notify_all()

    def reopen(self) -> None:
        with self.cond:
            self.closed = False


class Runtime:
    def __init__(self):
        self.apps: dict[str, Any] = {}
        self.channels: dict[str, Channel] = {}
        self.busy = threading.Lock()
        self.guard = threading.Lock()

    def app(self, router: Router) -> Any:
        with self.guard:
            if router not in self.apps:
                self.apps[router] = build_graph(router)
            return self.apps[router]

    def drop_apps(self) -> None:
        with self.guard:
            for app in self.apps.values():
                app.checkpointer.conn.close()
                app.store.conn.close()
            self.apps = {}

    def channel(self, order_id: str) -> Channel:
        with self.guard:
            return self.channels.setdefault(order_id, Channel())


rt = Runtime()
api = FastAPI(title="Sova Agent Demo API")


def jsonable(value):
    if isinstance(value, BaseMessage):
        return {"name": value.name, "content": value.content}
    if isinstance(value, Interrupt):
        return jsonable(value.value)
    if isinstance(value, dict):
        return {k: jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    return value


def main_snapshots(app, order_id: str) -> list:
    return [s for s in app.checkpointer.list(thread_config(order_id)) if not s.config.get("configurable", {}).get("checkpoint_ns")]


def router_of(app, order_id: str) -> Router | None:
    snaps = main_snapshots(app, order_id)
    return snaps[0].metadata.get("router") if snaps else None


def run_view(app, order_id: str) -> dict:
    snapshot = app.get_state(thread_config(order_id))
    if not snapshot.values:
        raise HTTPException(404, f"Không có đơn {order_id}")
    snaps = main_snapshots(app, order_id)
    interrupts = [jsonable(i) for i in snapshot.interrupts]
    return {
        "order_id": order_id,
        "thread_id": f"don-hang:{order_id}",
        "router": snaps[0].metadata.get("router") if snaps else None,
        "status": thread_status(snaps[0]) if snaps else "đang dở",
        "next": list(snapshot.next),
        "snapshots": len(snaps),
        "interrupt": interrupts[0] if interrupts else None,
        "state": jsonable(snapshot.values),
        "usage": usage.summary_line(usage.load(order_id)),
    }


def execute(router: Router, order_id: str, payload, channel: Channel) -> None:
    try:
        app = rt.app(router)
        cfg = thread_config(order_id, router)
        for chunk in app.stream(payload, cfg, stream_mode="updates", durability="sync"):
            for node, update in chunk.items():
                if node != "__interrupt__":
                    channel.emit({"type": "step", "node": node, "update": jsonable(update), "run": run_view(app, order_id)})
        view = run_view(app, order_id)
        kind = "paused" if view["next"] else "done"
        channel.emit({"type": kind, "run": view}, close=True)
    except Exception as e:
        channel.emit({"type": "error", "message": f"{type(e).__name__}: {e}"}, close=True)
    finally:
        rt.busy.release()


def launch(router: Router, order_id: str, payload, first_event: dict) -> Channel:
    if not rt.busy.acquire(blocking=False):
        raise HTTPException(409, "Đang có một đơn chạy. Đợi đơn đó xong rồi thử lại.")
    channel = rt.channel(order_id)
    channel.reopen()
    channel.emit(first_event)
    threading.Thread(target=execute, args=(router, order_id, payload, channel), daemon=True).start()
    return channel


def start_order(scenario: str, router: Router, order_id: str | None = None) -> str:
    req = request(scenario, order_id)
    if rt.app(router).get_state(thread_config(req["order_id"])).values:
        raise HTTPException(409, f"Đơn {req['order_id']} đã có checkpoint. Đặt lại demo để chạy lại.")
    launch(router, req["order_id"], initial_state(req), {"type": "start", "router": router, "request": dict(req)})
    return req["order_id"]


@api.get("/api/scenarios")
def scenarios() -> list[dict]:
    result = []
    for key, order in ORDERS.items():
        customer = erp.query("SELECT ten_cong_ty FROM customers WHERE customer_id = ?", (order["customer_id"],))[0]
        product = erp.query("SELECT ten_san_pham, don_vi, gia_ban_vnd FROM products WHERE sku = ?", (order["sku"],))[0]
        result.append({"key": key, **order, **SCENARIOS[key], "ten_cong_ty": customer["ten_cong_ty"], **product})
    return result


@api.get("/api/agents")
def agents() -> list[dict]:
    return list(identities().values())


@api.post("/api/runs")
def create_run(body: StartRun) -> dict:
    order_id = start_order(body.scenario, body.router)
    return {"order_id": order_id, "events": f"/api/runs/{order_id}/events"}


@api.get("/api/runs/{order_id}")
def get_run(order_id: str) -> dict:
    return run_view(rt.app("fixed"), order_id)


@api.get("/api/runs/{order_id}/events")
def run_events(order_id: str) -> StreamingResponse:
    with rt.guard:
        channel = rt.channels.get(order_id)
    if channel is None:
        raise HTTPException(404, f"Không có luồng sự kiện cho {order_id}; dùng GET /api/runs/{order_id}")

    def stream():
        sent = 0
        while True:
            with channel.cond:
                if sent >= len(channel.events) and not channel.closed:
                    channel.cond.wait(timeout=15)
                batch, closed = channel.events[sent:], channel.closed
            for event in batch:
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            sent += len(batch)
            if closed and not batch:
                return
            if not batch:
                yield ": ping\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@api.post("/api/runs/{order_id}/resume")
def resume_run(order_id: str, body: Decision) -> dict:
    app = rt.app("fixed")
    if not app.get_state(thread_config(order_id)).next:
        raise HTTPException(409, f"Đơn {order_id} không ở trạng thái chờ duyệt")
    router = router_of(app, order_id) or "fixed"
    decision = body.model_dump()
    launch(router, order_id, Command(resume=decision), {"type": "resume", "decision": decision})
    return {"order_id": order_id, "events": f"/api/runs/{order_id}/events"}


@api.post("/api/runs/{order_id}/restart")
def restart_run(order_id: str) -> dict:
    if rt.busy.locked():
        raise HTTPException(409, "Đang có một đơn chạy")
    rt.drop_apps()
    view = run_view(rt.app("fixed"), order_id)
    return {"next": view["next"], "snapshots": view["snapshots"], "audit_lines": len(view["state"]["audit"]), "run": view}


@api.post("/api/compare")
def compare() -> dict:
    base = ORDERS["compare"]["order_id"]
    ids: dict[Router, str] = {"fixed": f"{base}-fixed", "llm": f"{base}-llm"}
    for order_id in ids.values():
        if rt.app("fixed").get_state(thread_config(order_id)).values:
            raise HTTPException(409, f"Đơn {order_id} đã có checkpoint. Đặt lại demo để so sánh lại.")

    for router, order_id in ids.items():
        rt.channel(order_id).emit({"type": "queued", "router": router})

    def both():
        for router, order_id in ids.items():
            channel = rt.channel(order_id)
            with rt.busy:
                pass
            try:
                start_order("compare", router, order_id)
            except HTTPException as e:
                channel.emit({"type": "error", "message": e.detail}, close=True)
                continue
            with channel.cond:
                while not channel.closed:
                    channel.cond.wait()

    threading.Thread(target=both, daemon=True).start()
    return {r: {"order_id": o, "events": f"/api/runs/{o}/events"} for r, o in ids.items()}


@api.get("/api/inspect")
def inspect() -> dict:
    app = rt.app("fixed")
    latest, counts = {}, {}
    for snap in app.checkpointer.list(None):
        cfg = snap.config.get("configurable", {})
        if cfg.get("checkpoint_ns"):
            continue
        counts[cfg["thread_id"]] = counts.get(cfg["thread_id"], 0) + 1
        latest.setdefault(cfg["thread_id"], snap)
    threads = [
        {"thread_id": t, "router": s.metadata.get("router"), "snapshots": counts[t], "status": thread_status(s)}
        for t, s in latest.items()
    ]
    store = app.store
    mem = {}
    for namespace in memory.SCHEMAS:
        seed = memory.SEED.get(namespace, {})
        mem[namespace] = [
            {
                "key": item.key,
                "value": item.value,
                "flag": "MỚI" if item.key not in seed else ("CẬP NHẬT" if item.value != seed[item.key] else None),
            }
            for item in store.search((namespace,), limit=1000)
        ]
    erp_data = {
        "customers": erp.query("SELECT * FROM customers"),
        "inventory": erp.query("SELECT i.*, p.ten_san_pham, p.don_vi FROM inventory i JOIN products p USING (sku)"),
        "vendors": [{**v, "co_hoa_don_gtgt": bool(v["co_hoa_don_gtgt"])} for v in erp.query("SELECT * FROM vendors")],
        "purchase_orders": erp.query("SELECT * FROM purchase_orders ORDER BY po_id"),
    }
    return {"threads": threads, "memory": mem, "erp": erp_data}


@api.get("/api/usage")
def get_usage(order: str | None = None) -> dict:
    records = usage.load(order)
    return {
        "records": records,
        "totals": usage.totals(records),
        "groups": usage.grouped(records),
        "summary": usage.summary_line(records) if records else None,
        "orders": sorted({r["order_id"] for r in usage.load()}),
    }


@api.post("/api/reset")
def reset_demo() -> dict:
    if not rt.busy.acquire(blocking=False):
        raise HTTPException(409, "Đang có một đơn chạy")
    try:
        rt.drop_apps()
        with rt.guard:
            rt.channels = {}
        reset()
    finally:
        rt.busy.release()
    return {"ok": True}


app = api
if (UI_DIR / "index.html").exists():
    app.mount("/", StaticFiles(directory=UI_DIR, html=True), name="ui")
