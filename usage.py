import json
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from langchain_core.callbacks import BaseCallbackHandler

ROOT = Path(__file__).parent
USAGE_PATH = ROOT / "llm_usage.jsonl"
VN_TZ = timezone(timedelta(hours=7))

PRICES_PER_MTOK = {
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-4-5": (1.00, 5.00),
    "claude-opus-5-5": (4.00, 20.00),
}


def price_for(model: str) -> tuple[float, float] | None:
    for name, price in PRICES_PER_MTOK.items():
        if model.startswith(name):
            return price
    return None


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float | None:
    price = price_for(model)
    if price is None:
        return None
    return (input_tokens * price[0] + output_tokens * price[1]) / 1_000_000


def purpose_of(agent: str, message) -> str:
    calls = [c["name"] for c in getattr(message, "tool_calls", None) or []]
    if calls:
        return "gọi công cụ: " + ", ".join(calls)
    if agent == "orchestrator":
        return "chọn bước tiếp theo"
    return "trả kết quả cuối"


class UsageTracker(BaseCallbackHandler):
    def __init__(self, path: Path = USAGE_PATH):
        self.path = path
        self.started: dict = {}
        self.lock = threading.Lock()

    def on_chat_model_start(self, serialized, messages, *, run_id, parent_run_id=None, tags=None, metadata=None, **kwargs):
        self.started[run_id] = (time.perf_counter(), dict(metadata or {}))

    def on_llm_end(self, response, *, run_id, parent_run_id=None, tags=None, **kwargs):
        start, meta = self.started.pop(run_id, (time.perf_counter(), {}))
        message = response.generations[0][0].message
        usage = getattr(message, "usage_metadata", None) or {}
        model = (getattr(message, "response_metadata", None) or {}).get("model_name", "?")
        agent = meta.get("agent", "?")
        input_tokens, output_tokens = usage.get("input_tokens", 0), usage.get("output_tokens", 0)
        self._append({
            "thoi_diem": datetime.now(VN_TZ).isoformat(timespec="seconds"),
            "order_id": meta.get("order_id", "?"),
            "agent": agent,
            "model": model,
            "purpose": purpose_of(agent, message),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read_tokens": (usage.get("input_token_details") or {}).get("cache_read", 0),
            "latency_s": round(time.perf_counter() - start, 2),
            "cost_usd": cost_usd(model, input_tokens, output_tokens),
            "ok": True,
        })

    def on_llm_error(self, error, *, run_id, parent_run_id=None, tags=None, **kwargs):
        start, meta = self.started.pop(run_id, (time.perf_counter(), {}))
        self._append({
            "thoi_diem": datetime.now(VN_TZ).isoformat(timespec="seconds"),
            "order_id": meta.get("order_id", "?"),
            "agent": meta.get("agent", "?"),
            "model": "?",
            "purpose": f"lỗi: {type(error).__name__}",
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_read_tokens": 0,
            "latency_s": round(time.perf_counter() - start, 2),
            "cost_usd": 0.0,
            "ok": False,
        })

    def _append(self, record: dict) -> None:
        with self.lock, self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load(order_id: str | None = None) -> list[dict]:
    if not USAGE_PATH.exists():
        return []
    records = [json.loads(line) for line in USAGE_PATH.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in records if order_id is None or r["order_id"] == order_id]


def clear() -> None:
    USAGE_PATH.unlink(missing_ok=True)


def money(value: float | None) -> str:
    return "?" if value is None else f"${value:.4f}"


def totals(records: list[dict]) -> dict:
    known = [r["cost_usd"] for r in records if r["cost_usd"] is not None]
    return {
        "calls": len(records),
        "input": sum(r["input_tokens"] for r in records),
        "output": sum(r["output_tokens"] for r in records),
        "latency": sum(r["latency_s"] for r in records),
        "cost": sum(known) if len(known) == len(records) else None,
    }


def summary_line(records: list[dict]) -> str:
    t = totals(records)
    return (
        f"LLM: {t['calls']} lượt gọi, {t['input']:,} token vào / {t['output']:,} token ra, "
        f"{t['latency']:.1f}s, ước tính {money(t['cost'])}"
    )


def report(order_id: str | None = None) -> None:
    records = load(order_id)
    if not records:
        print("Chưa có lượt gọi LLM nào được ghi lại.")
        return

    header = f"{'#':>3}  {'Thời điểm':<8}  {'Đơn hàng':<14}  {'Agent':<17}  {'Model':<25}  {'Vào':>7}  {'Ra':>6}  {'Giây':>5}  {'Chi phí':>8}  Mục đích"
    print(header)
    print("-" * len(header))
    for i, r in enumerate(records, 1):
        print(
            f"{i:>3}  {r['thoi_diem'][11:19]:<8}  {r['order_id']:<14}  {r['agent']:<17}  {r['model']:<25}  "
            f"{r['input_tokens']:>7,}  {r['output_tokens']:>6,}  {r['latency_s']:>5.1f}  {money(r['cost_usd']):>8}  {r['purpose']}"
        )

    for label, key in (("THEO AGENT", "agent"), ("THEO ĐƠN HÀNG", "order_id"), ("THEO MODEL", "model")):
        groups = defaultdict(list)
        for r in records:
            groups[r[key]].append(r)
        print(f"\n{label}")
        for name, rows in groups.items():
            t = totals(rows)
            print(
                f"  {name:<25}  {t['calls']:>3} lượt  {t['input']:>8,} vào  {t['output']:>7,} ra  "
                f"{t['latency']:>6.1f}s  {money(t['cost']):>9}"
            )

    print("\nTỔNG: " + summary_line(records))
    print("Ghi chú: token ra của Sonnet 5.5 gồm cả phần suy nghĩ (thinking); chi phí ước tính theo bảng giá niêm yết, chưa tính cache.")
