#!/bin/bash
set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR"

VENV="$SCRIPT_DIR/venv"
PYTHON="$VENV/bin/python"
PIP="$VENV/bin/pip"
PYINSTALLER="$VENV/bin/pyinstaller"

echo "==> 检查虚拟环境..."
if [ ! -f "$PYTHON" ]; then
    echo "错误：未找到虚拟环境，请先运行: python3 -m venv venv && pip install -r requirements.txt"
    exit 1
fi

echo "==> 安装 PyInstaller..."
"$PIP" install --quiet pyinstaller

echo "==> 清理旧构建..."
rm -rf build dist

echo "==> 开始构建 rCapture.app ..."
"$PYINSTALLER" rCapture.spec --noconfirm

echo ""
echo "==> 构建完成！应用位于: dist/rCapture.app"
echo ""
echo "首次运行时，macOS 会要求授权以下权限："
echo "  • 屏幕录制（截图/录屏）"
echo "  • 输入监控（全局快捷键）"
echo "  • 麦克风（录音，可选）"
echo ""
echo "若 macOS 提示「无法打开」，请在终端执行："
echo "  xattr -cr dist/rCapture.app"
echo ""

# 可选：打包成 DMG（需要 create-dmg: brew install create-dmg）
if command -v create-dmg &>/dev/null; then
    echo "==> 正在生成 DMG..."
    create-dmg \
        --volname "rCapture" \
        --window-pos 200 120 \
        --window-size 600 400 \
        --icon-size 128 \
        --icon "rCapture.app" 150 185 \
        --hide-extension "rCapture.app" \
        --app-drop-link 450 185 \
        "dist/rCapture.dmg" \
        "dist/rCapture.app"
    echo "==> DMG 已生成: dist/rCapture.dmg"
fi
