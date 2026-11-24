'use strict';

const byId = id => document.getElementById(id);
const scope = byId('scope');
const wave = byId('wave');
const sleepWave = byId('sleep-wave');
const scopeSection = scope.closest('section');
const link = byId('link');
const renderScope = SignalScope.iqPlot();
const states = {
 quiet: 'Активность не обнаружена',
 motion: 'Обнаружено движение',
 rhythm: 'Ритм похож на дыхание',
 weak_stream: 'Недостаточно кадров CSI',
 needs_calibration: 'Нужен фон пустой комнаты',
};
const sleepStates = {
 quiet: 'Устойчивый дыхательный ритм пока не выделен',
 motion: 'Пауза · обнаружено движение',
 rhythm: 'Выделен дыхательный ритм',
 settling: 'Запись спокойного сигнала',
 weak_stream: 'Недостаточно кадров CSI',
 needs_calibration: 'Нужен фон для режима сна',
};
const events = {
 calibration_started: 'Начата запись фона',
 calibration_finished: 'Фон записан',
 calibration_failed: 'Фон не записан',
 calibration_cancelled: 'Запись фона отменена',
 armed: 'Тревога включена',
 disarmed: 'Тревога выключена',
 alarm: 'Сработала тревога',
 alarm_cleared: 'Тревога сброшена',
 guard_mode: 'Охрана помещения',
 sleep_mode: 'Опека · сон',
};
let snapshot = null;
let activePacket = null;
let activeLink = null;
let cachedKey = '';
let alarmWasActive = false;
let audio = null;
let operation = 'guard';
let modesAvailable = false;

function text(id, value) {
 byId(id).textContent = value;
}

function label(row) {
 const source = row.peer ? 'модуль ' + row.source : 'роутер ' + row.source;
 return source + ' → ' + row.receiver + ' · канал ' + row.channel;
}

function setLinks(lines) {
 const key = lines.map(row => row.id).join('|');
 if (key === cachedKey) return;
 const selected = link.value;
 cachedKey = key;
 link.replaceChildren(...lines.map(row => new Option(label(row), row.id)));
 if (lines.length === 0) link.add(new Option('Ожидание CSI', ''));
 else if (lines.some(row => row.id === selected)) link.value = selected;
 else link.value = (lines.find(row => !row.peer) || lines[0]).id;
 link.disabled = !lines.length;
}

function selectPacket(selected) {
 if (!selected || selected.age_ms > 450) {
  activePacket = null;
  return;
 }
 const key = selected.id + '/' + selected.sequence;
 if (activePacket?.key === key) return;
 const samples = SignalScope.decode(selected.iq);
 activePacket = samples ? { ...selected, samples, key, receivedAt: performance.now() - selected.age_ms } : null;
}

function canvasSurface(canvas) {
 const width = canvas.clientWidth;
 const height = canvas.clientHeight;
 if (!width || !height) return null;
 const ratio = Math.min(2, window.devicePixelRatio || 1);
 const pixels = [Math.round(width * ratio), Math.round(height * ratio)];
 if (canvas.width !== pixels[0] || canvas.height !== pixels[1]) {
  canvas.width = pixels[0];
  canvas.height = pixels[1];
 }
 const ctx = canvas.getContext('2d');
 ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
 return {ctx, width, height};
}

function drawWave(canvas, values) {
 canvas.hidden = !values?.length;
 const surface = canvasSurface(canvas);
 if (!surface) return;
 const {ctx, width, height} = surface;
 ctx.clearRect(0, 0, width, height);
 ctx.strokeStyle = '#344660';
 ctx.beginPath();
 ctx.moveTo(0, height / 2);
 ctx.lineTo(width, height / 2);
 ctx.stroke();
 ctx.strokeStyle = '#90daee';
 ctx.lineWidth = 1.7;
 ctx.beginPath();
 values.forEach((value, i) => {
  const x = i * width / (values.length - 1);
  const y = height * (.5 - value * .4);
  if (i === 0) ctx.moveTo(x, y);
  else ctx.lineTo(x, y);
 });
 ctx.stroke();
}

