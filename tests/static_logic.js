const assert = require('assert');
const { shouldApplySnapshot, normalizeEditor, shouldRollback, createSSEParser, matchChannel, relTime, backoffDelay, groupChannels,
  voiceTitle, voiceWarning, sayBlocker, voiceSwitchText, mergeVoiceSummary,
  segmentText, substituteTags, activeTagIds, uniqueLabel, insertAtCursor, counterText, counterState, pingTargets, pingSummary, messageBlocker,
  cpLen, READY_PRESET } = require('../static/app.js');

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
assert.strictEqual(shouldApplySnapshot({ rev: 5 }, { rev: 4 }, false), false);
assert.strictEqual(shouldApplySnapshot({ rev: 5 }, { rev: 5 }, false), true);
assert.strictEqual(shouldApplySnapshot({ rev: 5 }, { rev: 6 }, false), true);
assert.strictEqual(shouldApplySnapshot({ rev: 5 }, { rev: 4 }, true), true);
assert.strictEqual(shouldApplySnapshot(null, { rev: 0 }, false), true);
assert.strictEqual(shouldApplySnapshot({ rev: 5 }, null, true), false);

assert.strictEqual(voiceTitle('en_US-amy-medium'), 'Amy');
assert.strictEqual(voiceTitle('en_US-hfc_female-medium'), 'Hfc Female');
assert.strictEqual(voiceTitle('en_GB-jenny_dioco-medium'), 'Jenny Dioco');
assert.strictEqual(voiceTitle('en_US-ljspeech-high'), 'Ljspeech');
assert.strictEqual(voiceTitle('en_US-john-doe-x_low'), 'John Doe');
assert.strictEqual(voiceTitle('custom'), 'Custom');
assert.strictEqual(voiceTitle('medium'), 'Medium');
assert.strictEqual(voiceTitle('en_US-low'), 'Low');
assert.strictEqual(voiceTitle('a.b+c'), 'A.b+c');
assert.strictEqual(voiceWarning(null, 'x', ['x']), null);
assert.strictEqual(voiceWarning('x', 'x', ['x']), null);
assert.strictEqual(voiceWarning('gone', 'en_US-amy-medium', ['en_US-amy-medium']), 'Saved voice missing \u2014 using default');
assert.strictEqual(voiceWarning('en_GB-alba-medium', 'en_US-amy-medium', ['en_GB-alba-medium', 'en_US-amy-medium']),
  'Saved voice could not be loaded \u2014 using Amy');
assert.strictEqual(voiceWarning('a', 'en_US-amy-medium', null), 'Saved voice is not active \u2014 using Amy');
assert.strictEqual(voiceWarning('a', null, null), 'Saved voice is not active \u2014 using no voice');
assert.strictEqual(sayBlocker(false, { voice: 'Lounge' }, false), 'Offline');
assert.ok(sayBlocker(true, { voice: null }, false).includes('not in a voice channel'));
assert.ok(sayBlocker(true, undefined, false).includes('not in a voice channel'));
assert.strictEqual(sayBlocker(true, { voice: 'Lounge' }, true), 'Voice is loading');
assert.strictEqual(sayBlocker(true, { voice: 'Lounge' }, false), '');
assert.strictEqual(voiceSwitchText('bob', 'en_US-kristin-medium'), 'bob switched voice to Kristin');
assert.strictEqual(voiceSwitchText('bob', null), 'bob switched voice to the default voice');

