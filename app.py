#!/usr/bin/env python3
"""
TRUE DEX PRO - Production Backend API (CEX-Grade Architecture) 
with AUTO-RPC FAILOVER & Multi-Provider Support
"""

import time
import json
import threading
from collections import deque, defaultdict
from flask import Flask, Response, send_from_directory, request, jsonify
from flask_cors import CORS
import os
from web3 import Web3
from web3.middleware import geth_poa_middleware
import hashlib
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple
import redis
import msgpack
from functools import lru_cache
import random

# ================= AUTO RPC FAILOVER =================
RPC_LIST = [
    "https://bsc-dataseed.bnbchain.org",
    "https://bsc-dataseed1.binance.org",
    "https://bsc-dataseed2.binance.org",
    "https://bsc-dataseed3.binance.org",
    "https://bsc-dataseed4.binance.org",
    "https://rpc.ankr.com/bsc",
    "https://binance.nodereal.io",
    "https://bsc.publicnode.com",
    "https://bsc-rpc.publicnode.com",
    "https://bsc.blockvision.org",
    "https://rpc.ankr.com/bsc",
]

# TESTNET RPCs (uncomment if using testnet)
TESTNET_RPC_LIST = [
    "https://bsc-testnet.bnbchain.org",
    "https://bsc-testnet.publicnode.com",
    "https://bsc-testnet-rpc.publicnode.com",
    "https://rpc.ankr.com/bsc_testnet_chapel",
]

class RPCManager:
    """Production-grade RPC manager with failover and health checks"""
    
    def __init__(self, rpc_list, health_check_interval=30, max_failures=3):
        self.rpc_list = rpc_list
        self.health_check_interval = health_check_interval
        self.max_failures = max_failures
        self.current_rpc = 0
        self.failures = defaultdict(int)
        self.last_health_check = time.time()
        self.web3_instances = {}
        self.lock = threading.Lock()
        self.rpc_health = {rpc: True for rpc in rpc_list}
        
    def _create_web3(self, rpc_url):
        """Create Web3 instance with proper config"""
        try:
            w3 = Web3(Web3.HTTPProvider(
                rpc_url, 
                request_kwargs={
                    'timeout': 10,
                    'headers': {'Content-Type': 'application/json'}
                },
                session=None
            ))
            # Inject POA middleware for BSC
            w3.middleware_onion.inject(geth_poa_middleware, layer=0)
            return w3
        except Exception as e:
            print(f"❌ Failed to create Web3 for {rpc_url}: {e}")
            return None
    
    def health_check(self, w3, rpc_url):
        """Check if RPC is healthy"""
        try:
            if w3 and w3.is_connected():
                block_num = w3.eth.block_number
                if block_num > 0:
                    return True
        except Exception as e:
            print(f"⚠️ Health check failed for {rpc_url}: {str(e)[:50]}")
        return False
    
    def get_web3(self, force_refresh=False):
        """Get working Web3 instance with auto-failover"""
        with self.lock:
            # Periodic health check
            now = time.time()
            if now - self.last_health_check > self.health_check_interval:
                self._run_health_checks()
                self.last_health_check = now
            
            # Try all RPCs
            tried = set()
            for i in range(len(self.rpc_list)):
                idx = (self.current_rpc + i) % len(self.rpc_list)
                rpc_url = self.rpc_list[idx]
                tried.add(rpc_url)
                
                # Skip unhealthy RPCs
                if not self.rpc_health[rpc_url] and self.failures[rpc_url] >= self.max_failures:
                    continue
                
                # Get or create Web3 instance
                w3 = self.web3_instances.get(rpc_url)
                if w3 is None or force_refresh:
                    w3 = self._create_web3(rpc_url)
                    if w3:
                        self.web3_instances[rpc_url] = w3
                
                if w3 and self.health_check(w3, rpc_url):
                    if idx != self.current_rpc:
                        print(f"🔄 RPC switched to: {rpc_url}")
                        self.current_rpc = idx
                    self.failures[rpc_url] = 0
                    self.rpc_health[rpc_url] = True
                    return w3, rpc_url
                else:
                    self.failures[rpc_url] += 1
                    if self.failures[rpc_url] >= self.max_failures:
                        self.rpc_health[rpc_url] = False
                        print(f"❌ RPC marked unhealthy: {rpc_url} (failures: {self.failures[rpc_url]})")
            
            # If all RPCs failed, reset and retry
            if len(tried) == len(self.rpc_list):
                print("⚠️ All RPCs failed! Resetting health status...")
                self._reset_health()
                return None, None
            
            return None, None
    
    def _run_health_checks(self):
        """Background health check for all RPCs"""
        print("🔍 Running RPC health checks...")
        for rpc_url in self.rpc_list:
            if self.failures[rpc_url] >= self.max_failures:
                # Try to recover
                w3 = self._create_web3(rpc_url)
                if w3 and self.health_check(w3, rpc_url):
                    print(f"✅ RPC recovered: {rpc_url}")
                    self.failures[rpc_url] = 0
                    self.rpc_health[rpc_url] = True
                    self.web3_instances[rpc_url] = w3
    
    def _reset_health(self):
        """Reset all health statuses"""
        for rpc in self.rpc_list:
            self.failures[rpc] = 0
            self.rpc_health[rpc] = True
        time.sleep(5)  # Backoff before retry
    
    def get_healthy_rpc_count(self):
        """Get number of healthy RPCs"""
        return sum(1 for healthy in self.rpc_health.values() if healthy)

