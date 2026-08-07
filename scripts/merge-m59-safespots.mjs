#!/usr/bin/env node

import { readFileSync, writeFileSync } from 'node:fs';
import { resolve } from 'node:path';

const [localArg, upstreamArg, outputArg] = process.argv.slice(2);
if (!localArg || !upstreamArg || !outputArg) {
  console.error('usage: node scripts/merge-m59-safespots.mjs <local-json> <upstream-json> <output-json>');
  process.exit(2);
}

const localFile = resolve(localArg);
const upstreamFile = resolve(upstreamArg);
const output = resolve(outputArg);
const local = JSON.parse(readFileSync(localFile, 'utf8'));
const remote = JSON.parse(readFileSync(upstreamFile, 'utf8'));
const additive = ['held', 'failed', 'held_seconds', 'damage_taken'];

function stable(value) {
  if (Array.isArray(value)) return `[${value.map(stable).join(',')}]`;
  if (value && typeof value === 'object') {
    return `{${Object.keys(value).sort().map((key) => `${JSON.stringify(key)}:${stable(value[key])}`).join(',')}}`;
  }
  return JSON.stringify(value);
}

function mergeRecord(localRecord, remoteRecord) {
  if (!localRecord) return remoteRecord;
  if (!remoteRecord) return localRecord;
  if (stable(localRecord) === stable(remoteRecord)) return localRecord;

  const newest = [localRecord, remoteRecord]
    .filter(Boolean)
    .sort((a, b) => Number(a.at || 0) - Number(b.at || 0))
    .at(-1);
  const merged = { ...newest };
  for (const key of additive) {
    const localValue = Number(localRecord?.[key] || 0);
    const remoteValue = Number(remoteRecord?.[key] || 0);
    // The two books usually share an unknown common ancestor. Taking the
    // maximum preserves every adverse verdict and the strongest accumulated
    // evidence without double-counting observations present in both copies.
    const value = Math.max(localValue, remoteValue);
    if (value || key === 'held' || key === 'failed') merged[key] = value;
    else delete merged[key];
  }
  merged.most_attackers = Math.max(
    Number(localRecord?.most_attackers || 0),
    Number(remoteRecord?.most_attackers || 0),
  );
  merged.at = Math.max(Number(localRecord?.at || 0), Number(remoteRecord?.at || 0));
  return merged;
}

const rooms = {};
const roomIds = new Set([
  ...Object.keys(local.rooms || {}),
  ...Object.keys(remote.rooms || {}),
]);
for (const roomId of [...roomIds].sort((a, b) => Number(a) - Number(b))) {
  const localRoom = local.rooms?.[roomId] || {};
  const remoteRoom = remote.rooms?.[roomId] || {};
  const coordinates = new Set([
    ...Object.keys(localRoom),
    ...Object.keys(remoteRoom),
  ]);
  rooms[roomId] = {};
  for (const coordinate of [...coordinates].sort()) {
    rooms[roomId][coordinate] = mergeRecord(
      localRoom[coordinate],
      remoteRoom[coordinate],
    );
  }
}

writeFileSync(output, JSON.stringify({ rooms }));
console.log(JSON.stringify({
  output,
  rooms: Object.keys(rooms).length,
  spots: Object.values(rooms).reduce((count, room) => count + Object.keys(room).length, 0),
}));
