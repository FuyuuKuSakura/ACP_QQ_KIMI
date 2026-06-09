#!/bin/bash
# ACP-QQ Bridge 生产环境启动脚本
# 启动 Kimi Code Bridge + ACP-QQ Bridge

set -e
cd "$(dirname "$0")/.."
source .venv/bin/activate
export PATH="/Users/fuyuuku/.kimi-code/bin:$PATH"

echo "==================================="
echo "  ACP-QQ Bridge 生产环境启动器"
echo "==================================="
echo ""

# 检查 Kimi CLI
echo "🔍 检查 Kimi CLI..."
if ! command -v kimi >/dev/null 2>&1; then
    echo "❌ Kimi CLI 未找到，请确认已安装 Kimi Code"
    exit 1
fi
echo "✅ Kimi CLI 可用"

# 检查 QQ 是否在运行
echo ""
echo "🔍 检查 QQ..."
if pgrep -x "QQ" >/dev/null 2>&1; then
    echo "✅ QQ 正在运行"
else
    echo "⚠️  QQ 未运行，请先启动 QQ 并登录"
    read -p "按回车键继续..."
fi

# 启动 Kimi Code Bridge
echo ""
echo "🚀 启动 Kimi Code Bridge (ws://127.0.0.1:8765)..."
python scripts/kimi_code_bridge.py > logs/kimi_bridge.log 2>&1 &
KIMI_PID=$!
echo "   PID: $KIMI_PID"
sleep 3

# 启动 ACP-QQ Bridge
echo ""
echo "🚀 启动 ACP-QQ Bridge (ws://127.0.0.1:8080)..."
python -m acp_qq_bridge --config config.yaml > logs/acp_bridge.log 2>&1 &
BRIDGE_PID=$!
echo "   PID: $BRIDGE_PID"
sleep 2

echo ""
echo "==================================="
echo "✅ 所有服务已启动！"
echo "==================================="
echo ""
echo "📋 服务状态："
echo "   Kimi Bridge PID: $KIMI_PID"
echo "   ACP Bridge PID:  $BRIDGE_PID"
echo ""
echo "📁 日志文件："
echo "   logs/kimi_bridge.log"
echo "   logs/acp_bridge.log"
echo ""
echo "🛑 停止服务："
echo "   kill $KIMI_PID $BRIDGE_PID"
echo ""
echo "💡 提示：首次使用需要在 QQ 中私聊机器人，"
echo "   消息会自动转发给 Kimi AI 处理。"
echo ""

# 等待用户按 Ctrl+C
echo "按 Ctrl+C 停止所有服务..."
wait
