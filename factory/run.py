"""小游戏产线一键脚本：按顺序启动各岗位，在两道人工闸暂停，批准后推送到 GitHub Pages。

用法（在仓库根目录运行）：
    python factory/run.py run 01              从断点开始跑第 01 期（交互终端里会在闸口直接问你）
    python factory/run.py status [01]         查看进度
    python factory/run.py review 01 pass      闸 1 试玩复核：通过
    python factory/run.py review 01 reject "理由"
    python factory/run.py approve 01 yes      闸 2 上线批准：批准（随后自动推送上线）
    python factory/run.py approve 01 no "理由" --to marketer|builder

进度保存在云端共享文件夹的 <期号目录>/state.json，任何时候中断，重新执行 run 即可从断点继续。
"""
import argparse
import datetime as dt
import json
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

import kha

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
WORK = HERE / ".work"
CONFIG = json.loads((HERE / "config.json").read_text(encoding="utf-8"))
ISSUES = {i["issue"]: i for i in json.loads((HERE / "issues.json").read_text(encoding="utf-8"))}

PIPE = CONFIG["kha"]["pipeline_store_id"]
JOURNAL = CONFIG["kha"]["journal_store_id"]
ENV_ID = CONFIG["kha"]["environment_id"]
AGENTS = CONFIG["kha"]["agents"]
PAGES = CONFIG["github"]["pages_base"]

STEPS = ["design", "build", "review", "market", "package", "approve", "deploy", "done"]
STEP_NAMES = {
    "design": "① 创意官 写策划案", "build": "② 制作官 做游戏+自测", "review": "闸 1 你：试玩复核",
    "market": "③ 营销官 视频+文案", "package": "④ 发布官 打包发布包", "approve": "闸 2 你：上线批准",
    "deploy": "本地脚本 推送上线", "done": "本期完成",
}
ROLE_OF = {"design": "idea", "build": "builder", "market": "marketer", "package": "publisher"}
OUTPUTS = {
    "design": ["design.md"],
    "build": ["game.html", "selftest.md"],
    "market": ["demo.mp4", "cover.jpg", "copy.md"],
    "package": ["release/index.html", "release/demo.mp4", "release/cover.jpg",
                "release/card.json", "release/discord_draft.md", "release/checklist.md"],
}
TIME_LIMIT_MIN = {"design": 30, "build": 180, "market": 60, "package": 30}
MAX_NUDGES = 2
MAX_BUILD_ROUNDS = 3


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", flush=True)


def issue_dir(issue):
    return f"{issue['issue']}-{issue['slug']}"


def game_url(issue):
    return f"{PAGES}games/{issue['slug']}/"


# ---------- 进度 ----------

def load_state(issue):
    raw = kha.read_memory_by_path(PIPE, f"{issue_dir(issue)}/state.json")
    if raw:
        return json.loads(raw)
    return {"issue": issue["issue"], "step": "design", "build_round": 0, "feedback": None, "history": []}


def save_state(issue, state):
    kha.write_memory(PIPE, f"{issue_dir(issue)}/state.json", json.dumps(state, ensure_ascii=False, indent=2))


def record(state, **event):
    state["history"].append({"at": now(), **event})


# ---------- 岗位运行 ----------

def task_message(step, issue, state):
    d = issue_dir(issue)
    lines = [
        f"本期目录：/mnt/agents/memories/mini-games-pipeline/{d}/",
        f"期号：{issue['issue']}　游戏英文名（slug）：{issue['slug']}　游戏名：{issue['title']}",
    ]
    if step == "design":
        lines.append(f"一句话玩法：{issue['pitch']}")
        lines.append("请按岗位要求写出 design.md。")
    elif step == "build":
        lines.append(f"这是第 {state['build_round']} 版制作。请按 design.md 做出 game.html 并完成三查自测。")
    elif step == "market":
        lines.append("游戏已通过管理员试玩复核。请录制 demo.mp4、导出 cover.jpg、写 copy.md。")
    elif step == "package":
        lines.append(f"线上游戏地址（上线后生效）：{game_url(issue)}")
        lines.append("请核对本期材料并生成 release/ 发布包。")
    fb = state.get("feedback")
    if fb and fb.get("to") == step:
        lines.append(f"\n管理员反馈（{fb['from']}，必须逐条处理）：{fb['reason']}")
    return "\n".join(lines)


