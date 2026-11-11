const language = document.documentElement.lang.startsWith('zh') ? 'zh' : 'en';
const t = (english, chinese) => language === 'zh' ? chinese : english;
const MAX_PLAYERS = 21;

const questions = [
  {key: 'players', label: t('Players', '人数'), title: t('How many people are coming?', '今晚有多少人到场？'), description: t('Enter the total number of people, including anyone who might host.', '填写实际到场人数，包括愿意当主持的人。')},
  {key: 'has_host', label: t('Host', '主持'), title: t('Can someone host the game?', '有人愿意当独立主持吗？'), description: t('Some games need a dedicated host to run the session.', '有些游戏需要一位主持人推进流程。'), options: [[false, t('No dedicated host', '没人当主持')], [true, t('Someone can host', '有人愿意主持')]]},
  {key: 'has_dedicated_components', label: t('Equipment', '道具'), title: t('Can you get game-specific equipment?', '能准备专用游戏道具吗？'), description: t('Playing cards, ordinary dice, and pen and paper count as basic supplies.', '普通扑克牌、骰子和纸笔属于通用用品。'), options: [[false, t('Basic supplies only', '只用通用用品')], [true, t('Game-specific equipment is available', '可以准备专用道具')]]},
  {key: 'scene', label: t('Occasion', '场合'), title: t('What’s the occasion?', '你们为什么聚在一起？'), description: t('Pick the closest match.', '选一个最接近今晚的场合。'), options: [['friends', t('Friends’ get-together', '朋友聚会')], ['date', t('Date night', '约会时光')], ['family', t('Family time', '家庭时间')], ['new_people', t('Meeting new people', '认识新朋友')]]},
  {key: 'mood', label: t('Mood', '氛围'), title: t('What kind of game are you in the mood for?', '想要什么样的游戏气氛？'), description: t('Choose the mood that suits your group.', '选你们当下最想要的氛围。'), options: [['relaxed', t('Relaxed conversation', '轻松聊天')], ['lively', t('Lively icebreakers', '热闹破冰')], ['thinking', t('Quiet brain teasers', '安静动脑')], ['competitive', t('Friendly competition', '刺激对抗')]]},
  {key: 'free_text', label: t('Anything else', '其他想法'), title: t('Anything else you’d like to add?', '还有什么想说的？'), description: t('Optional · Up to 200 characters.', '选填，不超过 200 字。'), optional: true}
];

const $ = (id) => document.getElementById(id);
const screens = ['home', 'quiz', 'loading', 'results', 'detail', 'play', 'team', 'settings', 'login'];
const accents = ['game-signal', 'game-moon', 'game-flash'];
const symbols = ['✳', '☾', '↗'];
let step = 0;
let answers = {};
let pending = null;
let explanationRequest = null;
const explanations = new Map();
let activeCard = null;
let activeGame = null;
let latestResult = null;
let shownIds = [];
let teamReturn = 'home';
let liveTeamReturn = null;
const APP_STATE_KEY = 'game-night-state-v1';

function saveAppState(screen = document.body.dataset.screen || 'home') {
  sessionStorage.setItem(APP_STATE_KEY, JSON.stringify({answers, step, latestResult, shownIds, activeGame, screen, teamReturn}));
}

function element(tag, content, className) {
  const node = document.createElement(tag);
  node.textContent = content;
  if (className) node.className = className;
  return node;
}

function showScreen(name, addHistory = true) {
  if (name !== 'detail' && name !== 'team' && explanationRequest) {
    explanationRequest.abort();
    explanationRequest = null;
  }
  if (name !== 'loading' && pending) {
    pending.abort();
    pending = null;
  }
  if (name !== 'play' && name !== 'team') cancelPlayPreparation();
  for (const screen of screens) {
    const node = $(screen + '-screen');
    node.hidden = screen !== name;
    node.classList.toggle('active', screen === name);
  }
  document.body.dataset.screen = name;
  if (name === 'results') resizeRefineInput();
  if (name !== 'loading') {
    saveAppState();
    if (addHistory && history.state?.screen !== name) history.pushState({screen: name}, '', '#' + name);
  }
  window.scrollTo(0, 0);
  const heading = $(name + '-title');
  if (heading) {
    heading.tabIndex = -1;
    heading.focus({preventScroll: true});
  }
}

