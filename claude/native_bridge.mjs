// A local controller for the existing native session. The terminal owns permissions.
import net from 'node:net';
import fs from 'node:fs';
import {createRequire} from 'node:module';
import os from 'node:os';
import path from 'node:path';
import crypto from 'node:crypto';
import {isMainThread} from 'node:worker_threads';

const MAX_BYTES = 16 * 1024 * 1024 + 1; // Match the transport's frame limit plus newline.
const EXPLICIT_SOCKET_PATH = process.env.TASKSOLVER_CLAUDE_SOCKET;
delete process.env.TASKSOLVER_CLAUDE_SOCKET;
const wireEncoder = new TextEncoder();
let wireAddon = null;
if (isMainThread && process.env.WIRE_ENABLE && process.env.WIRE_ENABLE !== '0' && process.env.WIRE_NODE_ADDON) {
  try {
    const loaded = createRequire(import.meta.url)(process.env.WIRE_NODE_ADDON);
    if (loaded.start() === 0) {
      wireAddon = loaded;
      process.once('exit', () => {
        try { loaded.shutdown(); } catch {}
      });
    }
  } catch (error) {
    process.stderr.write(
      `[tasksolver-wirecap] addon load failed: ${error instanceof Error ? error.message : String(error)}\n`
    );
  } finally {
    delete process.env.WIRE_ENABLE;
    delete process.env.WIRE_NODE_ADDON;
    delete process.env.WIRE_CAPTURE;
    delete process.env.WIRE_MODULE;
    delete process.env.WIRE_MAXCOPY;
  }
}
let automaticSocket = null;
const state = {
  host: null, monitor: null, active: null,
  closing: Promise.resolve(), announced: false,
  clients: new Set(), initialized: new WeakSet(), pending: new Map(),
};

function sameFile(left, right) {
  return left.dev === right.dev && left.ino === right.ino;
}

function removeAutomaticSocket() {
  if (!automaticSocket) return;
  const {directory, socketPath, directoryStat, socketStat} = automaticSocket;
  try {
    if (!sameFile(fs.lstatSync(directory), directoryStat)) return;
    try {
      if (socketStat && sameFile(fs.lstatSync(socketPath), socketStat)) fs.unlinkSync(socketPath);
    } catch (error) { if (error.code !== 'ENOENT') return; }
    fs.rmdirSync(directory); // Refuse unexpected contents instead of deleting them.
  } catch {}
}

function getSocketPath() {
  if (EXPLICIT_SOCKET_PATH) return EXPLICIT_SOCKET_PATH;
  if (!automaticSocket) {
    const directory = fs.mkdtempSync(path.join(path.resolve(os.tmpdir()), `tasksolver-claude-${process.pid}-`));
    automaticSocket = {
      directory, socketPath: path.join(directory, 'sdk.sock'),
      directoryStat: fs.lstatSync(directory), socketStat: null,
    };
    process.once('exit', removeAutomaticSocket);
  }
  return automaticSocket.socketPath;
}

function send(client, frame) {
  if (client.destroyed || client.writableEnded) return;
  const line = JSON.stringify(frame) + '\n';
  if (client.writableLength + Buffer.byteLength(line) > MAX_BYTES) {
    client.destroy(Error('Local bridge output buffer limit exceeded'));
    return;
  }
  client.write(line);
}

function fail(client, error) {
  send(client, {type: 'tasksolver_error', error});
  client.end();
}

function respond(client, id, response, error) {
  send(client, {type: 'control_response', response: error
    ? {subtype: 'error', request_id: id, error}
    : {subtype: 'success', request_id: id, response}});
}

