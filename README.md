# nahida-translator

> Windows 划词翻译 · 本地 Qwen2.5-7B · 纳西妲主题 · 完全离线，无需任何 API Key

选中英文 → 松开鼠标 → 旁边浮出「译」小按钮 → 点一下 → 右侧弹出中文译文卡片。

体验类似豆包/浏览器自带的划词翻译，但**推理全部跑在你自己电脑上**：模型是本地量化的
Qwen2.5-7B-Instruct，引擎是 llama.cpp，不联网、不付费、不需要登录，也没有任何云端调用。

界面是纳西妲（草神）风格的暗绿主题：深林绿底、草绿高亮、奶白文字，标题带有叶脉纹理和金色微光，
全部由程序用 PIL 现场绘制，不含任何游戏素材。

> 为什么做这个项目 → [MOTIVATION.md](MOTIVATION.md)

![界面预览](docs/preview.png)

---

## 功能特性

- **划词即译**：拖选 / 双击选词都会浮出「译」按钮，`Ctrl+Alt+T` 可直接处理
- **五种模式**：自动识别、英译中、中译英、长文摘要、术语解释；面板左上角一键切换，并记住上次选择
- **单词详解**：选中单个英文单词时，自动给出音标、词性、释义、搭配和双语例句，而不是简单直译
- **流式输出**：译文边生成边显示，单句约 0.1~0.8 秒（RTX 5060 实测）
- **常驻面板 + 任务栏按钮**：右下角常驻「译」按钮随时把面板叫到最前；面板可拖动、四边/四角缩放
- **半透明动态背景**：本地 GIF 作为面板底层背景（按面板比例裁剪），面板闲置 6 秒自动鼠标穿透，不挡划词
- **无 API、无网络**：模型与引擎首次下载后，之后只访问 `127.0.0.1`

## 截图

| 模式选择 | 单词详解 |
|---|---|
| ![模式选择](docs/preview_mode.png) | ![单词详解](docs/preview_word.png) |

| 常驻按钮 | 动态背景 |
|---|---|
| ![常驻按钮](docs/preview_dock.png) | ![动态背景](docs/preview_panel_bg.png) |

## 环境要求

- Windows 10 / 11
- Python 3.10+（`python --version` 可用）
- 可选：NVIDIA 显卡（显存 ≥ 6GB 体验最佳；没有显卡会自动回退 CPU 推理，速度慢 3~5 倍）
- 磁盘约 6GB（模型 4.4GB + 引擎 0.7GB）

## 快速开始

```bash
git clone https://github.com/cwh-123456/nahida-translator.git
cd nahida-translator
```

1. 双击 `launchers\setup.bat`（只需一次）
   - 安装 Python 依赖；
   - 下载 llama.cpp 引擎（约 550MB）；
   - 下载 Qwen2.5-7B-Instruct Q4_K_M 模型（约 4.4GB）；
   - 中断了不要紧，重新运行会断点续传。
