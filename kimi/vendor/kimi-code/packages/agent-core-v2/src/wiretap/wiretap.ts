/**
 * wiretap — TaskSolver's wirecap instrumentation bridge (vendored patch).
 *
 * Loads the `wirecap_node` N-API addon (which embeds CPython and streams
 * events to the parent harness over the wirecap mp channel) and exposes
 * no-op-safe emit helpers for the three patched call sites:
 *
 *   - `wiretapEmitRequest`  — llmRequesterService: the full outgoing request
 *     (system prompt + tools + messages) as JSON; starts a new turn id.
 *   - `wiretapEmitEvent`    — llmRequesterService: every streamed
 *     `ModelRequestEvent` (`part` / `usage` / `finish` / `timing`) as JSON.
 *   - `wiretapEmitWireRecord` — WireService.execute: every persisted, live
 *     (non-replay) wire journal record, wrapped with its agent scope.
 *
 * Self-initializing on first import, gated on both `WIRE_ENABLE` and
 * `WIRE_NODE_ADDON` (the absolute addon path, set by pykimi's
 * `instrumented_env`) so a plain `kimi` run never loads libpython.
 * `start()` blocks through the embedded interpreter's init + WIRE_MODULE
 * import — accepted, it happens once at process start (codex does the
 * equivalent before its tokio runtime). Emits never throw into the agent
 * loop and the addon queue is fully async on the JS thread.
 */
import { createRequire } from 'node:module';

interface WirecapAddon {
  start(): number;
  ready(): number;
  emitRequest(data: Uint8Array): number;
  emitEvent(data: Uint8Array): void;
  emitWire(data: Uint8Array): void;
  shutdown(): void;
}

let addon: WirecapAddon | undefined;

(() => {
  const enable = process.env['WIRE_ENABLE'];
  const addonPath = process.env['WIRE_NODE_ADDON'];
  if (enable === undefined || enable === '' || addonPath === undefined || addonPath === '') {
    return;
  }
  try {
    const nodeRequire = createRequire(import.meta.url);
    const loaded = nodeRequire(addonPath) as WirecapAddon;
    loaded.start();
    addon = loaded;
    process.on('exit', () => {
      try {
        loaded.shutdown();
      } catch {
        // best-effort: never turn teardown into a crash
      }
    });
  } catch (error) {
    // Loud once: an instrumented launch that cannot instrument must not look
    // healthy — the harness relies on the capture existing.
    console.error('[wiretap] failed to load wirecap addon:', error);
  }
})();

const encoder = new TextEncoder();

function encode(payload: unknown): Uint8Array | undefined {
  try {
    return encoder.encode(JSON.stringify(payload));
  } catch {
    return undefined; // non-serializable payloads are dropped, never thrown
  }
}

export function wiretapEmitRequest(payload: unknown): void {
  if (addon === undefined) return;
  const data = encode(payload);
  if (data === undefined) return;
  try {
    addon.emitRequest(data);
  } catch {
    // never throw into the agent loop
  }
}

export function wiretapEmitEvent(payload: unknown): void {
  if (addon === undefined) return;
  const data = encode(payload);
  if (data === undefined) return;
  try {
    addon.emitEvent(data);
  } catch {
    // never throw into the agent loop
  }
}

export function wiretapEmitWireRecord(record: unknown, scope?: string): void {
  if (addon === undefined) return;
  const data = encode(scope === undefined ? { record } : { scope, record });
  if (data === undefined) return;
  try {
    addon.emitWire(data);
  } catch {
    // never throw into the agent loop
  }
}
