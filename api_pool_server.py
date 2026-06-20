"""
API Pool — 聚合 API 自动切换模块（GUI 版）

启动: python api_pool_server.py
访问: http://localhost:5100
"""

import os
import json
import time
import threading
import sqlite3
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, unquote
from concurrent.futures import ThreadPoolExecutor, as_completed
import queue
from datetime import datetime, timedelta

LATENCY_OK_MAX = 2000     
LATENCY_SLOW_MAX = 5000   
HEALTH_CHECK_INTERVAL = 120  

class LogManager:
    def __init__(self, max_history=300):
        self.history = []
        self.lock = threading.Lock()
        self.max_history = max_history
        self._counter = 0

    def log(self, level, msg):
        ts = time.time()
        time_str = datetime.fromtimestamp(ts).strftime('%H:%M:%S')
        with self.lock:
            self._counter += 1
            entry = {"id": self._counter, "time": time_str, "level": level, "msg": msg, "timestamp": ts}
            self.history.append(entry)
            if len(self.history) > self.max_history:
                self.history.pop(0)

    def get_logs_since(self, last_id):
        with self.lock:
            return [log for log in self.history if log["id"] > last_id]

sys_logger = LogManager()
def sys_log(msg, level="INFO"):
    sys_logger.log(level, msg)
    print(f"[{time.strftime('%H:%M:%S')}] [{level}] {msg}")

