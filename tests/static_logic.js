const assert = require('assert');
const { normalizeEditor, shouldRollback, createSSEParser, matchChannel, relTime, backoffDelay, groupChannels } = require('../static/app.js');

const got = [];
const feed = createSSEParser((n, d) => got.push([n, d]));
feed('event: hel'); feed('lo\ndata: {"a":1}\n\n: keepalive\n\n');
feed('event: status\r\ndata: x\r\ndata: y\r\n\r\n');
feed('data: bare\n\n');
assert.deepStrictEqual(got, [['hello', '{"a":1}'], ['status', 'x\ny'], ['message', 'bare']]);

assert.deepStrictEqual(matchChannel('General', 'Text', ''), { start: 0, end: 0 });
assert.deepStrictEqual(matchChannel('General', null, 'ENE'), { start: 1, end: 4 });
assert.deepStrictEqual(matchChannel('chat', 'Gaming', 'gam'), { start: 0, end: 0 });
assert.strictEqual(matchChannel('chat', 'Gaming', 'zzz'), null);

assert.strictEqual(relTime(null, 100), 'never');
assert.strictEqual(relTime(95, 100), 'just now');
assert.strictEqual(relTime(70, 100), '30s ago');
assert.strictEqual(relTime(1, 301), '5m ago');
assert.strictEqual(relTime(1, 7301), '2h ago');
assert.strictEqual(relTime(1, 200001), '2d ago');

assert.strictEqual(backoffDelay(0, 0.5), 1000);
assert.strictEqual(backoffDelay(10, 0.5), 10000);
assert.ok(backoffDelay(10, 0.999) <= 12000 && backoffDelay(0, 0) >= 800);

const g = groupChannels([{ category: null, id: 1 }, { category: 'A', id: 2 }, { category: null, id: 3 }]);
assert.deepStrictEqual(g.map((x) => [x.name, x.channels.length]), [[null, 2], ['A', 1]]);
assert.strictEqual(normalizeEditor('  a \t b\u0000c  '), 'a bc');
assert.strictEqual(normalizeEditor('\u7530\u4e2d'), '\u7530\u4e2d');
assert.strictEqual(normalizeEditor('z'.repeat(60)).length, 40);
assert.strictEqual(shouldRollback('5', '5'), true);
assert.strictEqual(shouldRollback('7', '5'), false);
console.log('ok');
