from fastapi import APIRouter
from pydantic import BaseModel
from typing import List

from app.state.var_batch import upsert_var_batch

router = APIRouter(prefix="/ingest", tags=["ingest"])


class VarQuote(BaseModel):
    base: str
    notional_usd: float
    bid: float
    ask: float
    mid: float | None = None
    qty_used: float | None = None


class VarBatchPayload(BaseModel):
    quotes: List[VarQuote]


@router.post("/var_batch")
async def ingest_var_batch(payload: VarBatchPayload):
    """
    Receive batch indicative quotes from Tampermonkey.
    """
    try:
        print("\n[ingest/var_batch] quotes count =", len(payload.quotes), flush=True)

        if payload.quotes:
            q0 = payload.quotes[0]

            # 兼容 pydantic v1 / v2
            q0_dict = q0.dict() if hasattr(q0, "dict") else q0.model_dump()

            print("[ingest/var_batch] first quote =", q0_dict, flush=True)

        rows = []
        for q in payload.quotes:
            rows.append(q.dict() if hasattr(q, "dict") else q.model_dump())

        upsert_var_batch(rows)

        return {"ok": True, "count": len(payload.quotes)}
    except Exception as e:
        print("[ingest/var_batch] ERROR:", repr(e), flush=True)
        raise


