"""VexFlow 五线谱 + 手写简谱 — 全本地加载，零 CDN 依赖。

用法：
  Python:
    from plugins.practice._vex_html import VEXFLOW_HTML, VEXFLOW_BASE_URL
    view.setHtml(VEXFLOW_HTML, VEXFLOW_BASE_URL)
    view.page().runJavaScript("render({notes: [...]});")

JS 全局函数：
  render({notes: [{midi, duration}], clef, key, meter})   → 重绘全部
  highlightVf(index, color)                                → 高亮第 index 个音符
"""

# VEXFLOW_BASE_URL 让 setHtml 里的相对路径 src="vexflow.js" 指向本地文件
import os as _os
import sys as _sys

# 找到 assets/ 目录
_HERE = _os.path.dirname(_os.path.abspath(__file__))
_ASSETS_DIR = _os.path.join(_HERE, "assets")
# QUrl 要求 file:/// 格式（注意 Windows 盘符要三斜杠）
# base URL 必须是目录（以 / 结尾），Qt 才能正确解析 <script src="vexflow.js">
# 取 assets 目录，不以文件名结尾
_ASSETS_DIR_URL = _ASSETS_DIR.replace("\\", "/") + "/"
_BASE_URL_STR = "file:///" + _ASSETS_DIR_URL

# 也导出一个 QUrl 方便调用
try:
    from PyQt6.QtCore import QUrl as _QUrl
    VEXFLOW_BASE_URL = _QUrl(_BASE_URL_STR)
except ImportError:
    VEXFLOW_BASE_URL = _BASE_URL_STR

