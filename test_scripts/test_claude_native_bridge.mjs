// Offline socket/lifecycle checks; the native UI integration lives in test_claude_native.py.
import assert from 'node:assert/strict';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import {once} from 'node:events';
import {execFileSync, spawnSync} from 'node:child_process';
import {test} from 'node:test';

const helper = new URL('../claude/native_bridge.mjs', import.meta.url);
let instance = 0;

async function waitFor(condition) {
  const deadline = Date.now() + 3000;
  while (!condition()) {
    assert.ok(Date.now() < deadline, 'Timed out waiting for socket activity');
    await new Promise(resolve => setTimeout(resolve, 10));
  }
}

async function fixture(t, {existingSymlink = false} = {}) {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'tasksolver-bridge-test-'));
  const socketPath = path.join(directory, 'sdk.sock');
  if (existingSymlink) fs.symlinkSync(path.join(directory, 'missing'), socketPath);
  const previous = process.env.TASKSOLVER_CLAUDE_SOCKET;
  process.env.TASKSOLVER_CLAUDE_SOCKET = socketPath;
  let bridge;
  try {
    bridge = await import(`${helper}?fixture=${++instance}`);
    assert.equal(process.env.TASKSOLVER_CLAUDE_SOCKET, undefined);
  } finally {
    if (previous === undefined) delete process.env.TASKSOLVER_CLAUDE_SOCKET;
    else process.env.TASKSOLVER_CLAUDE_SOCKET = previous;
  }
  const clients = new Set(), errors = [], queued = [];
  const engine = {initializationResult: async () => ({models: [], commands: ['native-command']})};
  const host = {
    engine, sessionId: 'native-session', isBusy: () => false,
    hasQueued: uuid => queued.some(frame => frame.uuid === uuid),
    enqueue: frame => queued.push(frame), interrupt: () => {},
    onError: error => errors.push(error),
  };
  let stop = bridge.attach(host);
  t.after(async () => {
    stop();
    for (const client of clients) client.destroy();
    if (!existingSymlink) await waitFor(() => !fs.existsSync(socketPath));
    fs.rmSync(directory, {recursive: true, force: true});
  });
  if (existingSymlink) await waitFor(() => errors.length > 0);
  else await waitFor(() => fs.existsSync(socketPath));

  async function connect() {
    const client = net.createConnection(socketPath);
    clients.add(client);
    const frames = [];
    let buffer = '';
    client.setEncoding('utf8');
    client.on('data', chunk => {
      buffer += chunk;
      let index;
      while ((index = buffer.indexOf('\n')) >= 0) {
        frames.push(JSON.parse(buffer.slice(0, index)));
        buffer = buffer.slice(index + 1);
      }
    });
    await once(client, 'connect');
    return {
      client,
      send: frame => client.write(JSON.stringify(frame) + '\n'),
      next: async () => { await waitFor(() => frames.length > 0); return frames.shift(); },
    };
  }
  return {
    bridge, host, socketPath, errors, queued, connect,
    remount() { stop(); stop = bridge.attach({...host}); },
  };
}

async function initialize(connection) {
  connection.send({type: 'control_request', request_id: 'init', request: {subtype: 'initialize'}});
  const frame = await connection.next();
  assert.equal(frame.type, 'control_response');
  assert.equal(frame.response.subtype, 'success');
  return frame.response.response;
}

test('a connected host can unmount and immediately rebind the same private socket', async t => {
  const f = await fixture(t);
  const first = await f.connect();
  assert.equal((await initialize(first)).session_id, 'native-session');
  const closed = once(first.client, 'close');
  f.remount();
  await closed;
  await waitFor(() => fs.existsSync(f.socketPath));
  const second = await f.connect();
  const info = await initialize(second);
  assert.deepEqual(info.commands, []);
  assert.equal(info.pid, process.pid);
  assert.equal(info.tasksolver_local_bridge.permission_owner, 'native_terminal');
  assert.deepEqual(f.errors, []);
});

test('a fatal user frame prevents a later frame in the same batch from submitting', async t => {
  const f = await fixture(t);
  const connection = await f.connect();
  await initialize(connection);
  const invalid = {type: 'user', message: {role: 'user', content: 'invalid', future_option: true}};
  const valid = {type: 'user', message: {role: 'user', content: 'must not submit'}};
  const closed = once(connection.client, 'close');
  connection.client.write(JSON.stringify(invalid) + '\n' + JSON.stringify(valid) + '\n');
  const error = await connection.next();
  assert.equal(error.type, 'tasksolver_error');
  assert.match(error.error, /optional user content/);
  await closed;
  assert.deepEqual(f.queued, []);
});

