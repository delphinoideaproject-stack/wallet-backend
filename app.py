#!/usr/bin/env python3
"""
TRUE DEX PRO - Production Backend API (CEX-Grade Architecture)
"""

import time
import json
import threading
import queue
from collections import deque, defaultdict
from flask import Flask, Response, send_from_directory
from flask_cors import CORS
from flask_socketio import SocketIO, emit
import os
from web3 import Web3
from web3.middleware import geth_poa_middleware
import hashlib
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
import redis
import msgpack
from functools import lru_cache

# ================= CONFIG =================
RPC = "https://bsc-testnet.bnbchain.org"
PAIR = Web3.to_checksum_address("0xD73aC8C6Eb2210E7093AF25C1E9480aBc1693B7E")
USD = Web3.to_checksum_address("0xBCf4FBE06fe75c4B95F393918Ed53dD9A18d3b95")

WINDOW = 20
MIN_USD = 0.01
POLL_INTERVAL = 0.1  # Reduced to 100ms for real-time
WHALE_THRESHOLD = 10000
BLOCK_POLL_INTERVAL = 1.5  # Faster block polling
CACHE_TTL = 10  # Reduced cache TTL for fresher data
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")

# ================= WEB3 =================
w3 = Web3(Web3.HTTPProvider(RPC, request_kwargs={'timeout': 10}))
w3.middleware_onion.inject(geth_poa_middleware, layer=0)

# ================= ABI =================
PAIR_ABI = [{"name": "token0", "type": "function", "inputs": [], "outputs": [{"type": "address"}], "stateMutability": "view"},
{"name": "token1", "type": "function", "inputs": [], "outputs": [{"type": "address"}], "stateMutability": "view"},
{"name": "getReserves", "type": "function", "inputs": [], "outputs": [{"type": "uint112"}, {"type": "uint112"}, {"type": "uint32"}], "stateMutability": "view"},
{"anonymous": False, "inputs": [{"indexed": True, "name": "sender", "type": "address"}, {"indexed": False, "name": "amount0In", "type": "uint256"}, {"indexed": False, "name": "amount1In", "type": "uint256"}, {"indexed": False, "name": "amount0Out", "type": "uint256"}, {"indexed": False, "name": "amount1Out", "type": "uint256"}, {"indexed": True, "name": "to", "type": "address"}], "name": "Swap", "type": "event"}]

ERC20_ABI = [{"name": "decimals", "type": "function", "inputs": [], "outputs": [{"type": "uint8"}], "stateMutability": "view"},
{"name": "symbol", "type": "function", "inputs": [], "outputs": [{"type": "string"}], "stateMutability": "view"}]

# ================= INIT CONNECTION =================
def init_web3():
    try:
        contract = w3.eth.contract(address=PAIR, abi=PAIR_ABI)
        token0 = contract.functions.token0().call()
        token1 = contract.functions.token1().call()
        t0 = w3.eth.contract(address=token0, abi=ERC20_ABI)
        t1 = w3.eth.contract(address=token1, abi=ERC20_ABI)
        dec0 = t0.functions.decimals().call()
        dec1 = t1.functions.decimals().call()
        sym0 = t0.functions.symbol().call()
        sym1 = t1.functions.symbol().call()
        
        if token0 == USD:
            usd_index, mtc_index, usd_dec, mtc_dec = 0, 1, dec0, dec1
        else:
            usd_index, mtc_index, usd_dec, mtc_dec = 1, 0, dec1, dec0
        
        print(f"✅ Connected: {sym0}/{sym1}")
        return contract, sym0, sym1, usd_index, mtc_index, usd_dec, mtc_dec, True
    except Exception as e:
        print(f"❌ Web3 init failed: {e}")
        return None, "ERROR", "ERROR", 0, 1, 18, 18, False

contract, sym0, sym1, usd_index, mtc_index, usd_dec, mtc_dec, CONNECTION_OK = init_web3()

# ================= REDIS SETUP =================
try:
    redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=False)
    redis_client.ping()
    REDIS_OK = True
    print("✅ Redis connected")
except:
    REDIS_OK = False
    print("⚠️ Redis unavailable, using memory fallback")

# ================= OPTIMIZED DATA STRUCTURES =================
@dataclass
class CandleData:
    time: int
    open: float
    high: float
    low: float
    close: float

