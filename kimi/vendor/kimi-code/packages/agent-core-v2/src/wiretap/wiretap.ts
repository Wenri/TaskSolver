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

function recordOf(value: unknown): Record<string, unknown> | undefined {
  return typeof value === 'object' && value !== null ? (value as Record<string, unknown>) : undefined;
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

export function wiretapEmitAttempt(resolved: unknown, attemptRequest: unknown): void {
  if (addon === undefined) return;
  const res = recordOf(resolved);
  const req = recordOf(attemptRequest);
  const model = recordOf(res?.['model']);
  const params = recordOf(res?.['params']);
  const reqModel = recordOf(req?.['model']);
  const profile = recordOf(req?.['profile']);
  const content = recordOf(req?.['content']);
  const maxTokens =
    typeof params?.['maxCompletionTokens'] === 'number'
      ? params['maxCompletionTokens']
      : typeof reqModel?.['maxOutputTokens'] === 'number'
        ? reqModel['maxOutputTokens']
        : undefined;
  wiretapEmitRequest({
    protocol: model?.['protocol'],
    providerType: model?.['providerType'],
    modelName: model?.['name'],
    modelAlias: res?.['modelAlias'],
    thinkingEffort: res?.['thinkingEffort'],
    maxTokens,
    systemPrompt: profile?.['systemPrompt'] ?? res?.['systemPrompt'],
    tools: profile?.['tools'] ?? res?.['tools'] ?? [],
    messages: content?.['messages'] ?? res?.['messages'] ?? [],
    fields: res?.['logFields'],
  });
}

export function wiretapEmitRunEvent(event: unknown, model?: string): void {
  if (addon === undefined) return;
  const ev = recordOf(event);
  const type = ev?.['type'];
  if (type === 'llm.streaming.part') {
    wiretapEmitEvent({ type: 'part', part: ev?.['part'] });
    return;
  }
  if (type !== 'llm.ended') return;
  const outcome = recordOf(ev?.['outcome']);
  if (outcome?.['type'] !== 'done') return;
  if (outcome['usage'] !== undefined) {
    wiretapEmitEvent({ type: 'usage', usage: outcome['usage'], model });
  }
  const timing = recordOf(outcome['timing']);
  if (timing !== undefined) {
    wiretapEmitEvent({ type: 'timing', ...timing });
  }
  const finish = recordOf(outcome['finish']);
  wiretapEmitEvent({
    type: 'finish',
    message: outcome['message'],
    providerFinishReason: finish?.['finishReason'] ?? undefined,
    rawFinishReason: finish?.['rawFinishReason'] ?? undefined,
    id: outcome['messageId'],
    requestId: outcome['requestId'],
  });
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
