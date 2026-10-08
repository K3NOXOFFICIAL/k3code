import { spawn } from "node:child_process";

export interface LaunchResult {
  code: null | number;
  error?: string;
}

const resolveCliBin = () => process.env.K3CODE_BIN?.trim() || "k3code";

export const launchK3codeCommand = (args: string[]): Promise<LaunchResult> =>
  new Promise((resolve) => {
    const child = spawn(resolveCliBin(), args, { stdio: "inherit" });

    child.on("error", (err) => resolve({ code: null, error: err.message }));
    child.on("exit", (code) => resolve({ code }));
  });
