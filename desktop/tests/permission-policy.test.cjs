const { test } = require('node:test');
const assert = require('node:assert/strict');
const { allowOwnedFullscreen } = require('../permission-policy.cjs');

const origin = 'http://127.0.0.1:18987';
const owned = { getURL: () => origin + '/desktop/' };
const main = { requestingUrl: origin + '/desktop/', isMainFrame: true };

test('only the owned primary learning page can enter fullscreen', () => {
  assert.equal(allowOwnedFullscreen('fullscreen', owned, owned, main, origin), true);
});

test('media, capture and all other renderer permissions remain denied', () => {
  for (const permission of ['media', 'display-capture', 'camera', 'microphone',
    'clipboard-read', 'notifications', 'pointerLock', 'unknown']) {
    assert.equal(allowOwnedFullscreen(permission, owned, owned, main, origin), false, permission);
  }
});

test('same-origin subframes cannot obtain fullscreen permission', () => {
  assert.equal(allowOwnedFullscreen('fullscreen', owned, owned, { ...main, isMainFrame: false }, origin), false);
});

test('a different window or origin cannot borrow the primary page grant', () => {
  assert.equal(allowOwnedFullscreen('fullscreen', {}, owned, main, origin), false);
  assert.equal(allowOwnedFullscreen('fullscreen', owned, owned, { ...main, requestingUrl: 'https://example.invalid/' }, origin), false);
  const navigated = { getURL: () => 'https://example.invalid/' };
  assert.equal(allowOwnedFullscreen('fullscreen', navigated, navigated, main, origin), false);
});

test('missing and malformed native request details fail closed', () => {
  for (const details of [undefined, null, {}, { ...main, requestingUrl: 'not a URL' },
    { requestingUrl: main.requestingUrl }]) {
    assert.equal(allowOwnedFullscreen('fullscreen', owned, owned, details, origin), false);
  }
  assert.equal(allowOwnedFullscreen('fullscreen', owned, undefined, main, origin), false);
});
