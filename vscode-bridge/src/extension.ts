import * as vscode from 'vscode';
import * as fs from 'fs';
import * as path from 'path';
import * as os from 'os';

const IPC_DIR = path.join(os.tmpdir(), 'kimi_vscode_bridge');

let bridgeEnabled = false;
let watcher: fs.FSWatcher | undefined;
let statusBarItem: vscode.StatusBarItem;

/**
 * VSCode 扩展入口
 * 通过文件系统 IPC 与 scripts/kimi_vscode_bridge.py 通信
 */
export function activate(context: vscode.ExtensionContext) {
    console.log('Kimi Bridge extension activated');

    // 状态栏按钮
    statusBarItem = vscode.window.createStatusBarItem(vscode.StatusBarAlignment.Right, 100);
    statusBarItem.command = 'kimiBridge.status';
    updateStatusBar();
    statusBarItem.show();
    context.subscriptions.push(statusBarItem);

    // 注册命令
    context.subscriptions.push(
        vscode.commands.registerCommand('kimiBridge.enable', enableBridge)
    );
    context.subscriptions.push(
        vscode.commands.registerCommand('kimiBridge.disable', disableBridge)
    );
    context.subscriptions.push(
        vscode.commands.registerCommand('kimiBridge.status', showStatus)
    );

    // 确保 IPC 目录存在
    if (!fs.existsSync(IPC_DIR)) {
        fs.mkdirSync(IPC_DIR, { recursive: true });
    }
}

export function deactivate() {
    disableBridge();
}

function updateStatusBar() {
    if (bridgeEnabled) {
        statusBarItem.text = "$(broadcast) Kimi Bridge: ON";
        statusBarItem.tooltip = "ACP-QQ Bridge is active. Messages from QQ will be forwarded to Kimi Code.";
        statusBarItem.backgroundColor = new vscode.ThemeColor('statusBarItem.prominentBackground');
    } else {
        statusBarItem.text = "$(debug-disconnect) Kimi Bridge: OFF";
        statusBarItem.tooltip = "Click to enable ACP-QQ Bridge";
        statusBarItem.backgroundColor = undefined;
    }
}

function enableBridge() {
    if (bridgeEnabled) {
        vscode.window.showInformationMessage('Kimi Bridge is already enabled');
        return;
    }

    bridgeEnabled = true;
    updateStatusBar();
    vscode.window.showInformationMessage('Kimi Bridge enabled! Waiting for QQ messages...');

    // 启动文件监控
    startFileWatcher();
}

function disableBridge() {
    if (!bridgeEnabled) {
        return;
    }
    bridgeEnabled = false;
    updateStatusBar();
    if (watcher) {
        watcher.close();
        watcher = undefined;
    }
    vscode.window.showInformationMessage('Kimi Bridge disabled');
}

function showStatus() {
    const status = bridgeEnabled ? 'Enabled' : 'Disabled';
    vscode.window.showInformationMessage(`Kimi Bridge: ${status} | IPC: ${IPC_DIR}`);
}

/**
 * 监控 IPC 目录中的 .in 文件（来自 QQ 的消息）
 */
function startFileWatcher() {
    if (watcher) {
        watcher.close();
    }

    // 先处理已有的文件
    processPendingFiles();

    // 然后启动监控
    watcher = fs.watch(IPC_DIR, (eventType, filename) => {
        if (eventType === 'rename' && filename && filename.endsWith('.in')) {
            setTimeout(() => handleIncomingFile(filename), 100);
        }
    });

    // 定时轮询兜底
    const interval = setInterval(() => {
        if (!bridgeEnabled) {
            clearInterval(interval);
            return;
        }
        processPendingFiles();
    }, 1000);
}

function processPendingFiles() {
    try {
        const files = fs.readdirSync(IPC_DIR);
        for (const file of files) {
            if (file.endsWith('.in')) {
                handleIncomingFile(file);
            }
        }
    } catch {
        // IPC 目录可能不存在
    }
}

