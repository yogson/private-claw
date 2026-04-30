/**
 * app.js
 * Main entry point for the Fill-in-the-Blanks Mini App.
 *
 * Flow:
 *   1. Init Telegram WebApp (or warn if running in browser)
 *   2. Parse URL params: `data` (required) and `dir` (optional, default 'forward')
 *   3. Decode `data` payload with decodePayload()
 *   4. Start fill-blanks exercise via initFillBlanks()
 *   5. On completion, show results via showFillResults()
 */

(function () {
  var tg = window.Telegram && window.Telegram.WebApp;

  if (tg) {
    tg.ready();
    tg.expand();
    applyTheme(tg.themeParams);
    tg.onEvent('themeChanged', function () { applyTheme(tg.themeParams); });
  } else {
    showBrowserWarning();
  }

  var screenLoading  = document.getElementById('screen-loading');
  var screenExercise = document.getElementById('screen-exercise');

  var params    = new URLSearchParams(window.location.search);
  var dataParam = params.get('data');
  var dirParam  = params.get('dir') || 'forward';
  var direction = (dirParam === 'reverse') ? 'reverse' : 'forward';

  if (!dataParam) {
    showError('Данные упражнения не найдены');
    return;
  }

  var payload;
  try {
    payload = decodePayload(dataParam);
  } catch (e) {
    console.error('Decode error:', e);
    showError('Ошибка декодирования данных');
    return;
  }

  if (!payload || !Array.isArray(payload.sentences) || payload.sentences.length === 0) {
    showError('Нет предложений для упражнения');
    return;
  }

  screenLoading.classList.remove('screen-active');
  screenExercise.classList.add('screen-active');

  initFillBlanks(payload, direction, function (results) {
    showFillResults(results, direction);
  });

  function applyTheme(themeParams) {
    if (!themeParams) return;
    var map = {
      bg_color:           '--tg-theme-bg-color',
      text_color:         '--tg-theme-text-color',
      hint_color:         '--tg-theme-hint-color',
      link_color:         '--tg-theme-link-color',
      button_color:       '--tg-theme-button-color',
      button_text_color:  '--tg-theme-button-text-color',
      secondary_bg_color: '--tg-theme-secondary-bg-color',
    };
    Object.keys(map).forEach(function (key) {
      if (themeParams[key]) {
        document.documentElement.style.setProperty(map[key], themeParams[key]);
      }
    });
  }

  function showError(message) {
    screenLoading.classList.add('screen-active');
    screenLoading.innerHTML =
      '<div class="error-content">' +
        '<div class="error-icon">⚠️</div>' +
        '<div class="error-message">' + escapeHtml(message) + '</div>' +
      '</div>';
  }

  function showBrowserWarning() {
    var banner = document.createElement('div');
    banner.className = 'browser-warning';
    banner.textContent =
      '⚠️ Приложение открыто вне Telegram. Отправка результатов недоступна.';
    document.body.prepend(banner);
  }

  function escapeHtml(str) {
    return str
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }
})();
