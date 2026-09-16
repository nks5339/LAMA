// Jest setup — loaded automatically by react-scripts before every suite.
import "@testing-library/jest-dom";

// jsdom implements neither of these, and both are load-bearing here:
//
//   matchMedia   — useBreakpoint/useMediaQuery is built on it, and it is
//                  called from the App shell, so almost every render needs it.
//   TextDecoder  — streamMessage decodes the SSE byte stream with it.
//
// The matchMedia double is controllable: tests set __setMatchMedia(matches)
// to simulate a breakpoint, and listeners fire so useSyncExternalStore
// re-renders exactly as it would in a browser.
const _mqlListeners = new Set();
let _matches = false;

global.__setMatchMedia = (matches) => {
  _matches = matches;
  _mqlListeners.forEach((cb) => cb({ matches }));
};

Object.defineProperty(window, "matchMedia", {
  writable: true,
  value: (query) => ({
    media: query,
    get matches() {
      return _matches;
    },
    onchange: null,
    addEventListener: (_evt, cb) => _mqlListeners.add(cb),
    removeEventListener: (_evt, cb) => _mqlListeners.delete(cb),
    addListener: (cb) => _mqlListeners.add(cb),
    removeListener: (cb) => _mqlListeners.delete(cb),
    dispatchEvent: () => false,
  }),
});

if (typeof global.TextDecoder === "undefined") {
  const { TextDecoder, TextEncoder } = require("util");
  global.TextDecoder = TextDecoder;
  global.TextEncoder = TextEncoder;
}

// Reset the media state between suites so a breakpoint set in one test
// cannot leak into the next.
afterEach(() => {
  _matches = false;
  _mqlListeners.clear();
});
