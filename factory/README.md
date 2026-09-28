# 小游戏生产线（factory）

4 个 Kimi 托管智能体（KHA）接力生产小游戏，经管理员两道人工闸后发布到 GitHub Pages。

```
创意官 写策划案 → 制作官 做游戏+自测 → 【闸 1 你：试玩复核】
→ 营销官 视频+文案 → 发布官 打包发布包 → 【闸 2 你：上线批准】→ 本地脚本推送 → GitHub Pages
```

## 文件

| 文件 | 作用 |
|-|-|
| `prompts/` | 4 个岗位的说明（`common.md` 是全员公共规则） |
| `issues.json` | 每期做什么游戏 |
| `setup_kha.py` | 创建执行环境、两个记忆库、4 个智能体，并开启 Pages；结果写入 `config.json` |
| `run.py` | 一键流程脚本，可断点续跑 |
| `kha.py` | KHA API 的最小客户端（只用标准库） |
| `config.json` | 资源 ID（不含任何密钥） |

## 使用

1. 设置 API Key：复制 Key 后运行 `set_kimi_key.ps1`（保存为 Windows 用户环境变量 `KIMI_API_KEY`）。
2. 首次准备：`python factory/setup_kha.py`
3. 跑一期：`python factory/run.py run 01`，到闸口时按提示试玩和决定。
4. 查看进度：`python factory/run.py status`

运行期间电脑需要保持开机、不休眠。中断后重新执行 `run` 即可从断点继续，进度保存在云端共享文件夹的 `state.json` 里。

## 共享文件夹（KHA 记忆库）

- `mini-games-pipeline`（岗位读写）：每期一个目录，存放全部产物与流程进度。
- `mini-games-journal`（岗位只读）：判例库 `journal.md`，由管理员维护。
