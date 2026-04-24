#!/usr/bin/env python3
"""
TRUE DEX PRO - Backend API (Deploy ke Railway)
"""

import time
import json
from collections import deque, defaultdict
from flask import Flask, Response
from flask_cors import CORS
from flask import send_from_directory
import os
from web3 import Web3
from web3.middleware import geth_poa_middleware
import hashlib

# ================= CONFIG =================
RPC = "https://bsc-testnet.bnbchain.org"
PAIR = Web3.to_checksum_address("0xD73aC8C6Eb2210E7093AF25C1E9480aBc1693B7E")
USD = Web3.to_checksum_address("0xBCf4FBE06fe75c4B95F393918Ed53dD9A18d3b95")
MATCHA = Web3.to_checksum_address("0xFff1EB07eb08d44bbD8E827b79c94B6bfdd6Ab5a")

WINDOW = 20
MIN_USD = 0.01
POLL_INTERVAL = 0.4
WHALE_THRESHOLD = 10000

# ================= WEB3 =================
w3 = Web3(Web3.HTTPProvider(RPC))
w3.middleware_onion.inject(geth_poa_middleware, layer=0)

# ================= ABI =================
PAIR_ABI = [{"name": "token0", "type": "function", "inputs": [], "outputs": [{"type": "address"}], "stateMutability": "view"},
{"name": "token1", "type": "function", "inputs": [], "outputs": [{"type": "address"}], "stateMutability": "view"},
{"name": "getReserves", "type": "function", "inputs": [], "outputs": [{"type": "uint112"}, {"type": "uint112"}, {"type": "uint32"}], "stateMutability": "view"},
{"anonymous": False, "inputs": [{"indexed": True, "name": "sender", "type": "address"}, {"indexed": False, "name": "amount0In", "type": "uint256"}, {"indexed": False, "name": "amount1In", "type": "uint256"}, {"indexed": False, "name": "amount0Out", "type": "uint256"}, {"indexed": False, "name": "amount1Out", "type": "uint256"}, {"indexed": True, "name": "to", "type": "address"}], "name": "Swap", "type": "event"}]

ERC20_ABI = [{"name": "decimals", "type": "function", "inputs": [], "outputs": [{"type": "uint8"}], "stateMutability": "view"},
{"name": "symbol", "type": "function", "inputs": [], "outputs": [{"type": "string"}], "stateMutability": "view"}]

# ================= CONTRACT =================
contract = w3.eth.contract(address=PAIR, abi=PAIR_ABI)

# ================= TOKEN INFO =================
token0 = contract.functions.token0().call()
token1 = contract.functions.token1().call()
t0 = w3.eth.contract(address=token0, abi=ERC20_ABI)
t1 = w3.eth.contract(address=token1, abi=ERC20_ABI)
dec0 = t0.functions.decimals().call()
dec1 = t1.functions.decimals().call()
sym0 = t0.functions.symbol().call()
sym1 = t1.functions.symbol().call()

# ================= MAPPING =================
if token0 == USD:
    usd_index, mtc_index, usd_dec, mtc_dec = 0, 1, dec0, dec1
else:
    usd_index, mtc_index, usd_dec, mtc_dec = 1, 0, dec1, dec0

print(f"✅ Pair: {sym0}/{sym1}")

# ================= DATA STRUCTURES =================
flow_window = deque(maxlen=WINDOW)
price_history = deque(maxlen=2000)
transactions = deque(maxlen=100)
whale_tracker = defaultdict(float)
top_traders = defaultdict(lambda: {'volume': 0, 'trades': 0})

last_r0, last_r1, last_timestamp = 0, 0, 0
total_buy_vol, total_sell_vol = 0, 0
high_24h, low_24h = 0, float('inf')
start_price = 0
last_price = 0

app = Flask(__name__)
CORS(app, origins=["https://your-app.vercel.app"])  # Ganti dengan URL Vercel lo

def normalize(x, dec): return x / (10 ** dec)
def calc_price(r_usd, r_mtc): return r_usd / r_mtc if r_mtc else 0

def calculate_rsi(prices, period=14):
    if len(prices) < period + 1:
        return 50
    gains, losses = [], []
    for i in range(1, len(prices)):
        change = prices[i] - prices[i-1]
        gains.append(change if change > 0 else 0)
        losses.append(abs(change) if change < 0 else 0)
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0:
        return 100
    return 100 - (100 / (1 + (avg_gain / avg_loss)))

