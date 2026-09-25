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
      }
    });
  } catch (error) {
    console.error('[wiretap] failed to load wirecap addon:', error);
  }
})();

const encoder = new TextEncoder();

function encode(payload: unknown): Uint8Array | undefined {
  try {
    return encoder.encode(JSON.stringify(payload));
  } catch {
    return undefined;
  }
}

export function wiretapEmitRequest(payload: unknown): void {
  if (addon === undefined) return;
  const data = encode(payload);
  if (data === undefined) return;
  try {
    addon.emitRequest(data);
  } catch {
  }
}

export function wiretapEmitEvent(payload: unknown): void {
  if (addon === undefined) return;
  const data = encode(payload);
  if (data === undefined) return;
  try {
    addon.emitEvent(data);
  } catch {
  }
}

export function wiretapEmitWireRecord(record: unknown, scope?: string): void {
  if (addon === undefined) return;
  const data = encode(scope === undefined ? { record } : { scope, record });
  if (data === undefined) return;
  try {
    addon.emitWire(data);
  } catch {
  }
}
