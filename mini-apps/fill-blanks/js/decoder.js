/**
 * decoder.js
 * Decodes the `data` URL parameter:
 *   base64url → binary → gzip decompress → JSON parse
 */

(function () {
  'use strict';

  /**
   * Decodes an encoded fill-blanks payload.
   *
   * @param {string} encoded - base64url-encoded, gzip-compressed JSON string
   * @returns {Object} FillBlanksPayload { type, sentences, word_bank }
   * @throws {Error} if decoding or parsing fails
   */
  function decodePayload(encoded) {
    let b64 = encoded
      .replace(/-/g, '+')
      .replace(/_/g, '/');

    while (b64.length % 4 !== 0) {
      b64 += '=';
    }

    const binaryString = atob(b64);
    const bytes = new Uint8Array(binaryString.length);
    for (let i = 0; i < binaryString.length; i++) {
      bytes[i] = binaryString.charCodeAt(i);
    }

    const decompressed = pako.ungzip(bytes, { to: 'string' });
    return JSON.parse(decompressed);
  }

  window.decodePayload = decodePayload;
})();