def summarize(ev):
    t = ev.get("type")
    data = ev.get("data") or {}
    if t == "agent.message":
        text = " ".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
        return f"💬 {text[:300]}" if text else None
    if t == "agent.tool_use":
        call = data.get("tool_use") or data
        name = call.get("name") or "tool"
        inp = call.get("input") or {}
        detail = inp.get("command") or inp.get("path") or inp.get("file_path") or inp.get("code") or ""
        detail = str(detail).replace("\n", " ")[:120]
        return f"🔧 {name} {detail}".rstrip()
    if t == "session.error":
        return f"⚠️ 会话错误 {data.get('error_type')}: {data.get('message')}"
    return None


def last_agent_text(events):
    for ev in reversed(events):
        if ev.get("type") == "agent.message":
            return " ".join(b.get("text", "") for b in ev["data"].get("content", []) if b.get("type") == "text")
    return ""


def wait_turn(session_id, seen, deadline):
    """等待本轮结束，实时打印进度。返回 (是否正常结束, 最后一条智能体消息)。"""
    saw_running = False
    idle_polls = 0
    while True:
        events = kha.recent_events(session_id, 50)
        for ev in events:
            if ev["id"] not in seen:
                seen.add(ev["id"])
                line = summarize(ev)
                if line:
                    log("   " + line)
        status = kha.get_session(session_id)["status"]
        if status == "running":
            saw_running = True
        elif status == "idle":
            idle_polls += 1
            last_status = next((e for e in reversed(events) if e.get("type") == "session.status"), None)
            finished = last_status and (last_status.get("data") or {}).get("status") == "idle"
            if (saw_running and finished) or idle_polls >= 8:
                stop = ((last_status or {}).get("data") or {}).get("stop_reason", {}).get("type")
                return stop in (None, "end_turn"), last_agent_text(events)
        if time.time() > deadline:
            log("   ⏰ 超出时间上限，中断本岗位。")
            try:
                kha.interrupt(session_id)
            except kha.KHAError:
                pass
            return False, last_agent_text(events)
        time.sleep(15)


def parse_ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def missing_outputs(step, issue, since):
    index = kha.list_memories(PIPE)
    d = issue_dir(issue)
    missing = []
    for rel in OUTPUTS[step]:
        meta = index.get(f"{d}/{rel}")
        if not meta or parse_ts(meta["updated_at"]) < parse_ts(since):
            missing.append(rel)
    return missing


def run_agent_step(step, issue, state):
    role = ROLE_OF[step]
    log(f"▶ {STEP_NAMES[step]}")
    session = kha.create_session(
        AGENTS[role], ENV_ID,
        resources=[
            {"type": "memory_store", "memory_store_id": PIPE, "access": "read_write",
             "instructions": "产线共享文件夹。只修改本期目录下你岗位负责的文件。"},
            {"type": "memory_store", "memory_store_id": JOURNAL, "access": "read_only",
             "instructions": "判例库，开工前必须先读 journal.md。"},
        ],
        title=f"{issue['issue']}-{issue['slug']}-{role}",
        metadata={"project": "mini_games", "issue": issue["issue"], "role": role},
    )
    sid, since = session["id"], session["created_at"]
    log(f"   会话 {sid}（可在 KHA 控制台查看完整过程）")
    record(state, step=step, event="session_started", session_id=sid)
    save_state(issue, state)

    deadline = time.time() + TIME_LIMIT_MIN[step] * 60
    seen = set()
    kha.send_message(sid, task_message(step, issue, state))
    for attempt in range(MAX_NUDGES + 1):
        ok, last = wait_turn(sid, seen, deadline)
        if "STATUS: BLOCKED" in last:
            record(state, step=step, event="blocked", session_id=sid, detail=last[-500:])
            save_state(issue, state)
            sys.exit(f"\n⛔ {STEP_NAMES[step]} 报告无法完成：\n{last}\n\n处理后重新执行 run 可从本步重试。")
        missing = missing_outputs(step, issue, since)
        if not missing:
            record(state, step=step, event="done", session_id=sid)
            log(f"   ✅ 完成：{', '.join(OUTPUTS[step])}")
            return
        if time.time() > deadline or attempt == MAX_NUDGES:
            break
        log(f"   ↻ 还缺 {', '.join(missing)}，提醒岗位继续……")
        kha.send_message(sid, f"交付物还不完整，缺少：{', '.join(missing)}（均在本期目录下）。请继续完成，完成后按汇报格式回复。")
    record(state, step=step, event="incomplete", session_id=sid, missing=missing)
    save_state(issue, state)
    sys.exit(f"\n⛔ {STEP_NAMES[step]} 没有交齐产物（缺 {', '.join(missing)}）。重新执行 run 会从本步重试。")


