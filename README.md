# rCapture

macOS 轻量截图 + 录屏小工具。GUI 用 PySide6；截图走 Quartz `CGDisplayCreateImage`（物理分辨率）+ Pillow；录屏用 `ffmpeg avfoundation`。

## 功能 (v0.5)

### 截图

| 模式 | 说明 |
|---|---|
| **全屏截图** | 一键保存整块主屏幕 |
| **区域截图** | 半透明叠层自由框选，支持调整 / 标注 / 导出 |
| **截长图** | 自动滚动拼接长页面（倒计时 3 秒后开始） |

#### 区域截图操作说明

**IDLE 状态（框选前）**
- 鼠标旁显示 15×15 放大镜，实时采样光标下像素颜色
- `C`：复制 RGB 值到剪贴板；`Shift+C`：复制 HEX 值（复制后自动退出）
- ESC：退出

**选区**
- 拖拽绘制选区；框选后可拖边角/边线调整大小，拖内部移动位置
- Shift 拖拽：等比正方形；Alt(⌥) 拖拽：以起点为中心框选
- 滚轮：以选区中心等比缩放选区
- 右键：清除当前选区，重新框选
- ESC：退出截图叠层

**圆角 / 阴影**
- 左上角弧形图标拖拽 → 调整圆角半径（保存为圆角透明 PNG）
- 四条边中点刻度拖拽 → 调整阴影大小
- 圆角使用 4× 超采样 + LANCZOS 缩放，边缘平滑无锯齿

**标注工具**（工具栏选中后在选区内操作）

| 工具 | 操作 |
|---|---|
| 画笔 | 自由笔迹；Shift 画直线；支持纯线条 / 终点箭头 / 双向箭头 |
| 矩形 | 拖拽绘制矩形框；Shift 正方形；支持空心 / 实心 |
| 马赛克 | 拖拽绘制打码区域 |
| 文字 | 点击输入；支持文字颜色 + 描边颜色 |

- 滚轮：调整画笔 / 矩形线宽 或 文字字号
- 工具栏悬停弹出颜色/样式二级菜单；**样式在截图之间持久保留**
- ⌘Z / 工具栏「撤销」逐步撤销标注

**导出**

| 操作 | 结果 |
|---|---|
| 回车 / 双击选区 / 「完成」 | 复制到剪贴板 |
| 「保存」 | 弹出保存对话框，存为 PNG |
| 「识文字」 | OCR 识别选区文字并复制到剪贴板（识别中显示转圈动画，ESC / 右键可取消） |
| **鼠标中键** | **Pin 到屏幕**（见下） |

---

### Pin 到屏幕

区域截图调整界面按**鼠标中键**，截图以浮动窗口钉在屏幕最上层，切换 App 后依然可见。

| 操作 | 效果 |
|---|---|
| 拖拽 | 移动位置 |
| 滚轮 | 以鼠标为中心缩放（0.2× ~ 5×） |
| 双击 / 右键 / ESC | 关闭 |
| 悬停右上角 × | 点击关闭 |

可同时 Pin 多张图。

---

### 录屏

- 全屏录屏 / 区域录屏（选区后立即开始）
- macOS AVFoundation，H.264 MP4
- 可选：帧率（5–60 fps）、包含鼠标指针、录制系统麦克风
- 录屏中显示计时器；再次点击停止并保存

---

### 其他

- **菜单栏托盘**：关闭主窗口后继续后台运行，从托盘图标触发所有功能
- **全局快捷键**：可在设置对话框自定义

  | 动作 | macOS 默认 |
  |---|---|
  | 全屏截图 | ⌘⇧1 |
  | 区域截图 | ⌘⇧2 |
  | 全屏录屏 开/停 | ⌘⇧R |
  | 区域录屏 开/停 | ⌘⇧E |

- **开机自启**：设置对话框中一键开关
- **自定义保存目录**（默认 `~/Pictures/rCapture`）
- **配置持久化**：`~/.config/rcapture/config.json`；圆角 / 阴影值在截图之间保留
- **单实例守护**：重复启动时自动唤起已有窗口

---

## 依赖

- macOS 12+
- Python 3.10+（仓库预建 venv 为 Python 3.12）
- 录屏需要 [ffmpeg](https://ffmpeg.org/)：`brew install ffmpeg`
- OCR 需要 `rapidocr-onnxruntime`（见 `requirements.txt`，离线可用，无需额外系统依赖）
- Python 依赖见 `requirements.txt`（PySide6、pynput、mss、Pillow、rapidocr-onnxruntime、onnxruntime）

## 安装 & 运行

```bash
python3.12 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
python main.py
```

仓库已预建 `venv/`（py3.13），可直接：

```bash
venv/bin/python main.py
```

## 打包为 .app

```bash
bash build_mac.sh
# 产物：dist/rCapture.app
```

首次运行 `.app` 若提示「无法打开」：
```bash
xattr -cr dist/rCapture.app
```

## 系统权限（macOS）

首次运行会分别请求：

| 功能 | 系统设置路径 |
|---|---|
| 截图 / 录屏 | 隐私与安全性 → 屏幕录制 |
| 全局快捷键 | 隐私与安全性 → 输入监控 |

给对应宿主进程（Terminal / PyCharm / Python）赋权后**重启**该进程即可。

## 自定义快捷键

主窗口右上角「设置…」→ 点击动作右侧按钮 → 按下组合键（须含至少一个修饰键）。Esc 取消录入，「清除」清空绑定，「恢复默认」一键复位。

## 项目结构

```
main.py                     # 入口
rcapture/
  app.py                    # 主窗口、托盘、PinnedOverlay、OCR 线程
  screenshot.py             # Quartz/Pillow 截图、标注渲染、圆角、阴影
  recorder.py               # ffmpeg avfoundation 录屏
  region_selector.py        # 半透明叠层：选区 / 调整 / 标注 / 放大镜
  long_screenshot.py        # 滚动截长图线程
  ocr.py                    # RapidOCR（离线）封装
  hotkeys.py                # pynput GlobalHotKeys → Qt signals
  settings_dialog.py        # 快捷键 + 偏好设置 UI
  config.py                 # JSON 配置 + 平台默认快捷键
  startup.py                # 开机自启（LaunchAgent）
```

## 已知限制

- 录屏目前只支持 macOS（avfoundation）
- 区域选择覆盖**主屏幕**；多屏支持待后续
