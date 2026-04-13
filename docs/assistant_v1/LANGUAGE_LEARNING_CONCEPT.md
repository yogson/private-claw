# Language Learning Feature — Concept Document

## Overview

Interactive language learning capability for Private Claw, starting with **Modern Greek** vocabulary building via **Telegram Mini Apps** (flashcards).

**User profile:** Beginner — knows Greek alphabet and basic phrases. Native language: Russian.

---

## Architecture Decision: Offline Mini App

### Rationale

The Private Claw server runs on a **home server without a static IP**. Instead of exposing
an API via tunnels (ngrok, Cloudflare Tunnel), we use a **fully offline Mini App** architecture:

- **Static files** hosted on **GitHub Pages** (separate repo or subtree)
- **Exercise data** passed to Mini App via **URL query parameters** (base64-encoded JSON)
- **Results** sent back via **Telegram `sendData()`** → Bot receives `web_app_data` message
- **No Exercise API, no CORS, no initData validation, no public endpoints**

### Benefits

| Benefit | Details |
|---------|---------|
| 🔒 No public API | No attack surface, no auth middleware needed |
| 🏠 Server stays private | No tunnels, no open ports |
| ⚡ Instant UX | Cards work without network after page load |
| 🛠 Simple infra | GitHub Pages + Bot, nothing else |
| 🔄 Guaranteed delivery | Data flows through Telegram's infrastructure |

### Constraints & Mitigations

| Constraint | Impact | Mitigation |
|------------|--------|------------|
| URL length ~4-8 KB usable | Limits words per session | 20-30 words ≈ 3-4 KB raw → fits. Use compression (pako) if needed |
| `sendData()` is one-shot, closes Mini App | Must collect all results before sending | Aggregate results array, send on "Finish" |
| No real-time sync during exercise | Can't update store mid-session | Not needed — session is atomic |

---

## Architecture Diagram

```
                        GitHub Pages
                        ─────────────
                        yogson.github.io/private-claw-apps/
                          └── flashcards/
                               ├── index.html
                               ├── app.js
                               └── style.css

┌──────────────────────────────────────────────────────────────┐
│                        Telegram                               │
│                                                               │
│  1. User: "Давай позанимаемся"                               │
│                                                               │
│  2. Agent:                                                    │
│     → get_due_words(limit=20)                                │
│     → encode words as base64 JSON                            │
│     → send WebApp button with URL:                           │
│       https://yogson.github.io/.../flashcards/               │
│         ?data={base64_payload}                                │
│                                                               │
│  3. ┌────────────────────────────┐                            │
│     │     Mini App (WebView)     │                            │
│     │                            │                            │
│     │  • Decode data from URL    │                            │
│     │  • Render flashcards       │                            │
│     │  • User flips & rates      │                            │
│     │  • Collect all results     │                            │
│     │                            │                            │
│     │  [Finish] → sendData()     │──── one-shot ────┐        │
│     └────────────────────────────┘                   │        │
│                                                      ▼        │
│  4. Bot receives web_app_data message:                        │
│     {results: [{word_id, rating, time_ms}, ...]}              │
│                                                               │
│  5. Agent processes results:                                  │
│     → update_vocabulary_sm2(results)                         │
│     → respond: "Отлично! 18/20, следующий повтор через 3 дня"│
│                                                               │
└──────────────────────────────────────────────────────────────┘
                               │
                               ▼
                    ┌─────────────────────┐
                    │   Home Server        │
                    │                     │
                    │  Telegram Bot        │
                    │  (long polling)      │
                    │                     │
                    │  Vocabulary Store    │
                    │  (JSON files)        │
                    │                     │
                    │  SM-2 Engine         │
                    └─────────────────────┘
```

---

## Phase 1 Scope

### Components

| Component | Location | Description |
|-----------|----------|-------------|
| **Capability manifest** | `config/capabilities/language_learning.yaml` | Tool definitions for the agent |
| **Vocabulary store** | `src/assistant/extensions/language_learning/` | Word storage + SM-2 spaced repetition |
| **Mini App (static)** | Separate GitHub Pages repo/dir | Flashcards HTML/JS/CSS |
| **Agent tools** | Part of capability | `add_vocabulary`, `get_due_words`, `start_exercise`, `process_exercise_results`, `get_progress` |
| **Channel integration** | `src/assistant/channels/telegram/` | WebApp button response + `web_app_data` handler |

