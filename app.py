"""RTSP 实时画面决策监控：OpenCV 拉流 → 采样多帧 → clef-flash (Ollama /v1/systemone) 决策 → Web 界面。

运行： conda activate research && python app.py   然后打开 http://127.0.0.1:8765
"""
import base64
import copy
import json
import os
import threading
import time
import uuid
from collections import deque
from pathlib import Path

os.environ.setdefault('OPENCV_FFMPEG_CAPTURE_OPTIONS', 'rtsp_transport;tcp|fflags;nobuffer|flags;low_delay')

import cv2
import httpx
import uvicorn
from fastapi import Body, FastAPI, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse

ROOT = Path(__file__).parent
CONFIG_PATH = ROOT / 'config.json'
PORT = 8765

BUFFER_FPS = 5  # 进入采样缓冲的帧率
BUFFER_SECONDS = 15
PREVIEW_FPS = 12
HISTORY = 120
MAX_EVENTS = 60


# ---------------------------------------------------------------- 规则预设 / 配置

def _rule(name, type_, instructions, **kw):
  rule = {
    'id': 'r' + uuid.uuid4().hex[:8],
    'name': name,
    'enabled': True,
    'type': type_,
    'instructions': instructions,
    'true_desc': '',
    'false_desc': '',
    'options': [],
    'threshold': 0.6,
    'hold': 1,
    'severity': 'none',  # none 仅显示 | info 记录变化 | warn / danger 记录并告警
  }
  rule.update(kw)
  return rule


def presets():
  return [
    _rule('有人', 'noul', '画面中是否能看到人？',
          true_desc='画面中至少能看到一个人（包括只露出部分身体）。',
          false_desc='画面中完全没有人。'),
    _rule('人物在运动', 'noul', '对比这几帧画面，画面中的人是否在明显运动（走动、挥手、身体位置或姿态发生变化）？',
          true_desc='人物在各帧之间的位置或姿态有明显变化。',
          false_desc='画面中没有人，或人物在各帧之间基本保持静止。',
          severity='info'),
    _rule('摔倒', 'noul', '画面中是否有人摔倒、倒在地上或躺在地面上？',
          true_desc='有人正在摔倒，或已经倒在/躺在地面上。',
          false_desc='画面中没有人，或所有人都在正常站立、行走、坐着。',
          threshold=0.6, hold=2, severity='danger'),
    _rule('人物姿态', 'choice', '最新一帧画面中，人物的主要姿态是什么？', options=[
      {'key': '站立或行走', 'desc': '人物直立，站着或正在走动', 'alert': False},
      {'key': '坐着', 'desc': '人物坐在椅子、沙发或床边', 'alert': False},
      {'key': '躺在地上', 'desc': '人物倒在或躺在地面上', 'alert': True},
      {'key': '无人', 'desc': '画面中没有人', 'alert': False},
    ], severity='warn', hold=2),
    _rule('活动强度', 'score', '根据这几帧画面的变化，评估画面中人物的活动强度。', options=[
      {'key': '', 'desc': '无人或完全静止', 'alert': False},
      {'key': '', 'desc': '轻微活动（小幅度动作）', 'alert': False},
      {'key': '', 'desc': '正常活动（走动、做事）', 'alert': False},
      {'key': '', 'desc': '剧烈活动（奔跑、打斗、挣扎）', 'alert': False},
    ], threshold=2.5, severity='warn'),
    _rule('明火或烟雾', 'noul', '画面中是否出现明火或明显的烟雾？',
          true_desc='能看到火焰或明显的烟雾。', false_desc='没有火焰也没有烟雾。',
          threshold=0.7, hold=2, severity='danger'),
  ]


def default_config():
  p = presets()
  return {
    'source': '',
    'ollama_host': 'http://localhost:11434',
    'model': 'clef-flash',
    'scene': '这是一个固定安装的监控摄像头拍摄的画面。',
    'frames': 3,
    'window': 2.0,
    'interval': 0.0,
    'max_side': 448,
    'rules': [p[1], p[2], p[3]],
  }


