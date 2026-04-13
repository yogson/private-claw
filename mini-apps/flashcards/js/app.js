/**
 * app.js
 * Main entry point for the Flashcards Mini App.
 *
 * Flow:
 *   1. Init Telegram WebApp (or warn if running in browser)
 *   2. Show loading screen
 *   3. Parse URL params: `words` (required) and `dir` (optional, default 'forward')
 *   4. Decode `words` payload with decodeWords()
 *   5. Start card exercise via initCards()
 *   6. On completion, show results via showResults()
 */

(function () {
  // ── Telegram WebApp bootstrap ────────────────────────────────────────
  const tg = window.Telegram && window.Telegram.WebApp;

  if (tg) {
    tg.ready();
    tg.expand();

    // Apply Telegram theme params as CSS custom properties
    applyTheme(tg.themeParams);

    // Keep CSS in sync if the theme changes
    tg.onEvent('themeChanged', function () {
      applyTheme(tg.themeParams);
    });
  } else {
    // Opened outside Telegram — show a warning banner but keep working
    showBrowserWarning();
  }

  // ── DOM refs ──────────────────────────────────────────────────────────
  const screenLoading  = document.getElementById('screen-loading');
  const screenExercise = document.getElementById('screen-exercise');

  // ── Parse URL parameters ──────────────────────────────────────────────
  const params    = new URLSearchParams(window.location.search);
  const wordsParam = params.get('words');
  const dirParam   = params.get('dir') || 'forward';

  // Validate direction
  const direction = (dirParam === 'reverse') ? 'reverse' : 'forward';

  // ── Guard: missing `words` param ──────────────────────────────────────
  if (!wordsParam) {
    showError('Данные упражнения не найдены');
    return;
  }

  // ── Decode payload ────────────────────────────────────────────────────
  let words;
  try {
    words = decodeWords(wordsParam);
  } catch (e) {
    console.error('Decode error:', e);
    showError('Ошибка декодирования данных');
    return;
  }

  // ── Guard: empty word list ────────────────────────────────────────────
  if (!Array.isArray(words) || words.length === 0) {
    showError('Нет слов для изучения');
    return;
  }

  // ── Start exercise ────────────────────────────────────────────────────
  screenLoading.classList.remove('screen-active');
  screenExercise.classList.add('screen-active');

  initCards(words, direction, function (results) {
    showResults(results, words.length);
  });

  // ── Helpers ───────────────────────────────────────────────────────────

  /**
   * Apply Telegram theme params as CSS variables on :root.
   * @param {Object} params - Telegram.WebApp.themeParams
   */
  function applyTheme(params) {
    if (!params) return;
    const map = {
      bg_color:            '--tg-theme-bg-color',
      text_color:          '--tg-theme-text-color',
      hint_color:          '--tg-theme-hint-color',
      link_color:          '--tg-theme-link-color',
      button_color:        '--tg-theme-button-color',
      button_text_color:   '--tg-theme-button-text-color',
      secondary_bg_color:  '--tg-theme-secondary-bg-color',
    };
    Object.keys(map).forEach(function (key) {
      if (params[key]) {
        document.documentElement.style.setProperty(map[key], params[key]);
      }
    });
  }

  /**
   * Replace loading screen content with an error message.
   * @param {string} message
   */
  function showError(message) {
    screenLoading.classList.add('screen-active');
    screenLoading.innerHTML =
      '<div class="error-content">' +
        '<div class="error-icon">⚠️</div>' +
        '<div class="error-message">' + escapeHtml(message) + '</div>' +
      '</div>';
  }

  /**
   * Show a non-blocking warning banner when running in a browser.
   */
  function showBrowserWarning() {
    const banner = document.createElement('div');
    banner.className = 'browser-warning';
    banner.textContent =
      '⚠️ Приложение открыто вне Telegram. Отправка результатов недоступна.';
    document.body.prepend(banner);
  }

  /**
   * Minimal HTML escaping to prevent XSS in error messages.
   */
  function escapeHtml(str) {
    return str
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }
})();