# ================= CONFIG =================
# Initialize RPC Manager (use testnet or mainnet)
USE_TESTNET = False  # Set to False for mainnet
rpc_manager = RPCManager(TESTNET_RPC_LIST if USE_TESTNET else RPC_LIST)

# Get initial Web3 connection
w3, active_rpc = rpc_manager.get_web3()
if not w3:
    print("❌ Failed to connect to any RPC!")
    exit(1)

PAIR = Web3.to_checksum_address("0xD73aC8C6Eb2210E7093AF25C1E9480aBc1693B7E")
USD = Web3.to_checksum_address("0xBCf4FBE06fe75c4B95F393918Ed53dD9A18d3b95")

WINDOW = 20
MIN_USD = 0.01
POLL_INTERVAL = 0.5
WHALE_THRESHOLD = 10000
BLOCK_POLL_INTERVAL = 2
CACHE_TTL = 10
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379")

# ================= ABI (same as before) =================
PAIR_ABI = [
    {"name": "token0", "type": "function", "inputs": [], "outputs": [{"type": "address"}], "stateMutability": "view"},
    {"name": "token1", "type": "function", "inputs": [], "outputs": [{"type": "address"}], "stateMutability": "view"},
    {"name": "getReserves", "type": "function", "inputs": [], "outputs": [{"type": "uint112"}, {"type": "uint112"}, {"type": "uint32"}], "stateMutability": "view"},
    {"anonymous": False, "inputs": [{"indexed": True, "name": "sender", "type": "address"}, {"indexed": False, "name": "amount0In", "type": "uint256"}, {"indexed": False, "name": "amount1In", "type": "uint256"}, {"indexed": False, "name": "amount0Out", "type": "uint256"}, {"indexed": False, "name": "amount1Out", "type": "uint256"}, {"indexed": True, "name": "to", "type": "address"}], "name": "Swap", "type": "event"}
]

ERC20_ABI = [
    {"name": "decimals", "type": "function", "inputs": [], "outputs": [{"type": "uint8"}], "stateMutability": "view"},
    {"name": "symbol", "type": "function", "inputs": [], "outputs": [{"type": "string"}], "stateMutability": "view"}
]

# ================= INIT CONNECTION WITH AUTO-RETRY =================
def init_web3_with_retry(max_retries=5):
    """Initialize Web3 with retry logic"""
    global w3, active_rpc
    
    for attempt in range(max_retries):
        w3, active_rpc = rpc_manager.get_web3(force_refresh=(attempt > 0))
        if w3:
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

                print(f"✅ Connected to: {active_rpc}")
                print(f"✅ Pair: {sym0}/{sym1}")
                return contract, sym0, sym1, usd_index, mtc_index, usd_dec, mtc_dec, True
            except Exception as e:
                print(f"❌ Init failed (attempt {attempt+1}/{max_retries}): {e}")
                rpc_manager.failures[active_rpc] += 1
                time.sleep(2 ** attempt)  # Exponential backoff
        else:
            print(f"❌ No RPC available (attempt {attempt+1}/{max_retries})")
            time.sleep(5)
    
    return None, "ERROR", "ERROR", 0, 1, 18, 18, False

contract, sym0, sym1, usd_index, mtc_index, usd_dec, mtc_dec, CONNECTION_OK = init_web3_with_retry()

# ================= REDIS SETUP (same as before) =================
# ... (keep all your existing Redis code) ...