def load_config():
  cfg = default_config()
  if CONFIG_PATH.exists():
    try:
      cfg.update(json.loads(CONFIG_PATH.read_text(encoding='utf-8')))
    except (OSError, ValueError) as e:
      print('config.json 读取失败，使用默认配置:', e)
  return cfg


def clean_config(raw):
  cfg = default_config()
  cfg.update({k: raw[k] for k in cfg if k in raw})
  cfg['frames'] = max(1, min(6, int(cfg['frames'])))
  cfg['window'] = max(0.2, min(10.0, float(cfg['window'])))
  cfg['interval'] = max(0.0, float(cfg['interval']))
  cfg['max_side'] = max(160, min(1280, int(cfg['max_side'])))
  rules = []
  for r in cfg['rules']:
    rule = _rule('', 'noul', '')
    rule.update({k: r[k] for k in rule if k in r})
    rule['hold'] = max(1, int(rule['hold']))
    rule['threshold'] = float(rule['threshold'])
    rules.append(rule)
  cfg['rules'] = rules
  return cfg


LOCK = threading.RLock()
CONFIG = load_config()


# ---------------------------------------------------------------- 拉流

def _fit(frame, max_side):
  h, w = frame.shape[:2]
  scale = max_side / max(h, w)
  if scale >= 1:
    return frame
  return cv2.resize(frame, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)


def _jpeg(frame, quality=80):
  ok, buf = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
  return buf.tobytes() if ok else b''


class Camera:
  def __init__(self):
    self.cond = threading.Condition()
    self.buf = deque()  # (ts, frame<=640)
    self.jpeg = None
    self.seq = 0
    self.status = {'state': 'idle', 'error': None, 'width': 0, 'height': 0, 'fps': 0.0, 'motion': 0.0}
    self._stop = None

  @property
  def running(self):
    return self._stop is not None and not self._stop.is_set()

  def start(self, source):
    self.stop()
    self._stop = threading.Event()
    threading.Thread(target=self._run, args=(source, self._stop), daemon=True).start()

  def stop(self):
    if self._stop:
      self._stop.set()
    with self.cond:
      self.buf.clear()
      self.jpeg = None
      self.status.update(state='idle', error=None, fps=0.0, motion=0.0)
      self.cond.notify_all()

  def _open(self, source):
    if source.isdigit():
      return cv2.VideoCapture(int(source))
    return cv2.VideoCapture(source, cv2.CAP_FFMPEG,
                            [cv2.CAP_PROP_OPEN_TIMEOUT_MSEC, 8000, cv2.CAP_PROP_READ_TIMEOUT_MSEC, 8000])

  def _run(self, source, stop):
    is_file = os.path.isfile(source)
    while not stop.is_set():
      self.status.update(state='connecting')
      cap = self._open(source)
      if not cap.isOpened():
        cap.release()
        self.status.update(state='error', error='无法打开视频源，3 秒后重试')
        stop.wait(3)
        continue
      self.status.update(state='live', error=None)
      frame_gap = 1.0 / (cap.get(cv2.CAP_PROP_FPS) or 25) if is_file else 0
      last_buf = last_jpg = 0.0
      prev_gray = None
      n, t_fps = 0, time.time()
      while not stop.is_set():
        t_read = time.time()
        ok, frame = cap.read()
        if not ok:
          if is_file and cap.set(cv2.CAP_PROP_POS_FRAMES, 0) and cap.grab():
            continue  # 本地文件循环播放
          break
        now = time.time()
        n += 1
        if now - t_fps >= 1:
          self.status.update(fps=round(n / (now - t_fps), 1), width=frame.shape[1], height=frame.shape[0])
          n, t_fps = 0, now
        if now - last_buf >= 1 / BUFFER_FPS:
          last_buf = now
          small = _fit(frame, 640)
          gray = cv2.GaussianBlur(cv2.cvtColor(cv2.resize(small, (160, 90)), cv2.COLOR_BGR2GRAY), (5, 5), 0)
          if prev_gray is not None:
            changed = float((cv2.absdiff(gray, prev_gray) > 18).mean())
            self.status['motion'] = round(0.6 * self.status['motion'] + 0.4 * changed, 4)
          prev_gray = gray
          with self.cond:
            self.buf.append((now, small))
            while self.buf and now - self.buf[0][0] > BUFFER_SECONDS:
              self.buf.popleft()
        if now - last_jpg >= 1 / PREVIEW_FPS:
          last_jpg = now
          data = _jpeg(_fit(frame, 960), 75)
          with self.cond:
            self.jpeg = data
            self.seq += 1
            self.cond.notify_all()
        if frame_gap:
          time.sleep(max(0.0, frame_gap - (time.time() - t_read)))
      cap.release()
      if not stop.is_set():
        self.status.update(state='error', error='视频流中断，正在重连…')
        stop.wait(2)

  def sample(self, n, window):
    """从缓冲区取 n 帧，均匀覆盖最近 window 秒；缓冲不足时返回 None。"""
    with self.cond:
      frames = list(self.buf)
    if not frames:
      return None
    if n <= 1:
      return frames[-1:]
    t_end = frames[-1][0]
    if t_end - frames[0][0] < window * 0.9:
      return None
    picked = []
    for i in range(n):
      target = t_end - window * (n - 1 - i) / (n - 1)
      picked.append(min(frames, key=lambda f: abs(f[0] - target)))
    return picked


