# mini_games · AI 小游戏生产线 demo

**在线画廊：** https://longsnow65-droid.github.io/mini_games/

这里的每款竖屏小游戏，都由 4 个 AI 员工（Kimi 托管智能体）接力完成：创意官写策划案，制作官写代码并自测，营销官录演示视频、写文案，发布官打包。每款游戏都要经过管理员亲手试玩、批准，然后才会上线。

| 期号 | 游戏 | 状态 |
|-|-|-|
| 01 | 接水果 | 生产中 |
| 02 | 打砖块 | 排队中 |

## 目录

- `index.html` + `games.json`：画廊首页。
- `games/<slug>/`：每款游戏（`index.html` 为游戏本体，`demo.mp4` 为演示视频）。
- `factory/`：生产线脚本与岗位说明，使用方法见 [factory/README.md](factory/README.md)。
