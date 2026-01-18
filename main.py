from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import sys

# Optional: MetaTrader5 is Windows-only.
try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False

from mangum import Mangum

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Data Models ---
class OrderRequest(BaseModel):
    symbol: str
    type: str # 'BUY' or 'SELL'
    volume: float
    sl: Optional[float] = None
    tp: Optional[float] = None
    comment: Optional[str] = "GainZAlgo Signal"

# --- Routes ---

@app.get("/autoconnect")
async def autoconnect():
    if not MT5_AVAILABLE:
        return {"success": False, "error": "MT5 Library not available on this platform (Linux/Cloud)."}
    
    if not mt5.initialize():
        return {"success": False, "error": "MT5 Not initialized"}
    acc = mt5.account_info()
    if acc:
        return {"success": True, "login": acc.login, "balance": acc.balance, "currency": acc.currency}
    return {"success": False}

@app.post("/place_order")
async def place_order(order: OrderRequest):
    if not MT5_AVAILABLE:
        raise HTTPException(status_code=500, detail="MT5 Library not available on this platform.")

    if not mt5.initialize():
        raise HTTPException(status_code=500, detail="Bridge not initialized")

    # Map BUY/SELL to MT5 constants
    order_type = mt5.ORDER_TYPE_BUY if order.type == "BUY" else mt5.ORDER_TYPE_SELL
    tick = mt5.symbol_info_tick(order.symbol)
    if not tick:
        raise HTTPException(status_code=400, detail=f"Symbol {order.symbol} not found")
        
    price = tick.ask if order.type == "BUY" else tick.bid

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": order.symbol,
        "volume": order.volume,
        "type": order_type,
        "price": price,
        "sl": order.sl,
        "tp": order.tp,
        "magic": 123456,
        "comment": order.comment,
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }

    result = mt5.order_send(request)
    
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return {"success": False, "error": f"Trade failed: {result.comment}"}
    
    return {"success": True, "ticket": result.order}

@app.get("/positions")
async def get_positions():
    if not MT5_AVAILABLE:
        return []
        
    positions = mt5.positions_get()
    if positions is None:
        return []
    
    return [
        {
            "ticket": p.ticket,
            "symbol": p.symbol,
            "volume": p.volume,
            "type": "BUY" if p.type == 0 else "SELL",
            "profit": p.profit,
            "price_open": p.price_open
        } for p in positions
    ]

@app.get("/")
async def root():
    return {"message": "API Working"}

# Handler for Netlify
handler = Mangum(app)

if __name__ == "__main__":
    import uvicorn
    print("🚀 FastAPI MT5 Bridge starting on http://localhost:8000")
    uvicorn.run(app, host="0.0.0.0", port=8000)