CAM = Camera()


# ---------------------------------------------------------------- 决策模型

HTTP = httpx.Client(trust_env=False, timeout=180)
INFER_LOCK = threading.Lock()


def build_state(cfg, frames):
  if len(frames) > 1:
    span = frames[-1][0] - frames[0][0]
    note = f'附带的 {len(frames)} 张图片是同一个固定摄像头在约 {span:.1f} 秒内按时间先后顺序拍摄的连续画面，最后一张是最新画面。'
  else:
    note = '附带的图片是摄像头的当前画面。'
  return f"{cfg['scene'].strip()}\n{note}".strip()


def to_question(rule):
  q = {'type': rule['type'], 'instructions': rule['instructions'].strip() or rule['name']}
  if rule['type'] == 'noul':
    if rule['true_desc'].strip() and rule['false_desc'].strip():
      q['criteria'] = {'true': rule['true_desc'].strip(), 'false': rule['false_desc'].strip()}
  elif rule['type'] == 'choice':
    q['criteria'] = {o['key'].strip(): (o.get('desc') or '').strip() or None for o in rule['options'] if o['key'].strip()}
    if len(q['criteria']) < 2:
      raise ValueError(f"规则「{rule['name']}」至少需要 2 个选项")
  else:
    q['criteria'] = [(o.get('desc') or '').strip() or f'等级 {i}' for i, o in enumerate(rule['options'])]
    if len(q['criteria']) < 2:
      raise ValueError(f"规则「{rule['name']}」至少需要 2 个等级")
  return q


def infer(cfg, rules, frames):
  """返回 {rule_id: 评估结果}。"""
  images = [base64.b64encode(_jpeg(_fit(f, cfg['max_side']), 85)).decode() for _, f in frames]
  body = {
    'model': cfg['model'],
    'state': build_state(cfg, frames),
    'questions': {r['id']: to_question(r) for r in rules},
    'images': images,
    'keep_alive': '30m',
  }
  with INFER_LOCK:
    resp = HTTP.post(cfg['ollama_host'].rstrip('/') + '/v1/systemone', json=body)
  if resp.status_code != 200:
    try:
      msg = resp.json().get('error', resp.text)
    except ValueError:
      msg = resp.text
    raise RuntimeError(f'模型返回 {resp.status_code}: {msg}')
  answers = resp.json()['answers']
  return {r['id']: evaluate(r, answers[r['id']]) for r in rules if r['id'] in answers}


