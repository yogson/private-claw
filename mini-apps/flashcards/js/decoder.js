/**
 * decoder.js
 * Decodes the `words` URL parameter:
 *   base64url → binary → gzip decompress → JSON parse
 */

(function () {
  'use strict';

  /**
   * Decodes an encoded words payload.
   *
   * @param {string} encoded - base64url-encoded, gzip-compressed JSON string
   * @returns {Array} Array of CompactWordPayload objects
   * @throws {Error} if decoding or parsing fails
   */
  function decodeWords(encoded) {
    // 1. Restore standard base64 characters and padding
    let b64 = encoded
      .replace(/-/g, '+')
      .replace(/_/g, '/');

    // Add required '=' padding so length is a multiple of 4
    while (b64.length % 4 !== 0) {
      b64 += '=';
    }

    // 2. Decode base64 → binary string → Uint8Array
    const binaryString = atob(b64);
    const bytes = new Uint8Array(binaryString.length);
    for (let i = 0; i < binaryString.length; i++) {
      bytes[i] = binaryString.charCodeAt(i);
    }

    // 3. Decompress gzip using pako
    const decompressed = pako.ungzip(bytes, { to: 'string' });

    // 4. Parse JSON and return
    return JSON.parse(decompressed);
  }

  window.decodeWords = decodeWords;
})();
