import json
import sqlite3
from datetime import datetime, timedelta, timezone
from functools import cache
from typing import Literal, TypedDict

from dotenv import load_dotenv
from langchain.agents import create_agent
from langchain.agents.structured_output import ProviderStrategy
from langchain.chat_models import init_chat_model
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.store.base import BaseStore
from langgraph.types import interrupt

import erp
import memory
from state import Approval, DraftPO, PurchaseState, SalesCheck, StockCheck
from tools import TOOLS, set_po_status
from usage import UsageTracker

load_dotenv(memory.ROOT / ".env")

VN_TZ = timezone(timedelta(hours=7))
ROUTES = ("sales", "inventory", "procurement", "finalize")
Router = Literal["fixed", "llm"]


class ProcurementResult(TypedDict):
    po_id: str; vendor_id: str; ly_do: str


class RouteDecision(TypedDict):
    route: Literal["sales", "inventory", "procurement", "finalize"]; ly_do: str


LY_DO_LIMITS = {
    "orchestrator": "tối đa 2 câu: bước tiếp theo và lý do chính",
    "sales_agent": "tối đa 2 câu: kết luận và lý do chính",
    "procurement_agent": "tối đa 3 câu: nhà cung cấp được chọn, 1–2 yếu tố quyết định, mã đơn hàng trước đây nếu bộ nhớ ảnh hưởng",
}

RESPONSE_FORMATS = {
    "orchestrator": RouteDecision,
    "sales_agent": SalesCheck,
    "inventory_agent": StockCheck,
    "procurement_agent": ProcurementResult,
}


@cache
def identities() -> dict[str, dict]:
    data = json.loads(memory.IDENTITIES_PATH.read_text(encoding="utf-8"))
    return {a["id"]: a for a in data["agents"]}


def now() -> str:
    return datetime.now(VN_TZ).isoformat(timespec="seconds")


def log(node: str, detail: str) -> dict:
    return {"node": node, "detail": detail, "thoi_diem": now()}


def to_json(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2)


def system_prompt(agent: dict) -> str:
    rules = "\n".join(f"- {c}" for c in agent["constraints"])
    if agent["approval_threshold_vnd"] is not None:
        rules += (
            f"\n- Ngưỡng phê duyệt: đơn mua trên {agent['approval_threshold_vnd']:,} VND phải được {agent['owner']} duyệt; "
            "hệ thống tự động chuyển duyệt, bạn chỉ cần tạo đơn nháp."
        )
    return (
        f"Bạn là {agent['role']}.\n{agent['backstory']}\n\n"
        f"Mục tiêu: {agent['goal']}\n\n"
        f"Nguyên tắc bắt buộc:\n{rules}\n"
        "- Chỉ sử dụng số liệu trả về từ công cụ, không phỏng đoán. Đơn vị tiền tệ: VND."
    )


def build_agent(agent: dict):
    return create_agent(
        init_chat_model(agent["model"]),
        tools=[TOOLS[name] for name in agent["scoped_tools"]],
        system_prompt=system_prompt(agent),
        response_format=ProviderStrategy(RESPONSE_FORMATS[agent["id"]]),
        name=agent["id"],
    )


def task_message(agent_id: str, store: BaseStore, state: PurchaseState, task: str, prior: tuple[str, ...] = ()) -> str:
    parts = [
        "Bộ nhớ dài hạn (đọc trước; chỉ để tham khảo, giá, tồn kho và công nợ hiện hành phải lấy từ công cụ):\n"
        + to_json(memory.read_for_agent(store, agent_id)),
        "Đơn hàng:\n" + to_json(state["request"]),
    ]
    done = {k: state[k] for k in prior if state.get(k)}
    if done:
        parts.append("Kết quả các bước trước:\n" + to_json(done))
    parts.append(f"Nhiệm vụ: {task}")
    if agent_id in LY_DO_LIMITS:
        parts.append(f"Trường ly_do: {LY_DO_LIMITS[agent_id]}.")
    return "\n\n".join(parts)


def fixed_route(state: PurchaseState) -> tuple[str, str]:
    sales, stock = state["sales_check"], state["stock_check"]
    if sales is None:
        return "sales", "Chưa kiểm tra công nợ"
    if not sales["hop_le"]:
        return "finalize", "Công nợ không hợp lệ, bỏ qua tồn kho và thu mua"
    if stock is None:
        return "inventory", "Công nợ hợp lệ, chưa kiểm tra tồn kho"
    if stock["thieu_hut"] > 0 and state["draft_po"] is None:
        return "procurement", "Có thiếu hụt và chưa có đơn mua"
    if state["draft_po"] is None:
        return "finalize", "Đủ hàng, không cần thu mua"
    return "finalize", "Đơn mua đã được quyết định"