/**
 * 处理来自 QQ 的输入消息
 */
async function handleIncomingFile(filename: string) {
    const filepath = path.join(IPC_DIR, filename);
    if (!fs.existsSync(filepath)) {
        return;
    }

    try {
        const content = fs.readFileSync(filepath, 'utf-8');
        const data = JSON.parse(content);
        const sessionId = data.session_id;
        const action = data.action;
        const text = data.text;

        // 删除已处理的文件
        fs.unlinkSync(filepath);

        if (action === 'interrupt') {
            vscode.window.showWarningMessage('QQ user requested interrupt');
            // TODO: 调用 Kimi Code 扩展的取消命令（如果有公开 API）
            writeReply(sessionId, 'interrupted', '任务已被打断');
            return;
        }

        // 显示状态提示
        vscode.window.setStatusBarMessage(`$(sync~spin) Kimi Bridge: processing from QQ...`, 3000);

        // 将消息发送到 Kimi Code 扩展
        // 方式 1: 如果有 Kimi Code 扩展的公开 API
        // const kimi = vscode.extensions.getExtension('moonshot-ai.kimi-code');
        // if (kimi && kimi.isActive) {
        //     const result = await kimi.exports.ask(text);
        //     writeReply(sessionId, 'idle', result);
        // }

        // 方式 2: 通过 VSCode 命令调用（当前使用）
        // 打开一个新的 Chat 视图并发送消息
        await sendToKimiCode(text, sessionId);

    } catch (err) {
        console.error('Failed to handle incoming file:', err);
    }
}

/**
 * 通过 VSCode 命令与 Kimi Code 交互
 */
async function sendToKimiCode(text: string, sessionId: string) {
    try {
        // 写入思考状态
        writeReply(sessionId, 'thinking', '正在通过 VSCode Kimi Code 处理...');

        // 尝试执行 Kimi Code 的命令（命令 ID 需要根据实际扩展调整）
        // 常见命令 ID 示例：
        // - kimi-code.startChat
        // - kimi-code.sendMessage
        // - workbench.action.chat.open
        // - workbench.action.chat.sendMessage

        // 步骤 1: 打开 Chat 视图
        await vscode.commands.executeCommand('workbench.panel.chatSidebar');

        // 步骤 2: 将文本写入剪贴板并粘贴（通用方案）
        await vscode.env.clipboard.writeText(text);

        // 步骤 3: 聚焦输入框并粘贴
        // 注意：这些命令 ID 是示例，需要根据 Kimi Code 扩展的实际命令调整
        await vscode.commands.executeCommand('workbench.action.focusPanel');

        // 模拟等待 Kimi Code 回复（实际应该监听扩展的事件或输出）
        setTimeout(() => {
            // 这里应该是收到 Kimi Code 回复后的回调
            // 当前作为演示，返回模拟结果
            const mockReply = `已收到 QQ 消息: "${text}"\n\n` +
                `（注意：此处需要接入 Kimi Code 扩展的实际回复。` +
                `请在 sendToKimiCode() 函数中实现与 Kimi Code 扩展的集成。）`;
            writeReply(sessionId, 'idle', mockReply);
        }, 2000);

    } catch (err) {
        console.error('Failed to send to Kimi Code:', err);
        writeReply(sessionId, 'idle', `调用 Kimi Code 失败: ${err}`);
    }
}

/**
 * 将处理结果写回 IPC 目录（Bridge 会读取并发送到 QQ）
 */
function writeReply(sessionId: string, status: string, content: string) {
    const filepath = path.join(IPC_DIR, `${sessionId}.out`);
    const data = {
        timestamp: Date.now() / 1000,
        session_id: sessionId,
        status: status,
        content: content,
    };
    fs.writeFileSync(filepath, JSON.stringify(data, null, 2), 'utf-8');
}