def build_candles(ticks, tf_seconds):
    if not ticks:
        return []
    candles = {}
    for tick in ticks:
        bucket = (tick["t"] // (tf_seconds * 1000)) * (tf_seconds * 1000)
        p = tick["v"]
        if bucket not in candles:
            candles[bucket] = {"time": bucket // 1000, "open": p, "high": p, "low": p, "close": p}
        else:
            candles[bucket]["high"] = max(candles[bucket]["high"], p)
            candles[bucket]["low"]  = min(candles[bucket]["low"],  p)
            candles[bucket]["close"] = p
    return sorted(candles.values(), key=lambda x: x["time"])

def event_stream():
    global last_r0, last_r1, last_timestamp, total_buy_vol, total_sell_vol, high_24h, low_24h, start_price, last_price

    swap_filter = contract.events.Swap.create_filter(fromBlock="latest")  

    while True:  
        try:  
            events = swap_filter.get_new_entries()  
            r0, r1, _ = contract.functions.getReserves().call()  

            r_usd = normalize(r0 if usd_index == 0 else r1, usd_dec)  
            r_mtc = normalize(r1 if usd_index == 0 else r0, mtc_dec)  
            current_price = calc_price(r_usd, r_mtc)  

            tvl_usd = r_usd * 2  
            liquidity_usd = r_usd  

            if start_price == 0:  
                start_price = current_price  
                last_price = current_price  

            high_24h = max(high_24h, current_price)  
            low_24h = min(low_24h, current_price)  

            if events:  
                for e in events:  
                    args = e["args"]  
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

                    if usd_vol >= MIN_USD:  
                        flow_window.append(usd_vol if direction == "BUY" else -usd_vol)  

                        if direction == "BUY":  
                            total_buy_vol += usd_vol  
                        else:  
                            total_sell_vol += usd_vol  

                        sender = args['sender']  
                        whale_tracker[sender] += usd_vol  
                        top_traders[sender]['volume'] += usd_vol  
                        top_traders[sender]['trades'] += 1  

                        is_whale = usd_vol >= WHALE_THRESHOLD  
                        transactions.appendleft({  
                            'hash': hashlib.md5(f"{e['blockNumber']}{sender}".encode()).hexdigest()[:12],  
                            'direction': direction,  
                            'usd_amount': round(usd_vol, 2),  
                            'mtc_amount': round(mtc_vol, 4),  
                            'price': round(current_price, 8),  
                            'wallet': f"{sender[:6]}...{sender[-4:]}",  
                            'is_whale': is_whale,  
                            'time': int(time.time() * 1000)  
                        })  

            elif last_r0 != 0 and (r0 != last_r0 or r1 != last_r1):  
                delta0 = (r0 - last_r0) / (10 ** usd_dec)  
                if abs(delta0) >= MIN_USD:  
                    direction = "BUY" if (r1 < last_r1 if mtc_index == 1 else r0 < last_r0) else "SELL"  
                    flow_window.append(abs(delta0) if direction == "BUY" else -abs(delta0))  

            last_r0, last_r1 = r0, r1  

            pressure = sum(flow_window)  

            now = int(time.time() * 1000)  
            price_history.append({"t": now, "v": current_price})  
            last_timestamp = now  

            change_24h = ((current_price - start_price) / start_price * 100) if start_price > 0 else 0  
            change_1m = ((current_price - last_price) / last_price * 100) if last_price > 0 else 0  
            last_price = current_price  

            price_values = [p["v"] for p in price_history]  
            rsi = calculate_rsi(price_values)  

            if pressure > 80 or rsi > 70:  
                signal = "BULLISH"  
                signal_color = "green"  
            elif pressure < -80 or rsi < 30:  
                signal = "BEARISH"  
                signal_color = "red"  
            else:  
                signal = "NEUTRAL"  
                signal_color = "neutral"  

            top_whales = sorted(whale_tracker.items(), key=lambda x: x[1], reverse=True)[:10]  
            top_whales_list = [{'wallet': f"{w[:6]}...{w[-4:]}", 'volume': round(v, 2)} for w, v in top_whales]  

            top_traders_list = sorted(top_traders.items(), key=lambda x: x[1]['volume'], reverse=True)[:10]  
            top_traders_data = [{  
                'wallet': f"{w[:6]}...{w[-4:]}",  
                'volume': round(t['volume'], 2),  
                'trades': t['trades']  
            } for w, t in top_traders_list]  

            bid_depth = 50 + (pressure * 0.5) if pressure > 0 else 50 + (pressure * 0.3)  
            ask_depth = 50 - (pressure * 0.5) if pressure < 0 else 50 - (pressure * 0.3)  
            bid_depth = max(10, min(90, bid_depth))  
            ask_depth = max(10, min(90, ask_depth))  

            ticks = list(price_history)  
            candles_1s  = build_candles(ticks, 1)  
            candles_5s  = build_candles(ticks, 5)  
            candles_15s = build_candles(ticks, 15)  
            candles_1m  = build_candles(ticks, 60)  

            state = {  
                "type": "state",  
                "data": {  
                    "price": current_price,  
                    "price_change": round(change_24h, 2),  
                    "change_1m": round(change_1m, 2),  
                    "pressure": round(pressure, 2),  
                    "signal": signal,  
                    "signal_color": signal_color,  
                    "rsi": round(rsi, 1),  
                    "buy_vol_24h": round(total_buy_vol, 2),  
                    "sell_vol_24h": round(total_sell_vol, 2),  
                    "high_24h": round(high_24h, 8),  
                    "low_24h": round(low_24h, 8),  
                    "tvl": round(tvl_usd, 2),  
                    "liquidity": round(liquidity_usd, 2),  
                    "transactions": list(transactions),  
                    "whales": top_whales_list,  
                    "top_traders": top_traders_data,  
                    "bid_depth": round(bid_depth, 1),  
                    "ask_depth": round(ask_depth, 1),  
                    "candles": {  
                        "1s":  candles_1s[-500:],  
                        "5s":  candles_5s[-500:],  
                        "15s": candles_15s[-500:],  
                        "1m":  candles_1m[-500:],  
                    }  
                }  
            }  

            yield f"data: {json.dumps(state)}\n\n"  
            time.sleep(POLL_INTERVAL)  

        except Exception as e:  
            print(f"SSE Error: {e}")  
            time.sleep(1)

@app.route('/stream')
def stream():
    return Response(event_stream(), mimetype="text/event-stream")

@app.route("/")
def home():
    return send_from_directory("frontend", "index.html")

@app.route('/health')
def health():
    return {"status": "ok", "pair": f"{sym0}/{sym1}"}

if __name__ == '__main__':
    print("\n" + "="*50)
    print("🎯 TRUE DEX PRO - BACKEND API")
    print("="*50)
    print(f"📡 Pair: {sym0}/{sym1}")
    print(f"🔄 SSE Stream: /stream")
    print(f"❤️ Health check: /health")
    print("="*50 + "\n")
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)