// --- Messages tab helpers ---
const bob = { id: '11', label: 'Bob' }, al = { id: '12', label: 'Al' }, alice = { id: '13', label: 'Alice' };
assert.strictEqual(substituteTags('hi @Bob!', [bob]), 'hi <@11>!');
assert.strictEqual(substituteTags('@Bob and @Bob', [bob]), '<@11> and <@11>');
assert.strictEqual(substituteTags('hi @Bob', []), 'hi @Bob');                 // never guessed
assert.strictEqual(substituteTags('hi @Carl', [bob]), 'hi @Carl');
assert.strictEqual(substituteTags('hi @bob', [bob]), 'hi @bob');             // exact (case-sensitive)
assert.strictEqual(substituteTags('  @Bob  ', [bob]), '<@11>');             // trimmed like the server
// longest label first and overlapping names
assert.strictEqual(substituteTags('@Alice @Al', [al, alice]), '<@13> <@12>');
assert.strictEqual(substituteTags('@Alice @Al', [alice, al]), '<@13> <@12>');
assert.strictEqual(substituteTags('@Alicex', [al, alice]), '@Alicex');       // not glued to a longer word
assert.strictEqual(substituteTags('x@Bob', [bob]), 'x@Bob');
assert.strictEqual(substituteTags('@Bob,@Bob.', [bob]), '<@11>,<@11>.');
// labels with regex metacharacters and spaces; output is not re-scanned
assert.strictEqual(substituteTags('hey @J.R. (x) ok', [{ id: '5', label: 'J.R. (x)' }]), 'hey <@5> ok');
assert.strictEqual(substituteTags('@a @b', [{ id: '1', label: 'a' }, { id: '2', label: '1' }]), '<@1> @b');
assert.strictEqual(substituteTags('@田中', [{ id: '9', label: '田中' }]), '<@9>');
assert.strictEqual(substituteTags('line1\n@Bob\nline3', [bob]), 'line1\n<@11>\nline3');
// segments (preview) with glued/start-of-string tests for Safari compatibility
assert.deepStrictEqual(segmentText('a @Bob b', [bob]), [{ text: 'a ', tag: null }, { text: '@Bob', tag: bob }, { text: ' b', tag: null }]);
assert.deepStrictEqual(segmentText('', [bob]), [{ text: '', tag: null }]);
assert.deepStrictEqual(segmentText('plain', []), [{ text: 'plain', tag: null }]);
// start of string case
assert.deepStrictEqual(segmentText('@Bob', [bob]), [{ text: '@Bob', tag: bob }]);
// glued to word character - should NOT match
assert.deepStrictEqual(segmentText('prefix@Bob', [bob]), [{ text: 'prefix@Bob', tag: null }]);
// glued to digit - should NOT match
assert.deepStrictEqual(segmentText('5@Bob', [bob]), [{ text: '5@Bob', tag: null }]);
// glued to underscore - should NOT match
assert.deepStrictEqual(segmentText('_@Bob', [bob]), [{ text: '_@Bob', tag: null }]);
// test that lookbehind patterns are not in app.js source
const fs = require('fs');
const appSource = fs.readFileSync(__dirname + '/../static/app.js', 'utf8');
assert.strictEqual(appSource.includes('(?<='), false, 'app.js must not contain positive lookbehind (?<=');
assert.strictEqual(appSource.includes('(?<!'), false, 'app.js must not contain negative lookbehind (?<!');
// chip active / inactive
assert.deepStrictEqual(Array.from(activeTagIds('hi @Bob', [bob, al])), ['11']);
assert.deepStrictEqual(Array.from(activeTagIds('hi', [bob, al])), []);
assert.deepStrictEqual(Array.from(activeTagIds('@Alice', [al, alice])), ['13']);
// unique labels
assert.strictEqual(uniqueLabel('Bob', 'bob99', '1', []), 'Bob');
assert.strictEqual(uniqueLabel('Bob', 'bob99', '2', [{ id: '1', label: 'Bob' }]), 'Bob (bob99)');
assert.strictEqual(uniqueLabel('Bob', 'bob99', '1', [{ id: '1', label: 'Bob' }]), 'Bob');
assert.strictEqual(uniqueLabel('Bob', 'bob99', '3', [{ id: '1', label: 'Bob' }, { id: '2', label: 'Bob (bob99)' }]), 'Bob (3)');
// insert at cursor
assert.deepStrictEqual(insertAtCursor('', 0, 0, '@Bob'), { value: '@Bob ', pos: 5 });
assert.deepStrictEqual(insertAtCursor('hello', 5, 5, '@Bob'), { value: 'hello @Bob ', pos: 11 });
assert.deepStrictEqual(insertAtCursor('hello world', 5, 5, '@Bob'), { value: 'hello @Bob world', pos: 10 });
assert.deepStrictEqual(insertAtCursor('hi there', 3, 8, '@Bob'), { value: 'hi @Bob ', pos: 8 });
assert.deepStrictEqual(insertAtCursor('abc', 0, 0, '@Bob'), { value: '@Bob abc', pos: 5 });
assert.deepStrictEqual(insertAtCursor('abc', 99, 99, '@B'), { value: 'abc @B ', pos: 7 });
// counter and ping text
assert.strictEqual(counterText(0), '0/2000');
assert.strictEqual(counterText(1999), '1999/2000');
assert.strictEqual(counterState(1899), '');
assert.strictEqual(counterState(1900), 'near');
assert.strictEqual(counterState(2000), 'near');
assert.strictEqual(counterState(2001), 'over');
assert.strictEqual(cpLen('a\u{1F600}b'), 3);
// pingTargets and pingSummary with new format
const chips1 = [{ id: '5', label: 'Alice', username: 'alice123' }, { id: '6', label: 'Bob', username: 'bob99' }];
assert.deepStrictEqual(pingTargets('<@5> hi', chips1), [{ id: '5', label: 'Alice', username: 'alice123', known: true }]);
assert.deepStrictEqual(pingTargets('<@5> <@5>', chips1), [{ id: '5', label: 'Alice', username: 'alice123', known: true }]);
assert.deepStrictEqual(pingTargets('<@!6> x <@5>', chips1), [{ id: '6', label: 'Bob', username: 'bob99', known: true }, { id: '5', label: 'Alice', username: 'alice123', known: true }]);
assert.deepStrictEqual(pingTargets('', []), []);
assert.deepStrictEqual(pingTargets('<@999>', []), [{ id: '999', label: null, username: null, known: false }]);
assert.deepStrictEqual(pingTargets('x@<@5> hi', chips1), [{ id: '5', label: 'Alice', username: 'alice123', known: true }]);
assert.strictEqual(pingSummary([]), 'No pings');
assert.strictEqual(pingSummary([{ id: '5', label: 'Alice', username: 'alice123', known: true }]), 'Will ping: @Alice (@alice123)');
assert.strictEqual(pingSummary([{ id: '5', label: 'Alice', username: 'alice123', known: true }, { id: '999', label: null, username: null, known: false }]), 'Will ping: @Alice (@alice123), unknown user 999');
// send blocker
assert.strictEqual(messageBlocker(false, true, 'x', false), 'Offline');
assert.strictEqual(messageBlocker(true, false, 'x', false), 'No channel configured');
assert.strictEqual(messageBlocker(true, true, '', false), 'Write a message first');
assert.strictEqual(messageBlocker(true, true, 'x'.repeat(2001), false), 'Message is too long');
assert.strictEqual(messageBlocker(true, true, 'x'.repeat(2000), false), '');
assert.strictEqual(messageBlocker(true, true, 'x', true), 'Please wait…');
assert.ok(READY_PRESET.startsWith('The voice bot is ready to use!') && cpLen(READY_PRESET) < 2000);
{
  const prev = { list: [{ name: 'old' }], current: 'b', configured: 'b', loading: true, cuda: true };
  const incoming = { voices: [{ name: 'a' }, { name: 'b' }], current: 'a', configured: null, loading: false, cuda: false };
  // stale GET: an SSE voice event arrived after the GET started, stream live -> SSE-owned fields kept, list taken
  assert.deepStrictEqual(mergeVoiceSummary(prev, incoming, { seqAtRequest: 3, seqNow: 4, live: true }),
    { list: incoming.voices, current: 'b', configured: 'b', loading: true, cuda: true });
  // fresh GET: no newer event -> everything from incoming
  assert.deepStrictEqual(mergeVoiceSummary(prev, incoming, { seqAtRequest: 4, seqNow: 4, live: true }),
    { list: incoming.voices, current: 'a', configured: null, loading: false, cuda: false });
  // not live: SSE ordering is meaningless, take incoming even if the counter moved
  assert.deepStrictEqual(mergeVoiceSummary(prev, incoming, { seqAtRequest: 3, seqNow: 9, live: false }),
    { list: incoming.voices, current: 'a', configured: null, loading: false, cuda: false });
  // missing optional fields normalise to null / false; summary without a list keeps the previous list
  assert.deepStrictEqual(mergeVoiceSummary(prev, { loading: 0 }, { seqAtRequest: 1, seqNow: 1, live: true }),
    { list: prev.list, current: null, configured: null, loading: false, cuda: null });
  // prev is never mutated
  assert.strictEqual(prev.current, 'b');
  assert.strictEqual(mergeVoiceSummary(prev, null, { seqAtRequest: 1, seqNow: 1, live: true }), prev);
}
console.log('ok');
