import { ChildProcessWithoutNullStreams, exec } from "child_process";
import { spawn } from "child_process";
import EventEmitter from "events";
import * as fs from "fs";
import * as os from "os";
import * as path from "path";
import * as semver from "semver";
import { StreamMessage } from "types";
import * as util from "util";
import { v4 } from "uuid";
import * as vscode from "vscode";
const aexec = util.promisify(exec);

class Server {
    private binFolder: string | undefined = undefined;
    private serverProcess: ChildProcessWithoutNullStreams | undefined;
    public messageEmitter: EventEmitter = new EventEmitter();
    private backlog: StreamMessage[] = [];

    constructor() {}

    private async installMentat(
        progress: vscode.Progress<{ message?: string; increment?: number }>
    ): Promise<string> {
        // Check mentat dir
        const mentatDir = path.join(os.homedir(), ".mentat");
        if (!fs.existsSync(mentatDir)) {
            fs.mkdirSync(mentatDir);
        }

        // Check python version
        const pythonCommands =
            process.platform === "win32"
                ? ["py -3.10", "py -3", "py"]
                : ["python3.10", "python3", "python"];
        let pythonCommand: string | undefined = undefined;
        for (const command of pythonCommands) {
            try {
                const { stdout } = await aexec(`${command} --version`);
                const versionMatch = stdout.match(
                    /Python (\d+\.\d+)(?:\.\d+)?/
                );
                if (versionMatch) {
                    const version = semver.coerce(versionMatch[1]);
                    if (
                        semver.gte(
                            version || new semver.SemVer("3.10.0"),
                            new semver.SemVer("3.10.0")
                        )
                    ) {
                        pythonCommand = command;
                        break;
                    }
                }
            } catch (error) {
                continue;
            }
        }
        if (pythonCommand === undefined) {
            throw new Error(
                "Python 3.10 or above is not found on your system. Please install it and try again."
            );
        }

        // Check if venv exists
        const venvPath = path.join(mentatDir, ".venv");
        if (!fs.existsSync(venvPath)) {
            progress.report({
                message: "Mentat: Creating Python environment...",
            });
            const createVenvCommand = `${pythonCommand} -m venv ${venvPath}`;
            await aexec(createVenvCommand);
        }
        const binFolder = path.join(
            venvPath,
            process.platform === "win32" ? "Scripts" : "bin"
        );
        const pythonLocation = path.join(binFolder, "python");

        // TODO: Auto update mentat if wrong version
        const { stdout } = await aexec(`${pythonLocation} -m pip show mentat`);
        const mentatVersion = stdout
            .split("\n")
            .at(1)
            ?.split("Version: ")
            ?.at(1);
        if (mentatVersion === undefined) {
            progress.report({ message: "Mentat: Installing..." });
            await aexec(`${pythonLocation} -m pip install mentat`);
            console.log("Installed Mentat");
        }

        return binFolder;
    }

    private async startMentat(workspaceRoot: string, binFolder: string) {
        const mentatExecutable: string = path.join(binFolder, "mentat-server");

        // TODO: Pass config options to mentat here
        // TODO: I don't think this will work on Windows (check to make sure)
        const serverProcess = spawn(mentatExecutable, [workspaceRoot]);
        serverProcess.stdout.setEncoding("utf-8");
        serverProcess.stdout.on("data", (data: any) => {
            // console.log(`Server Output: ${data}`);
        });
        serverProcess.stderr.on("data", (data: any) => {
            console.error(`Server Error: ${data}`);
        });
        serverProcess.on("close", (code: number) => {
            console.log(`Server exited with code ${code}`);
        });
        return serverProcess;
    }

    public async startServer(workspaceRoot: string) {
        if (this.binFolder === undefined) {
            this.binFolder = await vscode.window.withProgress(
                { location: vscode.ProgressLocation.Notification },
                async (progress) => {
                    return await this.installMentat(progress);
                }
            );
        }
        if (this.serverProcess !== undefined) {
            this.backlog = [];
            this.serverProcess.kill();
        }
        this.serverProcess = await this.startMentat(
            workspaceRoot,
            this.binFolder
        );
        this.serverProcess.stdout.on("data", (output: string) => {
            for (const serializedMessage of output.trim().split("\n")) {
                try {
                    const message: StreamMessage = JSON.parse(
                        serializedMessage.trim()
                    );
                    this.messageEmitter.emit("message", message);
                } catch (error) {
                    console.error("Error reading StreamMessage:", error);
                }
            }
        });
    }

    public closeServer() {
        if (this.serverProcess !== undefined) {
            this.serverProcess.kill();
        }
    }

    /**
     * Convenience method for sending data over a specific channel
     */
    public sendStreamMessage(
        data: any,
        channel: string,
        extra: { [key: string]: any } = {}
    ) {
        const message: StreamMessage = {
            id: v4(),
            channel: channel,
            source: "client",
            data: data,
            extra: extra,
        };
        this.sendMessage(message);
    }

    public sendMessage(message: StreamMessage) {
        if (this.serverProcess === undefined) {
            this.backlog.push(message);
        } else {
            for (const streamMessage of this.backlog) {
                this.serverProcess.stdin.write(
                    JSON.stringify(streamMessage) + "\n"
                );
            }
            this.backlog = [];
            this.serverProcess.stdin.write(JSON.stringify(message) + "\n");
        }
    }
}

export const server = new Server();