### What we DON'T need (removed vs original concept)

- ~~Exercise REST API endpoints~~
- ~~initData HMAC validation middleware~~
- ~~CORS configuration~~
- ~~StaticFiles mount in FastAPI~~
- ~~ExerciseSession server-side state~~

---

## Data Flow Details

### 1. Starting an Exercise

**User says:** "Хочу повторить слова" / "Давай карточки"

**Agent calls tool:** `start_exercise`
```python
# Tool: start_exercise
# 1. Gets due words from vocabulary store
words = vocabulary_store.get_due_words(user_id, limit=20)

# 2. Builds compact payload
payload = {
    "words": [
        {
            "id": "abc123",
            "w": "σπίτι",        # word
            "t": "spíti",        # transliteration
            "tr": "дом",         # translation
            "a": "το",           # article
            "ex": "Το σπίτι είναι μεγάλο.",  # example (optional)
            "et": "Дом большой."              # example translation (optional)
        },
        # ... more words
    ]
}

# 3. Encode: JSON → UTF-8 → gzip → base64url
encoded = base64url(gzip(json(payload)))

# 4. Build Mini App URL
url = f"https://yogson.github.io/private-claw-apps/flashcards/?d={encoded}"

# 5. Return as WebApp button response
```

**Payload size estimate:**
| Words | Raw JSON | Gzipped | Base64 | Fits URL? |
|-------|----------|---------|--------|-----------|
| 10 | ~1.5 KB | ~0.8 KB | ~1.1 KB | ✅ |
| 20 | ~3.0 KB | ~1.5 KB | ~2.0 KB | ✅ |
| 30 | ~4.5 KB | ~2.2 KB | ~3.0 KB | ✅ |
| 50 | ~7.5 KB | ~3.5 KB | ~4.7 KB | ⚠️ borderline |

### 2. Mini App Lifecycle

```javascript
// app.js (simplified)

// 1. Init Telegram WebApp SDK
const tg = window.Telegram.WebApp;
tg.ready();
tg.expand();

// 2. Decode exercise data from URL
const params = new URLSearchParams(window.location.search);
const compressed = base64urlDecode(params.get('d'));
const data = JSON.parse(pako.ungzip(compressed, { to: 'string' }));

// 3. Render flashcards, collect ratings
const results = [];  // [{id, rating, time_ms}, ...]

// 4. On finish — send results back via Telegram
function finishExercise() {
    tg.sendData(JSON.stringify({
        type: "exercise_results",
        results: results
    }));
    // Mini App closes automatically after sendData()
}
```

### 3. Receiving Results

```python
# In Telegram channel handler:
# Bot receives Update with message.web_app_data.data

data = json.loads(update.message.web_app_data.data)
# data = {"type": "exercise_results", "results": [...]}

# Route to agent or directly to vocabulary store
# Agent tool: process_exercise_results(results)
# → Updates SM-2 intervals for each word
# → Returns summary for agent to comment on
```

---

## Data Model

### VocabularyEntry

```python
class VocabularyEntry(BaseModel):
    id: str                          # UUID
    user_id: str                     # Owner

    # Word data
    word: str                        # Greek: "σπίτι"
    transliteration: str             # Latin: "spíti" (required — user is beginner)
    translation: str                 # Russian: "дом"

    # Linguistic metadata
    part_of_speech: str              # noun, verb, adjective, phrase, etc.
    gender: str | None = None        # m/f/n (for nouns)
    article: str | None = None       # ο/η/το (for nouns)
    example_sentence: str | None = None
    example_translation: str | None = None
    tags: list[str] = []             # ["basics", "home", "A1"]

    # SM-2 Spaced Repetition fields
    easiness_factor: float = 2.5     # EF (≥1.3)
    interval: int = 0                # Days until next review
    repetitions: int = 0             # Consecutive correct recalls
    next_review: datetime            # When to show again

    # Stats
    total_reviews: int = 0
    correct_reviews: int = 0

    # Timestamps
    created_at: datetime
    updated_at: datetime
```

### Exercise Result (from Mini App via sendData)

```python
class CardResult(BaseModel):
    id: str              # word_id
    rating: int          # 0=Again, 1=Hard, 2=Good, 3=Easy
    time_ms: int | None  # Response time (optional)

class ExerciseResultPayload(BaseModel):
    type: str = "exercise_results"
    results: list[CardResult]
```

