import { test } from 'node:test';
import assert from 'node:assert/strict';
import { voiceCues } from '../src/voice-cues.js';

test('voice cues use the timeline across packets, and silence has no estimated pitch', () => {
  const cues = voiceCues(2);
  const pcm = new Float32Array(32000);
  for (let i = 0; i < 16000; i++) pcm[i] = 0.4 * Math.sin(2 * Math.PI * 200 * i / 16000);
  for (let i = 0; i < pcm.length; i += 317) cues.add(pcm.subarray(i, i + 317), i);
  const result = Array.from(cues.bytes());
  assert.ok(result[0] >= 98 && result[0] <= 102);
  assert.equal(result[0], result[1]);
  assert.deepEqual(result.slice(2), [0, 0]);
});