class IncrementalRSI:
    """O(1) incremental RSI calculator"""
    def __init__(self, period=14):
        self.period = period
        self.prices = deque(maxlen=period + 1)
        self.gains = deque(maxlen=period)
        self.losses = deque(maxlen=period)
        self.avg_gain = 0
        self.avg_loss = 0
        self.initialized = False
    
    def add_price(self, price: float) -> float:
        if not self.prices:
            self.prices.append(price)
            return 50
        
        prev_price = self.prices[-1]
        change = price - prev_price
        gain = change if change > 0 else 0
        loss = abs(change) if change < 0 else 0
        
        self.prices.append(price)
        
        if not self.initialized and len(self.prices) > self.period:
            # Initialize with first period
            self.gains.append(gain)
            self.losses.append(loss)
            if len(self.gains) == self.period:
                self.avg_gain = sum(self.gains) / self.period
                self.avg_loss = sum(self.losses) / self.period
                self.initialized = True
        elif self.initialized:
            # Incremental update
            self.avg_gain = (self.avg_gain * (self.period - 1) + gain) / self.period
            self.avg_loss = (self.avg_loss * (self.period - 1) + loss) / self.period
        
        if self.initialized and self.avg_loss == 0:
            return 100
        
        if self.initialized:
            rs = self.avg_gain / self.avg_loss
            return 100 - (100 / (1 + rs))
        
        return 50

class MarketData:
    def __init__(self):
        self.flow_window = deque(maxlen=WINDOW)
        self.price_history = deque(maxlen=5000)  # Increased for better candles
        self.transactions = deque(maxlen=200)    # More tx history
        self.whale_tracker = defaultdict(float)
        self.whale_persistent = {}  # Persistent across restarts via Redis
        self.top_traders = defaultdict(lambda: {'volume': 0, 'trades': 0})
        self.last_block = 0
        self.total_buy_vol = 0
        self.total_sell_vol = 0
        self.high_24h = 0
        self.low_24h = float('inf')
        self.start_price = 0
        self.last_price = 0
        self.last_reserves = (0, 0)
        self.candle_cache = {}
        self.cache_timestamp = 0
        self.processed_blocks = set()  # Prevent double processing
        self.rsi_calculator = IncrementalRSI(14)
        self.last_processed_block = 0
        self.last_block_hash = ""
        self.write_buffer = []  # Batch writes to Redis
        self.last_redis_sync = time.time()
        
        # Load persistent data if Redis available
        if REDIS_OK:
            self._load_persistent_data()
    
    def _load_persistent_data(self):
        """Load whale and trader data from Redis"""
        try:
            whale_data = redis_client.get("dex:whale_tracker")
            if whale_data:
                self.whale_persistent = msgpack.unpackb(whale_data, raw=False)
                self.whale_tracker.update(self.whale_persistent)
            
            trader_data = redis_client.get("dex:top_traders")
            if trader_data:
                traders = msgpack.unpackb(trader_data, raw=False)
                for addr, data in traders.items():
                    self.top_traders[addr] = data
            
            stats = redis_client.get("dex:stats")
            if stats:
                stats_data = msgpack.unpackb(stats, raw=False)
                self.total_buy_vol = stats_data.get('buy_vol', 0)
                self.total_sell_vol = stats_data.get('sell_vol', 0)
                self.high_24h = stats_data.get('high', 0)
                self.low_24h = stats_data.get('low', float('inf'))
            
            print("✅ Persistent data loaded from Redis")
        except Exception as e:
            print(f"⚠️ Failed to load persistent data: {e}")
    
    def _sync_to_redis(self):
        """Batch sync to Redis"""
        if not REDIS_OK:
            return
        
        now = time.time()
        if now - self.last_redis_sync >= 10:  # Sync every 10 seconds
            try:
                # Use pipeline for atomic writes
                pipe = redis_client.pipeline()
                
                # Merge persistent with current
                merged_whales = {**self.whale_persistent, **self.whale_tracker}
                pipe.setex("dex:whale_tracker", 86400, msgpack.packb(merged_whales))
                pipe.setex("dex:top_traders", 86400, msgpack.packb(dict(self.top_traders)))
                pipe.setex("dex:stats", 86400, msgpack.packb({
                    'buy_vol': self.total_buy_vol,
                    'sell_vol': self.total_sell_vol,
                    'high': self.high_24h,
                    'low': self.low_24h
                }))
                
                pipe.execute()
                self.last_redis_sync = now
                self.whale_persistent = merged_whales
            except Exception as e:
                print(f"Redis sync error: {e}")
    
    def reset_whales_if_needed(self):
        # Reset whale tracker every 24 hours with persistence
        if time.time() - getattr(self, 'whale_last_reset', 0) >= 86400:
            # Archive old data before reset
            if REDIS_OK:
                redis_client.setex(f"dex:whale_archive_{int(time.time())}", 604800, 
                                 msgpack.packb(dict(self.whale_tracker)))
            
            self.whale_tracker.clear()
            self.whale_last_reset = time.time()
            print("🔄 Whale tracker reset (24h cycle)")
    
    def update_high_low(self, price):
        if price > 0:
            if self.high_24h == 0 or price > self.high_24h:
                self.high_24h = price
            if price < self.low_24h:
                self.low_24h = price

