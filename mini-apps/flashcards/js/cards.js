/**
 * cards.js
 * Card state machine: renders question/answer sides and collects ratings.
 *
 * Public API:
 *   initCards(words, direction, onComplete)
 *   - words:       Array of CompactWordPayload
 *   - direction:   'forward' | 'reverse'
 *   - onComplete:  function(results) called when all cards are rated
 */

(function () {
  // ── DOM refs ──────────────────────────────────────────────────────────
  const cardEl         = document.getElementById('card');
  const cardFront      = document.getElementById('card-front');
  const cardBack       = document.getElementById('card-back');
  const counterEl      = document.getElementById('card-counter');
  const progressBar    = document.getElementById('progress-bar');
  const btnShowAnswer  = document.getElementById('btn-show-answer');
  const btnShowWrap    = document.getElementById('btn-show-answer-wrap');
  const ratingButtons  = document.getElementById('rating-buttons');

  // ── State ─────────────────────────────────────────────────────────────
  let words       = [];
  let direction   = 'forward';
  let currentIdx  = 0;
  let results     = [];
  let cardStartTime = 0;
  let onComplete  = null;
  let revealed    = false;
  let initialized = false;

  // ── Init ──────────────────────────────────────────────────────────────
  function initCards(wordList, dir, completeCb) {
    if (initialized) return;
    initialized = true;
    words      = wordList;
    direction  = dir || 'forward';
    onComplete = completeCb;
    currentIdx = 0;
    results    = [];
    revealed   = false;

    // Attach rating button listeners once
    ratingButtons.querySelectorAll('.btn-rating').forEach(function (btn) {
      btn.addEventListener('click', function () {
        if (!revealed) return;
        const rating = parseInt(btn.getAttribute('data-rating'), 10);
        handleRating(rating);
      });
    });

    // "Show answer" button + card tap both reveal
    btnShowAnswer.addEventListener('click', revealAnswer);
    cardEl.addEventListener('click', function () {
      if (!revealed) revealAnswer();
    });

    showQuestion(currentIdx);
  }

  // ── Question side ─────────────────────────────────────────────────────
  function showQuestion(idx) {
    revealed = false;

    const word = words[idx];
    updateProgress(idx);
    updateCounter(idx);

    // Remove flip so front is visible
    cardEl.classList.remove('flipped');

    // Build front face content
    cardFront.innerHTML = '';

    if (direction === 'forward') {
      // Show Greek word (with article if available)
      const greekText = word.a ? (word.a + ' ' + word.w) : word.w;
      cardFront.appendChild(makeEl('div', 'word-primary', greekText));
      cardFront.appendChild(makeEl('div', 'word-transliteration', word.t));
    } else {
      // Show Russian translation
      cardFront.appendChild(makeEl('div', 'word-primary', word.tr));
    }

    // Show answer button, hide rating buttons
    btnShowWrap.classList.remove('hidden');
    ratingButtons.classList.add('hidden');

    cardStartTime = Date.now();
  }

  // ── Answer side ───────────────────────────────────────────────────────
  function revealAnswer() {
    if (revealed) return;
    revealed = true;

    const word = words[currentIdx];

    // Build back face content
    cardBack.innerHTML = '';

    if (direction === 'forward') {
      // Top label: Greek + transliteration
      const topLabel = (word.a ? word.a + ' ' : '') + word.w + '  ·  ' + word.t;
      cardBack.appendChild(makeEl('div', 'back-top-label', topLabel));

      // Big Russian translation
      cardBack.appendChild(makeEl('div', 'word-translation', word.tr));
    } else {
      // Top label: Russian translation
      cardBack.appendChild(makeEl('div', 'back-top-label', word.tr));

      // Big Greek word + article + transliteration
      const greekText = word.a ? (word.a + ' ' + word.w) : word.w;
      cardBack.appendChild(makeEl('div', 'word-translation', greekText));
      cardBack.appendChild(makeEl('div', 'word-transliteration', word.t));
    }

    // Verb forms table (if present)
    if (word.vf) {
      cardBack.appendChild(buildVerbForms(word.vf));
    }

    // Example sentence (if present)
    if (word.ex) {
      cardBack.appendChild(buildExample(word.ex, word.et));
    }

    // Flip the card
    cardEl.classList.add('flipped');

    // Toggle buttons
    btnShowWrap.classList.add('hidden');
    ratingButtons.classList.remove('hidden');
  }

  // ── Rating handler ────────────────────────────────────────────────────
  function handleRating(rating) {
    const timeMs = Date.now() - cardStartTime;
    const word   = words[currentIdx];

    results.push({
      word_id:   word.id,
      rating:    rating,
      time_ms:   timeMs,
      direction: direction,
    });

    currentIdx++;

    if (currentIdx < words.length) {
      // Brief pause then show next question
      setTimeout(function () {
        showQuestion(currentIdx);
      }, 150);
    } else {
      // All cards done — update progress bar to 100% then call back
      updateProgress(words.length);
      setTimeout(function () {
        if (typeof onComplete === 'function') {
          onComplete(results);
        }
      }, 300);
    }
  }

  // ── Progress & counter helpers ────────────────────────────────────────
  function updateProgress(idx) {
    const pct = words.length > 0 ? (idx / words.length) * 100 : 0;
    progressBar.style.width = pct + '%';
  }

  function updateCounter(idx) {
    counterEl.textContent = (idx + 1) + ' / ' + words.length;
  }

  // ── DOM helpers ───────────────────────────────────────────────────────
  function makeEl(tag, className, text) {
    const el = document.createElement(tag);
    el.className = className;
    if (text !== undefined) el.textContent = text;
    return el;
  }

  /**
   * Build verb-forms table element.
   * vf: { p, pt, ao, aot, f, ft }
   */
  function buildVerbForms(vf) {
    const container = document.createElement('div');
    container.className = 'verb-forms';

    const title = makeEl('div', 'verb-forms-title', 'Формы глагола');
    container.appendChild(title);

    const rows = [
      { label: 'Настоящее:', word: vf.p,  trans: vf.pt  },
      { label: 'Аорист:',    word: vf.ao, trans: vf.aot },
      { label: 'Будущее:',   word: vf.f,  trans: vf.ft  },
    ];

    rows.forEach(function (row) {
      if (!row.word) return;
      const rowEl = document.createElement('div');
      rowEl.className = 'verb-form-row';

      const labelEl = makeEl('span', 'verb-form-label', row.label);
      const valueEl = makeEl('span', 'verb-form-value', row.word);
      const transEl = makeEl('span', 'verb-form-trans', row.trans ? '(' + row.trans + ')' : '');

      rowEl.appendChild(labelEl);
      rowEl.appendChild(valueEl);
      rowEl.appendChild(transEl);
      container.appendChild(rowEl);
    });

    return container;
  }

  /**
   * Build example sentence block.
   */
  function buildExample(greek, russian) {
    const block = document.createElement('div');
    block.className = 'example-block';

    block.appendChild(makeEl('div', 'example-greek', greek));
    if (russian) {
      block.appendChild(makeEl('div', 'example-russian', russian));
    }
    return block;
  }

  // ── Export ────────────────────────────────────────────────────────────
  window.initCards = initCards;
})();
