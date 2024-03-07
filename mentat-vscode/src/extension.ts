import WebviewProvider from "lib/WebviewProvider";
import * as vscode from "vscode";
import * as os from "os";
import { excludeResource, includeResource } from "utils/commands";
import { ContextUpdateData, StreamMessage } from "types";
import path from "path";
import { server } from "utils/server";
import { ContextFileDecorationProvider } from "lib/ContextFileDecorationProvider";

function contextUpdate(
    data: ContextUpdateData,
    contextFileDecorationProvider: ContextFileDecorationProvider
) {
    const features = [...data.features, ...data.auto_features];
    const folders: string[] = [];
    for (const feature of features) {
        var dir = feature;
        while (dir !== path.dirname(dir)) {
            dir = path.dirname(dir);
            folders.push(dir);
        }
    }

    // Update context (needed for context menu commands)
    vscode.commands.executeCommand(
        "setContext",
        "mentat.includedFiles",
        features
    );
    vscode.commands.executeCommand(
        "setContext",
        "mentat.includedFolders",
        folders
    );

    // Update file decorations
    contextFileDecorationProvider.refresh([...features, ...folders]);
}

async function activateClient(context: vscode.ExtensionContext) {
    try {
        // Startup
        const workspaceRoot =
            vscode.workspace.workspaceFolders?.at(0)?.uri?.path ?? os.homedir();
        await server.startServer(workspaceRoot);

        // Commands
        context.subscriptions.push(
            vscode.commands.registerCommand(
                "mentat.includeFile",
                includeResource
            )
        );
        context.subscriptions.push(
            vscode.commands.registerCommand(
                "mentat.includeFolder",
                includeResource
            )
        );
        context.subscriptions.push(
            vscode.commands.registerCommand(
                "mentat.excludeFile",
                excludeResource
            )
        );
        context.subscriptions.push(
            vscode.commands.registerCommand(
                "mentat.excludeFolder",
                excludeResource
            )
        );

        // File decoration
        const contextFileDecorationProvider =
            new ContextFileDecorationProvider();
        context.subscriptions.push(
            vscode.window.registerFileDecorationProvider(
                contextFileDecorationProvider
            )
        );

        // Activity bar views
        /*
        Under "mentat-panel" in package.json:
        {
            "id": "mentat-context-view",
            "name": "Context"
        },

        In contextUpdate:
        contextTreeProvider.updateContext(data.features, data.auto_features);

        Here:
        const contextTreeProvider = new ContextTreeProvider(workspaceRoot);
        context.subscriptions.push(
            vscode.window.registerTreeDataProvider(
                "mentat-context-view",
                contextTreeProvider
            )
        );
        */

        const chatWebviewProvider = new WebviewProvider(context.extensionUri);
        context.subscriptions.push(
            vscode.window.registerWebviewViewProvider(
                "mentat-webview",
                chatWebviewProvider,
                {
                    webviewOptions: { retainContextWhenHidden: true },
                }
            )
        );

        // Misc
        server.messageEmitter.on("message", (message: StreamMessage) => {
            // We have to listen for and post the message here or the webview might miss it when not loaded
            chatWebviewProvider.postMessage(message);
            switch (message.channel) {
                case "context_update": {
                    contextUpdate(message.data, contextFileDecorationProvider);
                    break;
                }
            }
        });
    } catch (e) {
        vscode.window.showErrorMessage((e as any).message, "Close");
        throw e;
    }
}

export function activate(context: vscode.ExtensionContext) {
    activateClient(context);
}

export function deactivate() {
    server.closeServer();
}