function object(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

async function receive(client, frame) {
  const host = state.host;
  if (!host) throw Error('Native UI session is not ready');
  if (!object(frame)) throw Error('Expected an SDK frame object');
  if (frame.type === 'control_request') {
    if (typeof frame.request_id !== 'string' || !object(frame.request))
      throw Error('Malformed SDK control request');
    const request = frame.request;
    if (request.subtype === 'initialize') {
      const unsupported = Object.keys(request).filter(key => key !== 'subtype' &&
        !(key === 'hooks' && (request[key] == null || object(request[key]) && Object.keys(request[key]).length === 0)));
      if (unsupported.length) return respond(client, frame.request_id, undefined,
        `Local bridge does not support initialize fields: ${unsupported.join(', ')}`);
      const init = await host.engine.initializationResult();
      if (state.host !== host) throw Error('Native session changed during initialization');
      state.initialized.add(client);
      respond(client, frame.request_id, {...init, commands: [], pid: process.pid,
        session_id: host.sessionId, tasksolver_local_bridge: {
          version: 1, controls: ['initialize', 'interrupt'], input: 'text',
          permission_owner: 'native_terminal',
        }});
      return;
    }
    if (request.subtype === 'interrupt') {
      if (!state.active || state.pending.get(state.active) !== client)
        return respond(client, frame.request_id, undefined, 'This connection does not own the active turn');
      host.interrupt();
      respond(client, frame.request_id, {});
      return;
    }
    return respond(client, frame.request_id, undefined,
      `Unsupported local bridge control: ${request.subtype ?? 'missing subtype'}`);
  }
  if (frame.type !== 'user') throw Error(`Unsupported local bridge frame: ${frame.type}`);
  if (!state.initialized.has(client)) throw Error('Initialize this connection before querying');
  if (frame.session_id && frame.session_id !== 'default' && frame.session_id !== host.sessionId)
    throw Error('Query session_id does not match this native terminal');
  if (Object.keys(frame).some(key => !['type', 'message', 'parent_tool_use_id', 'session_id', 'uuid'].includes(key)))
    throw Error('Unsupported optional user-message fields');
  if (!object(frame.message) || frame.message.role !== 'user' || typeof frame.message.content !== 'string')
    throw Error('Local bridge accepts user text only');
  if (Object.keys(frame.message).some(key => !['role', 'content'].includes(key)))
    throw Error('Unsupported optional user content fields');
  if (!frame.message.content.trim()) throw Error('Local bridge requires nonempty text');
  if (frame.message.content.trimStart().startsWith('/')) throw Error('Local bridge does not support slash commands');
  if (frame.parent_tool_use_id != null) throw Error('Subagent input is unsupported');
  if (frame.uuid !== undefined && (typeof frame.uuid !== 'string' || !frame.uuid))
    throw Error('Invalid user-message UUID');
  if (state.pending.size) throw Error('Local bridge permits one pending SDK query');
  if (host.isBusy()) throw Error('Native terminal already has an active turn');
  const uuid = frame.uuid ?? crypto.randomUUID();
  state.pending.set(uuid, client);
  try { await host.enqueue({...frame, uuid, session_id: host.sessionId}); }
  catch (error) { state.pending.delete(uuid); throw error; }
}

function connect(client) {
  state.clients.add(client);
  let buffer = '', queued = 0, queuedBytes = 0, failed = false;
  let queue = Promise.resolve();
  function reject(error) {
    if (failed) return;
    failed = true;
    buffer = '';
    client.pause();
    fail(client, error);
  }
  client.setEncoding('utf8');
  client.on('data', chunk => {
    if (failed) return;
    buffer += chunk;
    if (queuedBytes + Buffer.byteLength(buffer) > MAX_BYTES) {
      reject('Local bridge input buffer limit exceeded');
      return;
    }
    let end;
    while ((end = buffer.indexOf('\n')) >= 0) {
      if (++queued > 128) {
        reject('Too many queued local bridge frames');
        return;
      }
      const line = buffer.slice(0, end);
      const size = Buffer.byteLength(line) + 1;
      queuedBytes += size;
      buffer = buffer.slice(end + 1);
      queue = queue.then(async () => {
        try {
          if (!failed && !client.destroyed && !client.writableEnded) await receive(client, JSON.parse(line));
        } catch (error) {
          reject(error instanceof Error ? error.message : String(error));
        } finally { queued--; queuedBytes -= size; }
      });
    }
  });
  client.on('error', () => {});
  client.on('close', () => {
    state.clients.delete(client);
    // A disconnected caller's turn still belongs to the native session.
  });
}

export function attach(host) {
  if (state.host) throw Error('Local bridge already owns a native session');
  state.host = host;
  let server = null, stopped = false;
  function stop() {
    if (stopped) return;
    stopped = true;
    if (state.host !== host) return;
    state.host = null;
    clearInterval(state.monitor);
    state.monitor = null;
    for (const client of state.clients) client.destroy();
    state.pending.clear();
    state.active = null;
    if (server) state.closing = new Promise(resolve => server.close(() => resolve()));
  }
  function unavailable(error) {
    if (stopped) return;
    stop();
    const message = `Local SDK bridge disabled: ${error instanceof Error ? error.message : String(error)}`;
    process.stderr.write(`[tasksolver-local-bridge] ${message}\n`);
    try { host.onError?.(message); } catch {} // The renderer may already be unmounting.
  }
  // React can unmount and immediately remount the host while net.Server is closing.
  state.closing.then(() => {
    if (stopped || state.host !== host) return;
    try {
      const socketPath = getSocketPath();
      const directory = fs.lstatSync(path.dirname(socketPath));
      if (!path.isAbsolute(socketPath) || !directory.isDirectory() || directory.isSymbolicLink() ||
          directory.uid !== process.getuid() || (directory.mode & 0o077))
        throw Error('socket requires an absolute path in a private directory owned by this user');
      try {
        fs.lstatSync(socketPath);
        throw Error('socket path already exists');
      } catch (error) {
        if (error.code !== 'ENOENT') throw error;
      }
      server = net.createServer(connect);
      server.on('error', unavailable);
      server.listen(socketPath, () => {
        if (stopped) return;
        try {
          fs.chmodSync(socketPath, 0o600);
          if (automaticSocket?.socketPath === socketPath) automaticSocket.socketStat = fs.lstatSync(socketPath);
        } catch (error) { unavailable(error); return; }
        if (!state.announced) {
          state.announced = true;
          process.stderr.write(`[tasksolver-claude-control] ${JSON.stringify({pid: process.pid, socket_path: socketPath})}\n`);
          try { host.onReady?.(socketPath); } catch {} // The renderer may be unmounting.
        }
      });
      server.unref();
      state.monitor = setInterval(() => {
        if (host.isBusy()) return;
        for (const [uuid, client] of state.pending) {
          if (host.hasQueued(uuid)) continue;
          state.pending.delete(uuid);
          if (state.active === uuid) state.active = null;
          fail(client, 'Native prompt ended or was removed without a result');
        }
      }, 100);
      state.monitor.unref();
    } catch (error) { unavailable(error); }
  });
  return stop;
}

export function userUuid(engine, uuid) {
  return state.host?.engine === engine && state.pending.has(uuid) ? uuid : undefined;
}

export function begin(engine, uuid) {
  if (state.host?.engine === engine) state.active = state.pending.has(uuid) ? uuid : null;
}

export function emit(engine, uuid, frame) {
  if (state.host?.engine !== engine) return;
  const client = state.pending.get(uuid);
  if (!client) return;
  state.active = uuid;
  send(client, frame);
  if (frame.type === 'result') {
    state.pending.delete(uuid);
    state.active = null;
  }
}

export function finish(engine, uuid) {
  if (state.host?.engine !== engine) return;
  const client = state.pending.get(uuid);
  if (!client) return;
  state.pending.delete(uuid);
  if (state.active === uuid) state.active = null;
  fail(client, 'Native engine stopped without a result');
}

function serializeRequestBody(body) {
  if (body instanceof Uint8Array) return body;
  if (typeof body === 'string') return wireEncoder.encode(body);
  if (body !== undefined) return wireEncoder.encode(JSON.stringify(body));
  return null;
}

export function tapResponse(responseInfo, parsed) {
  if (!wireAddon || !object(responseInfo) || !object(parsed)) return parsed;
  try {
    const options = responseInfo.options;
    if (!object(options) || typeof options.path !== 'string') return parsed;
    if (options.path.split('?')[0] !== '/v1/messages') return parsed;
    const requestBytes = serializeRequestBody(options.body);
    if (!requestBytes) return parsed;
    if (options.stream && typeof parsed.iterator === 'function') {
      const streamId = wireAddon.emitClaudeRequest(requestBytes);
      const originalIterator = parsed.iterator;
      let tapped = false;
      parsed.iterator = function(...args) {
        const inner = originalIterator.apply(this, args);
        if (tapped) return inner;
        tapped = true;
        return (async function* () {
          try {
            for await (const event of inner) {
              try {
                wireAddon.emitClaudeEvent(streamId, wireEncoder.encode(JSON.stringify(event)));
              } catch {}
              yield event;
            }
          } finally {
            try {
              wireAddon.emitClaudeEnd(streamId);
            } catch {}
          }
        })();
      };
    } else if (!options.stream && parsed.type === 'message') {
      const streamId = wireAddon.emitClaudeRequest(requestBytes);
      wireAddon.emitClaudeEvent(streamId, wireEncoder.encode(JSON.stringify(parsed)));
      wireAddon.emitClaudeEnd(streamId);
    }
  } catch {}
  return parsed;
}
