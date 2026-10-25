// The only renderer permission needed by the learning workspace is a user-
// activated fullscreen request from its owned primary page. Chromium enforces
// user activation; this policy additionally confines identity, frame and origin.
function allowOwnedFullscreen(permission, contents, ownedContents, details, origin) {
  if (permission !== 'fullscreen' || !ownedContents || contents !== ownedContents
    || details?.isMainFrame !== true) return false;
  try {
    return new URL(details.requestingUrl).origin === origin
      && new URL(ownedContents.getURL()).origin === origin;
  } catch {
    return false;
  }
}

module.exports = { allowOwnedFullscreen };
