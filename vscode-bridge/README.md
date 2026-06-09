# Kimi Bridge VSCode 扩展

将 VSCode 中的 Kimi Code 插件桥接到 ACP-QQ Bridge，实现 QQ 远程控制。

## 安装步骤

```bash
cd vscode-bridge
npm install
npm run compile
```

## 加载扩展

1. 打开 VSCode
2. 按 `Ctrl+Shift+P`（或 `Cmd+Shift+P`）
3. 输入 `Extensions: Install from VSIX...`（如果有打包）
   或者开发模式：按 `F5` 打开 Extension Host 窗口

## 使用

1. 启动 Python 桥接器：
   ```bash
   python scripts/kimi_vscode_bridge.py
   ```

2. 在 VSCode 中按 `Ctrl+Shift+P` → `Kimi Bridge: Enable`

3. 现在 QQ 发来的消息会通过文件系统 IPC 转发到 VSCode

## 与 Kimi Code 扩展集成

当前代码使用**剪贴板+命令**的通用方式与 Kimi Code 交互。

如需深度集成，需要：
1. 查看 Kimi Code 扩展的 `package.json` 中公开的 `commands`
2. 修改 `src/extension.ts` 中的 `sendToKimiCode()` 函数
3. 使用 `vscode.extensions.getExtension('publisher.name')?.exports` 调用扩展 API