def evaluate(rule, ans):
  """把模型答案转成 {level 0~1, triggered, ...}，level 用于历史曲线。"""
  if ans['type'] == 'noul':
    p = ans['noul']
    return {'type': 'noul', 'p': p, 'level': p, 'triggered': p >= rule['threshold']}
  if ans['type'] == 'choice':
    alert_keys = {o['key'].strip() for o in rule['options'] if o.get('alert')}
    probs = ans['probabilities']
    level = sum(v for k, v in probs.items() if k in alert_keys) if alert_keys else ans['confidence']
    return {'type': 'choice', 'choice': ans['choice'], 'probs': probs, 'confidence': ans['confidence'],
            'level': level, 'triggered': ans['choice'] in alert_keys}
  top = max(len(ans['legend']) - 1, 1)
  return {'type': 'score', 'score': ans['score'], 'probs': ans['probabilities'], 'legend': ans['legend'],
          'level': ans['score'] / top, 'triggered': ans['score'] >= rule['threshold']}


def describe(rule, res):
  if res['type'] == 'noul':
    return f"{res['p'] * 100:.0f}%"
  if res['type'] == 'choice':
    return f"{res['choice']}（{res['probs'].get(res['choice'], 0) * 100:.0f}%）"
  return f"{res['legend'].get(str(round(res['score'])), '')}（{res['score']:.1f}）"


class Analyzer:
  def __init__(self):
    self._stop = None
    self.runtime = {}  # rule_id -> {result, active, since, hits, misses, history}
    self.events = deque(maxlen=MAX_EVENTS)
    self.snaps = {}
    self.frames = []  # 最近一次送入模型的帧 (jpeg)
    self.info = {'latency': 0.0, 'ts': 0.0, 'error': None, 'note': '', 'count': 0, 'fseq': 0, 'frames': 0}

  @property
  def running(self):
    return self._stop is not None and not self._stop.is_set()

  def start(self):
    self.stop()
    self.runtime.clear()
    self.info.update(error=None, note='等待画面…', count=0)
    self._stop = threading.Event()
    threading.Thread(target=self._run, args=(self._stop,), daemon=True).start()

  def stop(self):
    if self._stop:
      self._stop.set()

  def _run(self, stop):
    last_ts = 0.0
    while not stop.is_set():
      with LOCK:
        cfg = copy.deepcopy(CONFIG)
      rules = [r for r in cfg['rules'] if r['enabled']]
      frames = CAM.sample(cfg['frames'], cfg['window']) if rules else None
      if not frames or frames[-1][0] <= last_ts:
        self.info['note'] = '等待画面…' if rules else '没有启用的规则'
        stop.wait(0.2)
        continue
      last_ts = frames[-1][0]
      t0 = time.time()
      try:
        results = infer(cfg, rules, frames)
      except Exception as e:  # 模型不可用时保持循环，界面上显示错误
        self.info.update(error=str(e))
        stop.wait(2)
        continue
      if stop.is_set():
        break
      self._apply(rules, results, frames, time.time() - t0)
      stop.wait(max(0.0, cfg['interval'] - (time.time() - t0)))

  def _apply(self, rules, results, frames, latency):
    now = time.time()
    with LOCK:
      self.frames = [_jpeg(_fit(f, 320), 70) for _, f in frames]
      self.info.update(latency=round(latency, 2), ts=now, error=None, note='', count=self.info['count'] + 1,
                       fseq=self.info['fseq'] + 1, frames=len(frames))
      for stale in set(self.runtime) - {r['id'] for r in rules}:
        del self.runtime[stale]
      for rule in rules:
        res = results.get(rule['id'])
        if res is None:
          continue
        rt = self.runtime.setdefault(rule['id'], {'active': False, 'since': now, 'hits': 0, 'misses': 0,
                                                  'history': deque(maxlen=HISTORY)})
        rt['result'] = res
        rt['history'].append((round(res['level'], 3), res['triggered']))
        rt['hits'] = rt['hits'] + 1 if res['triggered'] else 0
        rt['misses'] = 0 if res['triggered'] else rt['misses'] + 1
        flipped = (not rt['active'] and rt['hits'] >= rule['hold']) or (rt['active'] and rt['misses'] >= rule['hold'])
        if flipped:
          rt['active'] = not rt['active']
          rt['since'] = now
          if rule['severity'] != 'none':
            self._event(rule, res, rt['active'], frames[-1][1], now)

  def _event(self, rule, res, active, frame, now):
    eid = uuid.uuid4().hex[:10]
    self.snaps[eid] = _jpeg(frame, 80)
    if len(self.events) == self.events.maxlen:
      self.snaps.pop(self.events[0]['id'], None)
    self.events.append({'id': eid, 'ts': now, 'rule': rule['id'], 'name': rule['name'], 'severity': rule['severity'],
                        'kind': 'trigger' if active else 'clear', 'text': describe(rule, res)})

  def snapshot(self):
    with LOCK:
      results = {}
      for rid, rt in self.runtime.items():
        results[rid] = {**rt['result'], 'active': rt['active'], 'since': rt['since'], 'hits': rt['hits'],
                        'history': list(rt['history'])}
      return {'an': dict(self.info), 'results': results, 'events': list(self.events)}


