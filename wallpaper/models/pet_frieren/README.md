# 芙莉莲 Codex 桌宠 v2 · Frieren Pet for Codex（4 帧动画版）

葬送的芙莉莲 chibi 风格桌宠包，**9 状态 × 4 帧循环动画**版。
可装入 OpenAI Codex CLI/桌面端，或 PetDex / OpenPets 等兼容桌面伴侣。

## 安装

### A. 复制到 Codex pets 目录

```bash
# Windows (PowerShell)
Copy-Item -Recurse . "$env:USERPROFILE\.codex\pets\frieren-chibi"

# macOS / Linux
cp -r . ~/.codex/pets/frieren-chibi
```

然后打开 Codex 设置 → Pets → 选择「芙莉莲」。

### B. 打 zip 上传 PetDex / OpenPets

```bash
Compress-Archive -Path * -DestinationPath frieren-chibi.zip    # Windows
zip -r frieren-chibi.zip .                                       # macOS/Linux
```

## 状态 & 帧

| 行 | 状态 | 触发场景 | 帧索引 | fps | 循环 |
|---|---|---|---|---|---|
| 0 | idle | 待机呼吸 | 0~3 | 4 | ✓ |
| 1 | idle-blink | 眨眼 | 4~7 | 4 | ✓ |
| 2 | waving | 打招呼 | 8~11 | 6 | ✓ |
| 3 | failed | 任务失败 | 12~15 | 4 | ✓ |
| 4 | running | Codex 工作中（向右跑） | 16~19 | 10 | ✓ |
| 5 | running-left | 向左跑 | 20~23 | 10 | ✓ |
| 6 | jumping | 任务完成（跳跃） | 24~27 | 8 | ✓ |
| 7 | waiting | 等待 Codex | 28~31 | 4 | ✓ |
| 8 | review | 审查思考 | 32~35 | 4 | ✓ |

## 文件结构

```
frieren-chibi/
├── pet.json            # 配置（v2，多 frames 数组）
├── spritesheet.webp    # 36 帧 1280×2880 透明 WebP
├── spritesheet.png     # PNG 备份
├── README.md           # 本文件
├── preview.html        # 浏览器预览 demo（多帧循环动画）
└── preview_static.html # 单帧版 demo（保留兼容）
```

## v1 → v2 变更

- v1: 每状态 1 帧（共 9 帧 1536×1536）—— 桌宠不真正动
- **v2: 每状态 4 帧（共 36 帧 1280×2880）—— 4 帧循环动画**
- ImageGen 派生 28 张新状态帧
- 部分 RGB 图用 chroma key fallback（rembg SIGKILL）

## 许可

MIT