VEXFLOW_HTML = r"""<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  html, body { background:#16213e; color:#e0e0e0; overflow:hidden; font-family:sans-serif; }
  #treble-svg { width:100%; height:70px; display:block; }
  #jp-svg     { width:100%; height:90px; display:block; }
  .hint { color:#666; font-size:11px; text-align:center; padding:4px; }
</style>
</head>
<body>
<svg id="treble-svg"></svg>
<svg id="jp-svg"></svg>
<script src="vexflow.js"></script>
<script>
// ===== 全局状态 =====
let _vfNotes = [];
let _jpNotes = [];
let _currentHighlight = -1;

const { Factory, Renderer, Stave, StaveNote, Formatter, Voice } = Vex.Flow;

// MIDI → VexFlow pitch 字符串（C4 = 60 → "c/4"）
const pitchMap = {0:'c',1:'c#',2:'d',3:'d#',4:'e',5:'f',6:'f#',7:'g',8:'g#',9:'a',10:'a#',11:'b'};
const midiToVf = (m) => pitchMap[m%12] + '/' + (Math.floor(m/12)-1);

// duration 秒 → VexFlow duration code
const durToVf = (d) => {
  if (d >= 1.9) return 'w';
  if (d >= 1.4) return 'd';
  if (d >= 0.9) return 'h';
  if (d >= 0.7) return 'dh';
  if (d >= 0.45) return 'q';
  if (d >= 0.33) return 'dq';
  if (d >= 0.23) return '8';
  if (d >= 0.15) return 'd8';
  return '16';
};

// ===== 五线谱 =====
function renderTreble(notes, clef, key, meter) {
  const svg = document.getElementById('treble-svg');
  svg.innerHTML = '';
  const W = svg.clientWidth || 600;

  const r = new Renderer(svg, Renderer.Backends.SVG);
  r.resize(W, 70);
  const ctx = r.getContext();

  const stave = new Stave(40, 3, W - 80);
  stave.addClef(clef).addKeySignature(key).addTimeSignature(meter);
  stave.setContext(ctx).draw();

  const beat = parseInt(meter.split('/')[0]);
  const beatUnit = parseInt(meter.split('/')[1]);

  const vfNotes = [];
  let totalDur = 0;
  for (const n of notes) {
    const d = Math.min(n.duration || 1.0, 1.5);
    vfNotes.push(new StaveNote({
      keys: [midiToVf(n.midi)],
      duration: durToVf(d),
      clef: clef,
    }));
    totalDur += d;
    if (totalDur >= beat * beatUnit * 4) break;
  }

  const voice = new Voice({ numBeats: beat, beatValue: beatUnit });
  voice.setStrict(false);
  voice.addTickables(vfNotes);
  new Formatter().joinVoices([voice]).formatToStave([voice], stave);
  voice.draw(ctx, stave);

  _vfNotes = vfNotes;
}

// ===== 简谱（手写 SVG）=====
function renderJianpu(notes, meter) {
  const svg = document.getElementById('jp-svg');
  svg.innerHTML = '';
  const W = svg.clientWidth || 600;
  const H = 90;

  // SVG 根
  const NS = 'http://www.w3.org/2000/svg';

  // 调号下的简谱数字
  // C大调 pitch class → 简谱（白键用数字，黑键用 # 前缀）
  const jpMap = {0:'1',1:'#1',2:'2',3:'#2',4:'3',5:'4',6:'#4',7:'5',8:'#5',9:'6',10:'#6',11:'7'};

  // 水平均匀铺开（最多 16 个）
  const maxNotes = Math.min(notes.length, 16);
  const leftPad = 20;
  const rightPad = 20;
  const usable = W - leftPad - rightPad;
  const spacing = maxNotes > 1 ? usable / maxNotes : usable;

  // 画一下 4 条水平线（简谱有 4 线，从上到下是 1 2 3 4 5 6 7 i）
  // 简化：画一条基准线 + 数字
  const baseY = H * 0.55;  // 数字中心

  for (let i = 0; i < maxNotes; i++) {
    const n = notes[i];
    const pc = n.midi % 12;
    const digit = jpMap[pc] || '?';
    const octave = Math.floor(n.midi / 12) - 5;  // C4=60 → octave 1

    const cx = leftPad + spacing * (i + 0.5);

    // 文本
    const text = document.createElementNS(NS, 'text');
    text.setAttribute('x', cx);
    text.setAttribute('y', baseY);
    text.setAttribute('text-anchor', 'middle');
    text.setAttribute('font-size', '22');
    text.setAttribute('font-family', 'sans-serif');
    text.setAttribute('fill', '#d0d0d0');
    text.setAttribute('font-weight', 'bold');
    text.textContent = digit;
    svg.appendChild(text);

    // 八度加点（上加点 = octave>0, 下加点 = octave<0）
    if (octave > 0) {
      // 上加点：在数字上方画小圆点
      for (let d = 0; d < octave; d++) {
        const dot = document.createElementNS(NS, 'circle');
        dot.setAttribute('cx', cx);
        dot.setAttribute('cy', baseY - 16 - d * 6);
        dot.setAttribute('r', 2.5);
        dot.setAttribute('fill', '#d0d0d0');
        svg.appendChild(dot);
      }
    } else if (octave < 0) {
      // 下加点
      for (let d = 0; d < -octave; d++) {
        const dot = document.createElementNS(NS, 'circle');
        dot.setAttribute('cx', cx);
        dot.setAttribute('cy', baseY + 8 + d * 6);
        dot.setAttribute('r', 2.5);
        dot.setAttribute('fill', '#d0d0d0');
        svg.appendChild(dot);
      }
    }

    // 时值下划线（duration < 1 且不是 1 的倍数 → 加横线）
    const dur = n.duration || 1.0;
    if (dur < 1.0) {
      // 八分 = 1 条线，十六分 = 2 条
      const lines = dur < 0.3 ? 2 : 1;
      for (let li = 0; li < lines; li++) {
        const line = document.createElementNS(NS, 'line');
        line.setAttribute('x1', cx - 10);
        line.setAttribute('x2', cx + 10);
        line.setAttribute('y1', baseY + 12 + li * 4);
        line.setAttribute('y2', baseY + 12 + li * 4);
        line.setAttribute('stroke', '#888');
        line.setAttribute('stroke-width', '1.5');
        svg.appendChild(line);
      }
    }

    // 存引用（高亮用）
    text.dataset.idx = i;
    text.classList.add('jp-note');
  }

  // 存 SVG 元素引用
  _jpNotes = [];
  const jpElements = svg.querySelectorAll('.jp-note');
  jpElements.forEach(el => {
    _jpNotes.push(el);
  });

  // 也存所有 jp 相关元素（方便高亮）
  svg._allJxElements = svg.querySelectorAll('text, circle, line');
}

// ===== 主入口 =====
function render(data) {
  if (!window.Vex || !Vex.Flow) {
    document.getElementById('treble-svg').innerHTML =
      '<div class="hint">VexFlow 加载失败 — 请确认 vexflow.js 在 assets/ 目录</div>';
    return;
  }
  const notes = data.notes || [];
  const clef  = data.clef  || 'treble';
  const key   = data.key   || 'C';
  const meter = data.meter || '4/4';

  if (notes.length === 0) {
    document.getElementById('treble-svg').innerHTML =
      '<div class="hint">选择练习或导入 MIDI 开始</div>';
    document.getElementById('jp-svg').innerHTML = '';
    return;
  }

  renderTreble(notes, clef, key, meter);
  renderJianpu(notes, meter);
  _currentHighlight = -1;
}

// ===== 高亮当前音符 =====
function highlightVf(index, color) {
  color = color || '#fbbf24';
  // 五线谱
  if (index >= 0 && index < _vfNotes.length) {
    // 先清除之前的高亮
    if (_currentHighlight >= 0 && _currentHighlight < _vfNotes.length) {
      try { _vfNotes[_currentHighlight].setStyle({ fillStyle: '#000000' }); _vfNotes[_currentHighlight].draw(); } catch(e){}
    }
    try {
      _vfNotes[index].setStyle({ fillStyle: color });
      _vfNotes[index].draw();
      _currentHighlight = index;
    } catch(e){}
  }
  // 简谱 — 改 text fill
  const jpTexts = document.querySelectorAll('#jp-svg text.jp-note');
  jpTexts.forEach(el => {
    const i = parseInt(el.dataset.idx);
    el.setAttribute('fill', i === index ? color : '#d0d0d0');
    el.setAttribute('font-size', i === index ? '26' : '22');
  });
}
</script>
</body>
</html>
"""