test('an existing dangling symlink is refused and left intact', async t => {
  const f = await fixture(t, {existingSymlink: true});
  assert.match(f.errors[0], /socket path already exists/);
  assert.ok(fs.lstatSync(f.socketPath).isSymbolicLink());
});

test('the full transport frame limit is accepted and the next byte is rejected', async t => {
  const f = await fixture(t);
  const connection = await f.connect();
  await initialize(connection);
  const frame = {type: 'user', uuid: 'exact-limit', message: {role: 'user', content: ''}};
  const padding = 16 * 1024 * 1024 - Buffer.byteLength(JSON.stringify(frame));
  frame.message.content = 'x'.repeat(padding);
  connection.send(frame);
  await waitFor(() => f.queued.length > 0);
  assert.equal(f.queued[0].uuid, frame.uuid);
  f.bridge.begin(f.host.engine, frame.uuid);
  f.bridge.emit(f.host.engine, frame.uuid, {type: 'result', result: 'fixture'});
  assert.equal((await connection.next()).type, 'result');
  frame.message.content += 'x';
  connection.send(frame);
  const error = await connection.next();
  assert.equal(error.type, 'tasksolver_error');
  assert.match(error.error, /input buffer limit/);
  assert.equal(f.queued.length, 1);
});

test('loading the bridge without a native UI creates no listener or directory', t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'tasksolver-headless-test-'));
  t.after(() => fs.rmSync(directory, {recursive: true, force: true}));
  const env = {...process.env, TMPDIR: directory};
  delete env.TASKSOLVER_CLAUDE_SOCKET;
  const output = execFileSync(process.execPath,
    ['--input-type=module', '-e', `await import(${JSON.stringify(helper.href)})`],
    {env, encoding: 'utf8', timeout: 5000});
  assert.equal(output, '');
  assert.deepEqual(fs.readdirSync(directory), []);
});

test('automatic UI endpoints are private, stable across remounts, unique and cleaned on exit', t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'tasksolver-automatic-test-'));
  t.after(() => fs.rmSync(directory, {recursive: true, force: true}));
  const env = {...process.env, TMPDIR: directory};
  delete env.TASKSOLVER_CLAUDE_SOCKET;
  const source = `
    import assert from 'node:assert/strict';
    import fs from 'node:fs';
    import net from 'node:net';
    import path from 'node:path';
    import {once} from 'node:events';
    const bridge = await import(${JSON.stringify(helper.href)});
    const keepAlive = setInterval(() => {}, 1000);
    async function waitFor(condition) {
      const deadline = Date.now() + 2000;
      while (!condition()) {
        assert.ok(Date.now() < deadline, 'Automatic endpoint did not become ready');
        await new Promise(resolve => setTimeout(resolve, 10));
      }
    }
    const ready = [];
    const host = {
      engine: {initializationResult: async () => ({})}, sessionId: 'automatic',
      isBusy: () => false, hasQueued: () => false,
      onReady: socketPath => ready.push(socketPath), onError: error => { throw Error(error); },
    };
    let stop = bridge.attach(host);
    await waitFor(() => ready.length === 1);
    const socketPath = ready[0];
    assert.equal(fs.statSync(path.dirname(socketPath)).mode & 0o777, 0o700);
    assert.equal(fs.statSync(socketPath).mode & 0o777, 0o600);
    assert.equal(fs.statSync(path.dirname(socketPath)).uid, process.getuid());
    assert.equal(process.env.TASKSOLVER_CLAUDE_SOCKET, undefined);
    const first = net.createConnection(socketPath);
    await once(first, 'connect');
    const closed = once(first, 'close');
    stop();
    stop = bridge.attach({...host});
    await closed;
    await waitFor(() => fs.existsSync(socketPath));
    const second = net.createConnection(socketPath);
    await once(second, 'connect');
    second.write(JSON.stringify({type:'control_request',request_id:'init',request:{subtype:'initialize'}})+'\\n');
    const [data] = await once(second, 'data');
    assert.equal(JSON.parse(data.toString()).response.response.session_id, 'automatic');
    assert.equal(ready.length, 1);
    if (process.env.KEEP_EXTRA_FILE) fs.writeFileSync(path.join(path.dirname(socketPath), 'keep'), 'untouched');
    console.log(JSON.stringify({socket_path: socketPath, pid: process.pid}));
    clearInterval(keepAlive);
    process.exit(0); // Exercise owned-path exit cleanup while the server is still listening.
  `;
  const run = (extraEnv = {}) => {
    const result = spawnSync(process.execPath, ['--input-type=module', '-e', source],
      {env: {...env, ...extraEnv}, encoding: 'utf8', timeout: 5000});
    assert.equal(result.status, 0, result.stderr);
    const endpoint = JSON.parse(result.stdout);
    const prefix = '[tasksolver-claude-control] ';
    const announcements = result.stderr.split('\n').filter(line => line.startsWith(prefix));
    assert.equal(announcements.length, 1);
    assert.deepEqual(JSON.parse(announcements[0].slice(prefix.length)), endpoint);
    return endpoint;
  };
  const first = run();
  const second = run();
  assert.notEqual(first.socket_path, second.socket_path);
  assert.notEqual(first.pid, second.pid);
  assert.ok(!fs.existsSync(path.dirname(first.socket_path)));
  assert.ok(!fs.existsSync(path.dirname(second.socket_path)));
  assert.deepEqual(fs.readdirSync(directory), []);
  const preserved = run({KEEP_EXTRA_FILE: '1'});
  assert.ok(!fs.existsSync(preserved.socket_path));
  assert.equal(fs.readFileSync(path.join(path.dirname(preserved.socket_path), 'keep'), 'utf8'), 'untouched');
});