function renderEvents(entries) {
 const list = byId('events');
 if (!entries.length) return;
 list.replaceChildren(...entries.map(event => {
  const item = document.createElement('li');
  const description = document.createElement('span');
  const time = document.createElement('time');
  description.textContent = events[event.event] || event.event;
  if (event.reason) description.textContent += event.reason === 'motion' ? ' · движение' : ' · ритм';
  time.dateTime = event.at;
  time.textContent = new Date(event.at).toLocaleTimeString('ru-RU');
  item.append(description, time);
  return item;
 }));
}

function beep() {
 if (!audio || audio.state !== 'running' || !byId('sound').checked) return;
 const oscillator = audio.createOscillator();
 const volume = audio.createGain();
 oscillator.frequency.value = 680;
 oscillator.connect(volume).connect(audio.destination);
 volume.gain.value = .09;
 volume.gain.exponentialRampToValueAtTime(.001, audio.currentTime + .45);
 oscillator.start();
 oscillator.stop(audio.currentTime + .45);
}

function render(data) {
 const lines = data?.lines || [];
 if (data) {
  operation = data.operation || 'guard';
  modesAvailable = Boolean(data.operation);
 }
 const sleep = operation === 'sleep';
 byId('mode-switch').hidden = !modesAvailable;
 for (const name of ['guard', 'sleep']) {
  const button = byId(name + '-mode');
  button.setAttribute('aria-pressed', String(name === operation));
  button.disabled = !data || name === operation;
 }
 for (const id of ['guard-view', 'gain-control', 'guard-caption', 'motion-metric', 'sound-control', 'fullscreen', 'arm']) {
  byId(id).hidden = sleep;
 }
 for (const id of ['sleep-view', 'pulse-metric', 'strength-metric', 'care-note']) {
  byId(id).hidden = !sleep;
 }
 text('scope-title', sleep ? 'Дыхательный сигнал' : 'Обзор I/Q');
 text('state-heading', sleep ? 'Наблюдение во сне' : 'Наблюдение');
 setLinks(lines);
 activeLink = lines.find(row => row.id === link.value);
 selectPacket(activeLink);
 const connection = byId('connection');
 connection.dataset.state = lines.length ? 'live' : 'offline';
 connection.textContent = !data ? 'Сервер недоступен' : lines.length ? 'CSI поступает' : 'Ожидание CSI';
 const phase = data?.calibration;
 byId('cal-banner').hidden = !phase;
 if (phase) text('cal-status', (phase.state === 'countdown' ? 'Освободите комнату · ' : 'Запись пустой комнаты · ') + phase.seconds + ' с');
 const state = byId('state');
 state.dataset.state = activeLink?.state || '';
 const stateLabels = sleep ? sleepStates : states;
 state.textContent = phase ? 'Калибровка' : stateLabels[activeLink?.state] || 'Ожидание данных';
 text('motion', !phase && Number.isFinite(activeLink?.motion) ? activeLink.motion.toFixed(2) + '×' : '—');
 text('rpm', !phase && Number.isFinite(activeLink?.rpm) ? activeLink.rpm.toFixed(1) : '—');
 const values = phase ? null : activeLink?.wave;
 const hasPulse = sleep && !phase && Number.isFinite(activeLink?.pulse_candidate);
 text('strength', sleep && !phase && Number.isFinite(activeLink?.breath_strength) ?
  activeLink.breath_strength.toFixed(2) + '×' : '—');
 text('pulse', hasPulse ? String(activeLink.pulse_candidate) : '—');
 text('pulse-note', hasPulse ? 'Кандидат · уд/мин' : 'Недостаточно сигнала');
 drawWave(wave, sleep ? null : values);
 drawWave(sleepWave, sleep ? values : null);
 byId('sleep-placeholder').hidden = Boolean(values?.length);
 text('rhythm-note', values ?
  (sleep ? 'Уровень сигнала зависит от положения спящего и расстановки оборудования.' :
   'Изменение CSI в дыхательном диапазоне за 40 секунд.') :
  'Для оценки нужно около 40 секунд спокойного сигнала.');
 const armed = data?.armed || false;
 const arm = byId('arm');
 arm.dataset.action = armed ? 'disarm' : 'arm';
 arm.textContent = armed ? 'Выключить тревогу' : 'Включить тревогу';
 arm.disabled = !data || (!armed && (!!phase || !lines.some(row => row.calibrated)));
 byId('calibrate').disabled = !lines.length || !!phase || armed;
 const alarm = data?.alarm;
 byId('alarm-banner').hidden = !alarm;
 if (alarm) text('alarm-title', alarm === 'motion' ? 'Тревога · движение' : 'Тревога · периодический сигнал');
 if (alarm && !alarmWasActive) beep();
 if (data) alarmWasActive = Boolean(alarm);
 if (data) renderEvents(data.events);
 const result = data?.calibration_result;
 const feedback = byId('feedback');
 if (!feedback.dataset.error) {
  feedback.textContent = data?.storage_error ? 'Не удалось сохранить данные на диск.' :
   result === 'no_data' ? 'Фон не записан. Проверьте поток CSI и повторите.' :
   result === 'ready' && !phase ? 'Фон комнаты записан.' : '';
 }
 if (data) {
  const details = activeLink ? ' · ' + activeLink.rssi + ' дБм' : '';
  text('transport', (data.mode === 'bridge' ? 'Подключение через WiFi DensePose' : 'Прямой приём UDP') +
   ' · линий ' + lines.length + ' · кадров ' + data.received + details +
   (data.rejected ? ' · отклонено ' + data.rejected : ''));
 } else text('transport', '');
}