class TokenTracker:
    def __init__(self, db_path="token_stats.db"):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS token_usage (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    model TEXT,
                    prompt_tokens INTEGER,
                    completion_tokens INTEGER,
                    total_tokens INTEGER
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON token_usage(timestamp)")

    def add_usage(self, model, prompt_tokens, completion_tokens, total_tokens):
        def _do_insert():
            try:
                with sqlite3.connect(self.db_path, timeout=5) as conn:
                    conn.execute(
                        "INSERT INTO token_usage (model, prompt_tokens, completion_tokens, total_tokens) VALUES (?, ?, ?, ?)",
                        (model, prompt_tokens, completion_tokens, total_tokens)
                    )
            except Exception as e:
                sys_log(f"记录 token 消耗失败: {e}", "WARN")
        threading.Thread(target=_do_insert, daemon=True).start()

    def get_stats(self):
        with sqlite3.connect(self.db_path, timeout=5) as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT SUM(total_tokens) FROM token_usage WHERE timestamp >= date('now', 'localtime')")
            today = cursor.fetchone()[0] or 0
            cursor.execute("SELECT SUM(total_tokens) FROM token_usage WHERE timestamp >= date('now', '-2 days', 'localtime')")
            last_3_days = cursor.fetchone()[0] or 0
            cursor.execute("SELECT SUM(total_tokens) FROM token_usage WHERE timestamp >= date('now', '-6 days', 'localtime')")
            last_7_days = cursor.fetchone()[0] or 0
            cursor.execute("SELECT SUM(total_tokens) FROM token_usage WHERE timestamp >= date('now', '-29 days', 'localtime')")
            last_30_days = cursor.fetchone()[0] or 0
            
            cursor.execute("""
                SELECT date(timestamp, 'localtime') as d, SUM(total_tokens)
                FROM token_usage
                WHERE timestamp >= date('now', '-13 days', 'localtime')
                GROUP BY d
            """)
            raw_trend = dict(cursor.fetchall())
            trend_14d = []
            now = datetime.now()
            for i in range(13, -1, -1):
                d_str = (now - timedelta(days=i)).strftime('%Y-%m-%d')
                trend_14d.append({"date": d_str, "tokens": raw_trend.get(d_str, 0)})
                
            cursor.execute("""
                SELECT model, SUM(total_tokens)
                FROM token_usage
                WHERE date(timestamp, 'localtime') = date('now', 'localtime')
                GROUP BY model
                ORDER BY SUM(total_tokens) DESC
            """)
            today_models = [{"model": r[0], "tokens": r[1]} for r in cursor.fetchall()]
            
            cursor.execute("""
                SELECT model, SUM(total_tokens)
                FROM token_usage
                WHERE strftime('%Y-%m', timestamp, 'localtime') = strftime('%Y-%m', 'now', 'localtime')
                GROUP BY model
                ORDER BY SUM(total_tokens) DESC
            """)
            month_models = [{"model": r[0], "tokens": r[1]} for r in cursor.fetchall()]

            return {
                "today": today, "last_3_days": last_3_days, "last_7_days": last_7_days, "last_30_days": last_30_days,
                "trend_14d": trend_14d, "today_models": today_models, "month_models": month_models
            }

token_tracker = TokenTracker()

# ============================================================
#  数据结构
# ============================================================

@dataclass
class Endpoint:
    name: str = "unnamed"
    base_url: str = ""
    api_key: str = ""
    model: str = "gpt-4o-mini"
    priority: int = 999
    timeout: int = 15
    max_retries: int = 1
    enabled: bool = True
    cooldown_minutes: int = 5
    extra_headers: dict = field(default_factory=dict)

    _fail_count: int = field(default=0, repr=False)
    _last_error: str = field(default="", repr=False)
    _last_error_ts: float = field(default=0, repr=False)
    _last_success_ts: float = field(default=0, repr=False)
    _total_calls: int = field(default=0, repr=False)
    _total_failures: int = field(default=0, repr=False)
    _cooldown_until: float = field(default=0, repr=False)

    _health: str = field(default="unknown", repr=False) 
    _health_latency_ms: int = field(default=-1, repr=False)
    _health_last_check: float = field(default=0, repr=False)
    _health_error: str = field(default="", repr=False)


class AllEndpointsFailed(Exception):
    def __init__(self, errors: list):
        self.errors = errors
        super().__init__(f"All endpoints failed: {errors}")


# ============================================================
#  API Pool
# ============================================================

class APIPool:
    def __init__(self, endpoints=None, default_payload=None):
        self._lock = threading.Lock()
        self.default_payload = default_payload or {}
        self._endpoints: list[Endpoint] = []
        self._current_idx = 0
        if endpoints:
            for ep in endpoints:
                self.add_endpoint(ep)

    def add_endpoint(self, ep):
        if isinstance(ep, dict):
            ep = Endpoint(**{k: v for k, v in ep.items() if k in Endpoint.__dataclass_fields__})
        with self._lock:
            self._endpoints.append(ep)
            self._endpoints.sort(key=lambda e: e.priority)
            self._current_idx = 0

    def remove_endpoint(self, name):
        with self._lock:
            self._endpoints = [e for e in self._endpoints if e.name != name]
            self._current_idx = 0

    def set_enabled(self, name, enabled):
        with self._lock:
            for ep in self._endpoints:
                if ep.name == name:
                    ep.enabled = enabled
                    break

    def update_endpoint(self, name, updates: dict):
        with self._lock:
            for ep in self._endpoints:
                if ep.name == name:
                    for k, v in updates.items():
                        if hasattr(ep, k) and not k.startswith("_"):
                            setattr(ep, k, v)
                    self._endpoints.sort(key=lambda e: e.priority)
                    break

    def list_endpoints(self):
        now = time.time()
        with self._lock:
            active = [ep for ep in self._endpoints if ep.enabled]
            current_ep = active[self._current_idx] if active and self._current_idx < len(active) else None
            return [self._ep_to_dict(ep, ep is current_ep, now) for ep in self._endpoints]

    def _ep_to_dict(self, ep, is_current, now):
        return {
            "name": ep.name,
            "base_url": ep.base_url,
            "api_key": ep.api_key[:8] + "***" if len(ep.api_key) > 8 else "***",
            "api_key_full": ep.api_key,
            "model": ep.model,
            "priority": ep.priority,
            "timeout": ep.timeout,
            "max_retries": ep.max_retries,
            "enabled": ep.enabled,
            "cooldown_minutes": ep.cooldown_minutes,
            "fail_count": ep._fail_count,
            "last_error": ep._last_error,
            "last_success": ep._last_success_ts,
            "total_calls": ep._total_calls,
            "total_failures": ep._total_failures,
            "is_current": is_current,
            "in_cooldown": ep._cooldown_until > now,
            "cooldown_remaining": max(0, int(ep._cooldown_until - now)),
            "cooldown_until": ep._cooldown_until,
            "health": ep._health,
            "health_latency_ms": ep._health_latency_ms,
            "health_last_check": ep._health_last_check,
            "health_error": ep._health_error,
        }

    def get_active_chain(self):
        now = time.time()
        with self._lock:
            active = [ep for ep in self._endpoints if ep.enabled]
            current_ep = active[self._current_idx] if active and self._current_idx < len(active) else None
            return [
                {
                    "name": ep.name,
                    "model": ep.model,
                    "priority": ep.priority,
                    "is_current": ep is current_ep,
                    "fail_count": ep._fail_count,
                    "in_cooldown": ep._cooldown_until > now,
                    "cooldown_remaining": max(0, int(ep._cooldown_until - now)),
                    "health": ep._health,
                    "health_latency_ms": ep._health_latency_ms,
                    "health_error": ep._health_error,
                }
                for ep in active
            ]

    def reset(self):
        with self._lock:
            for ep in self._endpoints:
                ep._fail_count = 0
                ep._last_error = ""
                ep._last_error_ts = 0
                ep._cooldown_until = 0
            self._current_idx = 0

    def _check_one_health(self, ep):
        url = ep.base_url.rstrip("/") + "/chat/completions"
        payload = {"model": ep.model, "messages": [{"role": "user", "content": "ping"}], "max_tokens": 3}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {ep.api_key}")
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
                latency = int((time.time() - t0) * 1000)
                if latency <= LATENCY_OK_MAX:
                    return ep.name, "ok", latency, ""
                elif latency <= LATENCY_SLOW_MAX:
                    return ep.name, "slow", latency, ""
                else:
                    return ep.name, "bad", latency, f"延迟过高: {latency}ms"
        except urllib.error.HTTPError as e:
            latency = int((time.time() - t0) * 1000)
            err_body = ""
            try:
                err_body = e.read().decode("utf-8", errors="ignore")[:100]
            except Exception:
                pass
            return ep.name, "bad", latency, f"HTTP {e.code}: {err_body}"
        except Exception as e:
            latency = int((time.time() - t0) * 1000)
            return ep.name, "bad", latency, str(e)[:100]

    def check_all_health(self):
        with self._lock:
            endpoints = [ep for ep in self._endpoints if ep.enabled]
            for ep in endpoints:
                ep._health = "testing"
        if not endpoints:
            return []
        results = []
        with ThreadPoolExecutor(max_workers=min(len(endpoints), 10)) as pool_exec:
            futures = {pool_exec.submit(self._check_one_health, ep): ep for ep in endpoints}
            for future in as_completed(futures):
                try:
                    results.append(future.result())
                except Exception as e:
                    ep = futures[future]
                    results.append((ep.name, "bad", -1, str(e)))
        now = time.time()
        with self._lock:
            name_map = {ep.name: ep for ep in self._endpoints}
            for name, health, latency, error in results:
                ep = name_map.get(name)
                if ep:
                    ep._health = health
                    ep._health_latency_ms = latency
                    ep._health_last_check = now
                    ep._health_error = error
        sys_log(f"健康检测完成: 检测了 {len(endpoints)} 个端点", "INFO")
        return [{"name": n, "health": h, "latency_ms": l, "error": e} for n, h, l, e in results]

    def _is_in_cooldown(self, ep):
        return ep._cooldown_until > time.time()

    def _set_cooldown(self, ep):
        if ep.cooldown_minutes > 0:
            ep._cooldown_until = time.time() + ep.cooldown_minutes * 60

    def _clear_cooldown(self, ep):
        ep._cooldown_until = 0

    def _active_endpoints(self):
        available = [ep for ep in self._endpoints if ep.enabled and not self._is_in_cooldown(ep)]
        if available:
            return available
        return [ep for ep in self._endpoints if ep.enabled]

    def _pick_best(self, active):
        for ep in active:
            if not self._is_in_cooldown(ep):
                return ep
        return min(active, key=lambda e: e._cooldown_until) if active else None

    def _rotate(self, failed_ep, error_msg):
        failed_ep._fail_count += 1
        failed_ep._total_failures += 1
        failed_ep._last_error = error_msg
        failed_ep._last_error_ts = time.time()
        self._set_cooldown(failed_ep)
        sys_log(f"端点 '{failed_ep.name}' 触发冷却机制，下次可用时间在 {failed_ep.cooldown_minutes} 分钟后", "WARN")
        active = self._active_endpoints()
        if active:
            for i, ep in enumerate(active):
                if ep is failed_ep:
                    self._current_idx = (i + 1) % len(active)
                    return
            self._current_idx = 0

    def _on_success(self, ep):
        ep._total_calls += 1
        ep._last_success_ts = time.time()
        ep._fail_count = 0
        ep._last_error = ""
        self._clear_cooldown(ep)
        active = self._active_endpoints()
        best = self._pick_best(active)
        if best and best.priority < ep.priority:
            for i, e in enumerate(active):
                if e is best:
                    self._current_idx = i
                    return
        for i, e in enumerate(active):
            if e is ep:
                self._current_idx = i
                return

    def chat(self, messages, model=None, extra_payload=None, timeout=None):
        active = self._active_endpoints()
        if not active:
            raise ValueError("没有可用的 API 端点")
        errors = []
        tried = 0
        total = len(active)
        with self._lock:
            if self._current_idx >= total:
                self._current_idx = 0
            idx = self._current_idx
        while tried < total:
            ep = active[idx]
            ep_timeout = timeout or ep.timeout
            ep_model = model or ep.model
            payload = {
                "model": ep_model, "messages": messages,
                **self.default_payload, **(extra_payload or {}),
            }
            if tried == 0:
                sys_log(f"收到 API 请求，尝试请求端点 '{ep.name}' (模型: {ep_model})", "INFO")
            else:
                sys_log(f"重试请求，尝试端点 '{ep.name}' (模型: {ep_model})", "INFO")

            result, error = self._try_endpoint(ep, payload, ep_timeout)
            if result is not None:
                with self._lock:
                    self._on_success(ep)
                sys_log(f"端点 '{ep.name}' 请求成功 (延迟: 正常)", "INFO")
                return result
            errors.append(f"[{ep.name}] {error}")
            sys_log(f"端点 '{ep.name}' 请求失败: {error}", "ERROR")
            with self._lock:
                self._rotate(ep, error)
                active = self._active_endpoints()
                total = len(active)
                if total == 0:
                    break
                next_ep = None
                for i, e in enumerate(active):
                    if e is not ep and not self._is_in_cooldown(e):
                        next_ep = e
                        idx = i
                        break
                if next_ep is None:
                    for i, e in enumerate(active):
                        if e is ep:
                            idx = (i + 1) % len(active)
                            break
                tried += 1
        raise AllEndpointsFailed(errors)

    def _try_endpoint(self, ep, payload, timeout):
        url = ep.base_url.rstrip("/") + "/chat/completions"
        data = json.dumps(payload).encode("utf-8")
        is_stream = payload.get("stream", False)
        
        for attempt in range(ep.max_retries):
            req = urllib.request.Request(url, data=data, method="POST")
            req.add_header("Content-Type", "application/json")
            req.add_header("Authorization", f"Bearer {ep.api_key}")
            for k, v in ep.extra_headers.items():
                req.add_header(k, v)
            try:
                resp = urllib.request.urlopen(req, timeout=timeout)
                
                if is_stream:
                    def stream_generator():
                        try:
                            for line in resp:
                                if line.strip() and line.startswith(b"data: ") and not line.startswith(b"data: [DONE]"):
                                    try:
                                        chunk = json.loads(line[6:].decode("utf-8"))
                                        if "usage" in chunk and chunk["usage"]:
                                            u = chunk["usage"]
                                            token_tracker.add_usage(ep.model, u.get("prompt_tokens", 0), u.get("completion_tokens", 0), u.get("total_tokens", 0))
                                    except Exception:
                                        pass
                                yield line
                        except Exception:
                            pass
                        finally:
                            resp.close()
                    return stream_generator(), ""
                else:
                    body = json.loads(resp.read().decode("utf-8"))
                    u = body.get("usage", {})
                    if u:
                        token_tracker.add_usage(ep.model, u.get("prompt_tokens", 0), u.get("completion_tokens", 0), u.get("total_tokens", 0))
                    return body["choices"][0]["message"]["content"].strip(), ""
                    
            except urllib.error.HTTPError as e:
                err_body = ""
                try: err_body = e.read().decode("utf-8", errors="ignore")[:200]
                except Exception: pass
                msg = f"HTTP {e.code}: {err_body}"
                if e.code == 429: return None, msg + " (429 rate-limited)"
                if e.code in (401, 403): return None, msg + " (auth error)"
                if e.code >= 500:
                    if attempt < ep.max_retries - 1:
                        time.sleep(1.5 * (attempt + 1))
                        continue
                    return None, msg
                return None, msg
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                msg = f"连接/超时错误: {e}"
                if attempt < ep.max_retries - 1:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                return None, msg
            except Exception as e:
                return None, f"未知错误: {e}"
        return None, "重试次数用尽"

    def fetch_models(self, base_url, api_key, timeout=10):
        url = base_url.rstrip("/") + "/models"
        req = urllib.request.Request(url, method="GET")
        req.add_header("Authorization", f"Bearer {api_key}")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            raw = data.get("data", [])
            models = []
            for m in raw:
                mid = m.get("id", "")
                if not mid: continue
                info = {"id": mid}
                if "pricing" in m: info["pricing"] = m["pricing"]
                if "description" in m: info["description"] = m["description"][:120]
                info["modality"] = "unknown"
                info["modality_source"] = "none"
                models.append(info)
            models.sort(key=lambda x: x["id"])
            return models

    def test_vision(self, base_url, api_key, model, timeout=15):
        tiny_png = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwADhQGAWjR9awAAAABJRU5ErkJggg=="
        url = base_url.rstrip("/") + "/chat/completions"
        payload = {
            "model": model,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "describe this image in 3 words"}, {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{tiny_png}"}}]}],
            "max_tokens": 10,
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {api_key}")
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                latency = int((time.time() - t0) * 1000)
                reply = body["choices"][0]["message"]["content"].strip()
                return {"ok": True, "supports_vision": True, "latency_ms": latency, "reply": reply, "error": ""}
        except urllib.error.HTTPError as e:
            latency = int((time.time() - t0) * 1000)
            err_body = ""
            try: err_body = e.read().decode("utf-8", errors="ignore")[:200]
            except Exception: pass
            unsupported = e.code == 400 or "image" in err_body.lower() or "vision" in err_body.lower() or "content" in err_body.lower()
            return {"ok": True, "supports_vision": not unsupported, "latency_ms": latency, "reply": "", "error": f"HTTP {e.code}: {err_body}"}
        except Exception as e:
            latency = int((time.time() - t0) * 1000)
            return {"ok": False, "supports_vision": False, "latency_ms": latency, "reply": "", "error": str(e)}

    def test_model_latency(self, base_url, api_key, model, timeout=15):
        url = base_url.rstrip("/") + "/chat/completions"
        payload = {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        req.add_header("Authorization", f"Bearer {api_key}")
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
                latency = int((time.time() - t0) * 1000)
                reply = body["choices"][0]["message"]["content"].strip()
                status = "ok" if latency <= LATENCY_OK_MAX else ("slow" if latency <= LATENCY_SLOW_MAX else "bad")
                return {"ok": True, "status": status, "latency_ms": latency, "reply": reply, "error": ""}
        except urllib.error.HTTPError as e:
            latency = int((time.time() - t0) * 1000)
            err_body = ""
            try: err_body = e.read().decode("utf-8", errors="ignore")[:150]
            except Exception: pass
            return {"ok": False, "status": "bad", "latency_ms": latency, "reply": "", "error": f"HTTP {e.code}: {err_body}"}
        except Exception as e:
            latency = int((time.time() - t0) * 1000)
            return {"ok": False, "status": "bad", "latency_ms": latency, "reply": "", "error": str(e)}

CONFIG_FILE = "api_config.json"

def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f: return json.load(f).get("api_endpoints", [])
    except Exception:
        return []

def save_config(endpoints_data):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"api_endpoints": endpoints_data}, f, ensure_ascii=False, indent=2)

def ensure_config():
    if not os.path.exists(CONFIG_FILE): save_config([])

ensure_config()

def _health_check_loop():
    while True:
        time.sleep(HEALTH_CHECK_INTERVAL)
        try: pool.check_all_health()
        except Exception: pass

pool = APIPool(default_payload={"temperature": 0.7})
for ep_data in load_config(): pool.add_endpoint(ep_data)

_health_thread = threading.Thread(target=_health_check_loop, daemon=True)
_health_thread.start()
threading.Thread(target=pool.check_all_health, daemon=True).start()


def api_handler(method, path, body):
    parsed = urlparse(path)
    cp = parsed.path

    # ================= 代理接口 =================
    if method == "POST" and cp in ("/v1/chat/completions", "/chat/completions"):
        messages = body.get("messages", [])
        is_stream = body.get("stream", False)
        
        if is_stream:
            if "stream_options" not in body:
                body["stream_options"] = {"include_usage": True}
            elif isinstance(body["stream_options"], dict):
                body["stream_options"]["include_usage"] = True
                
        extra_payload = {k: v for k, v in body.items() if k not in ("messages", "model")}
        
        try:
            result = pool.chat(messages, extra_payload=extra_payload)
            if is_stream: return 200, result, True 
            
            response = {
                "id": f"chatcmpl-{int(time.time()*1000)}",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": "api-pool-aggregated",
                "choices": [{"index": 0, "message": {"role": "assistant", "content": result}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
            }
            return 200, response, False
            
        except AllEndpointsFailed as e:
            return 500, {"error": {"message": f"所有端点均已失效: {e.errors}", "type": "server_error"}}, False
        except Exception as e:
            return 500, {"error": {"message": str(e), "type": "server_error"}}, False

    if method == "GET" and cp == "/api/logs":
        qs = dict(q.split("=") for q in parsed.query.split("&") if "=" in q) if parsed.query else {}
        last_id = int(qs.get("since", 0))
        return 200, sys_logger.get_logs_since(last_id), False

    if method == "GET" and cp == "/api/token-stats": return 200, token_tracker.get_stats(), False

    if method == "GET" and cp == "/api/endpoints": return 200, pool.list_endpoints(), False
    if method == "GET" and cp == "/api/chain": return 200, pool.get_active_chain(), False
    if method == "POST" and cp == "/api/endpoints":
        pool.add_endpoint(body); _sync_to_config(); return 201, {"ok": True}, False
    if method == "POST" and cp == "/api/endpoints/batch":
        items = body.get("endpoints", []); base = body.get("base", {}); added = 0; start_priority = base.get("start_priority", 1)
        for i, item in enumerate(items):
            ep = {
                "name": item.get("name", f"ep_{i}"), "base_url": item.get("base_url", base.get("base_url", "")),
                "api_key": item.get("api_key", base.get("api_key", "")), "model": item.get("model", ""),
                "priority": item.get("priority", start_priority + i), "timeout": item.get("timeout", base.get("timeout", 15)),
                "max_retries": item.get("max_retries", base.get("max_retries", 1)), "cooldown_minutes": item.get("cooldown_minutes", base.get("cooldown_minutes", 5)),
                "enabled": item.get("enabled", True),
            }
            if ep["model"]: pool.add_endpoint(ep); added += 1
        _sync_to_config(); return 201, {"ok": True, "added": added}, False
    if method == "PUT" and cp.startswith("/api/endpoints/") and not cp.endswith("/toggle"):
        name = unquote(cp.split("/")[-1]); pool.update_endpoint(name, body); _sync_to_config(); return 200, {"ok": True}, False
    if method == "DELETE" and cp.startswith("/api/endpoints/"):
        name = unquote(cp.split("/")[-1]); pool.remove_endpoint(name); _sync_to_config(); return 200, {"ok": True}, False
    if method == "POST" and cp.endswith("/toggle"):
        name = unquote(cp.split("/")[3])
        for ep in pool.list_endpoints():
            if ep["name"] == name: pool.set_enabled(name, not ep["enabled"]); break
        _sync_to_config(); return 200, {"ok": True}, False
    if method == "POST" and cp == "/api/health-check": return 200, {"ok": True, "results": pool.check_all_health()}, False
    if method == "POST" and cp == "/api/fetch-models":
        base_url = body.get("base_url", ""); api_key = body.get("api_key", "")
        if not base_url or not api_key: return 400, {"error": "需要 base_url 和 api_key"}, False
        try:
            models = pool.fetch_models(base_url, api_key)
            return 200, {"ok": True, "models": models, "count": len(models)}, False
        except urllib.error.HTTPError as e:
            err_body = ""
            try: err_body = e.read().decode("utf-8", errors="ignore")[:200]
            except Exception: pass
            return 200, {"ok": False, "error": f"HTTP {e.code}: {err_body}"}, False
        except Exception as e: return 200, {"ok": False, "error": str(e)}, False
    if method == "POST" and cp == "/api/test-model": return 200, pool.test_model_latency(body.get("base_url", ""), body.get("api_key", ""), body.get("model", ""), timeout=body.get("timeout", 15)), False
    if method == "POST" and cp == "/api/test-vision": return 200, pool.test_vision(body.get("base_url", ""), body.get("api_key", ""), body.get("model", ""), timeout=body.get("timeout", 15)), False
    if method == "POST" and cp == "/api/test":
        name = body.get("name", ""); test_msg = body.get("message", "你好"); target_ep = None
        for ep in pool.list_endpoints():
            if ep["name"] == name: target_ep = ep; break
        if not target_ep: return 404, {"error": f"端点 {name} 不存在"}, False
        test_pool = APIPool(default_payload={"temperature": 0.7})
        test_pool.add_endpoint({"name": name, "base_url": target_ep["base_url"], "api_key": target_ep["api_key_full"], "model": target_ep["model"], "priority": 1, "timeout": target_ep["timeout"], "max_retries": target_ep["max_retries"], "enabled": True})
        try: return 200, {"ok": True, "result": test_pool.chat([{"role": "user", "content": test_msg}])}, False
        except Exception as e: return 200, {"ok": False, "error": str(e)}, False
    if method == "POST" and cp == "/api/test-pool":
        try: return 200, {"ok": True, "result": pool.chat([{"role": "user", "content": body.get("message", "你好")}])}, False
        except AllEndpointsFailed as e: return 200, {"ok": False, "errors": e.errors}, False
        except Exception as e: return 200, {"ok": False, "error": str(e)}, False
    if method == "POST" and cp == "/api/reset": pool.reset(); return 200, {"ok": True}, False

    return 404, {"error": "Not found"}, False

def _sync_to_config():
    save_config([{"name": ep["name"], "base_url": ep["base_url"], "api_key": ep["api_key_full"], "model": ep["model"], "priority": ep["priority"], "timeout": ep["timeout"], "max_retries": ep["max_retries"], "enabled": ep["enabled"], "cooldown_minutes": ep["cooldown_minutes"]} for ep in pool.list_endpoints()])


GUI_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>API Pool 聚合管理</title>
<style>
:root{
  --bg:#090a0f;--card:rgba(255,255,255,0.03);--card-hover:rgba(255,255,255,0.06);--border:rgba(255,255,255,0.08);
  --text:#ffffff;--text-dim:rgba(255,255,255,0.5);--accent:#5e5ce6;--accent-light:#7d7aff;
  --green:#32d74b;--green-dim:rgba(50,215,75,0.15);--red:#ff453a;--red-dim:rgba(255,69,58,0.15);
  --yellow:#ffd60a;--yellow-dim:rgba(255,214,10,0.15);--blue:#0a84ff;--blue-dim:rgba(10,132,255,0.15);
  --radius:16px;--shadow:0 8px 32px 0 rgba(0,0,0,0.3);--glass-blur:blur(24px);
}
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'SF Pro Text','Inter',system-ui,sans-serif;background-color:var(--bg);background-image:radial-gradient(circle at 15% 50%, rgba(94,92,230,0.2), transparent 50%),radial-gradient(circle at 85% 30%, rgba(10,132,255,0.2), transparent 50%),radial-gradient(circle at 50% 80%, rgba(255,69,58,0.15), transparent 50%);background-attachment:fixed;color:var(--text);min-height:100vh;padding:20px 24px;font-size:14px;line-height:1.5;-webkit-font-smoothing:antialiased}

.header{display:flex;align-items:center;justify-content:space-between;margin-bottom:24px;flex-wrap:wrap;gap:12px}
.header h1{font-size:20px;font-weight:700;letter-spacing:-.3px;display:flex;align-items:center;gap:10px}
.header h1 .logo{width:32px;height:32px;border-radius:8px;background:linear-gradient(135deg,var(--accent),var(--blue));display:flex;align-items:center;justify-content:center;font-size:16px}
.header-actions{display:flex;gap:8px;flex-wrap:wrap}

.btn{padding:7px 14px;border:none;border-radius:7px;font-size:12px;font-weight:600;cursor:pointer;transition:all .12s;display:inline-flex;align-items:center;gap:5px;letter-spacing:.2px}
.btn:hover{transform:translateY(-1px);filter:brightness(1.1)}
.btn:active{transform:translateY(0)}
.btn-primary{background:var(--accent);color:#fff}
.btn-green{background:var(--green);color:#000}
.btn-red{background:var(--red);color:#fff}
.btn-yellow{background:var(--yellow);color:#000}
.btn-ghost{background:transparent;color:var(--text-dim);border:1px solid var(--border)}
.btn-ghost:hover{border-color:var(--accent);color:var(--accent-light)}
.btn-sm{padding:4px 10px;font-size:11px;border-radius:6px}
.btn:disabled{opacity:.35;cursor:not-allowed;transform:none}

.api-info-card {
  background: rgba(94, 92, 230, 0.08); backdrop-filter: var(--glass-blur); -webkit-backdrop-filter: var(--glass-blur);
  border: 1px solid rgba(94, 92, 230, 0.3); border-radius: var(--radius); padding: 14px 18px; margin-bottom: 20px; box-shadow: var(--shadow);
}
.api-info-card code {
  background: var(--bg);
  padding: 3px 8px;
  border-radius: 4px;
  font-family: 'SF Mono', Menlo, Consolas, monospace;
  font-size: 12px;
  color: var(--accent-light);
  user-select: all;
  border: 1px solid var(--border);
}

.grid{display:grid;grid-template-columns:1fr 360px;gap:20px;align-items:start}
@media(max-width:920px){.grid{grid-template-columns:1fr}}

.card{background:var(--card);backdrop-filter:var(--glass-blur);-webkit-backdrop-filter:var(--glass-blur);border:1px solid var(--border);border-radius:var(--radius);padding:16px 18px;box-shadow:var(--shadow)}
.card-title{font-size:13px;font-weight:700;margin-bottom:14px;display:flex;align-items:center;gap:7px;color:var(--text-dim);text-transform:uppercase;letter-spacing:.6px}
.card-title .icon{font-size:15px}

.stats{display:grid;grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:16px}
.stat-item{background:rgba(255,255,255,.02);backdrop-filter:var(--glass-blur);-webkit-backdrop-filter:var(--glass-blur);border:1px solid var(--border);border-radius:12px;padding:12px 10px;text-align:center;transition:transform .2s,box-shadow .2s;box-shadow:0 2px 8px rgba(0,0,0,.1)}
.stat-item:hover{transform:translateY(-2px);box-shadow:0 4px 12px rgba(0,0,0,.2)}
.stat-item .num{font-size:20px;font-weight:700;font-variant-numeric:tabular-nums}
.stat-item .label{font-size:10px;color:var(--text-dim);margin-top:2px;text-transform:uppercase;letter-spacing:.5px}

.filter-bar{display:flex;gap:5px;margin-bottom:12px;flex-wrap:wrap;align-items:center}
.filter-btn{padding:4px 12px;border-radius:16px;font-size:11px;font-weight:600;cursor:pointer;border:1px solid var(--border);background:transparent;color:var(--text-dim);transition:all .12s}
.filter-btn:hover{border-color:var(--accent);color:var(--accent-light)}
.filter-btn.active{background:var(--accent);border-color:var(--accent);color:#fff}
.filter-count{font-size:11px;color:var(--text-dim);margin-left:auto}

.ep-list{display:flex;flex-direction:column;gap:6px}
.ep-item{background:rgba(255,255,255,.02);border:1px solid var(--border);border-radius:12px;padding:12px 14px;transition:all .2s cubic-bezier(0.2,0.8,0.2,1)}
.ep-item:hover{border-color:rgba(255,255,255,.15);background:var(--card-hover);transform:translateY(-1px);box-shadow:0 4px 12px rgba(0,0,0,.2)}
.ep-item.disabled{opacity:.4}
.ep-item.current{border-color:var(--green);background:var(--green-dim)}
.ep-item.in-cooldown{border-color:var(--yellow);background:var(--yellow-dim)}
.ep-item.has-error{border-color:var(--red);background:var(--red-dim)}
.ep-header{display:flex;align-items:center;justify-content:space-between;margin-bottom:6px;gap:6px;flex-wrap:wrap}
.ep-name{font-weight:700;font-size:13px;display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.badge{font-size:9px;padding:2px 7px;border-radius:12px;font-weight:700;display:inline-flex;align-items:center;gap:3px;text-transform:uppercase;letter-spacing:.3px}
.badge-current{background:var(--green-dim);color:var(--green)}
.badge-disabled{background:var(--red-dim);color:var(--red)}
.badge-cooldown{background:var(--yellow-dim);color:var(--yellow)}
.badge-priority{background:var(--accent);color:#fff;min-width:20px;justify-content:center}
.badge-h-ok{background:var(--green-dim);color:var(--green)}
.badge-h-slow{background:var(--yellow-dim);color:var(--yellow)}
.badge-h-bad{background:var(--red-dim);color:var(--red)}
.badge-h-unknown{background:rgba(255,255,255,.06);color:var(--text-dim)}
.ep-meta{display:flex;flex-wrap:wrap;gap:4px 14px;font-size:11px;color:var(--text-dim)}
.ep-meta span{display:flex;align-items:center;gap:3px}
.ep-actions{display:flex;gap:4px;flex-wrap:wrap}
.ep-error{margin-top:6px;font-size:11px;color:var(--red);background:var(--red-dim);padding:5px 8px;border-radius:6px;word-break:break-all}

.chain-list{display:flex;flex-direction:column;gap:0}
.chain-item{display:flex;align-items:center;gap:10px;padding:10px 12px;border-left:2px solid var(--border);font-size:12px;position:relative;transition:all .12s}
.chain-item:last-child{border-left-color:transparent}
.chain-item.active{border-left-color:var(--green);background:var(--green-dim);border-radius:0 8px 8px 0}
.chain-item.cooldown{border-left-color:var(--yellow);background:var(--yellow-dim);border-radius:0 8px 8px 0}
.chain-item.failed{border-left-color:var(--red);background:var(--red-dim);border-radius:0 8px 8px 0}
.chain-dot{width:8px;height:8px;border-radius:50%;background:var(--border);flex-shrink:0;position:absolute;left:-5px}
.chain-item.active .chain-dot{background:var(--green);box-shadow:0 0 6px var(--green)}
.chain-item.cooldown .chain-dot{background:var(--yellow)}
.chain-item.failed .chain-dot{background:var(--red)}
.chain-info{flex:1;min-width:0}
.chain-info .name{font-weight:600;font-size:12px}
.chain-info .model{font-size:10px;color:var(--text-dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chain-right{display:flex;flex-direction:column;align-items:flex-end;gap:2px;flex-shrink:0}
.chain-health{font-size:11px;font-weight:600}
.chain-err{font-size:10px;color:var(--red);max-width:120px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.chain-connector{height:12px;border-left:2px dashed var(--border);margin-left:0}

.modal-overlay{position:fixed;inset:0;background:rgba(0,0,0,.65);backdrop-filter:blur(6px);display:flex;align-items:center;justify-content:center;z-index:1000;opacity:0;pointer-events:none;transition:opacity .15s}
.modal-overlay.show{opacity:1;pointer-events:all}
.modal{background:var(--card);backdrop-filter:var(--glass-blur);-webkit-backdrop-filter:var(--glass-blur);border:1px solid var(--border);border-radius:16px;padding:24px;width:560px;max-width:94vw;max-height:88vh;overflow-y:auto;box-shadow:0 16px 48px rgba(0,0,0,.5)}
.modal h2{font-size:16px;margin-bottom:18px;font-weight:700}
.form-group{margin-bottom:12px}
.form-group label{display:block;font-size:11px;font-weight:600;color:var(--text-dim);margin-bottom:4px;text-transform:uppercase;letter-spacing:.5px}
.form-group input,.form-group select{width:100%;padding:9px 11px;background:rgba(0,0,0,0.2);border:1px solid var(--border);border-radius:10px;color:var(--text);font-size:13px;outline:none;transition:border-color .12s;box-shadow:inset 0 1px 2px rgba(0,0,0,0.1)}
.form-group input:focus,.form-group select:focus{border-color:var(--accent)}
.form-row{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.form-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:18px}

.model-row{display:flex;gap:8px;align-items:center}
.model-row input{flex:1}
.model-browser{margin-top:8px;border:1px solid var(--border);border-radius:8px;overflow:hidden}
.mb-toolbar{display:flex;gap:6px;padding:8px 10px;background:rgba(255,255,255,.02);border-bottom:1px solid var(--border);align-items:center;flex-wrap:wrap}
.mb-toolbar input[type=text]{flex:1;min-width:100px;padding:6px 9px;background:var(--bg);border:1px solid var(--border);border-radius:6px;color:var(--text);font-size:11px;outline:none}
.mb-toolbar input[type=text]:focus{border-color:var(--accent)}
.mb-toolbar label{font-size:11px;color:var(--text-dim);cursor:pointer;display:flex;align-items:center;gap:3px;white-space:nowrap}
.mb-toolbar .count{font-size:10px;color:var(--text-dim);white-space:nowrap}
.mb-table{max-height:300px;overflow-y:auto}
.mb-head{display:grid;grid-template-columns:28px 1fr 72px 80px 70px;gap:6px;padding:6px 10px;font-size:10px;color:var(--text-dim);text-transform:uppercase;letter-spacing:.4px;font-weight:600;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--card);z-index:1}
.mb-row{display:grid;grid-template-columns:28px 1fr 72px 80px 70px;gap:6px;padding:6px 10px;align-items:center;border-bottom:1px solid rgba(255,255,255,.03);font-size:12px;cursor:pointer;transition:background .08s}
.mb-row:last-child{border-bottom:none}
.mb-row:hover{background:var(--card-hover)}
.mb-row.selected{background:var(--accent);color:#fff}
.mb-row input[type=checkbox]{accent-color:var(--accent);cursor:pointer}
.mb-row .name-cell{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mb-row .mm-cell{text-align:center;font-size:11px}
.mm-yes{color:var(--green)}
.mm-no{color:var(--text-dim)}
.mm-unknown{color:var(--text-dim);opacity:.5}
.mb-row .price-cell{font-size:10px;color:var(--text-dim);white-space:nowrap}
.mb-row.selected .price-cell{color:rgba(255,255,255,.6)}
.free-tag{font-size:9px;background:var(--green-dim);color:var(--green);padding:1px 5px;border-radius:8px}
.mb-row.selected .free-tag{background:rgba(255,255,255,.2);color:#fff}
.mb-row .lat-cell{font-size:10px;white-space:nowrap}
.lat-ok{color:var(--green)}
.lat-slow{color:var(--yellow)}
.lat-bad{color:var(--red)}
.batch-bar{display:flex;align-items:center;justify-content:space-between;padding:8px 10px;background:var(--accent);color:#fff;border-radius:7px;margin-top:8px;font-size:12px;font-weight:600}
.pagination{display:flex;align-items:center;justify-content:center;gap:4px;padding:8px;border-top:1px solid var(--border);background:rgba(255,255,255,.015)}
.pagination .btn{min-width:28px;justify-content:center}
.pagination .page-info{font-size:11px;color:var(--text-dim)}

.toast{position:fixed;bottom:20px;right:20px;padding:10px 16px;border-radius:8px;font-size:12px;font-weight:600;z-index:2000;opacity:0;transform:translateY(8px);transition:all .2s;max-width:320px;word-break:break-all}
.toast.show{opacity:1;transform:translateY(0)}
.toast-success{background:var(--green);color:#000}
.toast-error{background:var(--red);color:#fff}
.toast-info{background:var(--accent);color:#fff}

.empty{text-align:center;padding:32px 16px;color:var(--text-dim);font-size:13px}
.test-input-row{display:flex;gap:6px}
.test-input-row input{flex:1;padding:7px 10px;background:var(--bg);border:1px solid var(--border);border-radius:7px;color:var(--text);font-size:12px;outline:none}
.test-result{margin-top:8px;padding:8px 10px;border-radius:7px;font-size:11px;word-break:break-all;max-height:130px;overflow-y:auto;white-space:pre-wrap;font-family:'SF Mono',Menlo,Consolas,monospace}
.test-result.success{background:var(--green-dim);color:var(--green)}
.test-result.failure{background:var(--red-dim);color:var(--red)}

.log-card { background: var(--card); backdrop-filter: var(--glass-blur); -webkit-backdrop-filter: var(--glass-blur); border: 1px solid var(--border); border-radius: var(--radius); padding: 16px 18px; box-shadow: var(--shadow); display: flex; flex-direction: column; }
.log-container { height: 280px; overflow-y: auto; background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 12px; font-family: 'SF Mono', Menlo, Consolas, monospace; font-size: 11px; display: flex; flex-direction: column; gap: 6px; scroll-behavior: smooth; }
.log-line { display: flex; gap: 8px; line-height: 1.5; word-break: break-all; }
.log-time { color: var(--text-dim); flex-shrink: 0; user-select: none; }
.log-INFO { color: var(--blue); flex-shrink: 0; min-width: 48px; text-align: center; }
.log-WARN { color: var(--yellow); flex-shrink: 0; min-width: 48px; text-align: center; }
.log-ERROR { color: var(--red); flex-shrink: 0; min-width: 48px; text-align: center; }
.log-msg { color: var(--text); }
</style>
</head>
<body>

<div class="header">
  <h1><span class="logo">⚡</span> API Pool</h1>
  <div class="header-actions">
    <button class="btn btn-ghost" onclick="openStatsModal()">📊 Token 统计</button>
    <button class="btn btn-ghost" onclick="runHealthCheck()">🩺 健康检测</button>
    <button class="btn btn-ghost" onclick="resetPool()">🔄 重置</button>
    <button class="btn btn-primary" onclick="openAddModal()">＋ 添加端点</button>
    <button class="btn btn-green" onclick="testPool()">🧪 测试聚合池</button>
  </div>
</div>

<div class="api-info-card">
  <div style="font-size: 13px; font-weight: 700; color: var(--accent-light); margin-bottom: 10px; text-transform: uppercase; letter-spacing: 0.5px;">🔗 客户端接入配置 (Client Config)</div>
  <div style="display: flex; gap: 24px; flex-wrap: wrap; font-size: 13px;">
    <div><span style="color: var(--text-dim); margin-right: 6px;">接口地址 (Base URL):</span><code id="displayUrl">http://localhost:5100/v1</code></div>
    <div><span style="color: var(--text-dim); margin-right: 6px;">API Key:</span><code>sk-any</code> <span style="font-size: 11px; color: var(--text-dim);">(任意填写)</span></div>
    <div><span style="color: var(--text-dim); margin-right: 6px;">模型 (Model):</span><code>api-pool</code> <span style="font-size: 11px; color: var(--text-dim);">(任意填写)</span></div>
  </div>
</div>

<div class="grid">
  <div>
    <div class="stats" id="stats"></div>
    <div class="card" style="margin-bottom:16px">
      <div class="card-title"><span class="icon">📋</span> 端点列表</div>
      <div class="filter-bar" id="filterBar"></div>
      <div class="ep-list" id="epList"></div>
    </div>
  </div>
  <div>
    <div class="card" style="margin-bottom:16px">
      <div class="card-title"><span class="icon">🔗</span> 聚合链</div>
      <div style="font-size:10px;color:var(--text-dim);margin-bottom:10px">遇 429/超时自动切换 · 冷却到期自动切回</div>
      <div class="chain-list" id="chainList"></div>
    </div>
    <div class="card">
      <div class="card-title"><span class="icon">🧪</span> 测试</div>
      <div class="test-input-row">
        <input type="text" id="testMsg" placeholder="测试消息..." value="用一句话介绍自己">
        <button class="btn btn-primary" onclick="testPool()">发送</button>
      </div>
      <div id="testResult"></div>
    </div>
  </div>
</div>

<div class="log-card" style="margin-top:20px;">
  <div class="card-title"><span class="icon">📝</span> 实时日志</div>
  <div class="log-container" id="logContainer"></div>
</div>

<div class="modal-overlay" id="modal">
  <div class="modal">
    <h2 id="modalTitle">添加端点</h2>
    <input type="hidden" id="editName">
    <div class="form-group"><label>名称</label><input type="text" id="fName" placeholder="主力 API"></div>
    <div class="form-group"><label>Base URL</label><input type="text" id="fUrl" placeholder="https://api.openai.com/v1" oninput="checkFetchBtn()"></div>
    <div class="form-group"><label>API Key</label><input type="password" id="fKey" placeholder="sk-..." oninput="checkFetchBtn()"></div>
    <div class="form-group">
      <label>模型</label>
      <div class="model-row">
        <input type="text" id="fModel" placeholder="gpt-4o">
        <button class="btn btn-yellow btn-sm" id="fetchModelsBtn" onclick="fetchModels()" disabled>🔍 获取</button>
      </div>
      <div id="modelBrowser" style="display:none"></div>
      <div id="batchBar" style="display:none"></div>
    </div>
    <div class="form-row">
      <div class="form-group"><label>优先级</label><input type="number" id="fPriority" value="1" min="1"></div>
      <div class="form-group"><label>超时 (秒)</label><input type="number" id="fTimeout" value="15" min="1"></div>
    </div>
    <div class="form-row">
      <div class="form-group"><label>重试次数</label><input type="number" id="fRetries" value="1" min="0"></div>
      <div class="form-group"><label>冷却 (分钟)</label><input type="number" id="fCooldown" value="5" min="0"></div>
    </div>
    <div class="form-group"><label>启用</label><select id="fEnabled"><option value="true">是</option><option value="false">否</option></select></div>
    <div class="form-actions">
      <button class="btn btn-ghost" onclick="closeModal()">取消</button>
      <button class="btn btn-green" id="batchAddBtn" style="display:none" onclick="batchAddEndpoints()">📦 批量添加</button>
      <button class="btn btn-primary" id="singleAddBtn" onclick="saveEndpoint()">保存</button>
    </div>
  </div>
</div>

<div class="modal-overlay" id="statsModal">
  <div class="modal" style="width: 800px; max-width: 95vw;">
    <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom: 18px;">
      <h2 style="margin:0;">📊 Token 使用统计</h2>
      <button class="btn btn-ghost btn-sm" onclick="closeStatsModal()">关闭</button>
    </div>
    
    <div class="stats" id="tokenStatsOverview"></div>
    
    <div class="card-title" style="margin-top:20px; font-size:11px;">近 14 天消耗趋势</div>
    <div id="tokenTrendChart" style="height: 140px; margin-bottom: 20px; position: relative;"></div>
    
    <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 20px;">
        <div>
            <div class="card-title" style="font-size:11px;">今日模型明细</div>
            <div style="max-height: 200px; overflow-y: auto;">
              <table style="width: 100%; border-collapse: collapse; font-size: 11px; text-align: left;">
                <thead><tr style="border-bottom: 1px solid var(--border); color: var(--text-dim);"><th style="padding: 6px;">模型</th><th style="padding: 6px; text-align:right;">Token</th></tr></thead>
                <tbody id="todayModelsTable"></tbody>
              </table>
            </div>
        </div>
        <div>
            <div class="card-title" style="font-size:11px;">本月模型明细</div>
            <div style="max-height: 200px; overflow-y: auto;">
              <table style="width: 100%; border-collapse: collapse; font-size: 11px; text-align: left;">
                <thead><tr style="border-bottom: 1px solid var(--border); color: var(--text-dim);"><th style="padding: 6px;">模型</th><th style="padding: 6px; text-align:right;">Token</th></tr></thead>
                <tbody id="monthModelsTable"></tbody>
              </table>
            </div>
        </div>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
document.getElementById('displayUrl').textContent = window.location.protocol + '//' + window.location.host + '/v1';

const API='';
async function api(method,path,body){const opts={method,headers:{'Content-Type':'application/json'}};if(body)opts.body=JSON.stringify(body);return(await fetch(API+path,opts)).json();}

let allModels=[],selectedModels=new Set(),latencyResults={},visionResults={};
let epFilter='all',modelPage=1,PP=50;

async function refresh(){
  const[eps,chain]=await Promise.all([api('GET','/api/endpoints'),api('GET','/api/chain')]);
  renderStats(eps);renderFilterBar(eps);renderEndpoints(eps);renderChain(chain);
}

function renderStats(eps){
  const t=eps.length,en=eps.filter(e=>e.enabled).length,h=eps.filter(e=>e.health==='ok').length;
  const cl=eps.filter(e=>e.in_cooldown).length,bad=eps.filter(e=>e.health==='bad').length;
  const calls=eps.reduce((s,e)=>s+e.total_calls,0);
  document.getElementById('stats').innerHTML=`
    <div class="stat-item"><div class="num">${t}</div><div class="label">端点</div></div>
    <div class="stat-item"><div class="num" style="color:var(--green)">${en}</div><div class="label">启用</div></div>
    <div class="stat-item"><div class="num" style="color:var(--green)">${h}</div><div class="label">健康</div></div>
    <div class="stat-item"><div class="num" style="color:${bad?'var(--red)':'var(--text-dim)'}">${bad}</div><div class="label">异常</div></div>
    <div class="stat-item"><div class="num" style="color:${cl?'var(--yellow)':'var(--text-dim)'}">${cl}</div><div class="label">冷却</div></div>
    <div class="stat-item"><div class="num" style="color:var(--blue)">${calls}</div><div class="label">调用</div></div>`;
}

function renderFilterBar(eps){
  const en=eps.filter(e=>e.enabled).length;
  document.getElementById('filterBar').innerHTML=`
    <button class="filter-btn ${epFilter==='all'?'active':''}" onclick="setFilter('all')">全部 ${eps.length}</button>
    <button class="filter-btn ${epFilter==='enabled'?'active':''}" onclick="setFilter('enabled')">启用 ${en}</button>
    <button class="filter-btn ${epFilter==='disabled'?'active':''}" onclick="setFilter('disabled')">禁用 ${eps.length-en}</button>
    <span class="filter-count" id="filterCount"></span>`;
}
function setFilter(f){epFilter=f;refresh();}

function hBadge(h,lat){
  const m={ok:['badge-h-ok','✅'],slow:['badge-h-slow','🐢'],bad:['badge-h-bad','❌'],unknown:['badge-h-unknown','❓'],testing:['badge-h-unknown','⏳']};
  const[c,l]=m[h]||m.unknown;
  return`<span class="badge ${c}">${l}${lat>=0?' '+lat+'ms':''}</span>`;
}

function renderEndpoints(eps){
  if(epFilter==='enabled')eps=eps.filter(e=>e.enabled);
  else if(epFilter==='disabled')eps=eps.filter(e=>!e.enabled);
  const c=document.getElementById('filterCount');if(c)c.textContent=`${eps.length} 个`;
  const el=document.getElementById('epList');
  if(!eps.length){el.innerHTML='<div class="empty">暂无端点</div>';return;}
  el.innerHTML=eps.map(ep=>{
    let cls='ep-item';
    if(!ep.enabled)cls+=' disabled';
    if(ep.is_current)cls+=' current';
    if(ep.in_cooldown)cls+=' in-cooldown';
    if(ep.last_error&&!ep.in_cooldown)cls+=' has-error';
    let b=`<span class="badge badge-priority">#${ep.priority}</span>${hBadge(ep.health,ep.health_latency_ms)}`;
    if(ep.is_current)b+='<span class="badge badge-current">● 当前</span>';
    if(!ep.enabled)b+='<span class="badge badge-disabled">禁用</span>';
    if(ep.in_cooldown)b+=`<span class="badge badge-cooldown">⏳${fmtTime(ep.cooldown_remaining)}</span>`;
    const last=ep.last_success?timeAgo(ep.last_success):'—';
    return`<div class="${cls}">
      <div class="ep-header">
        <div class="ep-name">${esc(ep.name)} ${b}</div>
        <div class="ep-actions">
          <button class="btn btn-ghost btn-sm" title="连通性测试" onclick="testEndpoint('${esc(ep.name)}')">🧪</button>
          ${ep.in_cooldown?`<button class="btn btn-yellow btn-sm" title="立刻解除冷却" onclick="clearCooldown('${esc(ep.name)}')">⏰</button>`:''}
          <button class="btn btn-ghost btn-sm" title="${ep.enabled?'禁用端点':'启用端点'}" onclick="toggleEndpoint('${esc(ep.name)}')">${ep.enabled?'⏸':'▶'}</button>
          <button class="btn btn-ghost btn-sm" title="编辑端点" onclick="editEndpoint('${esc(ep.name)}')">✏️</button>
          <button class="btn btn-ghost btn-sm" title="删除端点" onclick="deleteEndpoint('${esc(ep.name)}')" style="color:var(--red)">🗑</button>
        </div>
      </div>
      <div class="ep-meta">
        <span title="绑定的模型名称">🤖${esc(ep.model)}</span><span title="单次请求超时时间">⏱${ep.timeout}s</span><span title="失败后最大重试次数">🔁${ep.max_retries}次</span><span title="请求失败后的冷却惩罚时间">❄️${ep.cooldown_minutes}分</span><span title="累计成功响应次数">📞${ep.total_calls}次</span><span title="最后一次成功响应时间">🕐${last}</span>
      </div>
      ${ep.last_error?`<div class="ep-error">⚠ ${esc(ep.last_error)}</div>`:''}
    </div>`;
  }).join('');
}

function renderChain(chain){
  const el=document.getElementById('chainList');
  if(!chain.length){el.innerHTML='<div class="empty">没有启用的端点</div>';return;}
  el.innerHTML=chain.map((it,i)=>{
    let cls='chain-item';
    if(it.is_current)cls+=' active';
    if(it.in_cooldown)cls+=' cooldown';
    else if(it.health==='bad')cls+=' failed';
    const h=it.health,lat=it.health_latency_ms;
    let rh='';
    if(h==='ok')rh=`<div class="chain-health" style="color:var(--green)">✅${lat>=0?' '+lat+'ms':''}</div>`;
    else if(h==='slow')rh=`<div class="chain-health" style="color:var(--yellow)">🐢${lat>=0?' '+lat+'ms':''}</div>`;
    else if(h==='bad'){rh=`<div class="chain-health" style="color:var(--red)">❌${lat>=0?' '+lat+'ms':''}</div>`;if(it.health_error)rh+=`<div class="chain-err" title="${esc(it.health_error)}">${esc(it.health_error)}</div>`;}
    else if(h==='testing')rh='<div class="chain-health" style="color:var(--text-dim)">⏳</div>';
    else rh='<div class="chain-health" style="color:var(--text-dim)">❓</div>';
    let st='';
    if(it.is_current)st='<span style="color:var(--green);font-size:10px">← 当前</span>';
    else if(it.in_cooldown)st=`<span style="color:var(--yellow);font-size:10px">⏳${fmtTime(it.cooldown_remaining)}</span>`;
    const conn=i<chain.length-1?'<div class="chain-connector"></div>':'';
    return`<div class="${cls}"><div class="chain-dot"></div><div class="chain-info"><div class="name">${esc(it.name)} ${st}</div><div class="model">${esc(it.model)}</div></div><div class="chain-right">${rh}</div></div>${conn}`;
  }).join('');
}

async function runHealthCheck(){toast('正在检测...','info');const r=await api('POST','/api/health-check');if(r.ok){const o=r.results.filter(x=>x.health==='ok').length,s=r.results.filter(x=>x.health==='slow').length,b=r.results.filter(x=>x.health==='bad').length;toast(`✅${o} 🐢${s} ❌${b}`,'success');}refresh();}
async function toggleEndpoint(n){await api('POST',`/api/endpoints/${encodeURIComponent(n)}/toggle`);refresh();}
async function deleteEndpoint(n){if(!confirm(`删除「${n}」？`))return;await api('DELETE',`/api/endpoints/${encodeURIComponent(n)}`);toast('已删除','success');refresh();}
async function clearCooldown(n){await api('PUT',`/api/endpoints/${encodeURIComponent(n)}`,{cooldown_minutes:0});await api('POST','/api/reset');setTimeout(async()=>{await api('PUT',`/api/endpoints/${encodeURIComponent(n)}`,{cooldown_minutes:5});refresh();},200);toast('已解除冷却','success');refresh();}
async function testEndpoint(n){const m=document.getElementById('testMsg').value||'你好';toast('测试中...','info');const r=await api('POST','/api/test',{name:n,message:m});const el=document.getElementById('testResult');if(r.ok){el.className='test-result success';el.textContent='✅ '+r.result;}else{el.className='test-result failure';el.textContent='❌ '+(r.error||JSON.stringify(r.errors));}refresh();}
async function testPool(){const m=document.getElementById('testMsg').value||'你好';toast('测试聚合池...','info');const r=await api('POST','/api/test-pool',{message:m});const el=document.getElementById('testResult');if(r.ok){el.className='test-result success';el.textContent='✅ '+r.result;}else{el.className='test-result failure';el.textContent='❌ '+(r.error||r.errors?.join('\n'));}refresh();}
async function resetPool(){await api('POST','/api/reset');toast('已重置','success');refresh();}

function checkFetchBtn(){const u=document.getElementById('fUrl').value.trim(),k=document.getElementById('fKey').value.trim();document.getElementById('fetchModelsBtn').disabled=!(u&&k);}
async function fetchModels(){
  const u=document.getElementById('fUrl').value.trim(),k=document.getElementById('fKey').value.trim();
  if(!u||!k){toast('填写 URL 和 Key','error');return;}
  const b=document.getElementById('fetchModelsBtn');b.disabled=true;b.innerHTML='⏳';
  try{const r=await api('POST','/api/fetch-models',{base_url:u,api_key:k});
    if(r.ok&&r.models?.length){allModels=r.models;selectedModels=new Set();latencyResults={};visionResults={};modelPage=1;renderModelBrowser();toast(`${r.count} 个模型`,'success');}
    else{document.getElementById('modelBrowser').innerHTML=`<div style="padding:10px;color:var(--red);font-size:12px">❌ ${esc(r.error||'无模型')}</div>`;document.getElementById('modelBrowser').style.display='block';}
  }catch(e){toast('请求失败','error');}
  b.disabled=false;b.innerHTML='🔍 获取';
}
function isOpenRouter(){return document.getElementById('fUrl').value.includes('openrouter');}
function isFreeModel(m){if(!m.pricing)return false;return parseFloat(m.pricing.prompt||'1')===0&&parseFloat(m.pricing.completion||'1')===0;}

function renderModelBrowser(){
  const el=document.getElementById('modelBrowser');
  el.innerHTML=`<div class="model-browser">
    <div class="mb-toolbar">
      <input type="text" id="modelSearch" placeholder="搜索..." oninput="modelPage=1;filterModels()">
      <button class="btn btn-ghost btn-sm" onclick="selectAll()">全选</button>
      <button class="btn btn-ghost btn-sm" onclick="selectNone()">清空</button>
      <button class="btn btn-ghost btn-sm" onclick="testSelectedLatency()">⏱ 延迟</button>
      <button class="btn btn-ghost btn-sm" onclick="testSelectedVision()">🖼 多模态</button>
      ${isOpenRouter()?`<label><input type="checkbox" id="freeOnly" onchange="modelPage=1;filterModels()"> 🆓免费</label>`:''}
      <span class="count" id="modelCount"></span>
    </div>
    <div class="mb-head"><span></span><span>模型</span><span style="text-align:center">多模态</span><span>价格</span><span>延迟</span></div>
    <div class="mb-table" id="modelListInner"></div>
    <div class="pagination" id="modelPagination" style="display:none"></div>
  </div>`;
  el.style.display='block';
  filterModels();
}

function getFilteredModels(){
  const q=(document.getElementById('modelSearch')?.value||'').toLowerCase();
  const fo=document.getElementById('freeOnly')?.checked||false;
  let f=allModels;if(fo)f=f.filter(m=>isFreeModel(m));if(q)f=f.filter(m=>m.id.toLowerCase().includes(q));return f;
}

function filterModels(){
  const f=getFilteredModels();
  const tp=Math.max(1,Math.ceil(f.length/PP));
  if(modelPage>tp)modelPage=tp;
  const si=(modelPage-1)*PP,pg=f.slice(si,si+PP);
  const inner=document.getElementById('modelListInner');
  const c=document.getElementById('modelCount');if(c)c.textContent=`${f.length}/${allModels.length}`;
  inner.innerHTML=pg.map(m=>{
    const sel=selectedModels.has(m.id);
    const vr=visionResults[m.id];
    let mm='<span class="mm-unknown">—</span>';
    if(vr){mm=vr.supports_vision?'<span class="mm-yes">✅ 图片</span>':'<span class="mm-no">❌</span>';}
    const lat=latencyResults[m.id];
    let lh='';
    if(lat){if(lat.status==='ok')lh=`<span class="lat-ok">✓${lat.latency_ms}ms</span>`;else if(lat.status==='slow')lh=`<span class="lat-slow">🐢${lat.latency_ms}ms</span>`;else lh=`<span class="lat-bad">✗${lat.latency_ms}ms</span>`;}
    let ph='';
    if(m.pricing){if(isFreeModel(m))ph='<span class="free-tag">FREE</span>';else ph=`<span style="font-size:10px;color:var(--text-dim)">$${m.pricing.prompt||'0'}/$${m.pricing.completion||'0'}</span>`;}
    return`<div class="mb-row${sel?' selected':''}" onclick="event.target.tagName!=='INPUT'&&toggleModel('${esc(m.id)}')">
      <input type="checkbox" ${sel?'checked':''} onclick="event.stopPropagation();toggleModel('${esc(m.id)}')">
      <span class="name-cell" title="${esc(m.id)}">${esc(m.id)}</span>
      <span class="mm-cell">${mm}</span>
      <span class="price-cell">${ph}</span>
      <span class="lat-cell">${lh}</span>
    </div>`;
  }).join('');
  const pag=document.getElementById('modelPagination');
  if(tp>1){pag.style.display='flex';pag.innerHTML=`
    <button class="btn btn-ghost btn-sm" onclick="modelPage=1;filterModels()" ${modelPage===1?'disabled':''}>⏮</button>
    <button class="btn btn-ghost btn-sm" onclick="modelPage--;filterModels()" ${modelPage===1?'disabled':''}>◀</button>
    <span class="page-info">${modelPage}/${tp}</span>
    <button class="btn btn-ghost btn-sm" onclick="modelPage++;filterModels()" ${modelPage===tp?'disabled':''}>▶</button>
    <button class="btn btn-ghost btn-sm" onclick="modelPage=${tp};filterModels()" ${modelPage===tp?'disabled':''}>⏭</button>`;}
  else pag.style.display='none';
  updateBatchBar();
}

function toggleModel(id){selectedModels.has(id)?selectedModels.delete(id):selectedModels.add(id);if(selectedModels.size===1)document.getElementById('fModel').value=[...selectedModels][0];else if(!selectedModels.size)document.getElementById('fModel').value='';filterModels();}
function selectAll(){getFilteredModels().forEach(m=>selectedModels.add(m.id));filterModels();}
function selectNone(){selectedModels.clear();document.getElementById('fModel').value='';filterModels();}

function updateBatchBar(){
  const bar=document.getElementById('batchBar'),bb=document.getElementById('batchAddBtn'),sb=document.getElementById('singleAddBtn');
  if(selectedModels.size>1){bar.style.display='block';bar.innerHTML=`<div class="batch-bar"><span>已选 ${selectedModels.size} 个模型</span></div>`;bb.style.display='inline-flex';sb.style.display='none';}
  else{bar.style.display='none';bb.style.display='none';sb.style.display='inline-flex';}
}

async function testSelectedLatency(){
  const u=document.getElementById('fUrl').value.trim(),k=document.getElementById('fKey').value.trim();
  if(!u||!k){toast('填写 URL 和 Key','error');return;}
  if(!selectedModels.size){toast('勾选模型','error');return;}
  const ms=[...selectedModels];toast(`测试 ${ms.length} 个...`,'info');
  for(const mid of ms){latencyResults[mid]={status:'bad',latency_ms:0};filterModels();try{latencyResults[mid]=await api('POST','/api/test-model',{base_url:u,api_key:k,model:mid});}catch(e){latencyResults[mid]={status:'bad',latency_ms:0};}filterModels();}
  toast(`✅${Object.values(latencyResults).filter(r=>r.ok).length}/${ms.length}`,'success');
}

async function testSelectedVision(){
  const u=document.getElementById('fUrl').value.trim(),k=document.getElementById('fKey').value.trim();
  if(!u||!k){toast('填写 URL 和 Key','error');return;}
  if(!selectedModels.size){toast('勾选模型','error');return;}
  const ms=[...selectedModels];toast(`检测 ${ms.length} 个多模态...`,'info');
  let vis=0;
  for(const mid of ms){visionResults[mid]={supports_vision:false};filterModels();try{const r=await api('POST','/api/test-vision',{base_url:u,api_key:k,model:mid});visionResults[mid]=r;if(r.supports_vision)vis++;}catch(e){visionResults[mid]={supports_vision:false};}filterModels();}
  toast(`多模态: ${vis}/${ms.length} 支持`,'success');
}

function openAddModal(){
  document.getElementById('editName').value='';document.getElementById('modalTitle').textContent='添加端点';
  ['fName','fUrl','fKey','fModel'].forEach(id=>document.getElementById(id).value='');
  document.getElementById('fPriority').value=1;document.getElementById('fTimeout').value=15;document.getElementById('fRetries').value=1;document.getElementById('fCooldown').value=5;document.getElementById('fEnabled').value='true';
  document.getElementById('modelBrowser').style.display='none';document.getElementById('batchBar').style.display='none';
  document.getElementById('fetchModelsBtn').disabled=true;document.getElementById('batchAddBtn').style.display='none';document.getElementById('singleAddBtn').style.display='inline-flex';
  allModels=[];selectedModels=new Set();latencyResults={};visionResults={};
  document.getElementById('modal').classList.add('show');
}
function editEndpoint(name){
  api('GET','/api/endpoints').then(eps=>{const ep=eps.find(e=>e.name===name);if(!ep)return;
    document.getElementById('editName').value=name;document.getElementById('modalTitle').textContent='编辑端点';
    document.getElementById('fName').value=ep.name;document.getElementById('fUrl').value=ep.base_url;document.getElementById('fKey').value=ep.api_key_full||'';document.getElementById('fModel').value=ep.model;
    document.getElementById('fPriority').value=ep.priority;document.getElementById('fTimeout').value=ep.timeout;document.getElementById('fRetries').value=ep.max_retries;document.getElementById('fCooldown').value=ep.cooldown_minutes;document.getElementById('fEnabled').value=String(ep.enabled);
    document.getElementById('modelBrowser').style.display='none';document.getElementById('batchBar').style.display='none';document.getElementById('batchAddBtn').style.display='none';document.getElementById('singleAddBtn').style.display='inline-flex';
    allModels=[];selectedModels=new Set();latencyResults={};visionResults={};checkFetchBtn();document.getElementById('modal').classList.add('show');
  });
}
function closeModal(){document.getElementById('modal').classList.remove('show');}

async function saveEndpoint(){
  const en=document.getElementById('editName').value;
  const d={name:document.getElementById('fName').value.trim(),base_url:document.getElementById('fUrl').value.trim(),api_key:document.getElementById('fKey').value.trim(),model:document.getElementById('fModel').value.trim(),priority:parseInt(document.getElementById('fPriority').value)||1,timeout:parseInt(document.getElementById('fTimeout').value)||15,max_retries:parseInt(document.getElementById('fRetries').value)||1,cooldown_minutes:parseInt(document.getElementById('fCooldown').value)||0,enabled:document.getElementById('fEnabled').value==='true'};
  if(!d.name||!d.base_url||!d.api_key){toast('填写名称/URL/Key','error');return;}
  if(!d.model){toast('选择模型','error');return;}
  if(en){await api('PUT',`/api/endpoints/${encodeURIComponent(en)}`,d);toast('已更新','success');}
  else{await api('POST','/api/endpoints',d);toast('已添加','success');}
  closeModal();refresh();
}

async function batchAddEndpoints(){
  const fn=document.getElementById('fName').value.trim();
  const u=document.getElementById('fUrl').value.trim(),k=document.getElementById('fKey').value.trim();
  const sp=parseInt(document.getElementById('fPriority').value)||1,to=parseInt(document.getElementById('fTimeout').value)||15,re=parseInt(document.getElementById('fRetries').value)||1,cd=parseInt(document.getElementById('fCooldown').value)||5;
  if(!u||!k){toast('填写 URL 和 Key','error');return;}
  if(!selectedModels.size){toast('选择模型','error');return;}
  const ms=[...selectedModels];toast(`添加 ${ms.length} 个...`,'info');
  const r=await api('POST','/api/endpoints/batch',{endpoints:ms.map((m,i)=>({name:fn?`${fn} - ${m}`:m,model:m,priority:sp+i})),base:{base_url:u,api_key:k,timeout:to,max_retries:re,cooldown_minutes:cd,start_priority:sp}});
  if(r.ok){toast(`✅ ${r.added} 个`,'success');closeModal();refresh();}else toast('失败','error');
}

function esc(s){const d=document.createElement('div');d.textContent=s;return d.innerHTML;}
function timeAgo(ts){if(!ts)return'—';const s=Math.floor(Date.now()/1000-ts);if(s<60)return s+'s前';if(s<3600)return Math.floor(s/60)+'m前';if(s<86400)return Math.floor(s/3600)+'h前';return Math.floor(s/86400)+'d前';}
function fmtTime(s){if(s<=0)return'';if(s<60)return s+'s';const m=Math.floor(s/60);return(s%60)?`${m}m${s%60}s`:`${m}m`;}
function toast(msg,type){const el=document.getElementById('toast');el.textContent=msg;el.className='toast toast-'+type+' show';setTimeout(()=>el.classList.remove('show'),2500);}

function closeStatsModal(){document.getElementById('statsModal').classList.remove('show');}
function fmtNum(n) {
    if (!n) return '0';
    if (n >= 100000000) return (n / 100000000).toFixed(2).replace(/\.00$/, '') + ' 亿';
    if (n >= 10000) return (n / 10000).toFixed(2).replace(/\.00$/, '') + ' 万';
    return n.toLocaleString();
}

function drawSVGChart(containerId, data) {
    const container = document.getElementById(containerId);
    if (!data || data.length === 0) {
        container.innerHTML = '<div class="empty">暂无趋势数据</div>';
        return;
    }
    const maxVal = Math.max(...data.map(d => d.tokens)) || 1;
    const padding = 10;
    const w = container.clientWidth || 800;
    const h = 140;
    
    let pts = [];
    data.forEach((d, i) => {
        const x = padding + (i / Math.max(1, data.length - 1)) * (w - 2 * padding);
        const y = h - padding - (d.tokens / maxVal) * (h - 2 * padding);
        pts.push(`${x},${y}`);
    });
    
    container.innerHTML = `
        <svg viewBox="0 0 ${w} ${h}" style="width:100%; height:100%; overflow:visible;">
            <defs>
                <linearGradient id="chartGrad" x1="0" y1="0" x2="0" y2="1">
                    <stop offset="0%" stop-color="rgba(124, 109, 240, 0.4)"/>
                    <stop offset="100%" stop-color="rgba(124, 109, 240, 0.0)"/>
                </linearGradient>
            </defs>
            <polygon points="${pts[0].split(',')[0]},${h} ${pts.join(' ')} ${pts[pts.length-1].split(',')[0]},${h}" fill="url(#chartGrad)"/>
            <polyline points="${pts.join(' ')}" fill="none" stroke="var(--accent)" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>
            ${data.map((d, i) => {
                const [x, y] = pts[i].split(',');
                return `<circle cx="${x}" cy="${y}" r="4" fill="var(--bg)" stroke="var(--accent)" stroke-width="2" class="chart-point" data-idx="${i}" style="cursor:pointer; transition:r 0.1s;"/>`;
            }).join('')}
        </svg>
        <div id="${containerId}_tt" style="position:absolute; display:none; background:var(--card); border:1px solid var(--border); border-radius:6px; padding:6px 10px; font-size:11px; box-shadow:var(--shadow); pointer-events:none; z-index:10; white-space:nowrap;"></div>
    `;
    
    const tt = document.getElementById(`${containerId}_tt`);
    container.querySelectorAll('.chart-point').forEach(c => {
        c.addEventListener('mouseenter', (e) => {
            const idx = e.target.getAttribute('data-idx');
            const d = data[idx];
            c.setAttribute('r', '6');
            tt.style.display = 'block';
            tt.innerHTML = `<div style="color:var(--text-dim);margin-bottom:2px">${d.date}</div><div style="font-weight:600;color:var(--accent-light)">${fmtNum(d.tokens)} Tokens</div>`;
            
            let tx = parseFloat(c.getAttribute('cx')) + 10;
            let ty = parseFloat(c.getAttribute('cy')) - 30;
            if (tx + 100 > w) tx -= 120;
            tt.style.left = tx + 'px';
            tt.style.top = ty + 'px';
        });
        c.addEventListener('mouseleave', () => {
            c.setAttribute('r', '4');
            tt.style.display = 'none';
        });
    });
}

async function openStatsModal(){
    document.getElementById('statsModal').classList.add('show');
    document.getElementById('tokenStatsOverview').innerHTML = '<div class="empty">加载中...</div>';
    document.getElementById('tokenTrendChart').innerHTML = '';
    document.getElementById('todayModelsTable').innerHTML = '';
    document.getElementById('monthModelsTable').innerHTML = '';
    
    const r = await api('GET', '/api/token-stats');
    if(!r.today && r.today !== 0) {
        document.getElementById('tokenStatsOverview').innerHTML = '<div class="empty">加载失败</div>';
        return;
    }
    
    document.getElementById('tokenStatsOverview').innerHTML = `
        <div class="stat-item"><div class="num" style="color:var(--green)">${fmtNum(r.today)}</div><div class="label">今日消耗</div></div>
        <div class="stat-item"><div class="num" style="color:var(--blue)">${fmtNum(r.last_3_days)}</div><div class="label">近 3 天</div></div>
        <div class="stat-item"><div class="num" style="color:var(--yellow)">${fmtNum(r.last_7_days)}</div><div class="label">近 7 天</div></div>
        <div class="stat-item"><div class="num" style="color:var(--accent-light)">${fmtNum(r.last_30_days)}</div><div class="label">近 30 天</div></div>
    `;
    
    setTimeout(() => drawSVGChart('tokenTrendChart', r.trend_14d), 50);
    
    const renderTbl = (data) => data && data.length ? data.map(d => `
        <tr style="border-bottom: 1px solid rgba(255,255,255,0.03);">
            <td style="padding: 6px;"><code>${esc(d.model)}</code></td>
            <td style="padding: 6px; text-align:right; font-family: monospace;">${fmtNum(d.tokens)}</td>
        </tr>
    `).join('') : '<tr><td colspan="2" class="empty">暂无数据</td></tr>';
    
    document.getElementById('todayModelsTable').innerHTML = renderTbl(r.today_models);
    document.getElementById('monthModelsTable').innerHTML = renderTbl(r.month_models);
}

let logAutoScroll = true;
const logContainer = document.getElementById('logContainer');
if (logContainer) {
    logContainer.addEventListener('scroll', () => {
        logAutoScroll = logContainer.scrollHeight - logContainer.clientHeight <= logContainer.scrollTop + 20;
    });
}
function addLogLine(entry) {
    if (!logContainer) return;
    const d = document.createElement('div');
    d.className = 'log-line';
    d.innerHTML = `<span class="log-time">[${entry.time}]</span> <span class="log-${entry.level}">[${entry.level}]</span> <span class="log-msg">${esc(entry.msg)}</span>`;
    logContainer.appendChild(d);
    if (logContainer.children.length > 300) logContainer.removeChild(logContainer.firstChild);
    if (logAutoScroll) logContainer.scrollTop = logContainer.scrollHeight;
}
let _lastLogId = 0;
async function pollLogs() {
    try {
        const logs = await api('GET', '/api/logs?since=' + _lastLogId);
        if (logs && logs.length > 0) {
            for (let entry of logs) {
                addLogLine(entry);
                _lastLogId = Math.max(_lastLogId, entry.id);
            }
        }
    } catch(err){}
    setTimeout(pollLogs, 2000);
}
pollLogs();

refresh();setInterval(refresh,3000);
</script>
</body>
</html>"""


class Handler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def _send_json(self, code, data):
        try:
            body = json.dumps(data, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except ConnectionError:
            pass

    def _send_html(self, html):
        try:
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except ConnectionError:
            pass

    def _read_body(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            if length:
                return json.loads(self.rfile.read(length))
            return {}
        except Exception:
            return {}

    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
            self._send_html(GUI_HTML)
        elif self.path.startswith("/api/"):
            res = api_handler("GET", self.path, {})
            if len(res) == 3 and res[2] is True:
                code, stream_gen = res[0], res[1]
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "keep-alive")
                    self.end_headers()
                    for chunk in stream_gen:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                except ConnectionError:
                    pass
            else:
                self._send_json(res[0], res[1])
        else:
            self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        body = self._read_body()
        res = api_handler("POST", self.path, body)
        
        if len(res) == 3 and res[2] is True:
            code, stream_gen = res[0], res[1]
            try:
                self.send_response(code)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()
                
                for chunk in stream_gen:
                    self.wfile.write(chunk)
                    self.wfile.flush()
            except ConnectionError:
                pass
        else:
            self._send_json(res[0], res[1])

    def do_PUT(self):
        body = self._read_body()
        res = api_handler("PUT", self.path, body)
        self._send_json(res[0], res[1])

    def do_DELETE(self):
        res = api_handler("DELETE", self.path, {})
        self._send_json(res[0], res[1])


def main():
    port = 5100
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"\n  ⚡ API Pool 管理面板已启动")
    print(f"  🌐 管理面板访问: http://localhost:{port}")
    print(f"  🔗 客户端 Base URL: http://localhost:{port}/v1")
    print(f"  📋 已加载 {len(pool._endpoints)} 个端点")
    print(f"  🩺 健康检测: 启动时自动检测 + 每 {HEALTH_CHECK_INTERVAL}秒 复检\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
        server.server_close()


if __name__ == "__main__":
    main()