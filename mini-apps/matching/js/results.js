/**
 * results.js
 * Renders the results screen and sends data back to Telegram.
 *
 * Public API:
 *   showResults(results, totalMs)
 *   - results:  Array of { word_id, rating, time_ms, direction }
 *   - totalMs:  Total elapsed milliseconds for the whole exercise
 */

(function () {
  'use strict';

  var RATING_META = [
    { rating: 0, emoji: '❌', label: 'Снова'  },
    { rating: 1, emoji: '⚠️', label: 'Трудно' },
    { rating: 3, emoji: '⭐', label: 'Хорошо' },
  ];

  function showResults(results, totalMs) {
    // ── Switch screens ────────────────────────────────────────────────
    document.getElementById('screen-exercise').classList.remove('screen-active');
    var screenResults = document.getElementById('screen-results');
    screenResults.classList.add('screen-active');

    var totalWords = results.length;

    // ── Title ─────────────────────────────────────────────────────────
    document.getElementById('results-title').textContent =
      'Готово! ' + totalWords + ' ' + pluralWords(totalWords);

    // ── Total time ────────────────────────────────────────────────────
    var totalSec = totalMs > 0 ? (totalMs / 1000).toFixed(1) : '0.0';
    var mins = Math.floor(totalMs / 60000);
    var secs = Math.floor((totalMs % 60000) / 1000);
    var timeStr = mins > 0
      ? mins + ' мин ' + secs + ' сек'
      : totalSec + ' сек';

    document.getElementById('results-time').textContent = '⏱ Время: ' + timeStr;

    // ── Breakdown ─────────────────────────────────────────────────────
    var counts = { 0: 0, 1: 0, 3: 0 };

    results.forEach(function (r) {
      if (counts[r.rating] !== undefined) counts[r.rating]++;
    });

    var breakdownEl = document.getElementById('results-breakdown');
    breakdownEl.innerHTML = '';

    RATING_META.forEach(function (meta) {
      var row = document.createElement('div');
      row.className = 'results-row';

      var labelWrap = document.createElement('div');
      labelWrap.className = 'results-row-label';
      labelWrap.textContent = meta.emoji + '  ' + meta.label;

      var countEl = document.createElement('div');
      countEl.className = 'results-row-count';
      countEl.textContent = counts[meta.rating];

      row.appendChild(labelWrap);
      row.appendChild(countEl);
      breakdownEl.appendChild(row);
    });

    // ── Telegram MainButton ───────────────────────────────────────────
    var tg = window.Telegram && window.Telegram.WebApp;

    if (tg) {
      tg.MainButton.setText('Отправить результаты');
      tg.MainButton.show();
      var handler = function () {
        try {
          tg.sendData(JSON.stringify({
            type: 'exercise_results',
            results: results,
          }));
        } catch (e) {
          console.error('sendData failed:', e);
          var errEl = document.createElement('div');
          errEl.style.cssText =
            'color:#ff4444;font-size:0.85rem;text-align:center;margin-top:8px;';
          errEl.textContent = 'Ошибка отправки. Попробуйте снова.';
          document.querySelector('.results-content').appendChild(errEl);
        }
      };
      tg.MainButton.offClick(handler);
      tg.MainButton.onClick(handler);
    } else {
      // Browser mode — log to console
      console.info('[matching] sendData payload:', JSON.stringify({
        type: 'exercise_results',
        results: results,
      }));
    }
  }

  // ── Helpers ───────────────────────────────────────────────────────────
  function pluralWords(n) {
    var mod10  = n % 10;
    var mod100 = n % 100;
    if (mod10 === 1 && mod100 !== 11)              return 'слово';
    if (mod10 >= 2 && mod10 <= 4 &&
        (mod100 < 10 || mod100 >= 20))             return 'слова';
    return 'слов';
  }

  // ── Export ────────────────────────────────────────────────────────────
  window.showMatchingResults = showResults;
})();
