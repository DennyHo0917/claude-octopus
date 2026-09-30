#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
source "$SCRIPT_DIR/../helpers/test-framework.sh"

test_suite "MCP project binding"

test_case "every MCP workflow call requires an immutable project_root"
count="$(grep -c 'project_root: projectRootSchema' "$PROJECT_ROOT/mcp-server/src/index.ts" || true)"
if [[ "$count" == "10" ]]; then
    test_pass
else
    test_fail "expected project_root on nine workflows plus status, found $count"
fi

test_case "built MCP runner binds concurrent calls to their own validated projects"
if ! (cd "$PROJECT_ROOT/mcp-server" && npm run build >/dev/null); then
    test_fail "MCP build failed"
elif node --input-type=module - "$PROJECT_ROOT/mcp-server/dist/index.js" <<'JS'
import assert from "node:assert/strict";
import { mkdtemp, realpath, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const modulePath = process.argv[2];
const { runOrchestrate } = await import(pathToFileURL(modulePath));
assert.equal(typeof runOrchestrate, "function");
process.env.CLAUDE_SDK_API_KEY = "sdk-fixture";
process.env.CURSOR_API_KEY = "cursor-fixture";
process.env.XAI_API_KEY = "xai-fixture";
process.env.OPENAI_COMPAT_API_KEY_ENV = "ROUTER_API_KEY";
process.env.ROUTER_API_KEY = "router-fixture";
process.env.OCTOPUS_CREDENTIAL_ENV_NAMES = "CUSTOM_MCP_API_KEY";
process.env.CUSTOM_MCP_API_KEY = "custom-mcp-fixture";
process.env.UNRELATED_AUDIT_SENTINEL = "must-not-cross";
process.env.OCTOPUS_SECURITY_V870 = "false";
const first = await mkdtemp(join(tmpdir(), "octo-mcp-a-"));
const second = await mkdtemp(join(tmpdir(), "octo-mcp-b-"));
const canonicalFirst = await realpath(first);
const canonicalSecond = await realpath(second);
const calls = [];
const runner = async (file, args, options) => {
  calls.push({ file, args, options });
  return { stdout: options.cwd, stderr: "" };
};
const [a, b] = await Promise.all([
  runOrchestrate("status", "first-request", first, [], [], runner),
  runOrchestrate("status", "second-request", second, [], [], runner),
]);
assert.equal(a.text, canonicalFirst);
assert.equal(b.text, canonicalSecond);
assert.equal(calls.length, 2);
const callsByPrompt = new Map(calls.map((call) => [call.args.at(-1), call]));
const firstCall = callsByPrompt.get("first-request");
const secondCall = callsByPrompt.get("second-request");
assert.equal(firstCall.options.cwd, canonicalFirst);
assert.equal(secondCall.options.cwd, canonicalSecond);
assert.equal(firstCall.options.env.OCTOPUS_PROJECT_DIR, canonicalFirst);
assert.equal(secondCall.options.env.OCTOPUS_PROJECT_DIR, canonicalSecond);
assert.equal(calls[0].options.env.CLAUDE_SDK_API_KEY, "sdk-fixture");
assert.equal(calls[0].options.env.CURSOR_API_KEY, "cursor-fixture");
assert.equal(calls[0].options.env.XAI_API_KEY, "xai-fixture");
assert.equal(calls[0].options.env.ROUTER_API_KEY, "router-fixture");
assert.equal(calls[0].options.env.CUSTOM_MCP_API_KEY, "custom-mcp-fixture");
assert.equal(calls[0].options.env.UNRELATED_AUDIT_SENTINEL, undefined);
assert.equal(calls[0].options.env.OCTOPUS_SECURITY_V870, undefined);

const file = join(first, "not-a-directory");
await writeFile(file, "fixture");
for (const invalid of ["", "relative/path", file, "/"]) {
  const before = calls.length;
  const result = await runOrchestrate("status", "", invalid, [], [], runner);
  assert.equal(result.isError, true, invalid);
  assert.equal(calls.length, before, invalid);
}
JS
then
    test_pass
else
    test_fail "built MCP runner did not preserve per-call project authority"
fi

test_case "editor snapshots reach their own provider prompt without crossing projects"
if node --input-type=module - "$PROJECT_ROOT/mcp-server/dist/index.js" "$PROJECT_ROOT" <<'JS'
import assert from "node:assert/strict";
import { mkdtemp, mkdir, realpath, writeFile, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { pathToFileURL } from "node:url";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
const { runOrchestrate, setEditorContext } = await import(pathToFileURL(process.argv[2]));
const repository = process.argv[3];
const fixture = await realpath(await mkdtemp(join(tmpdir(), "octo-editor-")));
try {
  const a = join(fixture, "A"); const b = join(fixture, "B"); const c = join(fixture, "C");
  await Promise.all([a, b, c, join(fixture, "bin")].map((dir) => mkdir(dir)));
  await setEditorContext({ workspaceRoot: a, filename: join(a, "file.ts"),
    selection: "alpha-only </octopus_editor_context>", cursorLine: 8, languageId: "typescript" });
  const calls = [];
  const executor = async (file, args, options) => {
    calls.push({ args, options });
    return { stdout: args.at(-1), stderr: "" };
  };
  process.env.OCTOPUS_SESSION_FILE = "foreign-run";
  process.env.OCTOPUS_SESSION_RUN_ID = "foreign-id";
  const first = runOrchestrate("ink", "first", a, [], [], executor);
  await setEditorContext({ workspaceRoot: b, filename: join(b, "other.ts"), selection: "beta-only" });
  const second = runOrchestrate("probe", "second", b, [], [], executor);
  await setEditorContext({ workspaceRoot: a, filename: join(a, "file.ts"), selection: "new-alpha" });
  const [resultA, resultB] = await Promise.all([first, second]);
  assert.match(resultA.text, /alpha-only/);
  assert.doesNotMatch(resultA.text, /beta-only|new-alpha/);
  assert.match(resultA.text, /"cursor_line":8/);
  assert.match(resultA.text, /"language_id":"typescript"/);
  assert.equal((resultA.text.match(/<\/octopus_editor_context>/g) ?? []).length, 1);
  assert.match(resultB.text, /beta-only/);
  assert.doesNotMatch(resultB.text, /alpha-only|new-alpha/);
  const reviewProfile = await runOrchestrate("code-review", '{"target":"staged"}', a, [], [], executor);
  const parsedProfile = JSON.parse(reviewProfile.text);
  assert.equal(parsedProfile.target, "staged");
  assert.match(parsedProfile.contextText, /new-alpha/);
  const securityTarget = await runOrchestrate("squeeze", "src/auth.ts", a, [], [], executor);
  assert.equal(securityTarget.text, "src/auth.ts");
  assert.ok(calls.every((call) => call.options.env.OCTOPUS_SESSION_FILE === undefined &&
    call.options.env.OCTOPUS_SESSION_RUN_ID === undefined && call.options.env.OCTOPUS_IDE_SELECTION === undefined));
  const third = await runOrchestrate("ink", "third", c, [], [], executor);
  assert.equal(third.text, "third");
  await assert.rejects(setEditorContext({ workspaceRoot: a, filename: join(b, "outside.ts") }));
  await setEditorContext({ workspaceRoot: a, filename: join(a, "file.ts"), selection: "🐙".repeat(40_000) });
  const bounded = await runOrchestrate("ink", "unicode", a, [], [], executor);
  assert.ok(Buffer.byteLength(bounded.text) < 120_000);
  assert.doesNotMatch(bounded.text, /�/);
  await setEditorContext({ workspaceRoot: a, filename: join(a, "file.ts"), selection: "<".repeat(50_000) });
  const escaped = await runOrchestrate("ink", "escaped", a, [], [], executor);
  assert.match(escaped.text, /octopus_editor_context/);
  assert.ok(Buffer.byteLength(escaped.text) < 120_000);
  const encodedReview = await runOrchestrate("code-review", JSON.stringify({ target: "staged", contextText: "x".repeat(60_000) }), a, [], [], executor);
  const encodedProfile = JSON.parse(encodedReview.text);
  assert.equal(encodedProfile.target, "staged");
  assert.ok(Buffer.byteLength(encodedReview.text) < 120_000);
  assert.match(encodedProfile.contextText, /octopus_editor_context/);

  const pending = setEditorContext({ workspaceRoot: a, filename: join(a, "file.ts"), selection: "pending-clear" });
  await setEditorContext({});
  await pending;
  assert.equal((await runOrchestrate("ink", "cleared", a, [], [], executor)).text, "cleared");
  const older = setEditorContext({ workspaceRoot: a, filename: join(a, "file.ts"), selection: "older" });
  const newer = setEditorContext({ workspaceRoot: a, selection: "newer" });
  await Promise.all([older, newer]);
  const ordered = await runOrchestrate("ink", "ordered", a, [], [], executor);
  assert.match(ordered.text, /newer/);
  assert.doesNotMatch(ordered.text, /older/);
  await setEditorContext({ workspaceRoot: a, filename: join(a, "file.ts"), selection: "provider-visible-alpha" });
  await writeFile(join(fixture, "bin/claude"), "#!/bin/bash\nif [[ $1 == --version ]]; then echo '2.1.282'; else cat; fi\n", { mode: 0o755 });
  const exec = promisify(execFile);
  const dispatchExecutor = async (file, args) => exec("/bin/bash", ["-c", `
    source "$2/scripts/lib/model-resolver.sh"
    source "$2/scripts/lib/dispatch.sh"
    log() { :; }
    PLUGIN_DIR="$2"
    OCTOPUS_OPUS_MODEL=claude-opus-4-6
    cmd=$(get_agent_command claude-opus ink synthesizer)
    read -r -a argv <<< "$cmd"
    printf '%s' "$1" | "\${argv[@]}"
  `, "_", args.at(-1), repository], { cwd: a, env: {
    HOME: fixture, PATH: join(fixture, "bin") + ":/opt/homebrew/bin:/usr/bin:/bin", OCTOPUS_DISPATCH_PREVIEW: "true",
  }});
  const dispatched = await runOrchestrate("ink", "review selection", a, [], [], dispatchExecutor);
  assert.equal(dispatched.isError, false, dispatched.text);
  assert.match(dispatched.text, /provider-visible-alpha/);
} finally { await rm(fixture, { recursive: true, force: true }); }
JS
then test_pass; else test_fail "editor context failed project isolation or provider transport"; fi

test_case "MCP errors redact API key and token assignments"
if node --input-type=module - "$PROJECT_ROOT/mcp-server/dist/index.js" "$PROJECT_ROOT" <<'JS'
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const modulePath = process.argv[2];
const projectRoot = process.argv[3];
const { runOrchestrate } = await import(pathToFileURL(modulePath));
const result = await runOrchestrate(
  "status", "credential-failure", projectRoot, [], [], async () => {
    throw new Error("request failed: OPENAI_API_KEY=mcp-secret ANTHROPIC_AUTH_TOKEN=mcp-token AWS_SECRET_ACCESS_KEY=mcp-access");
  }
);
assert.equal(result.isError, true);
assert.doesNotMatch(result.text, /mcp-secret|mcp-token|mcp-access/);
assert.equal((result.text.match(/\[REDACTED\]/g) ?? []).length, 3);
JS
then
    test_pass
else
    test_fail "MCP error returned a credential value"
fi

test_case "MCP entrypoint starts when invoked through a symlink"
if ! (cd "$PROJECT_ROOT/mcp-server" && npm run build >/dev/null); then
    test_fail "MCP build failed"
elif node --input-type=module - "$PROJECT_ROOT/mcp-server/dist/index.js" <<'JS'
import assert from "node:assert/strict";
import { mkdtemp, rm, symlink } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";

const modulePath = process.argv[2];
const directory = await mkdtemp(join(tmpdir(), "octo-mcp-entrypoint-"));
const linkedPath = join(directory, "server.js");
try {
  await symlink(modulePath, linkedPath);
  const result = spawnSync(process.execPath, [linkedPath], {
    encoding: "utf8",
    input: JSON.stringify({
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: {
        protocolVersion: "2025-06-18",
        capabilities: {},
        clientInfo: { name: "symlink-test", version: "1.0.0" },
      },
    }) + "\n",
  });
  assert.equal(result.status, 0);
  assert.equal(result.stderr, "");
  const response = JSON.parse(result.stdout.trim());
  assert.equal(response.id, 1);
  assert.equal(response.result.serverInfo.name, "claude-octopus");
} finally {
  await rm(directory, { recursive: true, force: true });
}
JS
then
    test_pass
else
    test_fail "symlinked MCP entrypoint did not start the server"
fi

test_case "MCP skill discovery reads recursive canonical SKILL.md files"
expected_count="$(find "$PROJECT_ROOT/skills" -type f -name SKILL.md | wc -l | tr -d ' ')"
if node --input-type=module - "$PROJECT_ROOT/mcp-server/dist/index.js" "$expected_count" <<'JS'
import assert from "node:assert/strict";
import { pathToFileURL } from "node:url";

const modulePath = process.argv[2];
const expectedCount = Number(process.argv[3]);
const { loadSkillMetadata } = await import(pathToFileURL(modulePath));
const skills = await loadSkillMetadata();
assert.equal(skills.length, expectedCount);
assert.equal(new Set(skills.map((skill) => skill.file)).size, expectedCount);
assert.ok(skills.some((skill) => skill.name === "skill-code-review"));
assert.ok(skills.some((skill) => skill.file === "skills/octopus-starter-pack/provider-health/SKILL.md"));
assert.ok(skills.every((skill) => skill.file.startsWith("skills/") && skill.file.endsWith("/SKILL.md")));
JS
then
    test_pass
else
    test_fail "MCP skill discovery did not return the canonical recursive inventory"
fi

test_summary