test('tapResponse forwards streaming and non-streaming /v1/messages calls and scrubs WIRE_* env', t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'tasksolver-wiretap-test-'));
  t.after(() => fs.rmSync(directory, {recursive: true, force: true}));
  const addonPath = path.join(directory, 'mock_wire.cjs');
  const logPath = path.join(directory, 'calls.json');
  fs.writeFileSync(addonPath, `
    const fs = require('node:fs');
    const calls = [];
    let turn = 0;
    module.exports = {
      start() { calls.push(['start']); return 0; },
      emitClaudeRequest(bytes) {
        const id = ++turn;
        calls.push(['req', id, Buffer.from(bytes).toString('utf8')]);
        return id;
      },
      emitClaudeEvent(id, bytes) {
        calls.push(['ev', id, JSON.parse(Buffer.from(bytes).toString('utf8'))]);
      },
      emitClaudeEnd(id) {
        calls.push(['end', id]);
      },
      shutdown() {
        calls.push(['shutdown']);
        fs.writeFileSync(${JSON.stringify(logPath)}, JSON.stringify(calls));
      },
    };
  `);
  const script = `
    import assert from 'node:assert/strict';
    const bridge = await import(${JSON.stringify(helper.href)});
    for (const key of ['WIRE_ENABLE', 'WIRE_NODE_ADDON', 'WIRE_CAPTURE', 'WIRE_MODULE', 'WIRE_MAXCOPY']) {
      assert.equal(process.env[key], undefined, key);
    }
    const stream = {
      async *iterator() {
        yield {type: 'message_start', message: {model: 'claude-sonnet-4-6'}};
        yield {type: 'message_stop'};
      },
    };
    bridge.tapResponse({options: {path: '/v1/messages?beta=true', stream: true, body: {model: 's'}}}, stream);
    const seen = [];
    for await (const ev of stream.iterator()) seen.push(ev.type);
    assert.deepEqual(seen, ['message_start', 'message_stop']);
    bridge.tapResponse(
      {options: {path: '/v1/messages', stream: false, body: Buffer.from('{"model":"m"}')}},
      {type: 'message', id: 'msg_1'}
    );
    bridge.tapResponse(
      {options: {path: '/v1/messages/count_tokens', stream: false, body: {model: 's'}}},
      {type: 'message', id: 'ignored'}
    );
  `;
  const result = spawnSync(process.execPath, ['--input-type=module', '-e', script], {
    env: {
      ...process.env,
      WIRE_ENABLE: '1',
      WIRE_NODE_ADDON: addonPath,
      WIRE_CAPTURE: '/tmp/unused.jsonl',
      WIRE_MODULE: 'pyclaude.claude_process',
      WIRE_MAXCOPY: '8388608',
    },
    encoding: 'utf8',
    timeout: 5000,
  });
  assert.equal(result.status, 0, result.stderr);
  const calls = JSON.parse(fs.readFileSync(logPath, 'utf8'));
  assert.deepEqual(calls, [
    ['start'],
    ['req', 1, '{"model":"s"}'],
    ['ev', 1, {type: 'message_start', message: {model: 'claude-sonnet-4-6'}}],
    ['ev', 1, {type: 'message_stop'}],
    ['end', 1],
    ['req', 2, '{"model":"m"}'],
    ['ev', 2, {type: 'message', id: 'msg_1'}],
    ['end', 2],
    ['shutdown'],
  ]);
});