function startQuiz() {
  step = 0;
  answers = {};
  $('recommend-error').textContent = '';
  showScreen('quiz');
  renderQuestion();
}

function isValidPlayerCount(value) {
  return Number.isInteger(value) && value >= 1 && value <= MAX_PLAYERS;
}

function renderQuestion() {
  if (!isValidPlayerCount(answers.players)) {
    delete answers.players;
    step = 0;
  }
  const question = questions[step];
  const number = String(step + 1).padStart(2, '0');
  const next = $('quiz-next');
  $('step-count').textContent = number + ' / ' + String(questions.length).padStart(2, '0');
  $('progress-fill').style.width = ((step + 1) / questions.length * 100) + '%';
  $('question-kicker').textContent = t('Question ', '问题 ') + number + ' · ' + question.label;
  $('quiz-title').textContent = question.title;
  $('question-description').textContent = question.description;
  $('quiz-back').textContent = step === 0 ? t('← Home', '← 返回首页') : t('← Back', '← 上一步');
  next.firstChild.nodeValue = step === questions.length - 1 ? t('See recommendations ', '查看推荐 ') : t('Next ', '下一步 ');
  next.disabled = !question.optional && answers[question.key] === undefined;
  const grid = $('option-grid');
  grid.replaceChildren();

  if (question.optional) {
    $('question-helper').textContent = t('You can leave this blank.', '可以直接跳过。');
    const input = element('textarea', '', 'free-text-input');
    input.id = 'free-text';
    input.rows = 4;
    input.maxLength = 200;
    input.value = answers[question.key] ?? '';
    input.setAttribute('aria-label', question.title);
    input.setAttribute('aria-describedby', 'question-helper');
    input.addEventListener('input', () => { answers[question.key] = input.value; saveAppState(); });
    grid.append(input);
  } else if (!question.options) {
    $('question-helper').textContent = t(`Enter a whole number from 1 to ${MAX_PLAYERS}.`, `请输入 1 到 ${MAX_PLAYERS} 的整数`);
    const input = element('input', '', 'player-input');
    input.id = 'player-count';
    input.type = 'number';
    input.min = '1';
    input.max = String(MAX_PLAYERS);
    input.step = '1';
    input.required = true;
    input.inputMode = 'numeric';
    input.value = answers.players ?? '';
    input.setAttribute('aria-label', t('Total number of people', '实际到场人数'));
    input.setAttribute('aria-describedby', 'question-helper');
    input.addEventListener('input', () => {
      const value = input.valueAsNumber;
      if (/^\d+$/.test(input.value) && isValidPlayerCount(value)) answers.players = value;
      else delete answers.players;
      next.disabled = answers.players === undefined;
      saveAppState();
    });
    input.addEventListener('keydown', (event) => {
      if (event.key === 'Enter' && !next.disabled) next.click();
    });
    grid.append(input);
  } else {
    $('question-helper').textContent = t('Choose one option.', '选择最适合的一项');
    for (const [value, label] of question.options) {
      const button = element('button', label, 'option-card option-text');
      button.type = 'button';
      const selected = answers[question.key] === value;
      button.classList.toggle('selected', selected);
      button.setAttribute('aria-pressed', String(selected));
      button.addEventListener('click', () => {
        answers[question.key] = value;
        saveAppState();
        for (const option of grid.children) {
          option.classList.toggle('selected', option === button);
          option.setAttribute('aria-pressed', String(option === button));
        }
        next.disabled = false;
      });
      grid.append(button);
    }
  }
  $('quiz-title').focus();
  saveAppState();
}

