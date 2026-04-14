/**
 * matching.js
 * Core matching game logic.
 *
 * Public API:
 *   initMatching(words, direction, onComplete)
 *   - words:      Array of CompactWordPayload
 *   - direction:  'forward' (Greek→Russian) or 'reverse' (Russian→Greek)
 *   - onComplete: function(results) called when all pairs are matched
 *
 * Each result object:
 *   { word_id, rating, time_ms, direction }
 *
 * Rating:
 *   first attempt correct  → 3 (Good)
 *   second attempt correct → 1 (Hard)
 *   3+ attempts correct    → 0 (Again)
 */

(function () {
  'use strict';

  // ── DOM refs ──────────────────────────────────────────────────────────
  const colLeft          = document.getElementById('col-left');
  const colRight         = document.getElementById('col-right');
  const matchCounterEl   = document.getElementById('match-counter');
  const progressBarEl    = document.getElementById('progress-bar');
  const selectedIndicator = document.getElementById('selected-indicator');
  const timerEl          = document.getElementById('timer');

  // ── State ─────────────────────────────────────────────────────────────
  let wordStates    = [];   // Array of { word, leftText, rightText, attempts, matched, startTime }
  let direction     = 'forward';
  let onComplete    = null;
  let selectedIdx   = null; // index of currently selected left-column item
  let matchedCount  = 0;
  let totalWords    = 0;
  let timerInterval = null;
  let startTime     = null;
  let initialized   = false;
  let animating     = false; // guard flag: blocks taps during wrong-match flash

  // ── Init ──────────────────────────────────────────────────────────────
  function initMatching(wordList, dir, completeCb) {
    if (initialized) return;
    initialized = true;

    direction  = (dir === 'reverse') ? 'reverse' : 'forward';
    onComplete = completeCb;
    totalWords = wordList.length;
    matchedCount = 0;
    selectedIdx  = null;

    // Build per-word state
    wordStates = wordList.map(function (w) {
      var leftText, rightText;
      if (direction === 'forward') {
        leftText  = w.a ? (w.a + ' ' + w.w) : w.w;
        rightText = w.tr;
      } else {
        leftText  = w.tr;
        rightText = w.a ? (w.a + ' ' + w.w) : w.w;
      }
      return {
        word:      w,
        leftText:  leftText,
        rightText: rightText,
        attempts:  0,
        matched:   false,
        startTime: null,
      };
    });

    // Shuffle right-column order
    var rightOrder = shuffleIndices(totalWords);

    // Render columns
    renderColumns(rightOrder);

    // Timer starts on first tap (see onLeftTap) to avoid penalising slow
    // devices or users who read the words before starting.

    // Update header
    updateCounter();
  }

  // ── Rendering ─────────────────────────────────────────────────────────
  function renderColumns(rightOrder) {
    colLeft.innerHTML  = '';
    colRight.innerHTML = '';

    wordStates.forEach(function (state, idx) {
      // Left tile
      var leftTile = document.createElement('div');
      leftTile.className = 'match-tile match-tile-left';
      leftTile.dataset.idx = idx;
      leftTile.textContent = state.leftText;
      leftTile.addEventListener('click', function () { onLeftTap(idx); });
      colLeft.appendChild(leftTile);
    });

    rightOrder.forEach(function (idx) {
      var state = wordStates[idx];
      var rightTile = document.createElement('div');
      rightTile.className = 'match-tile match-tile-right';
      rightTile.dataset.idx = idx;
      rightTile.textContent = state.rightText;
      rightTile.addEventListener('click', function () { onRightTap(idx); });
      colRight.appendChild(rightTile);
    });
  }

  // ── Tap handlers ──────────────────────────────────────────────────────
  function onLeftTap(idx) {
    var state = wordStates[idx];
    if (state.matched) return;

    if (selectedIdx === idx) {
      // Toggle off
      deselectLeft(idx);
      selectedIdx = null;
      hideSelectedIndicator();
      return;
    }

    // Deselect previous
    if (selectedIdx !== null) {
      deselectLeft(selectedIdx);
    }

    selectedIdx = idx;

    // Start the global timer on the very first tap
    if (startTime === null) {
      startTime = Date.now();
      startTimer();
    }

    // Start timing this word on first tap
    if (state.startTime === null) {
      state.startTime = Date.now();
    }

    selectLeft(idx);
    showSelectedIndicator(state.leftText);
  }

  function onRightTap(idx) {
    if (selectedIdx === null) return; // nothing selected
    if (animating) return;            // block taps during wrong-match flash
    var state = wordStates[idx];
    if (state.matched) return;

    var selectedState = wordStates[selectedIdx];
    if (selectedState.matched) return;

    // Evaluate match: right tile's idx must equal selected left idx
    if (idx === selectedIdx) {
      // ✅ Correct match
      handleCorrectMatch(selectedIdx);
    } else {
      // ❌ Wrong match
      handleWrongMatch(selectedIdx, idx);
    }
  }

  // ── Match resolution ──────────────────────────────────────────────────
  function handleCorrectMatch(idx) {
    var state = wordStates[idx];
    state.matched  = true;
    state.attempts += 1;

    // Calculate time
    var timeMs = state.startTime ? (Date.now() - state.startTime) : 0;

    // Rating: 1st attempt=3, 2nd=1, 3rd+=0
    var rating;
    if (state.attempts === 1)      rating = 3;
    else if (state.attempts === 2) rating = 1;
    else                           rating = 0;

    state.result = {
      word_id:   state.word.id,
      rating:    rating,
      time_ms:   timeMs,
      direction: direction,
    };

    // Visual feedback: flash green then dim
    flashTiles(idx, 'correct', function () {
      dimTiles(idx);
    });

    matchedCount++;
    selectedIdx = null;
    hideSelectedIndicator();
    updateCounter();
    updateProgress();

    if (matchedCount === totalWords) {
      finishGame();
    }
  }

  function handleWrongMatch(leftIdx, rightIdx) {
    var state = wordStates[leftIdx];
    state.attempts += 1;

    // Set guard to block any further taps during the 500ms flash animation
    animating = true;

    // Flash red on both tiles
    flashWrongTiles(leftIdx, rightIdx, function () {
      deselectLeft(leftIdx);
      selectedIdx = null;
      hideSelectedIndicator();
      animating = false;
    });
  }

  // ── Visual helpers ────────────────────────────────────────────────────
  function getTileByIdx(side, idx) {
    var col = (side === 'left') ? colLeft : colRight;
    return col.querySelector('[data-idx="' + idx + '"]');
  }

  function selectLeft(idx) {
    var tile = getTileByIdx('left', idx);
    if (tile) tile.classList.add('selected');
  }

  function deselectLeft(idx) {
    var tile = getTileByIdx('left', idx);
    if (tile) tile.classList.remove('selected');
  }

  function flashTiles(idx, cls, cb) {
    var leftTile  = getTileByIdx('left',  idx);
    var rightTile = getTileByIdx('right', idx);
    [leftTile, rightTile].forEach(function (t) {
      if (t) {
        t.classList.remove('selected');
        t.classList.add(cls);
      }
    });
    setTimeout(cb, 500);
  }

  function flashWrongTiles(leftIdx, rightIdx, cb) {
    var leftTile   = getTileByIdx('left',  leftIdx);
    var rightTile  = getTileByIdx('right', rightIdx);
    [leftTile, rightTile].forEach(function (t) {
      if (t) t.classList.add('wrong');
    });
    setTimeout(function () {
      [leftTile, rightTile].forEach(function (t) {
        if (t) t.classList.remove('wrong');
      });
      cb();
    }, 500);
  }

  function dimTiles(idx) {
    var leftTile  = getTileByIdx('left',  idx);
    var rightTile = getTileByIdx('right', idx);
    [leftTile, rightTile].forEach(function (t) {
      if (t) {
        t.classList.remove('correct', 'selected');
        t.classList.add('matched');
      }
    });
  }

  // ── Selected indicator ────────────────────────────────────────────────
  function showSelectedIndicator(text) {
    selectedIndicator.textContent = '➤ ' + text;
    selectedIndicator.classList.remove('hidden');
  }

  function hideSelectedIndicator() {
    selectedIndicator.classList.add('hidden');
    selectedIndicator.textContent = '';
  }

  // ── Counter & progress ────────────────────────────────────────────────
  function updateCounter() {
    matchCounterEl.textContent = matchedCount + ' / ' + totalWords + ' совпадений';
  }

  function updateProgress() {
    var pct = totalWords > 0 ? (matchedCount / totalWords) * 100 : 0;
    progressBarEl.style.width = pct + '%';
  }

  // ── Timer ─────────────────────────────────────────────────────────────
  function startTimer() {
    timerInterval = setInterval(function () {
      var elapsed = Math.floor((Date.now() - startTime) / 1000);
      var mins = Math.floor(elapsed / 60);
      var secs = elapsed % 60;
      timerEl.textContent = '⏱ ' + mins + ':' + (secs < 10 ? '0' : '') + secs;
    }, 1000);
  }

  function stopTimer() {
    if (timerInterval) {
      clearInterval(timerInterval);
      timerInterval = null;
    }
  }

  // ── Finish ────────────────────────────────────────────────────────────
  function finishGame() {
    stopTimer();

    var totalMs = Date.now() - startTime;

    var results = wordStates.map(function (s) { return s.result; });

    setTimeout(function () {
      if (typeof onComplete === 'function') {
        onComplete(results, totalMs);
      }
    }, 400);
  }

  // ── Utilities ─────────────────────────────────────────────────────────
  function shuffleIndices(n) {
    var arr = [];
    for (var i = 0; i < n; i++) arr.push(i);
    for (var j = arr.length - 1; j > 0; j--) {
      var k = Math.floor(Math.random() * (j + 1));
      var tmp = arr[j];
      arr[j] = arr[k];
      arr[k] = tmp;
    }
    return arr;
  }

  // ── Export ────────────────────────────────────────────────────────────
  window.initMatching = initMatching;
})();
