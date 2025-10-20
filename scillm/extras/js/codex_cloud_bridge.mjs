#!/usr/bin/env node
// Experimental bridge: uses codex-ts-sdk CloudTasksClientBuilder to create a best-of-N task,
// polls until completion, then fetches the task diff and prints a single JSON object to stdout.
//
// Input: JSON on stdin
// {
//   "environmentId": "prod" | string,
//   "prompt": "task description",
//   "bestOfN": 6,
//   "timeoutMs": 90000,
//   "baseUrl": "https://..." (optional)
// }
//
// Auth: expects a bearer token (OPENAI_API_KEY or CODEX_CLOUD_API_KEY)
//
// Output: JSON on stdout
// { taskId, status, attempts, diff: {patch?:string, summary?:string}, timings: {createdMs, finishedMs} }

import { readFile } from 'node:fs/promises';
import { stdin } from 'node:process';

// Lazy import; users must run `npm install` in scillm/extras/js
let CloudTasksClientBuilder;
try {
  ({ CloudTasksClientBuilder } = await import('codex-ts-sdk/cloud'));
} catch (e) {
  console.error(JSON.stringify({
    error: 'codex-ts-sdk not installed',
    hint: 'cd scillm/extras/js && npm install',
  }));
  process.exit(2);
}

const readStdin = async () => {
  const chunks = [];
  for await (const c of stdin) chunks.push(Buffer.from(c));
  return Buffer.concat(chunks).toString('utf8');
};

const now = () => Date.now();

const main = async () => {
  const raw = await readStdin();
  let args;
  try {
    args = JSON.parse(raw || '{}');
  } catch (e) {
    console.error(JSON.stringify({ error: 'invalid_json', raw }));
    process.exit(1);
  }

  const token = process.env.CODEX_CLOUD_API_KEY || process.env.OPENAI_API_KEY;
  if (!token) {
    console.error(JSON.stringify({ error: 'missing_token', hint: 'set CODEX_CLOUD_API_KEY or OPENAI_API_KEY' }));
    process.exit(3);
  }

  const environmentId = args.environmentId || process.env.CODEX_CLOUD_ENV || 'prod';
  const bestOfN = Number(args.bestOfN || 6);
  const prompt = String(args.prompt || '').trim();
  const timeoutMs = Number(args.timeoutMs || 90000);
  const baseUrl = args.baseUrl || process.env.CODEX_CLOUD_BASE_URL || undefined;

  if (!prompt) {
    console.error(JSON.stringify({ error: 'missing_prompt' }));
    process.exit(4);
  }

  const builder = new CloudTasksClientBuilder({ token, baseUrl });
  const client = builder.build();
  const started = now();

  try {
    // Create a best-of-N code generation task
    const task = await client.createTask({ environmentId, prompt, bestOfN });
    const taskId = task?.id || task?.taskId || task?.task_id;
    if (!taskId) {
      console.error(JSON.stringify({ error: 'no_task_id', task }));
      process.exit(5);
    }
    // Poll until done or timeout
    const deadline = started + timeoutMs;
    let status;
    while (now() < deadline) {
      const t = await client.getTask({ id: taskId });
      status = t?.status || t?.state || 'unknown';
      if (['succeeded', 'failed', 'completed'].includes(status)) break;
      await new Promise(r => setTimeout(r, 1500));
    }
    // Fetch diff/summary if available
    let diff = null;
    try {
      diff = await client.getTaskDiff({ id: taskId });
    } catch {}
    const finished = now();
    const attempts = task?.attempts || [];
    const out = {
      taskId,
      status,
      attemptsCount: Array.isArray(attempts) ? attempts.length : undefined,
      diff,
      timings: { createdMs: started, finishedMs: finished },
    };
    console.log(JSON.stringify(out));
  } catch (e) {
    console.error(JSON.stringify({ error: 'sdk_error', message: String(e?.message || e) }));
    process.exit(6);
  }
};

main().catch(e => {
  console.error(JSON.stringify({ error: 'unhandled', message: String(e?.message || e) }));
  process.exit(7);
});