async function recommend() {
  $('recommend-error').textContent = '';
  showScreen('loading');
  const request = new AbortController();
  pending = request;
  try {
    const response = await apiFetch('/recommend', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({...answers, language}),
      signal: request.signal
    });
    if (!response.ok) throw await responseError(response);
    const result = await response.json();
    if (pending !== request) return;
    pending = null;
    latestResult = result;
    shownIds = result.recommendations.map(game => game.game_id);
    renderResults(result);
    showScreen('results');
  } catch (error) {
    if (pending !== request) return;
    pending = null;
    if (window.needsLogin?.()) { showScreen('login'); return showAppError(error); }
    showScreen('quiz');
    $('recommend-error').textContent = t('Couldn’t load recommendations. Your choices are saved. Please try again.', '暂时无法获取推荐。你的选择已保留，请再试一次。');
    showAppError(error, recommend);
  }
}

function renderResults(result) {
  const {recommendations, method, eligible_count: count} = result;
  $('results-count').textContent = t('Eligible games / ', '候选游戏 / ') + count;
  $('results-intro').textContent = recommendations.length
    ? t('These games fit your group size, equipment, and preferences.', '根据你们的人数、可用道具和聚会偏好，先看看这几款。')
    : t('No games match these conditions. Try changing the group size, equipment, or host availability.', '当前条件下没有找到可以玩的游戏。你可以调整人数、道具或主持条件后再试。');
  const modelName = {deepseek: 'DeepSeek', gemini: 'Gemini', compatible: t('AI', 'AI 模型'), mock: 'Mock'}[method];
  $('recommend-method').textContent = recommendations.length
    ? (modelName ? modelName + t(' ranked the eligible games for your preferences.', '根据你的偏好对合格候选排序。') : t('Ranked using catalog tags and recommended group sizes.', '根据游戏库的标签与推荐人数排序。'))
    : t('Adjust your preferences to try again.', '请调整条件后重新选择。');
  $('answer-summary').replaceChildren(...questions.map(question => {
    const value = answers[question.key];
    const label = question.optional ? (value || t('Not provided', '未填写'))
      : (question.options?.find(([key]) => key === value)?.[1] ?? (value + t(' players', ' 人')));
    return element('span', question.label + ' · ' + label);
  }));
  $('game-grid').replaceChildren(...recommendations.map((game, index) => {
    const button = element('button', '', 'game-card ' + accents[index]);
    button.type = 'button';
    button.append(
      element('span', String(index + 1).padStart(2, '0') + ' ↗', 'game-card-top'),
      element('span', symbols[index], 'game-card-icon')
    );
    button.children[1].setAttribute('aria-hidden', 'true');
    const content = element('span', '', 'game-card-content');
    content.append(
      element('span', game.categories[0] || t('Board game', '桌游'), 'game-category'),
      element('strong', game.name),
      ...(game.name !== game.english_name ? [element('span', game.english_name, 'game-english')] : []),
      element('span', game.reason, 'game-reason'),
      ...(game.caveat ? [element('span', game.caveat, 'game-caveat')] : []),
      element('span', t('View rules ↗', '查看规则 ↗'), 'game-card-bottom')
    );
    button.append(content);
    button.addEventListener('click', () => {
      activeCard = button;
      openGame(game, index);
    });
    return button;
  }));
}

function openGame(game, index, addHistory = true) {
  activeGame = {game, index};
  $('detail-visual').className = 'detail-visual ' + accents[index];
  $('detail-symbol').textContent = symbols[index];
  $('detail-visual-name').textContent = game.english_name;
  $('detail-kicker').textContent = game.categories.join(' · ');
  $('detail-title').textContent = game.name;
  $('detail-tagline').textContent = game.summary;
  $('detail-tags').replaceChildren(
    element('span', t('Players: ', '可玩人数：') + game.players.join(t(', ', '、'))),
    element('span', game.requires_host ? t('Dedicated host required', '需要独立主持') : t('No dedicated host required', '无需独立主持')),
    element('span', game.requires_dedicated_components ? t('Game-specific equipment required', '需要专用道具') : t('Basic supplies are enough', '可用通用用品'))
  );
  $('detail-description').textContent = game.core_rules;
  $('detail-scenario').textContent = game.detailed_rules;
  $('detail-reason').textContent = game.reason;
  $('player-form').hidden = true;
  $('play-requirement').hidden = true;
  showScreen('detail', addHistory);
  loadExplanation(game.game_id);
  loadGameExtras(game.game_id);
}