def route_error(state: PurchaseState, route: str) -> str | None:
    sales, stock, po = state["sales_check"], state["stock_check"], state["draft_po"]
    if route not in ROUTES:
        return "tuyến không tồn tại"
    if route == "sales" and sales is not None:
        return "đã kiểm tra công nợ"
    if route == "inventory" and stock is not None:
        return "đã kiểm tra tồn kho"
    if route == "procurement":
        if not (sales and sales["hop_le"]):
            return "công nợ chưa được xác nhận hợp lệ"
        if stock is None or stock["thieu_hut"] <= 0:
            return "không có lượng thiếu hụt cần mua"
        if po is not None:
            return "đã có đơn mua hàng"
    if route == "finalize":
        if sales is None:
            return "chưa kiểm tra công nợ"
        if sales["hop_le"] and stock is None:
            return "chưa kiểm tra tồn kho"
        if sales["hop_le"] and stock is not None and stock["thieu_hut"] > 0 and po is None:
            return "còn thiếu hụt chưa được thu mua"
    return None


class PurchaseWorkflow:
    def __init__(self, router: Router = "fixed"):
        self.router = router
        ids = ["sales_agent", "inventory_agent", "procurement_agent"] + (["orchestrator"] if router == "llm" else [])
        self.agents = {i: build_agent(identities()[i]) for i in ids}
        self.tracker = UsageTracker()

    def run_agent(self, agent_id: str, content: str, state: PurchaseState) -> dict:
        config: RunnableConfig = {
            "recursion_limit": 2 * identities()[agent_id]["max_iter"] + 1,
            "callbacks": [self.tracker],
            "metadata": {"agent": agent_id, "order_id": state["request"]["order_id"]},
        }
        result = self.agents[agent_id].invoke({"messages": [HumanMessage(content)]}, config)
        return result["structured_response"]

    def orchestrator(self, state: PurchaseState, *, store: BaseStore) -> dict:
        cap = identities()["orchestrator"]["max_iter"]
        if sum(e["node"] == "orchestrator" for e in state["audit"]) >= cap:
            return {"route": END, "audit": [log("orchestrator", f"Dừng: đạt giới hạn {cap} bước điều phối")]}
        if self.router == "fixed":
            (route, reason), entries = fixed_route(state), []
        else:
            task = (
                "Chọn bước tiếp theo: sales (kiểm tra khách hàng và công nợ), inventory (kiểm tra tồn kho), "
                "procurement (thu mua lượng thiếu hụt), finalize (kết thúc và ghi nhận). "
                "Mỗi bước chỉ chạy một lần. Trả về route và lý do ngắn gọn."
            )
            content = task_message("orchestrator", store, state, task, ("sales_check", "stock_check", "draft_po", "approvals"))
            decision = self.run_agent("orchestrator", content, state)
            route, reason, entries = decision["route"], decision["ly_do"], []
            error = route_error(state, route)
            if error:
                entries.append(log("orchestrator", f"Từ chối tuyến {route}: {error}"))
                route, rule = fixed_route(state)
                reason = f"dự phòng theo quy tắc cố định: {rule}"
        entries.append(log("orchestrator", f"→ {route} ({self.router}): {reason}"))
        return {"route": route, "audit": entries}

    def sales(self, state: PurchaseState, *, store: BaseStore) -> dict:
        r = state["request"]
        task = f"Kiểm tra khách hàng {r['customer_id']} và điều khoản công nợ cho đơn {r['quantity']} {r['sku']}."
        result = self.run_agent("sales_agent", task_message("sales_agent", store, state, task), state)
        return {
            "sales_check": result,
            "messages": [AIMessage(to_json(result), name="sales_agent")],
            "audit": [log("sales", f"hop_le={result['hop_le']}: {result['ly_do']}")],
        }

    def inventory(self, state: PurchaseState, *, store: BaseStore) -> dict:
        r = state["request"]
        task = f"Kiểm tra tồn kho và điểm đặt hàng lại của {r['sku']}; tính lượng thiếu hụt so với nhu cầu {r['quantity']}."
        result = self.run_agent("inventory_agent", task_message("inventory_agent", store, state, task, ("sales_check",)), state)
        row = erp.query("SELECT ton_kho, diem_dat_hang_lai FROM inventory WHERE sku = ?", (r["sku"],))[0]
        actual = StockCheck(ton_kho=row["ton_kho"], thieu_hut=max(0, r["quantity"] - row["ton_kho"]), diem_dat_hang_lai=row["diem_dat_hang_lai"])
        entries = []
        if dict(result) != dict(actual):
            entries.append(log("inventory", f"Hiệu chỉnh theo ERP: agent báo {result}, ERP {actual}"))
        entries.append(log("inventory", f"ton_kho={actual['ton_kho']}, thieu_hut={actual['thieu_hut']}, diem_dat_hang_lai={actual['diem_dat_hang_lai']}"))
        return {"stock_check": actual, "messages": [AIMessage(to_json(actual), name="inventory_agent")], "audit": entries}

    def procurement(self, state: PurchaseState, *, store: BaseStore) -> dict:
        r, stock = state["request"], state["stock_check"]
        assert stock is not None
        task = (
            f"Chọn nhà cung cấp phù hợp nhất và tạo đúng một đơn mua hàng cho {stock['thieu_hut']} {r['sku']} "
            "(tăng lên số lượng tối thiểu của nhà cung cấp nếu cần). "
            "Xem xét các quyết định trước đây (quyet_dinh) và đánh giá nhà cung cấp (nha_cung_cap) trong bộ nhớ dài hạn; "
            "nếu chúng ảnh hưởng đến lựa chọn, trích dẫn mã đơn hàng liên quan. "
            "Trả về po_id, vendor_id và lý do chọn."
        )
        result = self.run_agent("procurement_agent", task_message("procurement_agent", store, state, task, ("sales_check", "stock_check")), state)
        rows = erp.query("SELECT * FROM purchase_orders WHERE po_id = ?", (result["po_id"],))
        if not rows:
            raise RuntimeError(f"Agent trả về đơn mua hàng không tồn tại: {result['po_id']}")
        po = rows[0]
        if po["sku"] != r["sku"] or po["so_luong"] < stock["thieu_hut"] or po["trang_thai"] != "nhap":
            raise RuntimeError(f"Đơn mua hàng {po['po_id']} không khớp nhu cầu: {po}")
        threshold = identities()["procurement_agent"]["approval_threshold_vnd"]
        draft = DraftPO(
            po_id=po["po_id"],
            vendor_id=po["vendor_id"],
            sku=po["sku"],
            so_luong=po["so_luong"],
            don_gia_vnd=po["don_gia_vnd"],
            thanh_tien_vnd=po["thanh_tien_vnd"],
            thoi_gian_giao_ngay=po["thoi_gian_giao_ngay"],
            can_phe_duyet=threshold is not None and po["thanh_tien_vnd"] > threshold,
            ly_do=result["ly_do"],
        )
        return {
            "draft_po": draft,
            "messages": [AIMessage(to_json(draft), name="procurement_agent")],
            "audit": [log("procurement", f"{draft['po_id']} {draft['vendor_id']} {draft['so_luong']} × {draft['don_gia_vnd']:,} = {draft['thanh_tien_vnd']:,} VND: {draft['ly_do']}")],
        }

    def approval_gate(self, state: PurchaseState) -> dict:
        po, agent = state["draft_po"], identities()["procurement_agent"]
        assert po is not None
        if po["can_phe_duyet"]:
            decision = interrupt({
                "po": po,
                "nguong_phe_duyet_vnd": agent["approval_threshold_vnd"],
                "nguoi_phu_trach": agent["owner"],
                "cau_hoi": f"Duyệt đơn mua {po['po_id']} trị giá {po['thanh_tien_vnd']:,} VND?",
            })
        else:
            decision = {"hanh_dong": "approve", "nguoi_duyet": "he_thong", "ghi_chu": "Tự động duyệt: dưới ngưỡng phê duyệt"}
        if decision.get("hanh_dong") not in ("approve", "reject"):
            raise ValueError(f"Quyết định phê duyệt không hợp lệ: {decision}")
        set_po_status(po["po_id"], "da_duyet" if decision["hanh_dong"] == "approve" else "tu_choi")
        approval = Approval(
            hanh_dong=decision["hanh_dong"],
            nguoi_duyet=decision.get("nguoi_duyet", ""),
            ghi_chu=decision.get("ghi_chu", ""),
            thoi_diem=now(),
        )
        detail = (
            f"{po['po_id']} {po['thanh_tien_vnd']:,} VND (ngưỡng {agent['approval_threshold_vnd']:,} VND): "
            f"{approval['hanh_dong']} bởi {approval['nguoi_duyet']}"
        )
        return {"approvals": [approval], "audit": [log("approval_gate", detail)]}

    def finalize(self, state: PurchaseState, *, store: BaseStore) -> dict:
        r, sales, po = state["request"], state["sales_check"], state["draft_po"]
        approval = state["approvals"][-1] if state["approvals"] else None
        if po and approval is None:
            raise RuntimeError(f"Đơn mua hàng {po['po_id']} chưa được duyệt")
        if not (sales and sales["hop_le"]):
            reason = sales["ly_do"] if sales else "chưa kiểm tra công nợ"
            return {"audit": [log("finalize", f"Không tiếp nhận đơn {r['order_id']}: {reason}")]}

        customer = memory.read(store, "sales_agent", "khach_hang", r["customer_id"]) or {
            "uu_tien_giao_hang": "Chưa ghi nhận", "san_pham_hay_mua": [], "ghi_chu": ""
        }
        if r["sku"] not in customer["san_pham_hay_mua"]:
            customer = {**customer, "san_pham_hay_mua": [*customer["san_pham_hay_mua"], r["sku"]]}
        memory.write(store, "sales_agent", "khach_hang", r["customer_id"], customer)

        if not po:
            return {"audit": [log("finalize", f"Đơn {r['order_id']} đủ hàng, không cần thu mua")]}

        assert approval is not None
        approved = approval["hanh_dong"] == "approve"
        memory.write(store, "procurement_agent", "quyet_dinh", r["order_id"], {
            "customer_id": r["customer_id"],
            "nha_cung_cap_da_chon": po["vendor_id"],
            "sku": po["sku"],
            "so_tien_vnd": po["thanh_tien_vnd"],
            "nguoi_duyet": approval["nguoi_duyet"],
            "ly_do": po["ly_do"] if approved else f"Bị từ chối: {approval['ghi_chu']}",
            "thoi_diem": now(),
        })
        if approved:
            vendor = memory.read(store, "procurement_agent", "nha_cung_cap", po["vendor_id"]) or {
                "ten_ncc": erp.query("SELECT ten_ncc FROM vendors WHERE vendor_id = ?", (po["vendor_id"],))[0]["ten_ncc"],
                "do_tin_cay": 0.7,
            }
            memory.write(store, "procurement_agent", "nha_cung_cap", po["vendor_id"], {
                **vendor,
                "uu_tien": 1,
                "do_tin_cay": round(min(1.0, vendor["do_tin_cay"] + 0.05), 2),
                "lan_chon_gan_nhat": now()[:10],
                "ghi_chu": f"Được chọn cho đơn {r['order_id']} ({po['sku']}): {po['ly_do']}",
            })
        outcome = "đã duyệt" if approved else "bị từ chối"
        return {"audit": [log("finalize", f"Đơn {r['order_id']}: {po['po_id']} {outcome}; đã ghi bộ nhớ")]}

    def build(self):
        graph = StateGraph(PurchaseState)
        for name in ("orchestrator", "sales", "inventory", "procurement", "approval_gate", "finalize"):
            graph.add_node(name, getattr(self, name))
        graph.add_edge(START, "orchestrator")
        graph.add_conditional_edges("orchestrator", lambda s: s["route"], {**{r: r for r in ROUTES}, END: END})
        graph.add_edge("sales", "orchestrator")
        graph.add_edge("inventory", "orchestrator")
        graph.add_edge("procurement", "approval_gate")
        graph.add_edge("approval_gate", "orchestrator")
        graph.add_edge("finalize", END)
        store = memory.get_store()
        memory.seed(store)
        checkpointer = SqliteSaver(sqlite3.connect(memory.DB_PATH, check_same_thread=False))
        return graph.compile(checkpointer=checkpointer, store=store)


def build_graph(router: Router = "fixed"):
    return PurchaseWorkflow(router).build()


def thread_config(order_id: str, router: Router | None = None) -> RunnableConfig:
    config: RunnableConfig = {"configurable": {"thread_id": f"don-hang:{order_id}"}, "recursion_limit": 60}
    if router:
        config["metadata"] = {"router": router}
    return config
