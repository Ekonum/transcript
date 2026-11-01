import { test } from 'node:test';
import assert from 'node:assert/strict';
import { correspondance } from '../src/joindre.js';

test('le même enregistrement, à l’encodage près, est accepté', () => {
  assert.equal(correspondance(6480, 6480).verdict, 'ok');
  assert.equal(correspondance(6480, 6484.2).verdict, 'ok');
  assert.equal(correspondance(60, 63).verdict, 'ok');
});
test('un autre fichier se signale, dans un sens comme dans l’autre', () => {
  assert.deepEqual(correspondance(6480, 3600), { verdict: 'plus-court', ecart: 2880 });
  assert.deepEqual(correspondance(1800, 2400), { verdict: 'plus-long', ecart: 600 });
});
test('sans durée connue, on ne conclut rien', () => {
  for (const [a, b] of [[0, 10], [null, 10], [10, undefined], [10, NaN]]) {
    assert.equal(correspondance(a, b).verdict, 'inconnu');
  }
});