market = MarketData()

# ================= HELPER FUNCTIONS =================
def normalize(x, dec): 
    return x / (10 ** dec) if x else 0

def calc_price(r_usd, r_mtc): 
    return r_usd / r_mtc if r_mtc else 0

def build_candles_incremental(ticks, tf_seconds, last_candle=None):
    """Incremental candle builder - O(1) per tick"""
    if not ticks:
        return []
    
    candles = {}
    current_time = int(time.time() * 1000)
    for tick in ticks:
        bucket = (tick["t"] // (tf_seconds * 1000)) * (tf_seconds * 1000)
        p = tick["v"]
        if bucket not in candles:
            candles[bucket] = {"time": bucket // 1000, "open": p, "high": p, "low": p, "close": p}
        else:
            candles[bucket]["high"] = max(candles[bucket]["high"], p)
            candles[bucket]["low"] = min(candles[bucket]["low"], p)
            candles[bucket]["close"] = p
    
    return sorted(candles.values(), key=lambda x: x["time"])[-500:]

def fetch_swap_logs(from_block, to_block):
    """Optimized log fetching with deduplication"""
    if not CONNECTION_OK or not contract:
        return []
    try:
        logs = contract.events.Swap.get_logs(
            fromBlock=from_block, 
            toBlock=to_block
        )
        return logs
    except Exception as e:
        print(f"Log fetch error: {e}")
        return []

def process_swap_event(event, current_price, block_hash):
    """Process single swap event with deduplication"""
    try:
        # Create unique event ID to prevent double processing
        event_id = f"{event['blockNumber']}_{event['logIndex']}_{block_hash[:16]}"
        if event_id in market.processed_blocks:
            return False
        
        market.processed_blocks.add(event_id)
        # Keep set size manageable
        if len(market.processed_blocks) > 10000:
            market.processed_blocks.clear()
        
        args = event["args"]
        if usd_index == 0:
            usd_in, usd_out = args["amount0In"], args["amount0Out"]
            mtc_in, mtc_out = args["amount1In"], args["amount1Out"]
        else:
            usd_in, usd_out = args["amount1In"], args["amount1Out"]
            mtc_in, mtc_out = args["amount0In"], args["amount0Out"]
        
        usd_in_n, usd_out_n = normalize(usd_in, usd_dec), normalize(usd_out, usd_dec)
        mtc_in_n, mtc_out_n = normalize(mtc_in, mtc_dec), normalize(mtc_out, mtc_dec)
        
        if mtc_out > 0:
            direction, usd_vol, mtc_vol = "BUY", usd_in_n, mtc_out_n
        else:
            direction, usd_vol, mtc_vol = "SELL", usd_out_n, mtc_in_n
        
        if usd_vol >= MIN_USD and usd_vol > 0:
            market.flow_window.append(usd_vol if direction == "BUY" else -usd_vol)
            
            if direction == "BUY":
                market.total_buy_vol += usd_vol 
            else:
                market.total_sell_vol += usd_vol
            
            sender = args['sender']
            market.whale_tracker[sender] += usd_vol
            market.top_traders[sender]['volume'] += usd_vol
            market.top_traders[sender]['trades'] += 1
            
            is_whale = usd_vol >= WHALE_THRESHOLD
            market.transactions.appendleft({
                'hash': hashlib.md5(f"{event['blockNumber']}{sender}".encode()).hexdigest()[:12], 
                'direction': direction,
                'usd_amount': round(usd_vol, 2),
                'mtc_amount': round(mtc_vol, 4),
                'price': round(current_price, 8) if current_price > 0 else 0,
                'wallet': f"{sender[:6]}...{sender[-4:]}",
                'is_whale': is_whale,
                'time': int(time.time() * 1000)
            })
            
            # Sync to Redis periodically
            market._sync_to_redis()
            return True
    except Exception as e:
        print(f"Process swap error: {e}")
    return False

@lru_cache(maxsize=128)
def get_cached_price_history():
    """Cache price history for quick access"""
    return list(market.price_history)

def get_market_state():
    """Optimized market state generation"""
    if not CONNECTION_OK:
        return {
            "price": 0,
            "price_change": 0,
            "change_1m": 0,
            "pressure": 0,
            "signal": "OFFLINE",
            "signal_color": "gray",
            "rsi": 50,
            "buy_vol_24h": 0,
            "sell_vol_24h": 0,
            "high_24h": 0,
            "low_24h": 0,
            "tvl": 0,
            "liquidity": 0,
            "transactions": [],
            "whales": [],
            "top_traders": [],
            "bid_depth": 50,
            "ask_depth": 50,
            "candles": {"1s": [], "5s": [], "15s": [], "1m": []}
        }
    
    try:
        # Get latest reserves
        reserves = contract.functions.getReserves().call()
        if not reserves:
            return None
            
        r0, r1, _ = reserves
        
        r_usd = normalize(r0 if usd_index == 0 else r1, usd_dec)
        r_mtc = normalize(r1 if usd_index == 0 else r0, mtc_dec)
        current_price = calc_price(r_usd, r_mtc)
        
        tvl_usd = r_usd * 2
        liquidity_usd = r_usd
        
        # Initialize price tracking
        if market.start_price == 0 and current_price > 0:
            market.start_price = current_price
            market.last_price = current_price
        
        market.update_high_low(current_price)
        market.reset_whales_if_needed()
        
        # Add to price history with incremental RSI
        now = int(time.time() * 1000)
        if current_price > 0:
            market.price_history.append({"t": now, "v": current_price})
            rsi = market.rsi_calculator.add_price(current_price)
        else:
            rsi = 50
        
        # Calculate changes
        change_24h = ((current_price - market.start_price) / market.start_price * 100) if market.start_price > 0 else 0
        change_1m = ((current_price - market.last_price) / market.last_price * 100) if market.last_price > 0 else 0
        market.last_price = current_price if current_price > 0 else market.last_price
        
        # Volume pressure
        pressure = sum(market.flow_window)
        
        # Signal generation
        if pressure > 80 or rsi > 70:
            signal, signal_color = "BULLISH", "green"
        elif pressure < -80 or rsi < 30:
            signal, signal_color = "BEARISH", "red"
        else:
            signal, signal_color = "NEUTRAL", "neutral"
        
        # Top lists (optimized with slicing)
        top_whales = sorted(market.whale_tracker.items(), key=lambda x: x[1], reverse=True)[:10]
        top_whales_list = [{'wallet': f"{w[:6]}...{w[-4:]}", 'volume': round(v, 2)} for w, v in top_whales if v > 0]
        
        top_traders_list = sorted(market.top_traders.items(), key=lambda x: x[1]['volume'], reverse=True)[:10]
        top_traders_data = [{
            'wallet': f"{w[:6]}...{w[-4:]}",
            'volume': round(t['volume'], 2),
            'trades': t['trades']
        } for w, t in top_traders_list if t['volume'] > 0]
        
        # Order book simulation (improved)
        pressure_factor = max(-1, min(1, pressure / 100))
        bid_depth = max(10, min(90, 50 + (pressure_factor * 40)))
        ask_depth = max(10, min(90, 50 - (pressure_factor * 40)))
        
        # Candles with improved caching
        current_time = time.time()
        if current_time - market.cache_timestamp > CACHE_TTL:
            ticks = list(market.price_history)
            market.candle_cache = {
                "1s": build_candles_incremental(ticks, 1)[-500:],
                "5s": build_candles_incremental(ticks, 5)[-500:],
                "15s": build_candles_incremental(ticks, 15)[-500:],
                "1m": build_candles_incremental(ticks, 60)[-500:],
            }
            market.cache_timestamp = current_time
        
        # Clear stale processed blocks
        if len(market.processed_blocks) > 5000:
            market.processed_blocks.clear()
        
        return {
            "price": round(current_price, 8) if current_price > 0 else 0,
            "price_change": round(change_24h, 2),
            "change_1m": round(change_1m, 2),
            "pressure": round(pressure, 2),
            "signal": signal,
            "signal_color": signal_color,
            "rsi": round(rsi, 1),
            "buy_vol_24h": round(market.total_buy_vol, 2),
            "sell_vol_24h": round(market.total_sell_vol, 2),
            "high_24h": round(market.high_24h, 8),
            "low_24h": round(market.low_24h, 8),
            "tvl": round(tvl_usd, 2),
            "liquidity": round(liquidity_usd, 2),
            "transactions": list(market.transactions),
            "whales": top_whales_list,
            "top_traders": top_traders_data,
            "bid_depth": round(bid_depth, 1),
            "ask_depth": round(ask_depth, 1),
            "candles": market.candle_cache
        }
    except Exception as e:
        print(f"Market state error: {e}")
        return None

# ================= BACKGROUND THREAD WITH OPTIMIZATION =================
class SwapPoller(threading.Thread):
    """Optimized swap poller with block hash validation"""
    def __init__(self):
        super().__init__(daemon=True)
        self.last_block = w3.eth.block_number if CONNECTION_OK else 0
        self.running = True
        self.block_cache = {}
    
    def get_block_hash(self, block_num):
        """Cache block hashes for deduplication"""
        if block_num not in self.block_cache:
            try:
                block = w3.eth.get_block(block_num)
                self.block_cache[block_num] = block.hash.hex()
                # Keep cache manageable
                if len(self.block_cache) > 100:
                    oldest = min(self.block_cache.keys())
                    del self.block_cache[oldest]
                return self.block_cache[block_num]
            except:
                return ""
        return self.block_cache[block_num]
    
    def run(self):
        if not CONNECTION_OK:
            return
        
        print("🔥 Optimized swap poller started")
        consecutive_errors = 0
        
        while self.running:
            try:
                current_block = w3.eth.block_number
                block_hash = self.get_block_hash(current_block)
                
                # Skip if same block hash as last (no new blocks)
                if block_hash == market.last_block_hash and current_block == market.last_processed_block:
                    time.sleep(BLOCK_POLL_INTERVAL)
                    continue
                
                if current_block > self.last_block:
                    # Adaptive fetch range based on network speed
                    range_size = min(50, current_block - self.last_block)
                    from_block = max(self.last_block + 1, current_block - range_size)
                    
                    logs = fetch_swap_logs(from_block, current_block)
                    
                    # Get current price for processing
                    reserves = contract.functions.getReserves().call()
                    if reserves:
                        r0, r1, _ = reserves
                        r_usd = normalize(r0 if usd_index == 0 else r1, usd_dec)
                        r_mtc = normalize(r1 if usd_index == 0 else r0, mtc_dec)
                        current_price = calc_price(r_usd, r_mtc)
                        
                        for log in logs:
                            process_swap_event(log, current_price, block_hash)
                    
                    self.last_block = current_block
                    market.last_processed_block = current_block
                    market.last_block_hash = block_hash
                    consecutive_errors = 0
                
                time.sleep(BLOCK_POLL_INTERVAL)
                
            except Exception as e:
                print(f"Poller error: {e}")
                consecutive_errors += 1
                backoff = min(30, consecutive_errors * 2)
                time.sleep(backoff)

# ================= FLASK + SOCKETIO APP =================
app = Flask(__name__)
CORS(app, origins=[
    "https://your-app.vercel.app",
    "http://localhost:3000",
    "http://localhost:5000",
    "https://*.railway.app"
])

# Socket.IO for real-time bidirectional communication
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='threading')

# Start background poller
if CONNECTION_OK:
    poller = SwapPoller()
    poller.start()

# Track connected clients
connected_clients = set()

@app.route('/stream')
def stream():
    """Legacy SSE stream - maintained for compatibility"""
    def event_stream():
        while True:
            state = get_market_state()
            if state:
                payload = {
                    "type": "state",
                    "data": state,
                    "timestamp": int(time.time() * 1000)
                }
                yield f"data: {json.dumps(payload)}\n\n"
            time.sleep(POLL_INTERVAL)
    
    return Response(event_stream(), mimetype="text/event-stream")

@socketio.on('connect')
def handle_connect():
    """WebSocket connection handler"""
    connected_clients.add(request.sid)
    emit('connected', {'status': 'ok', 'message': 'Connected to TrueDEX Pro'})
    print(f"🔌 Client connected: {request.sid}")

@socketio.on('disconnect')
def handle_disconnect():
    connected_clients.discard(request.sid)
    print(f"🔌 Client disconnected: {request.sid}")

@socketio.on('subscribe')
def handle_subscribe(data):
    """Subscribe to specific data streams"""
    channel = data.get('channel', 'market')
    emit('subscribed', {'channel': channel, 'status': 'ok'})

def broadcast_market_data():
    """Background thread for broadcasting via WebSocket"""
    while True:
        state = get_market_state()
        if state and connected_clients:
            socketio.emit('market_update', {
                'data': state,
                'timestamp': int(time.time() * 1000)
            }, namespace='/')
        time.sleep(POLL_INTERVAL)

# Start WebSocket broadcaster
if CONNECTION_OK:
    broadcaster = threading.Thread(target=broadcast_market_data, daemon=True)
    broadcaster.start()

@app.route('/health')
def health():
    return {
        "status": "healthy" if CONNECTION_OK else "degraded",
        "pair": f"{sym0}/{sym1}",
        "connected": CONNECTION_OK,
        "redis": REDIS_OK,
        "websocket": len(connected_clients),
        "uptime": time.time() - app.config.get('START_TIME', time.time()),
        "transactions": len(market.transactions),
        "price_points": len(market.price_history),
        "processed_blocks": len(market.processed_blocks)
    }

@app.route('/')
def home():
    return send_from_directory("frontend", "index.html")

@app.route('/stats')
def stats():
    """Comprehensive stats endpoint"""
    return {
        "total_buy_volume": round(market.total_buy_vol, 2),
        "total_sell_volume": round(market.total_sell_vol, 2),
        "whale_count": len(market.whale_tracker),
        "active_traders": len(market.top_traders),
        "current_pressure": round(sum(market.flow_window), 2),
        "memory_transactions": len(market.transactions),
        "candles_cached": len(market.candle_cache.get("1m", [])),
        "redis_connected": REDIS_OK,
        "websocket_clients": len(connected_clients),
        "rsi_value": market.rsi_calculator.avg_gain if hasattr(market.rsi_calculator, 'avg_gain') else 0
    }

@app.route('/reset')
def reset_stats():
    """Admin endpoint to reset analytics"""
    if os.environ.get('ADMIN_KEY') == request.headers.get('X-Admin-Key'):
        market.total_buy_vol = 0
        market.total_sell_vol = 0
        market.whale_tracker.clear()
        market.top_traders.clear()
        market.flow_window.clear()
        market.start_price = 0
        market.last_price = 0
        return {"status": "reset_complete"}
    return {"error": "unauthorized"}, 401

# ================= MAIN =================
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.config['START_TIME'] = time.time()
    
    print("\n" + "="*60)
    print("🎯 TRUE DEX PRO - CEX-GRADE PRODUCTION READY")
    print("="*60)
    print(f"📡 Pair: {sym0}/{sym1}")
    print(f"🔌 Web3: {'✅ Connected' if CONNECTION_OK else '❌ Failed'}")
    print(f"💾 Redis: {'✅ Connected' if REDIS_OK else '⚠️ Using Memory Fallback'}")
    print(f"🔄 SSE Stream: /stream")
    print(f"🔌 WebSocket: ws://localhost:{port}")
    print(f"❤️ Health: /health")
    print(f"📊 Stats: /stats")
    print(f"🚪 Port: {port}")
    print(f"🧵 Threads: 4")
    print(f"⚡ Architecture: Event-Driven + WebSocket")
    print("="*60 + "\n")
    
    # Use SocketIO for production-grade WebSocket support
    socketio.run(app, host='0.0.0.0', port=port, debug=False, allow_unsafe_werkzeug=True)