---

## Mini App: Flashcards UI

### Card Front

```
┌─────────────────────────┐
│                         │
│       το σπίτι          │  ← Greek word with article
│        (spíti)          │  ← Transliteration
│                         │
│     [ Tap to flip ]     │
│                         │
└─────────────────────────┘
│  ████████░░░░  12/20    │  ← Progress bar
└─────────────────────────┘
```

### Card Back

```
┌─────────────────────────┐
│                         │
│          дом            │  ← Translation
│                         │
│   Το σπίτι είναι        │
│      μεγάλο.            │  ← Example sentence
│   "Дом большой."        │
│                         │
│  ┌──────┐ ┌──────┐     │
│  │Again │ │ Hard │     │  ← SM-2 rating
│  └──────┘ └──────┘     │
│  ┌──────┐ ┌──────┐     │
│  │ Good │ │ Easy │     │
│  └──────┘ └──────┘     │
│                         │
└─────────────────────────┘
│  ████████░░░░  12/20    │
└─────────────────────────┘
```

### Summary Screen (before sendData)

```
┌─────────────────────────┐
│                         │
│     🎉 Готово!          │
│                         │
│   Карточек: 20          │
│   Again: 2              │
│   Hard:  3              │
│   Good:  10             │
│   Easy:  5              │
│                         │
│   Время: 4:32           │
│                         │
│   ┌───────────────┐     │
│   │   Завершить   │     │  ← calls sendData()
│   └───────────────┘     │
│                         │
└─────────────────────────┘
```

### Tech Stack

- **Vanilla JS** — no framework, minimal bundle for fast WebView loading
- **Telegram Web App SDK** (`telegram-web-app.js`)
- **pako.js** — gzip decompression for URL payload
- CSS with Telegram theme variables (`var(--tg-theme-bg-color)`, etc.)
- Touch: tap to flip, swipe gestures optional

---

## SM-2 Algorithm (Spaced Repetition)

```python
def update_sm2(entry: VocabularyEntry, rating: int) -> VocabularyEntry:
    """
    rating: 0=Again, 1=Hard, 2=Good, 3=Easy
    Mapped to SM-2 quality: 0→0, 1→3, 2→4, 3→5
    """
    quality_map = {0: 0, 1: 3, 2: 4, 3: 5}
    q = quality_map[rating]

    # Update easiness factor
    entry.easiness_factor = max(
        1.3,
        entry.easiness_factor + (0.1 - (5 - q) * (0.08 + (5 - q) * 0.02))
    )

    if q < 3:  # Failed recall
        entry.repetitions = 0
        entry.interval = 1
    else:
        if entry.repetitions == 0:
            entry.interval = 1
        elif entry.repetitions == 1:
            entry.interval = 6
        else:
            entry.interval = round(entry.interval * entry.easiness_factor)
        entry.repetitions += 1

    entry.next_review = now() + timedelta(days=entry.interval)
    entry.total_reviews += 1
    if rating >= 2:
        entry.correct_reviews += 1
    entry.updated_at = now()

    return entry
```

---

## Agent Tools (Capability Manifest)

| Tool | Description | Parameters |
|------|-------------|------------|
| `add_vocabulary` | Add word to user's vocabulary | word, transliteration, translation, part_of_speech, gender?, article?, example_sentence?, example_translation?, tags? |
| `search_vocabulary` | Search/browse user's vocabulary | query?, tags?, limit? |
| `get_due_words` | Get words due for review today | limit?, tags? |
| `start_exercise` | Build exercise payload, return WebApp button | type="flashcards", limit?, tags? |
| `process_exercise_results` | Update SM-2 after Mini App returns results | results (from web_app_data) |
| `get_progress` | Learning statistics | period? (week/month/all) |

### Capability Manifest Structure

