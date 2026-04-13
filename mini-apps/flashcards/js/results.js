/**
 * results.js
 * Renders the results screen and sends data back to Telegram.
 *
 * Public API:
 *   showResults(results, totalWords)
 *   - results:    Array of { word_id, rating, time_ms, direction }
 *   - totalWords: total number of words in the exercise
 */

(function () {
  const RATING_META = [
    { rating: 0, emoji: '❌', label: 'Again' },
    { rating: 1, emoji: '⚠️', label: 'Hard'  },
    { rating: 2, emoji: '✅', label: 'Good'  },
    { rating: 3, emoji: '⭐', label: 'Easy'  },
  ];

  function showResults(results, totalWords) {
    // ── Switch screens ────────────────────────────────────────────────
    document.getElementById('screen-exercise').classList.remove('screen-active');
    const screenResults = document.getElementById('screen-results');
    screenResults.classList.add('screen-active');

    // ── Title ─────────────────────────────────────────────────────────
    document.getElementById('results-title').textContent =
      'Готово! ' + totalWords + ' ' + pluralWords(totalWords);

    // ── Breakdown ─────────────────────────────────────────────────────
    const counts = { 0: 0, 1: 0, 2: 0, 3: 0 };
    let totalTime = 0;

    results.forEach(function (r) {
      if (counts[r.rating] !== undefined) counts[r.rating]++;
      totalTime += r.time_ms || 0;
    });

    const breakdownEl = document.getElementById('results-breakdown');
    breakdownEl.innerHTML = '';

    RATING_META.forEach(function (meta) {
      const row = document.createElement('div');
      row.className = 'results-row';

      const labelWrap = document.createElement('div');
      labelWrap.className = 'results-row-label';
      labelWrap.textContent = meta.emoji + '  ' + meta.label;

      const countEl = document.createElement('div');
      countEl.className = 'results-row-count';
      countEl.textContent = counts[meta.rating];

      row.appendChild(labelWrap);
      row.appendChild(countEl);
      breakdownEl.appendChild(row);
    });

    // ── Average time ──────────────────────────────────────────────────
    const avgSec = results.length > 0
      ? (totalTime / results.length / 1000).toFixed(1)
      : '0.0';

    document.getElementById('results-avg-time').textContent =
      'Среднее время: ' + avgSec + ' сек';

    // ── Telegram MainButton ───────────────────────────────────────────
    const tg = window.Telegram && window.Telegram.WebApp;

    if (tg) {
      tg.MainButton.setText('Отправить результаты');
      tg.MainButton.show();
      tg.MainButton.onClick(function () {
        try {
          tg.sendData(JSON.stringify({
            type: 'exercise_results',
            results: results,
          }));
        } catch (e) {
          console.error('sendData failed:', e);
          // Show a brief inline error without crashing
          const errEl = document.createElement('div');
          errEl.style.cssText =
            'color:#ff4444;font-size:0.85rem;text-align:center;margin-top:8px;';
          errEl.textContent = 'Ошибка отправки. Попробуйте снова.';
          document.querySelector('.results-content').appendChild(errEl);
        }
      });
    } else {
      // Browser mode — log to console
      console.info('[flashcards] sendData payload:', JSON.stringify({
        type: 'exercise_results',
        results: results,
      }));
    }
  }

  // ── Helpers ───────────────────────────────────────────────────────────
  function pluralWords(n) {
    const mod10  = n % 10;
    const mod100 = n % 100;
    if (mod10 === 1 && mod100 !== 11)              return 'слово';
    if (mod10 >= 2 && mod10 <= 4 &&
        (mod100 < 10 || mod100 >= 20))             return 'слова';
    return 'слов';
  }

  // ── Export ────────────────────────────────────────────────────────────
  window.showResults = showResults;
})();