async function poll() {
 try {
  const response = await fetch('/api/state', { cache: 'no-store', signal: AbortSignal.timeout(1800) });
  if (!response.ok) throw new Error(response.status);
  snapshot = await response.json();
  render(snapshot);
 } catch (_) {
  snapshot = null;
  render(null);
 }
 setTimeout(poll, 100);
}

async function action(button) {
 const feedback = byId('feedback');
 delete feedback.dataset.error;
 button.disabled = true;
 try {
  const response = await fetch('/api/action/' + button.dataset.action, {
   method: 'POST', headers: {'Content-Type': 'application/json'},
  });
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || 'Действие не выполнено.');
  snapshot = await (await fetch('/api/state')).json();
  render(snapshot);
 } catch (error) {
  text('feedback', error.message);
  feedback.dataset.error = '1';
 } finally {
  if (button.id === 'clear' || !button.id) button.disabled = false;
 }
}

document.querySelectorAll('button[data-action]').forEach(button => {
 button.addEventListener('click', () => action(button));
});
link.addEventListener('change', () => render(snapshot));
byId('sound').addEventListener('change', async event => {
 if (!event.target.checked) return;
 try {
  audio ||= new AudioContext();
  await audio.resume();
  if (snapshot?.alarm) beep();
 } catch (_) {
  event.target.checked = false;
  text('feedback', 'Браузер не разрешил звук.');
  byId('feedback').dataset.error = '1';
 }
});
byId('fullscreen').addEventListener('click', async () => {
 try {
  if (document.fullscreenElement) await document.exitFullscreen();
  else await scopeSection.requestFullscreen();
 } catch (_) {
  text('feedback', 'Полноэкранный режим недоступен.');
  byId('feedback').dataset.error = '1';
 }
});
document.addEventListener('fullscreenchange', () => {
 text('fullscreen', document.fullscreenElement ? 'Выйти из полного экрана' : 'На весь экран');
});

function frame(now) {
 const surface = canvasSurface(scope);
 if (surface) {
  const { ctx, width, height } = surface;
  const packet = activePacket && now - activePacket.receivedAt < 500 ? activePacket : null;
  renderScope(ctx, width, height, packet, byId('gain').valueAsNumber, now, activeLink?.id);
 }
 requestAnimationFrame(frame);
}

poll();
requestAnimationFrame(frame);
