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
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- State Management ---
LAST_ACC = None

def ensure_mt5():
    """Helper to ensure MT5 is initialized with stored credentials if possible"""
    if not MT5_AVAILABLE:
        return False
    
    # If already initialized and terminal is connected, just return True
    if mt5.terminal_info() is not None:
        return True
        
    # If not initialized but we have credentials, try connecting
    if LAST_ACC:
        return mt5.initialize(
            login=LAST_ACC["login"], 
            password=LAST_ACC["password"], 
            server=LAST_ACC["server"]
        )
    
    # Fallback to default initialization (checks for active terminal)
    return mt5.initialize()

# --- Data Models ---
class OrderRequest(BaseModel):
    symbol: str
    type: str # 'BUY' or 'SELL'
    volume: float
    sl: Optional[float] = None
    tp: Optional[float] = None
    comment: Optional[str] = "GainZAlgo Signal"

# --- Routes ---

@app.get("/ping")
async def ping():
    return {"status": "ok"}

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

class ConnectRequest(BaseModel):
    login: int
    password: str
    server: str

@app.post("/connect")
async def connect(req: ConnectRequest):
    global LAST_ACC
    if not MT5_AVAILABLE:
        return {"success": False, "error": "MT5 Library not available"}
    
    # Save credentials for future auto-initialization
    LAST_ACC = {
        "login": req.login,
        "password": req.password,
        "server": req.server
    }
    
    if not mt5.initialize(login=req.login, password=req.password, server=req.server):
        return {"success": False, "error": f"Failed to connect to {req.server}: {mt5.last_error()}"}
    
    return {"success": True}

@app.get("/account")
async def get_account():
    if not ensure_mt5():
        return {"isConnected": False}
    
    acc = mt5.account_info()
    if acc:
        return {
            "login": acc.login,
            "balance": acc.balance,
            "equity": acc.equity,
            "currency": acc.currency,
            "isConnected": True
        }
    return {"isConnected": False}

@app.post("/order")
async def place_order(order: OrderRequest):
    if not ensure_mt5():
        return {"success": False, "error": "Bridge not initialized and no credentials available."}

    # Map BUY/SELL to MT5 constants
    order_type = mt5.ORDER_TYPE_BUY if order.type == "BUY" else mt5.ORDER_TYPE_SELL
    
    # Smart Symbol Detection (Handles Exness 'm' suffix and other variants)
    actual_symbol = order.symbol
    symbol_info = mt5.symbol_info(actual_symbol)
    
    if not symbol_info:
        # Try appending 'm' (Exness), '.pro', '.x', etc.
        for suffix in ['m', '.pro', '.m', '.x']:
            alt_symbol = order.symbol + suffix
            info = mt5.symbol_info(alt_symbol)
            if info:
                actual_symbol = alt_symbol
                symbol_info = info
                break
                
    if not symbol_info:
        return {"success": False, "error": f"Symbol {order.symbol} (or variants) not found in MT5"}
    
    # Ensure symbol is visible in Market Watch
    if not symbol_info.visible:
        if not mt5.symbol_select(actual_symbol, True):
            return {"success": False, "error": f"Failed to select symbol {actual_symbol}"}

    tick = mt5.symbol_info_tick(actual_symbol)
    if not tick:
        return {"success": False, "error": f"Could not get tick for {actual_symbol}"}
        
    price = tick.ask if order.type == "BUY" else tick.bid

    # Determine optimal filling type
    # Safe constant retrieval to prevent AttributeError
    filling_fok = getattr(mt5, "SYMBOL_FILLING_FOK", 1) # 1 is standard FOK
    filling_ioc = getattr(mt5, "SYMBOL_FILLING_IOC", 2) # 2 is standard IOC

    # Determine optimal filling type
    if symbol_info.filling_mode & filling_fok:
        filling_type = mt5.ORDER_FILLING_FOK
    elif symbol_info.filling_mode & filling_ioc:
        filling_type = mt5.ORDER_FILLING_IOC
    else:
        # Fallback for many brokers (especially ECN/Exness)
        filling_type = mt5.ORDER_FILLING_RETURN

    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": actual_symbol,
        "volume": order.volume,
        "type": order_type,
        "price": price,
        "sl": float(order.sl) if order.sl else 0.0,
        "tp": float(order.tp) if order.tp else 0.0,
        "magic": 123456,
        "comment": order.comment or "GainZAlgo Signal",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": filling_type,
    }

    result = mt5.order_send(request)
    
    if result is None:
        return {"success": False, "error": f"Internal MT5 Error: order_send returned None. Error: {mt5.last_error()}"}
        
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return {
            "success": False, 
            "error": f"Trade failed (Code {result.retcode}): {result.comment if hasattr(result, 'comment') else 'Unknown error'}",
            "retcode": result.retcode
        }
    
    return {"success": True, "ticket": result.order}

class CloseRequest(BaseModel):
    ticket: int

@app.post("/close")
async def close_position(req: CloseRequest):
    if not ensure_mt5():
        return {"success": False, "error": "Bridge not initialized"}
    
    positions = mt5.positions_get(ticket=req.ticket)
    if not positions:
        return {"success": False, "error": "Position not found"}
    
    p = positions[0]
    order_type = mt5.ORDER_TYPE_SELL if p.type == mt5.ORDER_TYPE_BUY else mt5.ORDER_TYPE_BUY
    tick = mt5.symbol_info_tick(p.symbol)
    price = tick.bid if order_type == mt5.ORDER_TYPE_SELL else tick.ask
    
    request = {
        "action": mt5.TRADE_ACTION_DEAL,
        "symbol": p.symbol,
        "volume": p.volume,
        "type": order_type,
        "position": p.ticket,
        "price": price,
        "magic": 123456,
        "comment": "Close Position",
        "type_time": mt5.ORDER_TIME_GTC,
        "type_filling": mt5.ORDER_FILLING_IOC,
    }
    
    result = mt5.order_send(request)
    if result.retcode != mt5.TRADE_RETCODE_DONE:
        return {"success": False, "error": f"Close failed: {result.comment}"}
    return {"success": True}

@app.get("/positions")
async def get_positions():
    if not ensure_mt5():
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

@app.get("/history")
async def get_history():
    if not ensure_mt5():
        return []
    
    deals = mt5.history_deals_get(group="*")
    if deals is None:
        return []
    
    return [
        {
            "ticket": d.ticket,
            "symbol": d.symbol,
            "volume": d.volume,
            "type": "BUY" if d.type == 0 else "SELL",
            "profit": d.profit,
            "openPrice": d.price, # simplified
            "closeTime": d.time * 1000
        } for d in deals
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
