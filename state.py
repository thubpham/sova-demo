from __future__ import annotations
from operator import add
from typing import Annotated, Optional, TypedDict
from langgraph.graph import add_messages

class OrderRequest(TypedDict):
    order_id: str; customer_id: str; sku: str; quantity: int
    due_date: str; budget_vnd: Optional[int]

class SalesCheck(TypedDict):
    hop_le: bool; cong_no_con_lai_vnd: int; ly_do: str

class StockCheck(TypedDict):
    ton_kho: int; thieu_hut: int; diem_dat_hang_lai: int

class DraftPO(TypedDict):
    po_id: str; vendor_id: str; sku: str; so_luong: int
    don_gia_vnd: int; thanh_tien_vnd: int; thoi_gian_giao_ngay: int
    can_phe_duyet: bool

class Approval(TypedDict):
    hanh_dong: str; nguoi_duyet: str; ghi_chu: str; thoi_diem: str

class BaseState(TypedDict):
    messages: Annotated[list, add_messages]
    request: OrderRequest
    audit: Annotated[list, add]

class PurchaseState(BaseState):
    route: Optional[str]
    sales_check: Optional[SalesCheck]
    stock_check: Optional[StockCheck]
    draft_po: Optional[DraftPO]
    approvals: Annotated[list, add]

def base_initial(request: OrderRequest) -> dict:
    return {"messages": [], "request": request, "audit": []}

def initial_state(request: OrderRequest) -> PurchaseState:
    return {**base_initial(request), "route": None, "sales_check": None,
            "stock_check": None, "draft_po": None, "approvals": []}