async function loadGameExtras(gameId) {
  $('detail-setup').textContent = $('detail-win').textContent = '…';
  try {
    const response = await apiFetch('/api/games/' + encodeURIComponent(gameId) + '?lang=' + language);
    if (!response.ok) throw await responseError(response);
    const game = await response.json();
    if (activeGame?.game.game_id !== gameId) return;
    $('detail-setup').textContent = game.setup || t('Unavailable in this language.', '暂无资料。');
    $('detail-win').textContent = game.win || t('Unavailable in this language.', '暂无资料。');
  } catch (error) {
    if (activeGame?.game.game_id !== gameId) return;
    $('detail-setup').textContent = $('detail-win').textContent = t('Could not load.', '暂时无法加载。');
    showAppError(error, () => loadGameExtras(gameId));
  }
}

async function loadExplanation(gameId) {
  explanationRequest?.abort();
  explanationRequest = null;
  const output = $('detail-ai-explanation');
  if (explanations.has(gameId)) {
    output.textContent = explanations.get(gameId);
    return;
  }
  output.textContent = t('Generating your beginner guide…', '正在生成新手讲解…');
  const request = new AbortController();
  explanationRequest = request;
  try {
    const response = await apiFetch('/rules/explain', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        game_id: gameId, language, style: 'step_by_step', mode: 'llm', include_detailed: true
      }),
      signal: request.signal
    });
    if (!response.ok) throw await responseError(response);
    const {answer} = await response.json();
    if (explanationRequest !== request) return;
    explanations.set(gameId, answer);
    output.textContent = answer;
  } catch (error) {
    if (explanationRequest === request) {
      output.textContent = t('The AI guide is temporarily unavailable. You can read the rules below.', '暂时无法生成讲解，可以先阅读下方的游戏规则。');
      if (error.status !== 503) showAppError(error, () => loadExplanation(gameId));
    }
  } finally {
    if (explanationRequest === request) explanationRequest = null;
  }
}

$('brand-home').addEventListener('click', event => {
  event.preventDefault();
  showScreen('home');
});
$('header-explore').addEventListener('click', () => {
  const current = document.body.dataset.screen || 'home';
  if (current === 'team') return;
  teamReturn = current === 'loading' ? history.state?.screen || 'home' : current;
  liveTeamReturn = teamReturn;
  if (history.state?.screen !== teamReturn) {
    history.replaceState({screen: teamReturn}, '', '#' + teamReturn);
  }
  history.pushState({screen: 'team', teamReturn}, '', '#team');
  showScreen('team', false);
});
$('team-back').addEventListener('click', () => {
  if (history.state?.teamReturn === teamReturn) return history.back();
  history.replaceState({screen: teamReturn}, '', '#' + teamReturn);
  restoreScreen(teamReturn);
});
$('hero-start').addEventListener('click', startQuiz);
$('quiz-home').addEventListener('click', () => showScreen('home'));
$('quiz-back').addEventListener('click', () => {
  $('recommend-error').textContent = '';
  if (step === 0) showScreen('home');
  else {
    step -= 1;
    renderQuestion();
  }
});
$('quiz-next').addEventListener('click', () => {
  if (questions[step].optional && answers[questions[step].key] === undefined) answers[questions[step].key] = '';
  if (answers[questions[step].key] === undefined) return;
  if (step === questions.length - 1) recommend();
  else {
    step += 1;
    renderQuestion();
  }
});
$('results-home').addEventListener('click', () => showScreen('home'));
$('restart-quiz').addEventListener('click', () => {
  showScreen('quiz');
  renderQuestion();
});
$('detail-back').addEventListener('click', () => {
  showScreen('results');
  activeCard?.focus();
});

