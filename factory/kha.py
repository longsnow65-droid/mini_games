"""Kimi 托管智能体（KHA）API 的最小客户端，只用标准库。

API Key 读取顺序：进程环境变量 KIMI_API_KEY → Windows 用户环境变量（注册表 HKCU\\Environment）。
密钥只放进请求头，不打印、不落盘。
"""
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API_BASE = os.environ.get("KIMI_API_BASE", "https://api.moonshot.cn")
API_VERSION = "2026-09-01-beta"


class KHAError(Exception):
    def __init__(self, status, body):
        self.status = status
        self.body = body
        super().__init__(f"HTTP {status}: {body[:500]}")


def _load_key():
    key = os.environ.get("KIMI_API_KEY", "").strip()
    if not key and sys.platform == "win32":
        import winreg
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                key = str(winreg.QueryValueEx(k, "KIMI_API_KEY")[0]).strip()
        except OSError:
            key = ""
    if len(key) < 20:
        sys.exit("没有找到有效的 KIMI_API_KEY。请先复制 Key，再运行 set_kimi_key.ps1。")
    return key


_KEY = None


def request(method, path, body=None, params=None, retries=4):
    global _KEY
    if _KEY is None:
        _KEY = _load_key()
    url = API_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {
        "Authorization": "Bearer " + _KEY,
        "kimi-api-version": API_VERSION,
        "User-Agent": "mini-games-factory/0.1",
    }
    if data is not None:
        headers["Content-Type"] = "application/json"
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", "replace")
            if e.code in (429, 500, 502, 503, 504) and attempt < retries:
                wait = int(e.headers.get("Retry-After") or 0) or 2 ** (attempt + 1)
                time.sleep(wait)
                continue
            raise KHAError(e.code, text) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt < retries:
                time.sleep(2 ** (attempt + 1))
                continue
            raise


def paginate(path, params=None):
    params = dict(params or {})
    params.setdefault("page_size", 100)
    while True:
        page = request("GET", path, params=params)
        yield from page.get("items", [])
        token = page.get("next_page_token")
        if not token:
            return
        params["page_token"] = token


# ---------- 记忆库 ----------

def list_memories(store_id):
    """返回 {path: 元数据}（不含内容）。"""
    return {m["path"]: m for m in paginate(f"/v1/memory-stores/{store_id}/memories", {"page_size": 1000})}


def read_memory(store_id, memory_id):
    m = request("GET", f"/v1/memory-stores/{store_id}/memories/{memory_id}")
    return base64.b64decode(m.get("content") or "")


def read_memory_by_path(store_id, path, index=None):
    index = index if index is not None else list_memories(store_id)
    meta = index.get(path)
    return read_memory(store_id, meta["id"]) if meta else None


def write_memory(store_id, path, content, index=None):
    """按路径写入（存在则更新，不存在则创建）。content 为 bytes 或 str。"""
    if isinstance(content, str):
        content = content.encode("utf-8")
    b64 = base64.b64encode(content).decode("ascii")
    index = index if index is not None else list_memories(store_id)
    meta = index.get(path)
    if meta:
        return request("PATCH", f"/v1/memory-stores/{store_id}/memories/{meta['id']}",
                       {"content": b64, "content_sha256": meta["content_sha256"]})
    return request("POST", f"/v1/memory-stores/{store_id}/memories", {"path": path, "content": b64})


def delete_memory(store_id, memory_id):
    return request("DELETE", f"/v1/memory-stores/{store_id}/memories/{memory_id}")


# ---------- 会话 ----------

def create_session(agent_id, environment_id, resources, title=None, metadata=None):
    body = {"agent_id": agent_id, "environment_id": environment_id, "resources": resources}
    if title:
        body["title"] = title
    if metadata:
        body["metadata"] = metadata
    return request("POST", "/v1/sessions", body)


def send_message(session_id, text):
    return request("POST", f"/v1/sessions/{session_id}/events",
                   {"events": [{"type": "user.message", "data": {"content": [{"type": "text", "text": text}]}}]})


def get_session(session_id):
    return request("GET", f"/v1/sessions/{session_id}")


def recent_events(session_id, n=50):
    """最近 n 条事件，按时间正序返回。"""
    page = request("GET", f"/v1/sessions/{session_id}/events", params={"order": "desc", "page_size": n})
    return list(reversed(page.get("items", [])))


def interrupt(session_id):
    return request("POST", f"/v1/sessions/{session_id}/events", {"events": [{"type": "user.interrupt"}]})


def archive_session(session_id):
    return request("POST", f"/v1/sessions/{session_id}/archive")