```yaml
id: language_learning
name: Language Learning
description: >
  Greek vocabulary learning with spaced repetition flashcards.
  Can add words, schedule reviews, launch flashcard exercises via Telegram Mini App,
  and track learning progress.

tools:
  - name: add_vocabulary
    description: Add a new word to the user's Greek vocabulary
    parameters:
      word: { type: string, required: true, description: "Greek word" }
      transliteration: { type: string, required: true, description: "Latin transliteration" }
      translation: { type: string, required: true, description: "Russian translation" }
      part_of_speech: { type: string, required: true, enum: [noun, verb, adjective, adverb, phrase, other] }
      gender: { type: string, required: false, enum: [m, f, n], description: "Noun gender" }
      article: { type: string, required: false, enum: ["ο", "η", "το"], description: "Greek article" }
      example_sentence: { type: string, required: false }
      example_translation: { type: string, required: false }
      tags: { type: array, items: string, required: false }

  - name: start_exercise
    description: >
      Launch a flashcard exercise. Returns a Telegram WebApp button.
      The Mini App runs offline; results come back via sendData().
    parameters:
      limit: { type: integer, required: false, default: 20, description: "Max words" }
      tags: { type: array, items: string, required: false }

  # ... other tools
```

---

## Channel Integration

### New: WebApp Button Response

The agent needs a way to send a Telegram message with a **WebApp keyboard button**.
This requires a new response type in the channel layer:

```python
# New response type for channel
class WebAppButtonResponse:
    text: str                  # Message text ("Нажми чтобы начать!")
    button_text: str           # Button label ("🃏 Карточки")
    web_app_url: str           # Mini App URL with encoded data
```

### New: web_app_data Handler

When user completes exercise, Telegram sends `message.web_app_data`:

```python
# In Telegram channel adapter
if update.message and update.message.web_app_data:
    data = json.loads(update.message.web_app_data.data)
    if data.get("type") == "exercise_results":
        # Route to agent for processing
        # Agent calls process_exercise_results tool
```

---

## GitHub Pages Setup

### Repository Structure

```
private-claw-apps/          (separate repo or gh-pages branch)
├── flashcards/
│   ├── index.html
│   ├── app.js              ← Main logic
│   ├── style.css           ← Telegram-themed styles
│   └── lib/
│       ├── telegram-web-app.js
│       └── pako.min.js     ← gzip decompression
├── shared/
│   ├── theme.css           ← Shared Telegram theme variables
│   └── utils.js            ← Shared encoding/decoding
└── README.md
```

**URL:** `https://yogson.github.io/private-claw-apps/flashcards/`

---

## Greek-Specific Considerations

- **Transliteration is mandatory** — stored for every word (user is a beginner)
- **Articles with nouns** — always store and display: ο (m), η (f), το (n)
- **Stress marks** — preserve accents in both Greek and transliteration (σπίτι → spíti)
- **UTF-8** — full support throughout pipeline (JSON encoding, URL encoding, display)
- **Font** — system fonts work for Greek; CSS fallback: `'Noto Sans', sans-serif`

---

## Implementation Order

### Step 1: Vocabulary Store + SM-2 Engine
- `VocabularyEntry` model
- JSON file storage (one file per user)
- SM-2 update logic
- CRUD operations + due words query

### Step 2: Capability Manifest + Agent Tools
- `language_learning.yaml` manifest
- Tool implementations: `add_vocabulary`, `search_vocabulary`, `get_due_words`, `get_progress`
- Test adding words via chat

### Step 3: Mini App (GitHub Pages)
- Flashcards HTML/JS/CSS
- URL payload decoding (base64 + gzip)
- Card flip UI with SM-2 rating buttons
- Summary screen + `sendData()`
- Deploy to GitHub Pages

### Step 4: Channel Integration
- `start_exercise` tool: builds payload, returns WebApp button
- WebApp button response type in Telegram channel
- `web_app_data` handler in Telegram channel
- `process_exercise_results` tool: SM-2 update from results

### Step 5: End-to-End Testing
- Add words via chat → launch exercise → flip cards → get results → verify SM-2 update

---

## Future Phases

### Phase 2: Quiz Mini App + Grammar
- Multiple choice quizzes (Greek → Russian, Russian → Greek)
- Audio pronunciation (TTS)
- Verb conjugation drills

### Phase 3: Advanced Exercises
- Word matching
- Sentence builder
- Listening comprehension
- Analytics dashboard
- Adaptive difficulty

---

## Open Questions

1. **Storage backend** — JSON files (simple, matches existing store patterns) vs SQLite?
2. **Reverse cards** — show Russian → Greek direction too, or only Greek → Russian for now?
3. **Bulk import** — support importing word lists (CSV) from textbooks?
4. **GitHub Pages repo** — separate repo `private-claw-apps` or `gh-pages` branch in main repo?
