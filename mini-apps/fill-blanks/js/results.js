/**
 * results.js
 * Renders the results screen and sends data back to Telegram.
 *
 * Public API:
 *   showFillResults(results, direction)
 *   - results:   Array of { word_id, correct, time_ms }
 *   - direction: 'forward' | 'reverse'
 */

(function () {
  'use strict';

  function showFillResults(results, direction) {
    document.getElementById('screen-exercise').classList.remove('screen-active');
    var screenResults = document.getElementById('screen-results');
    screenResults.classList.add('screen-active');

    var total = results.length;
    var correct = results.filter(function (r) { return r.correct; }).length;

    document.getElementById('results-title').textContent =
      correct + ' / ' + total + ' ' + _pluralRight(correct);

    var breakdownEl = document.getElementById('results-breakdown');
    breakdownEl.innerHTML = '';

    var correctRow = _makeRow('✅ Правильно', correct);
    var wrongRow   = _makeRow('❌ Неверно',   total - correct);
    breakdownEl.appendChild(correctRow);
    breakdownEl.appendChild(wrongRow);

    var tg = window.Telegram && window.Telegram.WebApp;

    if (tg) {
      tg.MainButton.setText('Отправить результаты');
      tg.MainButton.show();
      var handler = function () {
        try {
          tg.sendData(JSON.stringify({
            type: 'fill_blanks_results',
            direction: direction || 'forward',
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
      console.info('[fill-blanks] sendData payload:', JSON.stringify({
        type: 'fill_blanks_results',
        direction: direction || 'forward',
        results: results,
      }));
    }
  }

  function _makeRow(label, count) {
    var row = document.createElement('div');
    row.className = 'results-row';

    var labelEl = document.createElement('div');
    labelEl.className = 'results-row-label';
    labelEl.textContent = label;

    var countEl = document.createElement('div');
    countEl.className = 'results-row-count';
    countEl.textContent = count;

    row.appendChild(labelEl);
    row.appendChild(countEl);
    return row;
  }

  function _pluralRight(n) {
    var mod10  = n % 10;
    var mod100 = n % 100;
    if (mod10 === 1 && mod100 !== 11)                    return 'правильно';
    if (mod10 >= 2 && mod10 <= 4 && (mod100 < 10 || mod100 >= 20)) return 'правильно';
    return 'правильно';
  }

  window.showFillResults = showFillResults;
})();
