"""一次性创建（或更新）产线需要的 KHA 资源，并把资源 ID 写进 factory/config.json。

可以反复运行：已存在的资源直接复用；岗位说明（prompts/*.md）有改动时，自动给对应智能体生成新版本。

    python factory/setup_kha.py
"""
import json
import subprocess
import sys
import time
from pathlib import Path

import kha

HERE = Path(__file__).resolve().parent
CONFIG_PATH = HERE / "config.json"
PROMPTS = HERE / "prompts"

MODEL = "kimi-k3"
ENV_NAME = "mini-games-sandbox"
PIPELINE_STORE = "mini-games-pipeline"
JOURNAL_STORE = "mini-games-journal"
ROLES = {
    "idea": "小游戏产线-创意官",
    "builder": "小游戏产线-制作官",
    "marketer": "小游戏产线-营销官",
    "publisher": "小游戏产线-发布官",
}

ENV_CONFIG = {
    "type": "cloud",
    # demo 阶段用默认的 unrestricted：需要访问 jsDelivr（three.js）和 Playwright 浏览器下载源。
    "packages": {"pip": ["playwright"]},
    "setup_script": (
        "python3 -m playwright install chromium "
        "|| echo 'playwright chromium download failed; agents will fall back to system chromium'"
    ),
}

PIPELINE_README = """# 小游戏产线共享文件夹

- `games.md`：游戏履历（本地脚本维护，岗位只读）。
- `<期号>-<slug>/`：每期一个目录，例如 `01-catch-fruit/`。
  - `design.md` 策划案（创意官）
  - `game.html`、`selftest.md`、`selftest/` 游戏与自测（制作官）
  - `review.json` 试玩复核结论（管理员）
  - `demo.mp4`、`cover.jpg`、`copy.md` 演示视频、封面与文案（营销官）
  - `release/` 发布包（发布官）
  - `approval.json` 上线批准结论（管理员）
  - `state.json` 流程进度（本地脚本维护，岗位不要改）
  - `reports/` 各岗位汇报
"""

GAMES_MD = """# 游戏履历

| 期号 | 游戏 | 状态 | 线上地址 | 上线时间 |
|-|-|-|-|-|
"""

JOURNAL_README = "# 判例库\n\n产线的踩坑记录与规则都在 `journal.md`，每个岗位开工前必须先读。\n"

JOURNAL_MD = """# 判例库（管理员维护，岗位只读）

每条格式：编号｜规则｜来源。

1. 自测必须在真实无头浏览器里跑，不能只做静态检查代码。｜来源：原方案“三查留证据”要求。
2. 做不完就如实写卡点，禁止交付带病版本或伪造完成。｜来源：原方案红线“不硬造产出”。
3. 文案只写自测验证过的功能。｜来源：原方案营销官规范。
"""


def load_config():
    if CONFIG_PATH.exists():
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    return {
        "github": {
            "owner": "longsnow65-droid",
            "repo": "mini_games",
            "pages_base": "https://longsnow65-droid.github.io/mini_games/",
        },
        "kha": {},
    }


def save_config(cfg):
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def ensure_environment(cfg):
    env_id = cfg["kha"].get("environment_id")
    if not env_id:
        existing = [e for e in kha.paginate("/v1/environments") if e["name"] == ENV_NAME]
        if existing:
            env_id = existing[0]["id"]
        else:
            env_id = kha.request("POST", "/v1/environments", {
                "name": ENV_NAME,
                "description": "小游戏产线沙箱：Playwright + 系统预装的 ffmpeg/Chromium",
                "config": ENV_CONFIG,
            })["id"]
            print(f"已创建执行环境 {env_id}，等待构建……")
        cfg["kha"]["environment_id"] = env_id
        save_config(cfg)
    while True:
        status = kha.request("GET", f"/v1/environments/{env_id}").get("build_status")
        if status == "ready":
            print(f"执行环境就绪：{env_id}")
            return
        if status == "failed":
            sys.exit(f"执行环境构建失败：{env_id}，请到 KHA 控制台查看构建日志。")
        print(f"  构建状态：{status}，30 秒后再查……")
        time.sleep(30)


def ensure_store(cfg, key, name, description, seed_files):
    store_id = cfg["kha"].get(key)
    if not store_id:
        existing = [s for s in kha.paginate("/v1/memory-stores") if s["name"] == name]
        store_id = existing[0]["id"] if existing else kha.request(
            "POST", "/v1/memory-stores", {"name": name, "description": description})["id"]
        cfg["kha"][key] = store_id
        save_config(cfg)
    index = kha.list_memories(store_id)
    for path, content in seed_files.items():
        if path not in index:
            kha.write_memory(store_id, path, content, index=index)
            print(f"  写入 {name}/{path}")
    print(f"记忆库就绪：{name}（{store_id}）")


def ensure_agents(cfg):
    common = (PROMPTS / "common.md").read_text(encoding="utf-8")
    agents = cfg["kha"].setdefault("agents", {})
    for role, name in ROLES.items():
        system = common + "\n\n" + (PROMPTS / f"{role}.md").read_text(encoding="utf-8")
        agent_id = agents.get(role)
        if agent_id:
            current = kha.request("GET", f"/v1/agents/{agent_id}")
            if current.get("system") != system:
                kha.request("POST", f"/v1/agents/{agent_id}", {"version": current["version"], "system": system})
                print(f"已更新智能体 {name}（岗位说明有改动）")
            else:
                print(f"智能体就绪：{name}（{agent_id}）")
            continue
        agent = kha.request("POST", "/v1/agents", {
            "name": name,
            "model": {"id": MODEL},
            "description": "AI 小游戏生产线 demo 岗位",
            "system": system,
            "metadata": {"project": "mini_games", "role": role},
        })
        agents[role] = agent["id"]
        save_config(cfg)
        print(f"已创建智能体 {name}（{agent['id']}）")


def ensure_pages(cfg):
    gh = cfg["github"]
    repo = f"{gh['owner']}/{gh['repo']}"
    probe = subprocess.run(["gh", "api", f"repos/{repo}/pages"], capture_output=True, text=True)
    if probe.returncode == 0:
        print(f"GitHub Pages 已开启：{gh['pages_base']}")
        return
    made = subprocess.run(
        ["gh", "api", "-X", "POST", f"repos/{repo}/pages",
         "-f", "source[branch]=main", "-f", "source[path]=/"],
        capture_output=True, text=True)
    if made.returncode != 0:
        sys.exit(f"开启 GitHub Pages 失败：{made.stderr.strip()}")
    print(f"已开启 GitHub Pages：{gh['pages_base']}")


def main():
    cfg = load_config()
    save_config(cfg)
    ensure_environment(cfg)
    ensure_store(cfg, "pipeline_store_id", PIPELINE_STORE, "小游戏产线共享文件夹：各岗位交接产物",
                 {"README.md": PIPELINE_README, "games.md": GAMES_MD})
    ensure_store(cfg, "journal_store_id", JOURNAL_STORE, "小游戏产线判例库：踩坑记录与规则",
                 {"README.md": JOURNAL_README, "journal.md": JOURNAL_MD})
    ensure_agents(cfg)
    ensure_pages(cfg)
    print("\n全部就绪。配置已写入 factory/config.json")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