# ---------- 人工闸 ----------

def download(issue, rels, sub):
    index = kha.list_memories(PIPE)
    d = issue_dir(issue)
    out = WORK / d / sub
    out.mkdir(parents=True, exist_ok=True)
    got = {}
    for path, meta in index.items():
        rel = path[len(d) + 1:] if path.startswith(d + "/") else None
        if rel and any(rel == r or (r.endswith("/") and rel.startswith(r)) for r in rels):
            target = out / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(kha.read_memory(PIPE, meta["id"]))
            got[rel] = target
    return out, got


def gate_review_prepare(issue):
    out, got = download(issue, ["game.html", "selftest.md", "selftest/", "design.md"], "review")
    log(f"闸 1 试玩复核：文件已下载到 {out}")
    if "game.html" in got:
        webbrowser.open(got["game.html"].as_uri())
        log("   已在浏览器打开游戏。建议再用浏览器开发者工具切到手机视图试一试。")
    if "selftest.md" in got:
        webbrowser.open(got["selftest.md"].as_uri())


def gate_approve_prepare(issue):
    out, got = download(issue, ["release/"], "approve")
    log(f"闸 2 上线批准：发布包已下载到 {out / 'release'}")
    for name in ("release/demo.mp4", "release/discord_draft.md", "release/checklist.md", "release/card.json"):
        if name in got:
            webbrowser.open(got[name].as_uri())


def decide_review(issue, state, verdict, reason=""):
    passed = verdict == "pass"
    kha.write_memory(PIPE, f"{issue_dir(issue)}/review.json", json.dumps(
        {"status": "pass" if passed else "reject", "reason": reason, "build_round": state["build_round"],
         "decided_at": now(), "by": "管理员"}, ensure_ascii=False, indent=2))
    record(state, step="review", event="pass" if passed else "reject", reason=reason)
    if passed:
        state["step"], state["feedback"] = "market", None
    else:
        state["step"] = "build"
        state["feedback"] = {"from": "试玩复核", "to": "build", "reason": reason}
    save_state(issue, state)
    log("闸 1 结论已记录：" + ("通过" if passed else f"打回（{reason}）"))


def decide_approve(issue, state, verdict, reason="", to="marketer"):
    yes = verdict == "yes"
    kha.write_memory(PIPE, f"{issue_dir(issue)}/approval.json", json.dumps(
        {"status": "approved" if yes else "rejected", "reason": reason, "send_back_to": None if yes else to,
         "decided_at": now(), "by": "管理员"}, ensure_ascii=False, indent=2))
    record(state, step="approve", event="approved" if yes else "rejected", reason=reason, to=None if yes else to)
    if yes:
        state["step"], state["feedback"] = "deploy", None
    else:
        target = "build" if to == "builder" else "market"
        state["step"] = target
        state["feedback"] = {"from": "上线批准", "to": target, "reason": reason}
    save_state(issue, state)
    log("闸 2 结论已记录：" + ("批准上线" if yes else f"驳回给{'制作官' if to == 'builder' else '营销官'}（{reason}）"))


def ask(prompt):
    try:
        return input(prompt).strip()
    except EOFError:
        return ""


