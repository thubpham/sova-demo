import argparse
import json
import sqlite3
from collections import Counter

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

import erp
import memory
import usage
from graph import Router, build_graph, thread_config
from state import OrderRequest, initial_state

DUE_DATE = "2026-10-20"

ORDERS = {
    "run1": {"order_id": "DH-1001", "customer_id": "KH-001", "sku": "SP-123", "quantity": 500},
    "run3": {"order_id": "DH-1002", "customer_id": "KH-001", "sku": "SP-456", "quantity": 80},
    "reject": {"order_id": "DH-1003", "customer_id": "KH-003", "sku": "SP-789", "quantity": 5},
    "instock": {"order_id": "DH-1004", "customer_id": "KH-002", "sku": "SP-101", "quantity": 300},
    "compare": {"order_id": "DH-2001", "customer_id": "KH-002", "sku": "SP-456", "quantity": 70},
}


def request(name: str, order_id: str | None = None) -> OrderRequest:
    order = ORDERS[name]
    return OrderRequest(
        order_id=order_id or order["order_id"],
        customer_id=order["customer_id"],
        sku=order["sku"],
        quantity=order["quantity"],
        due_date=DUE_DATE,
        budget_vnd=None,
    )


def banner(title: str) -> None:
    print(f"\n{'=' * 80}\n{title}\n{'=' * 80}")


def show_json(label: str, value) -> None:
    print(f"{label}:\n{json.dumps(value, ensure_ascii=False, indent=2)}")


def show_audit(entries: list[dict]) -> None:
    for e in entries:
        print(f"  [{e['node']}] {e['detail']}")


def reset() -> None:
    for path in memory.DB_PATH.parent.glob(memory.DB_PATH.name + "*"):
        path.unlink()
    erp.build(reset=True)
    memory.seed(memory.get_store())
    usage.clear()
    print("Đã đặt lại: erp.sqlite (từ mock_data.json), checkpoint và bộ nhớ (seed).")


def start(router: Router, req: OrderRequest) -> dict | None:
    app, cfg = build_graph(router), thread_config(req["order_id"], router)
    if app.get_state(cfg).values:
        print(f"Đơn {req['order_id']} đã có checkpoint. Dùng `run2` để tiếp tục hoặc `--reset` để làm lại.")
        return None
    show_json("Đơn hàng", req)
    out = app.invoke(initial_state(req), cfg)
    print("Audit:")
    show_audit(out["audit"])
    print(usage.summary_line(usage.load(req["order_id"])))
    if out.get("__interrupt__"):
        show_json("Tạm dừng chờ duyệt (interrupt payload)", out["__interrupt__"][0].value)
        print(f"Checkpoint đã lưu cho thread {cfg.get('configurable', {})['thread_id']}. Chạy `python run.py run2` để tiếp tục.")
    return out


def run1(router: Router) -> None:
    banner(f"RUN 1: đặt đơn chính, dừng ở bước phê duyệt (router={router})")
    start(router, request("run1"))


def run2(router: Router, decision: str, approver: str, note: str) -> None:
    banner(f"RUN 2: tiếp tục cùng thread sau khi khởi động lại (router={router})")
    app = build_graph(router)
    cfg = thread_config(ORDERS["run1"]["order_id"], router)
    snapshot = app.get_state(cfg)
    if not snapshot.next:
        print("Không có đơn nào đang chờ duyệt. Chạy `run1` trước.")
        return
    seen = len(snapshot.values["audit"])
    print(f"Nạp checkpoint: bước tiếp theo = {snapshot.next}, đã có {seen} dòng audit.")
    resume = {"hanh_dong": decision, "nguoi_duyet": approver, "ghi_chu": note}
    show_json("Quyết định phê duyệt", resume)
    out = app.invoke(Command(resume=resume), cfg)
    print("Audit (phần tiếp theo):")
    show_audit(out["audit"][seen:])
    po = out["draft_po"]
    store = memory.get_store()
    show_json(f"Bộ nhớ nha_cung_cap/{po['vendor_id']}", memory.read(store, "procurement_agent", "nha_cung_cap", po["vendor_id"]))
    show_json(f"Bộ nhớ quyet_dinh/{ORDERS['run1']['order_id']}", memory.read(store, "procurement_agent", "quyet_dinh", ORDERS["run1"]["order_id"]))


def run3(router: Router) -> None:
    banner(f"RUN 3: đơn mới, cùng khách hàng, SKU khác; thu mua nhớ lại run 1 (router={router})")
    store = memory.get_store()
    recalled = {k: v for k, v in memory.read(store, "procurement_agent", "quyet_dinh").items() if v["customer_id"] == ORDERS["run3"]["customer_id"]}
    show_json("Quyết định trước đây của khách hàng này trong bộ nhớ", recalled or "(chưa có, hãy chạy run1 + run2 trước)")
    start(router, request("run3"))


def extra(name: str, title: str, router: Router) -> None:
    banner(f"{title} (router={router})")
    start(router, request(name))