2. 双击 `start.bat` 启动（无控制台，托盘和右下角出现「译」）。
3. （可选）想要动态看板娘：把任意 GIF 命名为 `nahida_swing.gif` 放进 `assets\`。

> 仓库不包含模型、引擎和图片素材（体积与版权原因），首次运行必须执行第 1 步。

## 使用说明

| 操作 | 说明 |
|---|---|
| 拖选 / 双击选词 | 松开后浮出「译」，点它开始翻译 |
| `Ctrl+Alt+T` | 选中后直接翻译，不用点图标 |
| 切换模式 | 点面板左上角模式按钮（如「自动识别 ▾」）；或右键右下角「译」→ 模式 |
| 关闭面板 | 面板右上角 `✕`；之后点托盘图标或右下角「译」可重新打开 |
| 移动 / 缩放面板 | 拖标题栏移动，拖任意一条边或四个角缩放，位置和尺寸自动记住 |
| 面板置顶 | 默认置顶，点其他软件不会把面板盖住 |
| 复制 / 重译 | 面板底部按钮，或选中译文用 `Ctrl+C` |
| 退出程序 | 右键右下角常驻「译」→ 退出程序（最可靠）；托盘右键 → 退出 |
| 强制结束 | 双击 `launchers\quit.bat`；`launchers\restart.bat` 可一键重启 |

## 五种模式

| 模式 | 作用 | 示例 |
|---|---|---|
| 自动识别（默认） | 单个英文单词 → 单词详解；含中文 → 中译英；其余 → 英译中 | 选中 `resilient` 自动查词 |
| 英译中 | 强制英译中 | 整段英文 → 中文 |
| 中译英 | 强制中译英（会自动校正混入的中文） | 中文句子 → 英文 |
| 长文摘要 | 中文要点 + 一句话总结，长文自动分段再合并 | 文章 / 邮件 → 3~6 条要点 |
| 术语解释 | 中文译名、领域、释义、用法、中英示例 | `zero-shot learning`、`ROI` |

## 配置

首次运行会生成 `config\config.json`（已在 `.gitignore` 中忽略）。改完重启程序生效，常用项：

| 配置 | 含义 |
|---|---|
| `mode` | 当前模式：`auto` / `translate` / `zh2en` / `summary` / `term` |
| `hotkey` | 全局快捷键，默认 `<ctrl>+<alt>+t`，留空关闭 |
| `server.gpu_layers` | 交给显卡的层数，99 = 全部；显存不足改 30 或 0（纯 CPU） |
| `server.port` | 本地引擎端口，默认 8377 |
| `server.model` | 模型路径，可换成更小/更大的 GGUF |
| `ui.panel_alpha` | 面板不透明度（默认 0.72） |
| `ui.panel_bg` | 是否启用动态背景 |
| `ui.panel_auto_passthrough` | 面板闲置 6 秒后自动鼠标穿透（默认开） |
| `ui.mascot_enabled` / `ui.mascot_size` / `ui.mascot_gif` | 看板娘开关、尺寸、动图路径 |
| `ui.window_pos` / `ui.popup_width` / `ui.popup_height` | 面板位置与尺寸（自动记录） |
| `translate.temperature` | 越低越稳定，翻译建议 0.2~0.4 |

换成更小的模型（如 Qwen2.5-3B）：把 GGUF 放进 `models\`，改 `server.model` 指向它即可。

## 目录结构

```
nahida-translator/
├─ start.bat                入口：启动（无控制台）
├─ launchers/
│  ├─ start_debug.bat       调试启动（保留控制台看日志）
│  ├─ setup.bat             首次安装（依赖 + 引擎 + 模型下载，可断点续传）
│  ├─ quit.bat              强制结束正在运行的实例
│  └─ restart.bat           强制结束旧实例并重新启动
├─ config/
│  ├─ config.json           配置（首次运行生成，git 忽略）
│  └─ requirements.txt      Python 依赖
├─ app/
│  ├─ main.py               入口：托盘、常驻按钮、单实例、事件编排
│  ├─ engine.py             本地推理：拉起 llama-server、流式翻译
│  ├─ selection.py          划词捕获：鼠标钩子 + Ctrl+C + 剪贴板还原
│  ├─ ui.py                 悬浮图标 / 常驻按钮 / 看板娘 / 译文面板（tkinter）
│  └─ config.py             配置读写
├─ assets/                  看板娘动图（自行放入，git 忽略）
├─ docs/                    截图
├─ tools/
│  ├─ download.py           引擎/模型下载（断点续传）
│  ├─ bench.py              引擎基准测试
│  └─ kill_app.ps1          强制结束实例（quit/restart 调用）
├─ logs/                    运行日志（git 忽略）
├─ models/                  GGUF 模型（git 忽略）
└─ runtime/                 llama.cpp 引擎与 CUDA 运行库（git 忽略）
```

## 技术实现

- **界面**：tkinter 无边框窗口 + PIL 抗锯齿绘制（圆角、按钮、标题带、动图逐帧）
- **取词**：`pynput` 全局鼠标/键盘钩子，模拟 `Ctrl+C` 后读剪贴板再还原（带哨兵值检测，剪贴板已有相同内容也能识别）
- **推理**：`llama-server`（llama.cpp）子进程，SSE 流式输出；显存不足自动回退 CPU
- **窗口细节**：Win32 `SetWindowRgn` 圆角、`WS_EX_TRANSPARENT` 鼠标穿透、`SetWindowLong` 动态开关
- **单实例**：命名互斥量 + PID 文件 + 退出看门狗，避免旧进程残留

## 常见问题

**某些程序里划词没反应？**
以管理员权限运行的程序会拦截普通程序的模拟按键（Windows UIPI 限制），换普通窗口即可；
终端里 `Ctrl+C` 是中断信号，建议避开。

**第一次翻译很慢？**
4.4GB 模型正在读入显存（约 3 秒）；若长时间不动，看 `logs\llama-server.log`。
显存不足会回退 CPU，速度慢但可用。

**会不会偷偷联网？**
不会。下载只在 `launchers\setup.bat` 执行一次；程序只访问 `127.0.0.1:8377`，
`llama-server` 也只绑定本机，外网无法访问。

**想确认引擎状态？**
`python app\main.py --test "Hello world"` 可在命令行直接翻译并打印耗时。

## 版权与隐私

- 代码为本项目原创。界面主题为程序绘制的配色，不包含《原神》游戏素材。
- 若你自行放入同人图 / 游戏截图作为背景（`assets\nahida_swing.gif`），请仅本地个人使用，
  版权归原作者 / 米哈游所有，不要二次分发。
- 所有翻译在本机完成，不上传任何文本。