def interactive_gate(step, issue, state):
    """交互终端里直接询问；否则打印命令后退出。返回 True 表示已做决定。"""
    if step == "review":
        gate_review_prepare(issue)
    else:
        gate_approve_prepare(issue)
    if not sys.stdin.isatty():
        n = issue["issue"]
        cmds = (f"  python factory/run.py review {n} pass\n  python factory/run.py review {n} reject \"理由\""
                if step == "review" else
                f"  python factory/run.py approve {n} yes\n  python factory/run.py approve {n} no \"理由\" --to marketer|builder")
        print(f"\n⏸ 等待{STEP_NAMES[step]}。做出决定后运行：\n{cmds}")
        return False
    if step == "review":
        ans = ask("\n试玩后，是否通过？[y 通过 / n 打回]：").lower()
        if ans == "y":
            decide_review(issue, state, "pass")
        else:
            decide_review(issue, state, "reject", ask("打回理由：") or "未说明")
    else:
        ans = ask("\n是否批准上线？[y 批准 / n 驳回]：").lower()
        if ans == "y":
            decide_approve(issue, state, "yes")
        else:
            to = "builder" if ask("问题出在游戏本身吗？[y 退回制作官 / n 退回营销官]：").lower() == "y" else "marketer"
            decide_approve(issue, state, "no", ask("驳回理由：") or "未说明", to)
    return True


# ---------- 上线 ----------

def git(*args):
    r = subprocess.run(["git", *args], cwd=REPO, capture_output=True, text=True, encoding="utf-8")
    if r.returncode != 0:
        sys.exit(f"git {' '.join(args)} 失败：{r.stderr.strip()}")
    return r.stdout.strip()


