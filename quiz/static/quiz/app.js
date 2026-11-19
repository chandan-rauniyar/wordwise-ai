/* WordWise front end. Progress, the word list and the audio voice all stay in this browser. */
(() => {
  'use strict';

  const STORE_KEY = 'wordwise.progress.v1';
  const LIST_KEY = 'wordwise.wordlist.v1';
  const LEVEL_KEY = 'wordwise.difficulty';
  const TOPIC_KEY = 'wordwise.topic';
  const MAX_SEEN = 500;
  const MAX_WRONG = 100;
  const MAX_LIST = 20;

  const $ = (id) => document.getElementById(id);
  const views = { home: $('view-home'), loading: $('view-loading'), quiz: $('view-quiz'), results: $('view-results') };

  // ---------- Small helpers ----------

  function recall(key) {
    try { return localStorage.getItem(key); } catch (e) { return null; }
  }

  function remember(key, value) {
    try { localStorage.setItem(key, value); } catch (e) { /* storage blocked: keep going in memory */ }
  }

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  const cap = (s) => s.charAt(0).toUpperCase() + s.slice(1);
  const sentence = (s) => cap(s) + (/[.!?]$/.test(s) ? '' : '.');

  function isWord(text) {
    return text.length >= 2 && text.length <= 30 && /^[A-Za-z]+(?:['-][A-Za-z]+)*$/.test(text);
  }

  function getCookie(name) {
    const hit = document.cookie.split('; ').find((row) => row.startsWith(name + '='));
    return hit ? decodeURIComponent(hit.split('=')[1]) : '';
  }

  async function api(path, body) {
    let res;
    try {
      res = await fetch(path, {
        method: 'POST',
        credentials: 'same-origin',
        headers: { 'Content-Type': 'application/json', 'X-CSRFToken': getCookie('csrftoken') },
        body: JSON.stringify(body),
      });
    } catch (e) {
      throw new Error('Could not reach the server. Check your connection and try again.');
    }
    let data = null;
    try { data = await res.json(); } catch (e) { /* not JSON */ }
    if (!res.ok) throw new Error((data && data.error) || 'Something went wrong. Please try again.');
    return data;
  }

  // ---------- Audio: the browser's own speech voices, nothing is sent anywhere ----------

  const canSpeak = 'speechSynthesis' in window && typeof window.SpeechSynthesisUtterance === 'function';
  if (!canSpeak) document.documentElement.classList.add('no-speech');
  let voice = null;

  function chooseVoice() {
    if (!canSpeak) return;
    const wanted = (navigator.language || 'en-US').toLowerCase();
    const english = speechSynthesis.getVoices().filter((v) => v.lang && v.lang.toLowerCase().startsWith('en'));
    if (!english.length) { voice = null; return; }
    const rank = (v) => {
      const lang = v.lang.toLowerCase().replace('_', '-');
      return (lang === wanted ? 4 : 0) + (lang === 'en-us' ? 1 : 0) + (/google|natural|neural|online/i.test(v.name) ? 2 : 0);
    };
    voice = english.slice().sort((a, b) => rank(b) - rank(a))[0];
  }

  if (canSpeak) {
    chooseVoice();
    if (typeof speechSynthesis.addEventListener === 'function') speechSynthesis.addEventListener('voiceschanged', chooseVoice);
  }

  function stopSpeech() {
    if (!canSpeak) return;
    try { speechSynthesis.cancel(); } catch (e) { /* audio is optional */ }
    document.querySelectorAll('.speak.playing').forEach((b) => b.classList.remove('playing'));
  }

  function speak(text, button) {
    if (!canSpeak || !text) return;
    try {
      stopSpeech();
      const utterance = new SpeechSynthesisUtterance(text);
      utterance.lang = voice ? voice.lang : 'en-US';
      if (voice) utterance.voice = voice;
      utterance.rate = /\s/.test(text) ? 0.95 : 0.85;
      if (button) {
        button.classList.add('playing');
        const done = () => button.classList.remove('playing');
        utterance.onend = done;
        utterance.onerror = done;
      }
      speechSynthesis.speak(utterance);
    } catch (e) { /* audio is optional */ }
  }

  function addSpeakerIcon(button) {
    const ns = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', '0 0 24 24');
    svg.setAttribute('width', '18');
    svg.setAttribute('height', '18');
    svg.setAttribute('aria-hidden', 'true');
    const body = document.createElementNS(ns, 'path');
    body.setAttribute('d', 'M3 9v6h4l5 4V5L7 9H3z');
    body.setAttribute('fill', 'currentColor');
    const waves = document.createElementNS(ns, 'path');
    waves.setAttribute('d', 'M15.5 8.5a5 5 0 0 1 0 7M18.5 5.5a9 9 0 0 1 0 13');
    waves.setAttribute('fill', 'none');
    waves.setAttribute('stroke', 'currentColor');
    waves.setAttribute('stroke-width', '2');
    waves.setAttribute('stroke-linecap', 'round');
    svg.append(body, waves);
    button.prepend(svg);
  }

  /** Wire an existing button so it speaks whatever `getText()` returns. */
  function wireSpeaker(button, getText) {
    addSpeakerIcon(button);
    button.addEventListener('click', () => speak(getText(), button));
  }

  /** A small round speaker button for explanations. */
  function speakerButton(label, text) {
    const button = el('button', 'speak sm');
    button.type = 'button';
    button.setAttribute('aria-label', label);
    wireSpeaker(button, () => text);
    return button;
  }

  // ---------- Progress, stored in the browser only ----------

  function freshProgress() { return { seen: [], wrong: {}, totalScore: 0, rounds: 0 }; }

  function loadProgress() {
    try {
      const p = JSON.parse(recall(STORE_KEY) || 'null');
      if (p && Array.isArray(p.seen) && p.wrong && typeof p.wrong === 'object') {
        return {
          seen: p.seen.filter((s) => typeof s === 'string').slice(-MAX_SEEN),
          wrong: p.wrong,
          totalScore: Number(p.totalScore) || 0,
          rounds: Number(p.rounds) || 0,
        };
      }
    } catch (e) { /* corrupt: start fresh */ }
    return freshProgress();
  }

  let progress = loadProgress();

  function saveProgress() { remember(STORE_KEY, JSON.stringify(progress)); }

  function recordAnswer(id, correct, points) {
    if (!progress.seen.includes(id)) progress.seen.push(id);
    progress.seen = progress.seen.slice(-MAX_SEEN);
    if (correct) {
      delete progress.wrong[id];
    } else {
      const old = progress.wrong[id];
      progress.wrong[id] = { count: (old ? old.count : 0) + 1, last: Date.now() };
      const ids = Object.keys(progress.wrong);
      if (ids.length > MAX_WRONG) {
        ids.sort((a, b) => progress.wrong[a].last - progress.wrong[b].last)
          .slice(0, ids.length - MAX_WRONG)
          .forEach((k) => delete progress.wrong[k]);
      }
    }
    progress.totalScore += points;
    saveProgress();
  }

  function wrongIds() {
    return Object.keys(progress.wrong).sort((a, b) => progress.wrong[b].last - progress.wrong[a].last).slice(0, MAX_WRONG);
  }

  // ---------- The user's own word list, stored in the browser only ----------

  function loadList() {
    try {
      const list = JSON.parse(recall(LIST_KEY) || '[]');
      return Array.isArray(list) ? list.filter((w) => typeof w === 'string' && isWord(w)).slice(0, MAX_LIST) : [];
    } catch (e) { return []; }
  }

  let wordList = loadList();

  /** Split pasted text into valid words, plus the entries that cannot be used. */
  function parseWords(text) {
    const words = [];
    const rejected = [];
    let extra = 0;
    text.split(/[\s,;]+/).filter(Boolean).forEach((token) => {
      const cleaned = token.replace(/^[^A-Za-z0-9]+|[^A-Za-z0-9]+$/g, '');
      if (!cleaned) return;
      if (!isWord(cleaned)) { if (rejected.length < 5) rejected.push(cleaned); return; }
      const word = cleaned.toLowerCase();
      if (words.includes(word)) return;
      if (words.length >= MAX_LIST) { extra += 1; return; }
      words.push(word);
    });
    return { words, rejected, extra };
  }

  function updateListCount() {
    const n = parseWords($('listInput').value).words.length;
    $('listCount').textContent = n ? '(' + n + (n === 1 ? ' word)' : ' words)') : '';
  }

  // ---------- Views ----------

  function show(name) {
    stopSpeech();
    Object.entries(views).forEach(([key, node]) => { node.hidden = key !== name; });
    window.scrollTo(0, 0);
  }

  function showError(node, message) {
    node.textContent = message;
    node.hidden = !message;
  }

  function difficulty() {
    const checked = document.querySelector('input[name="difficulty"]:checked');
    return checked ? checked.value : 'EASY';
  }

  function topic() { return $('topicSelect').value || ''; }

  function topicLabel() {
    const option = $('topicSelect').selectedOptions[0];
    return option ? option.textContent : '';
  }

  // ---------- Home ----------

  function updateChip() {
    const chip = $('scoreChip');
    chip.hidden = progress.totalScore <= 0;
    chip.textContent = progress.totalScore.toLocaleString() + ' points';
  }

  function updateStartLabel() {
    const word = $('wordInput').value.trim();
    $('startBtn').textContent = word ? 'Practise ' + word : 'Start quiz';
  }

  function showHome(error) {
    const n = Object.keys(progress.wrong).length;
    const note = $('reviewNote');
    note.hidden = n === 0;
    if (n) note.textContent = n === 1 ? '1 question you missed will come back for review.' : n + ' questions you missed will come back for review.';
    showError($('homeError'), error || '');
    showError($('listError'), '');
    $('startBtn').disabled = false;
    $('listStart').disabled = false;
    $('wotdPractise').disabled = false;
    updateChip();
    updateStartLabel();
    updateListCount();
    show('home');
  }

  $('wordInput').addEventListener('input', updateStartLabel);
  $('listInput').addEventListener('input', updateListCount);

  $('startForm').addEventListener('submit', (event) => {
    event.preventDefault();
    const word = $('wordInput').value.trim();
    if (word && !isWord(word)) {
      showError($('homeError'), 'Enter one word, using letters only.');
      return;
    }
    remember(LEVEL_KEY, difficulty());
    remember(TOPIC_KEY, topic());
    startRound({ word });
  });

  $('topicSelect').addEventListener('change', () => remember(TOPIC_KEY, topic()));

  $('listStart').addEventListener('click', () => {
    const { words, rejected, extra } = parseWords($('listInput').value);
    if (!words.length) {
      showError($('listError'), rejected.length
        ? 'These are not usable words: ' + rejected.join(', ') + '. Use letters only.'
        : 'Add at least one word to your list.');
      return;
    }
    wordList = words;
    remember(LIST_KEY, JSON.stringify(wordList));
    $('listInput').value = wordList.join('\n');
    updateListCount();
    const notes = [];
    if (rejected.length) notes.push('Left out: ' + rejected.join(', ') + '.');
    if (extra) notes.push('Only the first ' + MAX_LIST + ' words are used.');
    showError($('listError'), notes.join(' '));
    remember(LEVEL_KEY, difficulty());
    startRound({ words: wordList });
  });

  $('listClear').addEventListener('click', () => {
    wordList = [];
    remember(LIST_KEY, '[]');
    $('listInput').value = '';
    showError($('listError'), '');
    updateListCount();
  });

  $('clearBtn').addEventListener('click', () => {
    if (window.confirm('Clear your points and review list from this browser?')) {
      progress = freshProgress();
      saveProgress();
      showHome();
    }
  });

  $('brand').addEventListener('click', (event) => { event.preventDefault(); showHome(); });
  $('homeBtn').addEventListener('click', () => { $('wordInput').value = ''; showHome(); });

  const savedLevel = recall(LEVEL_KEY);
  const levelRadio = savedLevel && document.querySelector('input[name="difficulty"][value="' + savedLevel + '"]');
  if (levelRadio) levelRadio.checked = true;
  const savedTopic = recall(TOPIC_KEY);
  if (savedTopic && Array.from($('topicSelect').options).some((o) => o.value === savedTopic)) $('topicSelect').value = savedTopic;
  $('listInput').value = wordList.join('\n');

  // ---------- Word of the day ----------

  let wordOfTheDay = null;

  async function loadWordOfTheDay() {
    try {
      const now = new Date();
      const day = now.getFullYear() + '-' + String(now.getMonth() + 1).padStart(2, '0') + '-' + String(now.getDate()).padStart(2, '0');
      const res = await fetch('/api/word-of-the-day?date=' + day, { credentials: 'same-origin' });
      if (!res.ok) return;
      const data = await res.json();
      if (!data || typeof data.word !== 'string') return;
      wordOfTheDay = data;
      $('wotdWord').textContent = data.word;
      $('wotdDef').textContent = sentence(data.definition || '');
      $('wotdExample').textContent = data.example ? 'For example: “' + data.example + '”' : '';
      $('wotdExample').hidden = !data.example;
      $('wotd').hidden = false;
    } catch (e) { /* the card is optional */ }
  }

  wireSpeaker($('wotdSpeak'), () => (wordOfTheDay ? wordOfTheDay.word : ''));

  $('wotdPractise').addEventListener('click', () => {
    if (!wordOfTheDay) return;
    $('wordInput').value = wordOfTheDay.word;
    remember(LEVEL_KEY, difficulty());
    startRound({ word: wordOfTheDay.word });
  });

  // ---------- Round ----------

  let round = null;

  function loadingMessage(opts) {
    if (opts.words) return 'Preparing questions for your list…';
    if (opts.word) return 'Preparing questions for “' + opts.word + '”…';
    return topic() ? 'Preparing ' + topicLabel().toLowerCase() + ' questions…' : 'Preparing your questions…';
  }

  async function startRound(opts) {
    const options = opts || {};
    $('loadingText').textContent = loadingMessage(options);
    $('startBtn').disabled = true;
    $('listStart').disabled = true;
    $('wotdPractise').disabled = true;
    show('loading');
    try {
      const data = await api('/api/round/start', {
        word: options.word || null,
        words: options.words || null,
        topic: topic() || null,
        difficulty: difficulty(),
        seenIds: progress.seen.slice(-MAX_SEEN),
        wrongIds: wrongIds(),
      });
      round = {
        token: data.roundToken, questions: data.questions, notice: data.notice, index: 0, answers: [], points: 0,
        busy: false, answered: false, listenText: '', mode: options.words ? 'list' : options.word ? 'word' : 'mix',
      };
      renderQuestion();
    } catch (e) {
      showHome(e.message);
    }
  }

  function renderSegments() {
    const list = $('segments');
    list.replaceChildren();
    round.questions.forEach((q, i) => {
      const li = el('li');
      const done = round.answers[i];
      if (done) li.className = done.skipped ? 'skip' : done.correct ? 'ok' : 'no';
      else if (i === round.index) li.className = 'current';
      list.appendChild(li);
    });
  }

  /** Put a sentence into `node`, turning ___ into a drawn blank. */
  function fillSentence(node, text) {
    node.replaceChildren();
    text.split('___').forEach((part, i, all) => {
      node.appendChild(document.createTextNode(part));
      if (i < all.length - 1) {
        const blank = el('span', 'blank');
        blank.setAttribute('aria-label', 'blank');
        node.appendChild(blank);
      }
    });
  }

  const WORD_PROMPTS = {
    SYNONYM: { re: /^Which word is (?:similar to|the opposite of) (.+)\?$/, lead: (q) => (/similar to/.test(q.text) ? 'Similar to' : 'Opposite of') },
    ANTONYM: { re: /^Which word is (?:similar to|the opposite of) (.+)\?$/, lead: (q) => (/similar to/.test(q.text) ? 'Similar to' : 'Opposite of') },
    MEANING: { re: /^What does (.+) mean\?$/, lead: () => 'What does this word mean?' },
  };
  const SENTENCE_LEADS = {
    FILL_BLANK: 'Fill in the blank',
    WORD_FORM: 'Choose the right form',
    ODD_ONE_OUT: 'Odd one out',
    SPELLING: 'Spelling',
  };

  function renderPrompt(q) {
    const lead = $('promptLead');
    const line = $('promptLine');
    const body = $('promptSentence');
    const spec = WORD_PROMPTS[q.type];
    const match = spec && q.text.match(spec.re);
    $('prompt').setAttribute('aria-label', q.text);

    if (match) {
      lead.hidden = false;
      lead.textContent = spec.lead(q);
      line.hidden = false;
      $('promptWord').textContent = match[1];
      body.hidden = true;
      return match[1];
    }
    line.hidden = true;
    body.hidden = false;
    lead.hidden = !SENTENCE_LEADS[q.type];
    lead.textContent = SENTENCE_LEADS[q.type] || '';
    fillSentence(body, q.text);
    return '';
  }

  let promptWordText = '';

  function renderQuestion() {
    const q = round.questions[round.index];
    round.answered = false;
    round.busy = false;

    $('qCount').textContent = 'Question ' + (round.index + 1) + ' of ' + round.questions.length;
    $('reviewTag').hidden = !q.review;
    promptWordText = renderPrompt(q);

    const options = $('options');
    const many = q.options.length > 2;
    options.replaceChildren();
    options.classList.toggle('many', many);
    q.options.forEach((text, i) => {
      if (!many && i > 0) options.appendChild(el('div', 'or', 'or'));
      const btn = el('button', 'opt' + (text.length > 30 ? ' long' : ''));
      btn.type = 'button';
      btn.dataset.value = text;
      btn.appendChild(el('span', 'label', cap(text)));
      btn.addEventListener('click', () => submitAnswer(text));
      options.appendChild(btn);
    });

    $('feedback').hidden = true;
    showError($('quizError'), '');
    $('skipBtn').hidden = false;
    $('nextBtn').hidden = true;
    renderSegments();
    show('quiz');
    $('prompt').focus({ preventScroll: true });
  }

  wireSpeaker($('promptSpeak'), () => promptWordText);
  wireSpeaker($('listenBtn'), () => (round ? round.listenText : ''));

  function optionButtons() { return Array.from(document.querySelectorAll('#options .opt')); }

  async function submitAnswer(chosen) {
    if (round.busy || round.answered) return;
    round.busy = true;
    const q = round.questions[round.index];
    const buttons = optionButtons();
    buttons.forEach((b) => { b.disabled = true; });
    $('skipBtn').disabled = true;
    showError($('quizError'), '');

    let result;
    try {
      result = await api('/api/answer', { roundToken: round.token, questionId: q.id, chosen });
    } catch (e) {
      round.busy = false;
      buttons.forEach((b) => { b.disabled = false; });
      $('skipBtn').disabled = false;
      showError($('quizError'), e.message);
      return;
    }

    round.busy = false;
    round.answered = true;
    round.points += result.points;
    round.answers[round.index] = { q, chosen, correct: result.correct, skipped: result.skipped, correctAnswer: result.correctAnswer, explanation: result.explanation };
    recordAnswer(q.id, result.correct, result.points);
    updateChip();

    buttons.forEach((b) => {
      const isRight = b.dataset.value === result.correctAnswer;
      if (!result.skipped && b.dataset.value === chosen) {
        b.classList.add(result.correct ? 'is-correct' : 'is-wrong');
        b.appendChild(el('span', 'mark', result.correct ? '✓' : '✕'));
      } else if (isRight) {
        b.classList.add(result.skipped ? 'reveal' : 'is-correct');
      }
    });

    const box = $('feedback');
    box.hidden = false;
    box.className = 'feedback ' + (result.skipped ? 'skip' : result.correct ? 'good' : 'bad');
    $('feedbackText').textContent = result.skipped ? 'Skipped. The answer is ' + result.correctAnswer + '.'
      : result.correct ? 'Correct. +' + result.points
      : 'Not quite. The answer is ' + result.correctAnswer + '.';
    // Read the completed sentence for blank questions, otherwise the answer itself.
    round.listenText = result.explanation.answerSentence && /^(FILL_BLANK|WORD_FORM)$/.test(q.type)
      ? result.explanation.answerSentence
      : result.correctAnswer;

    $('skipBtn').hidden = true;
    $('skipBtn').disabled = false;
    const last = round.index === round.questions.length - 1;
    $('nextBtn').textContent = last ? 'See results' : 'Next';
    $('nextBtn').hidden = false;
    renderSegments();
    $('nextBtn').focus();
  }

  $('skipBtn').addEventListener('click', () => submitAnswer(null));

  $('nextBtn').addEventListener('click', () => {
    if (!round.answered) return;
    if (round.index === round.questions.length - 1) { showResults(); return; }
    round.index += 1;
    renderQuestion();
  });

  document.addEventListener('keydown', (event) => {
    if (views.quiz.hidden || !round || round.answered || event.ctrlKey || event.metaKey || event.altKey) return;
    const i = Number(event.key) - 1;
    const buttons = optionButtons();
    if (i >= 0 && i < buttons.length) buttons[i].click();
  });

  // ---------- Results ----------

  function explanationBody(a) {
    const body = el('div', 'detail-body');
    const answerRow = el('div', 'answer-row');
    const spoken = a.explanation.answerSentence || a.correctAnswer;
    answerRow.appendChild(el('p', 'answer-line', a.explanation.answerSentence || 'Answer: ' + cap(a.correctAnswer)));
    answerRow.appendChild(speakerButton('Hear the answer', /^(FILL_BLANK|WORD_FORM)$/.test(a.q.type) ? spoken : a.correctAnswer));
    body.appendChild(answerRow);

    a.explanation.sections.forEach((s) => {
      const head = el('div', 'sec-head');
      head.appendChild(el('h4', null, s.heading));
      if (s.word) head.appendChild(speakerButton('Hear ' + s.word, s.word));
      body.appendChild(head);
      body.appendChild(el('p', null, sentence(s.text)));
      if (s.example) {
        const row = el('div', 'example-row');
        row.appendChild(el('p', 'example', 'For example: “' + s.example + '”'));
        row.appendChild(speakerButton('Hear the example', s.example));
        body.appendChild(row);
      }
    });
    return body;
  }

  function showResults() {
    const total = round.questions.length;
    const correct = round.answers.filter((a) => a && a.correct).length;
    const missed = round.answers.filter((a) => a && !a.correct).length;
    progress.rounds += 1;
    saveProgress();

    $('ring').style.setProperty('--p', Math.round((correct / total) * 100) + '%');
    $('ringText').textContent = correct + '/' + total;
    $('scoreLine').textContent = 'Score ' + round.points;
    $('scoreMessage').textContent = correct === total ? 'Perfect round' : correct >= 4 ? 'Great work' : correct >= 3 ? 'Good going' : 'Keep practising';

    const note = $('scoreNote');
    const parts = [];
    if (missed) parts.push(missed === 1 ? '1 question will come back for review.' : missed + ' questions will come back for review.');
    if (round.notice) parts.push(round.notice);
    note.hidden = parts.length === 0;
    note.textContent = parts.join(' ');

    const list = $('explanations');
    list.replaceChildren();
    round.answers.forEach((a) => {
      const details = el('details');
      const summary = el('summary');
      const status = el('span', 'status ' + (a.skipped ? 'skip' : a.correct ? 'ok' : 'no'), a.skipped ? '–' : a.correct ? '✓' : '✕');
      status.setAttribute('aria-label', a.skipped ? 'Skipped' : a.correct ? 'Correct' : 'Incorrect');
      summary.append(status, el('span', 'title', a.explanation.title), el('span', 'chev', '⌄'));
      details.appendChild(summary);
      details.appendChild(explanationBody(a));
      list.appendChild(details);
    });

    show('results');
  }

  $('nextRoundBtn').addEventListener('click', () => {
    startRound(round && round.mode === 'list' && wordList.length ? { words: wordList } : {});
  });

  showHome();
  loadWordOfTheDay();
})();