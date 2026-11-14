// Gemini CLI 로그인용 미리 읽기(--import): Gemini 가 브라우저를 직접 여는 대신 로그인 주소를 파일(AGENT_LOGIN_URL_FILE)로 넘긴다.
// 관리 화면이 그 주소를 받아 연다 (agent/claude_code/engines.py LoginFlows). Windows 의 `open` 패키지는
// powershell -EncodedCommand 'Start "<주소>"' 로 브라우저를 여므로 그 호출만 가로챈다. 그 밖의 실행은 그대로.
import cp from "node:child_process";
import fs from "node:fs";

const original = cp.spawn;
cp.spawn = function spawn(cmd, args, opts) {
  try {
    const out = process.env.AGENT_LOGIN_URL_FILE;
    if (out && typeof cmd === "string" && /powershell/i.test(cmd) && Array.isArray(args) && args.includes("-EncodedCommand")) {
      const decoded = Buffer.from(args[args.indexOf("-EncodedCommand") + 1] || "", "base64").toString("utf16le");
      const m = decoded.match(/https?:\/\/[^"'\s`]+/);
      if (m) {
        fs.writeFileSync(out, m[0].replace(/`/g, ""), "utf8");
        return original.call(this, process.execPath, ["-e", ""], { stdio: "ignore" });
      }
    }
  } catch {
    // 가로채지 못하면 원래대로 브라우저를 연다
  }
  return original.apply(this, arguments);
};