# ================= ENHANCED SWAP POLLER WITH RPC FAILOVER =================
class SwapPoller(threading.Thread):
    def __init__(self):
        super().__init__(daemon=True)
        self.last_block = w3.eth.block_number if w3 else 0
        self.running = True
        self.block_cache = {}
        self.consecutive_errors = 0
        self.rpc_refresh_counter = 0
        
    def refresh_web3_if_needed(self):
        """Auto-refresh Web3 connection if issues detected"""
        global w3, contract
        
        self.rpc_refresh_counter += 1
        
        # Refresh every 100 iterations or if errors > 5
        if self.rpc_refresh_counter >= 100 or self.consecutive_errors > 5:
            print("🔄 Refreshing Web3 connection...")
            new_w3, new_rpc = rpc_manager.get_web3(force_refresh=True)
            if new_w3:
                w3 = new_w3
                w3.middleware_onion.inject(geth_poa_middleware, layer=0)
                contract = w3.eth.contract(address=PAIR, abi=PAIR_ABI)
                active_rpc = new_rpc
                print(f"✅ Web3 refreshed to: {active_rpc}")
                self.consecutive_errors = 0
                self.rpc_refresh_counter = 0
                return True
            else:
                print("❌ Failed to refresh Web3 connection")
                self.consecutive_errors += 1
        return False
    
    def get_block_hash(self, block_num):
        if block_num not in self.block_cache:
            try:
                block = w3.eth.get_block(block_num)
                self.block_cache[block_num] = block.hash.hex()
                if len(self.block_cache) > 100:
                    oldest = min(self.block_cache.keys())
                    del self.block_cache[oldest]
                return self.block_cache[block_num]
            except:
                return ""
        return self.block_cache[block_num]

    def run(self):
        if not CONNECTION_OK:
            print("❌ Connection failed, poller not starting")
            return
            
        print("🔥 Swap poller started with AUTO-RPC FAILOVER")
        
        while self.running:
            try:
                # Auto-refresh Web3 if needed
                self.refresh_web3_if_needed()
                
                current_block = w3.eth.block_number
                block_hash = self.get_block_hash(current_block)
                
                if current_block > self.last_block:
                    range_size = min(50, current_block - self.last_block)
                    from_block = max(self.last_block + 1, current_block - range_size)
                    
                    # Fetch logs with retry for this specific RPC
                    try:
                        logs = contract.events.Swap.get_logs(
                            fromBlock=from_block, 
                            toBlock=current_block
                        )
                    except Exception as log_error:
                        print(f"⚠️ Log fetch failed: {log_error}")
                        self.consecutive_errors += 1
                        time.sleep(2)
                        continue
                    
                    # Get current price
                    reserves = contract.functions.getReserves().call()
                    if reserves:
                        r0, r1, _ = reserves
                        r_usd = normalize(r0 if usd_index == 0 else r1, usd_dec)
                        r_mtc = normalize(r1 if usd_index == 0 else r0, mtc_dec)
                        current_price = calc_price(r_usd, r_mtc)
                        
                        for log in logs:
                            process_swap_event(log, current_price, block_hash)
                    
                    self.last_block = current_block
                    self.consecutive_errors = 0
                    
                time.sleep(BLOCK_POLL_INTERVAL)
                
            except Exception as e:
                print(f"❌ Poller error: {e}")
                self.consecutive_errors += 1
                backoff = min(30, self.consecutive_errors * 2)
                time.sleep(backoff)

# ================= ADD RPC STATUS ENDPOINT =================
@app.route('/api/rpc-status')
def rpc_status():
    """Get RPC health status"""
    return {
        "healthy_rpcs": rpc_manager.get_healthy_rpc_count(),
        "total_rpcs": len(RPC_LIST),
        "current_rpc": RPC_LIST[rpc_manager.current_rpc] if rpc_manager.current_rpc < len(RPC_LIST) else "none",
        "failures": dict(rpc_manager.failures),
        "health": rpc_manager.rpc_health
    }

@app.route('/api/force-rpc-rotate')
def force_rpc_rotate():
    """Manually rotate RPC"""
    admin_key = request.headers.get('X-Admin-Key')
    if admin_key == os.environ.get('ADMIN_KEY'):
        rpc_manager.current_rpc = (rpc_manager.current_rpc + 1) % len(RPC_LIST)
        rpc_manager.failures.clear()
        return {"status": "rotated", "new_rpc": RPC_LIST[rpc_manager.current_rpc]}
    return {"error": "unauthorized"}, 401

# ================= MAIN (same as before) =================
if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.config['START_TIME'] = time.time()

    print("\n" + "="*60)
    print("🎯 TRUE DEX PRO - PRODUCTION READY (AUTO-RPC FAILOVER)")
    print("="*60)
    print(f"📡 Pair: {sym0}/{sym1}")
    print(f"🔌 Web3: {'✅ Connected' if CONNECTION_OK else '❌ Failed'}")
    print(f"🌐 Active RPC: {active_rpc if w3 else 'None'}")
    print(f"💪 Healthy RPCs: {rpc_manager.get_healthy_rpc_count()}/{len(RPC_LIST)}")
    print(f"💾 Redis: {'✅ Connected' if REDIS_OK else '⚠️ Using Memory Fallback'}")
    print(f"🔄 SSE Stream: /stream")
    print(f"📡 REST API: /api/state")
    print(f"🔍 RPC Status: /api/rpc-status")
    print(f"❤️ Health: /health")
    print(f"📊 Stats: /stats")
    print(f"🚪 Port: {port}")
    print("="*60 + "\n")

    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