AN = Analyzer()


# ---------------------------------------------------------------- HTTP

app = FastAPI(title='灵眸 · AI 实时守望')


@app.get('/')
def index():
  return FileResponse(ROOT / 'static' / 'index.html', headers={'Cache-Control': 'no-store'})


@app.get('/api/config')
def get_config():
  with LOCK:
    return CONFIG


@app.put('/api/config')
def put_config(raw: dict = Body(...)):
  global CONFIG
  try:
    cfg = clean_config(raw)
    for r in cfg['rules']:
      to_question(r)
  except (ValueError, TypeError, KeyError) as e:
    raise HTTPException(400, str(e))
  with LOCK:
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
    CONFIG = cfg
  return cfg


@app.get('/api/presets')
def get_presets():
  return presets()


@app.post('/api/start')
def start():
  with LOCK:
    source = CONFIG['source'].strip()
  if not source:
    raise HTTPException(400, '请先填写视频源地址')
  CAM.start(source)
  AN.start()
  return {'ok': True}


@app.post('/api/stop')
def stop():
  AN.stop()
  CAM.stop()
  return {'ok': True}


@app.get('/api/state')
def state():
  snap = AN.snapshot()
  snap['cam'] = dict(CAM.status)
  snap['running'] = CAM.running
  snap['now'] = time.time()
  return snap


@app.post('/api/test')
def test_rule(payload: dict = Body(...)):
  """用当前画面试跑一条（尚未保存的）规则，供设计提示词时使用。"""
  with LOCK:
    cfg = copy.deepcopy(CONFIG)
  cfg['scene'] = payload.get('scene', cfg['scene'])
  try:
    rule = clean_config({'rules': [payload['rule']]})['rules'][0]
    to_question(rule)
  except (ValueError, TypeError, KeyError) as e:
    raise HTTPException(400, str(e))
  frames = CAM.sample(cfg['frames'], cfg['window'])
  if not frames:
    raise HTTPException(409, '还没有可用画面，请先开始监控并等待几秒')
  t0 = time.time()
  try:
    res = infer(cfg, [rule], frames)[rule['id']]
  except Exception as e:
    raise HTTPException(502, str(e))
  return {'result': res, 'text': describe(rule, res), 'latency': round(time.time() - t0, 2)}


@app.get('/api/frame/{i}')
def sampled_frame(i: int):
  with LOCK:
    if not 0 <= i < len(AN.frames):
      raise HTTPException(404)
    return Response(AN.frames[i], media_type='image/jpeg', headers={'Cache-Control': 'no-store'})


@app.get('/api/snap/{eid}')
def event_snap(eid: str):
  data = AN.snaps.get(eid)
  if not data:
    raise HTTPException(404)
  return Response(data, media_type='image/jpeg')


@app.get('/video.mjpg')
def video():
  def gen():
    last = -1
    while CAM.running:
      with CAM.cond:
        CAM.cond.wait_for(lambda: CAM.seq != last or not CAM.running, timeout=1)
        last, data = CAM.seq, CAM.jpeg
      if data:  # 超时也重发上一帧，便于发现客户端已断开
        yield b'--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n%s\r\n' % (len(data), data)
  return StreamingResponse(gen(), media_type='multipart/x-mixed-replace; boundary=frame')


if __name__ == '__main__':
  print(f'打开 http://127.0.0.1:{PORT}')
  uvicorn.run(app, host='127.0.0.1', port=PORT, log_level='warning', timeout_graceful_shutdown=1)
