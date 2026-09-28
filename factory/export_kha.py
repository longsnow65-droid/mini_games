"""把 KHA 平台上与本项目相关的全部内容导出到本地（可重复运行，覆盖更新）。

    python factory/export_kha.py

导出位置（仓库根目录，均不提交到 GitHub）：
    .state/                       平台配置快照
      agents/<岗位>.json            智能体当前版本（含 system prompt）
      agents/<岗位>.versions.json   智能体版本历史
      environment.json              执行环境
      memory_stores.json            记忆库元数据
      sessions.json                 全部会话索引
    out/
      <期号目录>/<序号>-<岗位>/      每个会话一个目录
        session.json                会话对象（含绑定资源、固定的智能体版本）
        events.jsonl                完整事件历史（消息、思考、工具调用与结果）
        workspace/                  沙箱工作区文件（测试脚本、截图、录屏中间件等）
        artifacts/                  save_artifact 交付的不可变产物
      memory/<记忆库名>/             记忆库当前全部文件，_versions.json 为每个文件的版本记录
"""
import json
import re
import shutil
import sys
import urllib.parse
from pathlib import Path

import kha

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
STATE = REPO / ".state"
OUT = REPO / "out"
CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
ROLE_ORDER = {"idea": 1, "builder": 2, "marketer": 3, "publisher": 4}
SKIP_DIRS = {"__pycache__", "node_modules"}
MAX_FILE = 50 * 1024 * 1024


def dump(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def raw_get(path, params=None):
    """下载二进制内容（记忆库以外的文件接口返回原始字节）。"""
    import urllib.request
    kha.request("GET", "/v1/agents", params={"page_size": 1})  # 确保已加载密钥
    url = kha.API_BASE + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + kha._KEY,
                                               "kimi-api-version": kha.API_VERSION})
    with urllib.request.urlopen(req, timeout=300) as r:
        return r.read()


def export_config():
    cfg = CONFIG["kha"]
    for role, agent_id in cfg["agents"].items():
        dump(STATE / "agents" / f"{role}.json", kha.request("GET", f"/v1/agents/{agent_id}"))
        dump(STATE / "agents" / f"{role}.versions.json", list(kha.paginate(f"/v1/agents/{agent_id}/versions")))
    dump(STATE / "environment.json", kha.request("GET", f"/v1/environments/{cfg['environment_id']}"))
    dump(STATE / "memory_stores.json", [kha.request("GET", f"/v1/memory-stores/{cfg[k]}")
                                        for k in ("pipeline_store_id", "journal_store_id")])
    print(f"配置快照 → {STATE}")


def list_sessions():
    seen, sessions = set(), []
    for params in ({}, {"statuses": "terminated"}):
        for s in kha.paginate("/v1/sessions", {"page_size": 100, **params}):
            if (s.get("metadata") or {}).get("project") == "mini_games" and s["id"] not in seen:
                seen.add(s["id"])
                sessions.append(s)
    # 列表接口不一定返回已归档会话；再按各期 state.json 历史里记录过的会话 ID 逐个补齐
    store = CONFIG["kha"]["pipeline_store_id"]
    for path, meta in kha.list_memories(store).items():
        if not path.endswith("/state.json"):
            continue
        history = json.loads(kha.read_memory(store, meta["id"])).get("history", [])
        ids = set(re.findall(r"sesn_[0-9a-z]+", json.dumps(history, ensure_ascii=False)))
        for sid in sorted(ids - seen):
            seen.add(sid)
            sessions.append(kha.request("GET", f"/v1/sessions/{sid}"))
    return sorted(sessions, key=lambda s: s["created_at"])


def walk_fs(sid, prefix=""):
    params = {"prefix": prefix} if prefix else None
    for node in kha.request("GET", f"/v1/sessions/{sid}/filesystem", params=params)["items"]:
        name = node["path"].rsplit("/", 1)[-1]
        if name.startswith(".") or name in SKIP_DIRS or name.endswith(".pyc"):
            continue
        if node["is_dir"]:
            yield from walk_fs(sid, node["path"])
        else:
            yield node


def export_session(s, folder):
    sid = s["id"]
    dump(folder / "session.json", kha.request("GET", f"/v1/sessions/{sid}"))
    events = list(kha.paginate(f"/v1/sessions/{sid}/events", {"page_size": 1000, "order": "asc"}))
    with (folder / "events.jsonl").open("w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
    files = skipped = 0
    for node in walk_fs(sid):
        if node["size_bytes"] > MAX_FILE:
            skipped += 1
            continue
        target = folder / "workspace" / node["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw_get(f"/v1/sessions/{sid}/filesystem/raw", {"path": node["path"]}))
        files += 1
    arts = list(kha.paginate("/v1/artifacts", {"session_id": sid, "page_size": 100}))
    for a in arts:
        target = folder / "artifacts" / a["name"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(raw_get(f"/v1/artifacts/{a['id']}/content"))
    if arts:
        dump(folder / "artifacts" / "_manifest.json", arts)
    return {"events": len(events), "files": files, "skipped_large": skipped, "artifacts": len(arts)}


def export_sessions():
    sessions = list_sessions()
    counts, index = {}, []
    for s in sessions:
        meta = s.get("metadata") or {}
        issue_dir = (s.get("title") or "").rsplit("-", 1)[0] or f"{meta.get('issue', 'unknown')}"
        role = meta.get("role", "unknown")
        counts[(issue_dir, role)] = counts.get((issue_dir, role), 0) + 1
        n = counts[(issue_dir, role)]
        name = f"{ROLE_ORDER.get(role, 9)}-{role}" + (f"-第{n}次" if n > 1 else "")
        folder = OUT / issue_dir / name
        if folder.exists():
            shutil.rmtree(folder)
        stats = export_session(s, folder)
        index.append({"issue_dir": issue_dir, "folder": f"out/{issue_dir}/{name}", "session_id": s["id"],
                      "role": role, "status": s["status"], "created_at": s["created_at"], **stats})
        print(f"会话 {s['id']} → out/{issue_dir}/{name}  事件 {stats['events']}，文件 {stats['files']}，产物 {stats['artifacts']}")
    dump(STATE / "sessions.json", index)


def export_memory():
    for key in ("pipeline_store_id", "journal_store_id"):
        store = kha.request("GET", f"/v1/memory-stores/{CONFIG['kha'][key]}")
        root = OUT / "memory" / store["name"]
        if root.exists():
            shutil.rmtree(root)
        versions = {}
        for path, meta in sorted(kha.list_memories(store["id"]).items()):
            target = root / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(kha.read_memory(store["id"], meta["id"]))
            versions[path] = [{k: v.get(k) for k in ("id", "operation", "source", "source_id", "created_at")}
                              for v in kha.paginate(f"/v1/memory-stores/{store['id']}/memories/{meta['id']}/versions")]
        dump(root / "_versions.json", versions)
        print(f"记忆库 {store['name']} → out/memory/{store['name']}（{len(versions)} 个文件）")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    export_config()
    export_memory()
    export_sessions()
    print("\n导出完成。")