def deploy(issue, state):
    log("▶ 本地脚本 推送上线")
    _, got = download(issue, ["release/"], "deploy")
    need = ["release/index.html", "release/demo.mp4", "release/cover.jpg", "release/card.json"]
    lacking = [n for n in need if n not in got]
    if lacking:
        sys.exit(f"发布包不完整，缺少 {lacking}，无法上线。")
    dest = REPO / "games" / issue["slug"]
    dest.mkdir(parents=True, exist_ok=True)
    for name in ("index.html", "demo.mp4", "cover.jpg"):
        (dest / name).write_bytes(got[f"release/{name}"].read_bytes())

    card = json.loads(got["release/card.json"].read_text(encoding="utf-8"))
    card.update({"issue": issue["issue"], "slug": issue["slug"], "url": f"games/{issue['slug']}/",
                 "video": f"games/{issue['slug']}/demo.mp4", "cover": f"games/{issue['slug']}/cover.jpg",
                 "published_at": dt.date.today().isoformat()})
    games_path = REPO / "games.json"
    games = json.loads(games_path.read_text(encoding="utf-8")) if games_path.exists() else []
    games = sorted([g for g in games if g.get("slug") != issue["slug"]] + [card], key=lambda g: g["issue"])
    games_path.write_text(json.dumps(games, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    git("add", f"games/{issue['slug']}", "games.json")
    if git("status", "--porcelain", "--", f"games/{issue['slug']}", "games.json"):
        git("commit", "-m", f"上线第 {issue['issue']} 期：{card.get('title', issue['title'])}")
    git("push", "origin", "HEAD")
    sha = git("rev-parse", "HEAD")
    log(f"   已推送 {sha[:7]}，等待 GitHub Pages 生效……")

    url = game_url(issue)
    expected = (dest / "index.html").read_bytes().replace(b"\r\n", b"\n")
    live = False
    for _ in range(40):
        try:
            with urllib.request.urlopen(url + f"?v={sha[:7]}", timeout=20) as r:
                if r.read().replace(b"\r\n", b"\n") == expected:
                    live = True
                    break
        except Exception:
            pass
        time.sleep(15)
    if live:
        log(f"   ✅ 已上线：{url}")
        webbrowser.open(PAGES)
        webbrowser.open(url)
    else:
        log(f"   ⚠️ 10 分钟内没有检测到线上生效，请稍后打开 {url} 确认，或检查仓库 Settings → Pages。")

    index = kha.list_memories(PIPE)
    kha.write_memory(PIPE, f"{issue_dir(issue)}/published.md",
                     f"# 第 {issue['issue']} 期上线记录\n\n- 线上地址：{url}\n- 画廊首页：{PAGES}\n"
                     f"- 提交：{sha}\n- 时间：{now()}\n- 线上校验：{'通过' if live else '未确认'}\n", index=index)
    games_md = (kha.read_memory_by_path(PIPE, "games.md", index) or b"").decode("utf-8")
    row = f"| {issue['issue']} | {card.get('title', issue['title'])} | 已上线 | {url} | {dt.date.today()} |"
    lines = [l for l in games_md.splitlines() if not l.startswith(f"| {issue['issue']} |")]
    kha.write_memory(PIPE, "games.md", "\n".join(lines).rstrip() + "\n" + row + "\n", index=index)
    record(state, step="deploy", event="done", commit=sha, url=url, live=live)
    state["step"] = "done"
    save_state(issue, state)


# ---------- 命令 ----------

def cmd_run(issue):
    state = load_state(issue)
    log(f"第 {issue['issue']} 期「{issue['title']}」，当前进度：{STEP_NAMES[state['step']]}")
    while state["step"] != "done":
        step = state["step"]
        if step in ROLE_OF:
            if step == "build":
                if state["build_round"] >= MAX_BUILD_ROUNDS and state.get("feedback"):
                    log(f"⚠️ 已制作 {state['build_round']} 版仍被打回，按备选方案应缩小范围或降级题材。继续第 {state['build_round'] + 1} 版。")
                state["build_round"] += 1
                save_state(issue, state)
            run_agent_step(step, issue, state)
            state["step"] = STEPS[STEPS.index(step) + 1]
            if (state.get("feedback") or {}).get("to") == step:
                state["feedback"] = None
            save_state(issue, state)
        elif step in ("review", "approve"):
            if not interactive_gate(step, issue, state):
                return
        elif step == "deploy":
            deploy(issue, state)
    log(f"🎉 第 {issue['issue']} 期完成：{game_url(issue)}")


def cmd_status(issues):
    for issue in issues:
        state = load_state(issue)
        print(f"第 {issue['issue']} 期「{issue['title']}」：{STEP_NAMES[state['step']]}（第 {state['build_round']} 版制作）")
        for h in state["history"][-6:]:
            print(f"   {h['at']}  {h.get('step')}  {h.get('event')}  {h.get('reason') or h.get('session_id') or ''}")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    p = argparse.ArgumentParser(description="小游戏产线一键脚本")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run"); r.add_argument("issue")
    s = sub.add_parser("status"); s.add_argument("issue", nargs="?")
    v = sub.add_parser("review"); v.add_argument("issue"); v.add_argument("verdict", choices=["pass", "reject"]); v.add_argument("reason", nargs="?", default="")
    a = sub.add_parser("approve"); a.add_argument("issue"); a.add_argument("verdict", choices=["yes", "no"]); a.add_argument("reason", nargs="?", default="")
    a.add_argument("--to", choices=["marketer", "builder"], default="marketer")
    args = p.parse_args()

    if args.cmd == "status":
        cmd_status([ISSUES[args.issue]] if args.issue else list(ISSUES.values()))
        return
    issue = ISSUES.get(args.issue)
    if not issue:
        sys.exit(f"未知期号 {args.issue}，可选：{', '.join(ISSUES)}")
    if args.cmd == "run":
        cmd_run(issue)
        return
    state = load_state(issue)
    if args.cmd == "review":
        if state["step"] != "review":
            sys.exit(f"当前不在试玩复核闸口（进度：{STEP_NAMES[state['step']]}）。")
        if args.verdict == "reject" and not args.reason:
            sys.exit("打回需要写理由。")
        decide_review(issue, state, args.verdict, args.reason)
    elif args.cmd == "approve":
        if state["step"] != "approve":
            sys.exit(f"当前不在上线批准闸口（进度：{STEP_NAMES[state['step']]}）。")
        if args.verdict == "no" and not args.reason:
            sys.exit("驳回需要写理由。")
        decide_approve(issue, state, args.verdict, args.reason, args.to)
    print(f"继续运行：python factory/run.py run {issue['issue']}")


if __name__ == "__main__":
    main()
