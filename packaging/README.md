# Maestro 桌面打包说明

## 快速开始

```bash
# 装打包依赖
pip install -r requirements.txt -r requirements-dev.txt

# 一键打包（资源裁剪 + PyInstaller onedir）
python packaging/build.py

# 产物
dist/Maestro/Maestro.exe              # 双击启动
dist/Maestro/_internal/                 # 资源 + Python 包
```

## 产物结构

```
dist/Maestro/
├── Maestro.exe                       # 启动器（< 5 MB，PyInstaller bootloader）
├── README.md                          # 你正在看的这份（自动复制）
└── _internal/
    ├── maestro/                       # Python 包（src/maestro/）
    ├── web/                           # 前端三件套 + pet_q/pet_poses/vendor
    ├── wallpaper/                     # 壁纸素材（裁剪后约 50 MB）
    ├── configs/                       # 工作流 + 技能 + providers + keys
    └── ...                            # PyInstaller 依赖
```

## 运行时数据目录

**首次双击 Maestro.exe** 会自动创建：

```
%MAESTRO_HOME%/                       # 默认 = Maestro.exe 同级目录
├── data/maestro.db                    # SQLite 状态库（任务 / 子任务 / 会话 / 历史）
├── outputs/                           # 任务产物落盘（自动创建）
├── backups/                           # SQLite 在线备份（保留最近 7 份，每 24h）
├── port.txt                           # 实际服务端口（重启复用，8787-8797 探测）
└── logs/                              # 未来扩展
```

`MAESTRO_HOME` 环境变量可覆盖默认根目录（运维场景）。首次启动若 `dist/Maestro` 是只读（如装在 Program Files），请把它复制到用户目录再双击。

## API 密钥

`%APPDATA%/Maestro/.env` 是推荐存放密钥的位置（同名环境变量也生效）：

```env
DEEPSEEK_API_KEY=sk-xxxxxxxx
KIMI_API_KEY=sk-yyyyyyyy
MMX_API_KEY=mmx-zzzzzz
```

## 双窗口架构

- **背景壁纸层**（仅 Windows）：透明 PyWebView 窗口挂到 Program Manager 的 WorkerW 子层；鼠标点击穿透到桌面图标，不抢焦点；关掉主窗口时随服务退到后台。
- **主交互窗口**：PyWebView 嵌入的系统 Chromium/Edge 窗口，1400x900 起步，可缩可拖；展示聊天框 / 工作流 / 任务卡片 / 桌宠；关掉则 launcher 清理壁纸层 + 终止后端 + 写端口文件。

非 Windows（macOS / Linux）：自动跳过壁纸层，只起一个主交互窗口。

## 端口策略

- 默认 `8787`，被占则递增探测 `8788..8797`，找到第一个空闲
- 探测结果写入 `MAESTRO_HOME/port.txt`，下次启动优先复用
- 前端始终用 `location.origin`，多实例/端口漂移无感

## 测试

```bash
pytest tests/ -q                      # 单元 + 集成测试（不需要打包）
pytest tests/test_e2e_smoke.py -q      # Playwright 浏览器冒烟（需 playwright）
```

## 故障排查

| 症状 | 排查 |
|---|---|
| 双击后没反应 | 检查 `_internal/configs/` 路径，删除 `dist/Maestro/data/maestro.db` 重试 |
| 端口 8787 占用 | 删除 `port.txt` 让启动器重新探测 |
| 壁纸层没显示 | 确认 Wallpaper Engine / 其他壁纸软件未占用 WorkerW；检查 `dist/Maestro/data/logs/` |
| PyInstaller 报 hook 错误 | `pip install --upgrade pyinstaller` 后重试 |
| Linux 缺 WebKit | `sudo apt install libwebkit2gtk-4.0-dev` |

## 不在 v1 范围

- 自动更新（用 nssm 注册成 Windows 服务更稳）
- 多实例隔离（当前单实例启动）
- 代码签名（数字签名需要购买证书）
- 安装包制作（NSIS / Inno Setup）