def route_path(audit: list[dict]) -> list[str]:
    return [e["node"] for e in audit if e["node"] != "orchestrator"]


def compare() -> None:
    banner("SO SÁNH ROUTER: cùng một đơn, fixed vs llm")
    paths = {}
    for router in ("fixed", "llm"):
        req = request("compare", f"{ORDERS['compare']['order_id']}-{router}")
        print(f"\n--- router={router}")
        out = start(router, req)
        if out is None:
            return
        paths[router] = route_path(out["audit"])
    for router, path in paths.items():
        print(f"{router:>5}: {' → '.join(path)}")
    print("Kết quả:", "khớp" if paths["fixed"] == paths["llm"] else "khác nhau")


def thread_status(snapshot) -> str:
    if any(w[1] == "__interrupt__" for w in snapshot.pending_writes or []):
        return "đang chờ duyệt"
    values = snapshot.checkpoint["channel_values"]
    audit = values.get("audit", [])
    if not audit or audit[-1]["node"] != "finalize":
        return "đang dở"
    sales, approvals = values.get("sales_check"), values.get("approvals", [])
    if (sales and not sales["hop_le"]) or (approvals and approvals[-1]["hanh_dong"] == "reject"):
        return "từ chối"
    return "hoàn tất"


def inspect_db(order_id: str | None) -> None:
    if not memory.DB_PATH.exists():
        print(f"Chưa có {memory.DB_PATH.name}. Chạy một lệnh (vd. `run1`) trước.")
        return
    banner(f"BỘ NHỚ DÀI HẠN: {memory.DB_PATH.name} → bảng `store` (namespace / key → JSON)")
    store = memory.get_store()
    for namespace in memory.SCHEMAS:
        for item in store.search((namespace,), limit=1000):
            print(f"  {namespace} / {item.key}: {json.dumps(item.value, ensure_ascii=False)}")

    banner(f"CHECKPOINT: {memory.DB_PATH.name} → bảng `checkpoints` (một ảnh chụp state sau mỗi bước)")
    saver = SqliteSaver(sqlite3.connect(memory.DB_PATH, check_same_thread=False))
    counts, latest = Counter(), {}
    for snapshot in saver.list(None):
        if snapshot.config.get("configurable", {}).get("checkpoint_ns"):
            continue
        thread_id = snapshot.config.get("configurable", {})["thread_id"]
        counts[thread_id] += 1
        latest.setdefault(thread_id, snapshot)
    if not latest:
        print("  (chưa có đơn nào)")
    for thread_id, snapshot in latest.items():
        print(f"  {thread_id}: {counts[thread_id]} ảnh chụp, {thread_status(snapshot)}")

    if order_id:
        thread_id = thread_config(order_id).get("configurable", {})["thread_id"]
        if thread_id not in latest:
            print(f"\nKhông tìm thấy {thread_id}.")
            return
        values = dict(latest[thread_id].checkpoint["channel_values"])
        values["messages"] = [m.content for m in values.get("messages", [])]
        banner(f"STATE MỚI NHẤT CỦA {thread_id}")
        print(json.dumps(values, ensure_ascii=False, indent=2, default=str))


def run_all(router: Router, decision: str, approver: str, note: str) -> None:
    reset()
    run1(router)
    run2(router, decision, approver, note)
    run3(router)
    extra("reject", "THÊM: khách vượt hạn mức công nợ", router)


def main() -> None:
    parser = argparse.ArgumentParser(description="Sova agent demo")
    parser.add_argument("command", choices=["run1", "run2", "run3", "reject", "instock", "compare", "all", "reset", "inspect", "usage"])
    parser.add_argument("--router", choices=["fixed", "llm"], default="fixed")
    parser.add_argument("--reset", action="store_true", help="đặt lại ERP, checkpoint và bộ nhớ trước khi chạy")
    parser.add_argument("--decision", choices=["approve", "reject"], default="approve")
    parser.add_argument("--approver", default="truongphong.thumua@besgroup.vn")
    parser.add_argument("--note", default="Đồng ý theo đề xuất của bộ phận thu mua")
    parser.add_argument("--order", help="lọc theo mã đơn hàng (dùng với inspect, usage)")
    args = parser.parse_args()

    if args.reset or args.command == "reset":
        reset()
    if args.command == "run1":
        run1(args.router)
    elif args.command == "run2":
        run2(args.router, args.decision, args.approver, args.note)
    elif args.command == "run3":
        run3(args.router)
    elif args.command == "reject":
        extra("reject", "THÊM: khách vượt hạn mức công nợ", args.router)
    elif args.command == "instock":
        extra("instock", "THÊM: đủ hàng, không cần thu mua", args.router)
    elif args.command == "compare":
        compare()
    elif args.command == "all":
        run_all(args.router, args.decision, args.approver, args.note)
    elif args.command == "inspect":
        inspect_db(args.order)
    elif args.command == "usage":
        banner("LƯỢT GỌI LLM" + (f" CỦA ĐƠN {args.order}" if args.order else ""))
        usage.report(args.order)


if __name__ == "__main__":
    main()