function resizeRefineInput() {
  if ($('results-screen').hidden) return;
  const input = $('refine-input');
  input.style.height = 'auto';
  const borderHeight = input.offsetHeight - input.clientHeight;
  input.style.height = `${input.scrollHeight + borderHeight}px`;
}

async function refinePicks(message) {
  if (!latestResult?.rid) return;
  showScreen('loading');
  const request = new AbortController();
  pending = request;
  try {
    const response = await apiFetch('/recommend/refine', {method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({rid: latestResult.rid, message}), signal: request.signal});
    if (!response.ok) throw await responseError(response);
    const result = await response.json();
    if (pending !== request) return;
    pending = null;
    latestResult = result;
    shownIds = [...new Set([...shownIds, ...result.recommendations.map(game => game.game_id)])];
    $('refine-input').value = '';
    renderResults(result);
    showScreen('results');
  } catch (error) {
    if (pending !== request) return;
    pending = null;
    if (window.needsLogin?.()) { showScreen('login'); return showAppError(error); }
    showScreen('results');
    if (error.status === 422) {
      error.userMessage = t('The combined preferences can be at most 600 characters. Get new picks with saved preferences or edit your answers.',
        '已提交偏好和补充内容合计最多 600 字。你可以按已提交偏好换一批，或返回问卷调整偏好。');
      showAppError(error, () => $('refine-input').focus());
    } else showAppError(error, () => refinePicks(message));
  }
}

$('refine-submit').addEventListener('click', () => refinePicks($('refine-input').value.trim()));
$('refine-input').addEventListener('input', resizeRefineInput);
$('refine-input').addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.isComposing) {
    event.preventDefault();
    $('refine-submit').click();
  }
});
$('refine-new').addEventListener('click', () => refinePicks(''));
window.addEventListener('resize', resizeRefineInput);

async function restoreScreen(name) {
  if (name === 'team') {
    teamReturn = history.state?.teamReturn || teamReturn;
    return showScreen('team', false);
  }
  if (document.body.dataset.screen === 'team' && name === liveTeamReturn && !window.needsLogin?.()
      && (name !== 'play' || playSession || playPreparation)) {
    return showScreen(name, false);
  }
  if (name === 'settings') return openSettings(false);
  if (window.needsLogin?.()) return showScreen('login', false);
  if (name === 'play' && sessionStorage.getItem('game-night-session')) {
    if (await restorePlay()) return showScreen('play', false);
    if (window.needsLogin?.()) return showScreen('login', false);
  }
  if (name === 'detail' && activeGame) return openGame(activeGame.game, activeGame.index, false);
  if (name === 'results' && latestResult) {
    renderResults(latestResult);
    return showScreen('results', false);
  }
  if (name === 'quiz') { showScreen('quiz', false); return renderQuestion(); }
  showScreen('home', false);
}

function loadAppState() {
  let state = {};
  try { state = JSON.parse(sessionStorage.getItem(APP_STATE_KEY) || '{}'); } catch { /* start fresh */ }
  answers = state.answers || {};
  step = Math.min(Math.max(Number(state.step) || 0, 0), questions.length - 1);
  latestResult = state.latestResult || null;
  shownIds = state.shownIds || [];
  activeGame = state.activeGame || null;
  if (screens.includes(state.teamReturn) && state.teamReturn !== 'team') teamReturn = state.teamReturn;
  return state;
}

async function restoreApp() {
  const state = loadAppState();
  const screen = state.screen || 'home';
  history.replaceState(history.state?.screen === screen ? history.state : {screen}, '', '#' + screen);
  await restoreScreen(screen);
}

window.addEventListener('popstate', event => restoreScreen(event.state?.screen || 'home'));

