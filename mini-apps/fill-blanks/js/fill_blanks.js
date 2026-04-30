/**
 * fill_blanks.js
 * Core game logic for fill-in-the-blanks exercise.
 *
 * Public API:
 *   initFillBlanks(payload, direction, onComplete)
 *   - payload:    FillBlanksPayload { sentences, word_bank }
 *   - direction:  'forward' | 'reverse'
 *   - onComplete: function(results) called when all sentences are done
 *                 results: Array of { word_id, correct, time_ms }
 */

(function () {
  'use strict';

  var _payload = null;
  var _direction = 'forward';
  var _onComplete = null;

  // Current sentence index
  var _sentenceIdx = 0;

  // word_bank chips currently in the bank (not placed)
  var _bankItems = [];

  // For each blank slot: { blankEl, wordId, placedChipEl, startTime }
  var _slots = [];

  // Per-sentence start time
  var _sentenceStartTime = 0;

  // Accumulated results across all sentences
  var _allResults = [];

  // DOM refs (set in initFillBlanks)
  var _sentencesArea = null;
  var _wordBankEl = null;
  var _btnCheck = null;
  var _btnNext = null;
  var _progressBar = null;
  var _counter = null;

  // Drag state
  var _dragging = null; // { chipEl, wordId, originEl } — chipEl is the element being dragged

  function initFillBlanks(payload, direction, onComplete) {
    _payload = payload;
    _direction = direction || 'forward';
    _onComplete = onComplete;
    _sentenceIdx = 0;
    _allResults = [];

    _sentencesArea = document.getElementById('sentences-area');
    _wordBankEl    = document.getElementById('word-bank');
    _btnCheck      = document.getElementById('btn-check');
    _btnNext       = document.getElementById('btn-next');
    _progressBar   = document.getElementById('progress-bar');
    _counter       = document.getElementById('sentence-counter');

    _btnCheck.addEventListener('click', _handleCheck);
    _btnNext.addEventListener('click', _handleNext);

    _renderSentence(_sentenceIdx);
  }

  // ── Rendering ─────────────────────────────────────────────────────────

  function _renderSentence(idx) {
    var sentence = _payload.sentences[idx];
    var total = _payload.sentences.length;

    // Update progress
    _progressBar.style.width = (idx / total * 100) + '%';
    _counter.textContent = (idx + 1) + ' / ' + total;

    // Build slots map: position → word_id
    var blanksMap = {};
    sentence.blanks.forEach(function (b) {
      blanksMap[b.position] = b.word_id;
    });

    // Reset slots
    _slots = [];

    // Build sentence HTML with blank slots
    _sentencesArea.innerHTML = '';
    var sentenceEl = document.createElement('div');
    sentenceEl.className = 'sentence-block';

    // Template line with blank slots
    var templateEl = document.createElement('div');
    templateEl.className = 'sentence-template';
    _buildTemplateHTML(sentence.template, blanksMap, templateEl);
    sentenceEl.appendChild(templateEl);

    // Transliteration (if present) with ___ for blanks
    if (sentence.transliteration) {
      var trEl = document.createElement('div');
      trEl.className = 'sentence-transliteration';
      trEl.textContent = sentence.transliteration;
      sentenceEl.appendChild(trEl);
    }

    // Translation (if present)
    if (sentence.translation) {
      var transEl = document.createElement('div');
      transEl.className = 'sentence-translation';
      transEl.textContent = sentence.translation;
      sentenceEl.appendChild(transEl);
    }

    _sentencesArea.appendChild(sentenceEl);

    // Build word bank — all word_bank items are available each sentence
    // (only the ones referenced in blanks need to be placed; others are distractors)
    _bankItems = _payload.word_bank.slice(); // shallow copy
    _renderWordBank();

    // Reset buttons
    _btnCheck.classList.remove('hidden');
    _btnNext.classList.add('hidden');

    _sentenceStartTime = Date.now();
  }

  function _buildTemplateHTML(template, blanksMap, container) {
    // Split on ___ to find blank positions
    var parts = template.split('___');
    var blankIdx = 0;

    parts.forEach(function (part, i) {
      if (part) {
        var span = document.createElement('span');
        span.className = 'sentence-text';
        span.textContent = part;
        container.appendChild(span);
      }

      if (i < parts.length - 1) {
        // Insert a blank slot
        var wordId = blanksMap[blankIdx] || null;
        var slot = _createSlot(wordId, blankIdx);
        container.appendChild(slot.el);
        _slots.push(slot);
        blankIdx++;
      }
    });
  }

  function _createSlot(wordId, position) {
    var el = document.createElement('span');
    el.className = 'blank-slot';
    el.dataset.wordId = wordId || '';
    el.dataset.position = position;

    // Drop target events
    el.addEventListener('dragover', function (e) {
      e.preventDefault();
      el.classList.add('drag-over');
    });
    el.addEventListener('dragleave', function () {
      el.classList.remove('drag-over');
    });
    el.addEventListener('drop', function (e) {
      e.preventDefault();
      el.classList.remove('drag-over');
      if (_dragging) {
        _placeChipInSlot(_dragging, el, position);
      }
    });

    // Tap-to-place: tapping an empty slot selects it as target
    el.addEventListener('click', function () {
      _handleSlotTap(el, wordId, position);
    });

    return { el: el, wordId: wordId, placedChipEl: null, position: position, startTime: Date.now() };
  }

  function _renderWordBank() {
    _wordBankEl.innerHTML = '';

    _bankItems.forEach(function (item) {
      var chip = _createChip(item);
      _wordBankEl.appendChild(chip);
    });
  }

  function _createChip(item) {
    var chip = document.createElement('div');
    chip.className = 'word-chip';
    chip.dataset.wordId = item.id;
    chip.draggable = true;

    var wordEl = document.createElement('span');
    wordEl.className = 'chip-word';
    wordEl.textContent = item.word;
    chip.appendChild(wordEl);

    if (item.transliteration) {
      var trEl = document.createElement('span');
      trEl.className = 'chip-tr';
      trEl.textContent = item.transliteration;
      chip.appendChild(trEl);
    }

    // Drag events
    chip.addEventListener('dragstart', function (e) {
      _dragging = { chipEl: chip, wordId: item.id, originEl: chip.parentElement };
      chip.classList.add('dragging');
      e.dataTransfer.effectAllowed = 'move';
    });
    chip.addEventListener('dragend', function () {
      chip.classList.remove('dragging');
      _dragging = null;
    });

    // Tap-to-select behavior
    chip.addEventListener('click', function () {
      _handleChipTap(chip, item.id);
    });

    return chip;
  }

  // ── Tap-to-place state ─────────────────────────────────────────────────

  var _selectedChip = null; // { chipEl, wordId }

  function _handleChipTap(chipEl, wordId) {
    if (_selectedChip && _selectedChip.chipEl === chipEl) {
      // Deselect
      chipEl.classList.remove('selected');
      _selectedChip = null;
      return;
    }
    // Deselect previous
    if (_selectedChip) {
      _selectedChip.chipEl.classList.remove('selected');
    }
    _selectedChip = { chipEl: chipEl, wordId: wordId };
    chipEl.classList.add('selected');
  }

  function _handleSlotTap(slotEl, wordId, position) {
    if (!_selectedChip) {
      // If slot already filled, return chip to bank
      var slot = _getSlot(position);
      if (slot && slot.placedChipEl) {
        _returnChipToBank(slot);
      }
      return;
    }

    // Place selected chip into this slot
    var slot = _getSlot(position);
    if (!slot) return;

    // If slot is occupied, return existing chip first
    if (slot.placedChipEl) {
      _returnChipToBank(slot);
    }

    _placeChipInSlotBySelected(slot, slotEl);
  }

  function _placeChipInSlotBySelected(slot, slotEl) {
    if (!_selectedChip) return;

    var chipEl = _selectedChip.chipEl;
    var wordId = _selectedChip.wordId;

    chipEl.classList.remove('selected');
    _selectedChip = null;

    // Remove from bank display
    chipEl.remove();
    _bankItems = _bankItems.filter(function (item) { return item.id !== wordId; });

    // Place in slot
    slotEl.innerHTML = '';
    slotEl.classList.add('filled');
    var mini = _createMiniChip(wordId);
    slotEl.appendChild(mini);
    slot.placedChipEl = mini;
    slot.placedWordId = wordId;
  }

  function _placeChipInSlot(dragging, slotEl, position) {
    var slot = _getSlot(position);
    if (!slot) return;

    // If slot occupied, return existing chip to bank first
    if (slot.placedChipEl) {
      _returnChipToBank(slot);
    }

    var wordId = dragging.wordId;

    // Remove original chip from bank
    dragging.chipEl.remove();
    _bankItems = _bankItems.filter(function (item) { return item.id !== wordId; });

    // Place mini chip in slot
    slotEl.innerHTML = '';
    slotEl.classList.add('filled');
    var mini = _createMiniChip(wordId);
    slotEl.appendChild(mini);
    slot.placedChipEl = mini;
    slot.placedWordId = wordId;
  }

  function _createMiniChip(wordId) {
    var item = _payload.word_bank.find(function (w) { return w.id === wordId; });
    var mini = document.createElement('span');
    mini.className = 'mini-chip';
    mini.textContent = item ? item.word : wordId;
    mini.dataset.wordId = wordId;

    // Click on placed chip returns it to bank
    mini.addEventListener('click', function (e) {
      e.stopPropagation();
      var slot = _getSlotByWordId(wordId);
      if (slot) _returnChipToBank(slot);
    });
    return mini;
  }

  function _returnChipToBank(slot) {
    var wordId = slot.placedWordId;
    if (!wordId) return;

    slot.el.innerHTML = '';
    slot.el.classList.remove('filled', 'correct', 'incorrect');
    slot.placedChipEl = null;
    slot.placedWordId = null;

    // Re-add to bank
    var item = _payload.word_bank.find(function (w) { return w.id === wordId; });
    if (item) {
      _bankItems.push(item);
      var chip = _createChip(item);
      _wordBankEl.appendChild(chip);
    }
  }

  // ── Check & Next ───────────────────────────────────────────────────────

  function _handleCheck() {
    var sentence = _payload.sentences[_sentenceIdx];
    var now = Date.now();

    // Check each blank
    _slots.forEach(function (slot) {
      var expected = slot.wordId;
      var placed = slot.placedWordId || null;
      var correct = placed === expected;

      slot.el.classList.remove('correct', 'incorrect');
      slot.el.classList.add(correct ? 'correct' : 'incorrect');

      if (!correct) {
        slot.el.classList.add('shake');
        setTimeout(function () { slot.el.classList.remove('shake'); }, 500);
      }

      // Only record results for blanks that are part of this sentence
      if (expected) {
        var timeMs = now - _sentenceStartTime;
        _allResults.push({
          word_id: expected,
          correct: correct,
          time_ms: timeMs,
        });
      }
    });

    _btnCheck.classList.add('hidden');
    _btnNext.classList.remove('hidden');
  }

  function _handleNext() {
    _sentenceIdx++;

    if (_sentenceIdx >= _payload.sentences.length) {
      // Done — update progress to 100% and call completion
      _progressBar.style.width = '100%';
      if (_onComplete) _onComplete(_allResults);
      return;
    }

    _renderSentence(_sentenceIdx);
  }

  // ── Helpers ────────────────────────────────────────────────────────────

  function _getSlot(position) {
    for (var i = 0; i < _slots.length; i++) {
      if (_slots[i].position === position) return _slots[i];
    }
    return null;
  }

  function _getSlotByWordId(wordId) {
    for (var i = 0; i < _slots.length; i++) {
      if (_slots[i].placedWordId === wordId) return _slots[i];
    }
    return null;
  }

  // ── Export ─────────────────────────────────────────────────────────────
  window.initFillBlanks = initFillBlanks;
})();
