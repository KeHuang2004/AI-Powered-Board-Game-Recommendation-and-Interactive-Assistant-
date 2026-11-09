const language = document.documentElement.lang.startsWith('zh') ? 'zh' : 'en';
const t = (english, chinese) => language === 'zh' ? chinese : english;

const questions = [
  {key: 'players', label: t('Players', '人数'), title: t('How many people are coming?', '今晚有多少人到场？'), description: t('Enter the total number of people, including anyone who might host.', '填写实际到场人数，包括愿意当主持的人。')},
  {key: 'has_host', label: t('Host', '主持'), title: t('Can someone host the game?', '有人愿意当独立主持吗？'), description: t('Some games need a dedicated host to run the session.', '有些游戏需要一位主持人推进流程。'), options: [[false, t('No dedicated host', '没人当主持')], [true, t('Someone can host', '有人愿意主持')]]},
  {key: 'has_dedicated_components', label: t('Equipment', '道具'), title: t('Can you get game-specific equipment?', '能准备专用游戏道具吗？'), description: t('Playing cards, ordinary dice, and pen and paper count as basic supplies.', '普通扑克牌、骰子和纸笔属于通用用品。'), options: [[false, t('Basic supplies only', '只用通用用品')], [true, t('Game-specific equipment is available', '可以准备专用道具')]]},
  {key: 'scene', label: t('Occasion', '场合'), title: t('What’s the occasion?', '你们为什么聚在一起？'), description: t('Pick the closest match.', '选一个最接近今晚的场合。'), options: [['friends', t('Friends’ get-together', '朋友聚会')], ['date', t('Date night', '约会时光')], ['family', t('Family time', '家庭时间')], ['new_people', t('Meeting new people', '认识新朋友')]]},
  {key: 'mood', label: t('Mood', '氛围'), title: t('What kind of game are you in the mood for?', '想要什么样的游戏气氛？'), description: t('Choose the mood that suits your group.', '选你们当下最想要的氛围。'), options: [['relaxed', t('Relaxed conversation', '轻松聊天')], ['lively', t('Lively icebreakers', '热闹破冰')], ['thinking', t('Quiet brain teasers', '安静动脑')], ['competitive', t('Friendly competition', '刺激对抗')]]}
];

const $ = (id) => document.getElementById(id);
const screens = ['home', 'quiz', 'loading', 'results', 'detail'];
const accents = ['game-signal', 'game-moon', 'game-flash'];
const symbols = ['✳', '☾', '↗'];
let step = 0;
let answers = {};
let pending = null;
let explanationRequest = null;
const explanations = new Map();
let activeCard = null;

function element(tag, content, className) {
  const node = document.createElement(tag);
  node.textContent = content;
  if (className) node.className = className;
  return node;
}

function showScreen(name) {
  if (name !== 'detail' && explanationRequest) {
    explanationRequest.abort();
    explanationRequest = null;
  }
  if (name !== 'loading' && pending) {
    pending.abort();
    pending = null;
  }
  for (const screen of screens) {
    const node = $(screen + '-screen');
    node.hidden = screen !== name;
    node.classList.toggle('active', screen === name);
  }
  document.body.dataset.screen = name;
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

function renderQuestion() {
  const question = questions[step];
  const number = String(step + 1).padStart(2, '0');
  const next = $('quiz-next');
  $('step-count').textContent = number + ' / 05';
  $('progress-fill').style.width = ((step + 1) / questions.length * 100) + '%';
  $('question-kicker').textContent = t('Question ', '问题 ') + number + ' · ' + question.label;
  $('quiz-title').textContent = question.title;
  $('question-description').textContent = question.description;
  $('quiz-back').textContent = step === 0 ? t('← Home', '← 返回首页') : t('← Back', '← 上一步');
  next.firstChild.nodeValue = step === questions.length - 1 ? t('See recommendations ', '查看推荐 ') : t('Next ', '下一步 ');
  next.disabled = answers[question.key] === undefined;
  const grid = $('option-grid');
  grid.replaceChildren();

  if (!question.options) {
    $('question-helper').textContent = t('Enter a whole number from 1 to 100.', '请输入 1 到 100 的整数');
    const input = element('input', '', 'player-input');
    input.id = 'player-count';
    input.type = 'number';
    input.min = '1';
    input.max = '100';
    input.step = '1';
    input.required = true;
    input.inputMode = 'numeric';
    input.value = answers.players ?? '';
    input.setAttribute('aria-label', t('Total number of people', '实际到场人数'));
    input.setAttribute('aria-describedby', 'question-helper');
    input.addEventListener('input', () => {
      if (input.validity.valid) answers.players = input.valueAsNumber;
      else delete answers.players;
      next.disabled = answers.players === undefined;
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
}

async function recommend() {
  $('recommend-error').textContent = '';
  showScreen('loading');
  const request = new AbortController();
  pending = request;
  try {
    const response = await fetch('/recommend', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({...answers, language}),
      signal: request.signal
    });
    if (!response.ok) throw new Error('Recommendation request failed');
    const result = await response.json();
    if (pending !== request) return;
    pending = null;
    renderResults(result);
    showScreen('results');
  } catch {
    if (pending !== request) return;
    pending = null;
    showScreen('quiz');
    $('recommend-error').textContent = t('Couldn’t load recommendations. Your choices are saved. Please try again.', '暂时无法获取推荐。你的选择已保留，请再试一次。');
  }
}

function renderResults(result) {
  const {recommendations, method, eligible_count: count} = result;
  $('results-count').textContent = t('Eligible games / ', '候选游戏 / ') + count;
  $('results-intro').textContent = recommendations.length
    ? t('These games fit your group size, equipment, and preferences.', '根据你们的人数、可用道具和聚会偏好，先看看这几款。')
    : t('No games match these conditions. Try changing the group size, equipment, or host availability.', '当前条件下没有找到可以玩的游戏。你可以调整人数、道具或主持条件后再试。');
  const modelName = {deepseek: 'DeepSeek', gemini: 'Gemini', compatible: t('AI', 'AI 模型')}[method];
  $('recommend-method').textContent = recommendations.length
    ? (modelName ? modelName + t(' ranked the eligible games for your preferences.', '根据你的偏好对合格候选排序。') : t('Ranked using catalog tags and recommended group sizes.', '根据游戏库的标签与推荐人数排序。'))
    : t('Adjust your preferences to try again.', '请调整条件后重新选择。');
  $('answer-summary').replaceChildren(...questions.map(question => {
    const value = answers[question.key];
    const label = question.options?.find(([key]) => key === value)?.[1] ?? (value + t(' players', ' 人'));
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

function openGame(game, index) {
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
  showScreen('detail');
  loadExplanation(game.game_id);
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
    const response = await fetch('/rules/explain', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        game_id: gameId, language, style: 'step_by_step', mode: 'llm', include_detailed: true
      }),
      signal: request.signal
    });
    if (!response.ok) throw new Error('Rule explanation failed');
    const {answer} = await response.json();
    if (explanationRequest !== request) return;
    explanations.set(gameId, answer);
    output.textContent = answer;
  } catch {
    if (explanationRequest === request) output.textContent = t('The AI guide is temporarily unavailable. You can read the rules below.', '暂时无法生成讲解，可以先阅读下方的游戏规则。');
  } finally {
    if (explanationRequest === request) explanationRequest = null;
  }
}

$('brand-home').addEventListener('click', event => {
  event.preventDefault();
  showScreen('home');
});
$('header-start').addEventListener('click', startQuiz);
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

