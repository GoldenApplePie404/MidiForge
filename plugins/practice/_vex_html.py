"""VexFlow 五线谱 + 手写简谱 — 全本地加载，零 CDN 依赖。

用法：
  Python:
    from plugins.practice._vex_html import VEXFLOW_HTML, VEXFLOW_BASE_URL
    view.setHtml(VEXFLOW_HTML, VEXFLOW_BASE_URL)
    view.page().runJavaScript("render({notes: [...]});")

JS 全局函数：
  render({notes, clef, key, meter, bpm}) → 重绘全部
    - time: 音符起始绝对时间（秒），用于按小节排版
    - duration: 秒 → 内部转成拍数
    - track: 轨道名（可选）；不同轨道分谱表行渲染
  setMode(mode)              → 'treble' 或 'jianpu'，谱子区一次只显示一种
  highlightVf(index, color)  → 高亮第 index 个音符（当前显示模式内）
  clearHighlight()           → 清除高亮
"""

import os as _os
_HERE = _os.path.dirname(_os.path.abspath(__file__))
_ASSETS_DIR = _os.path.join(_HERE, "assets")
_ASSETS_DIR_URL = _ASSETS_DIR.replace("\\", "/") + "/"
_BASE_URL_STR = "file:///" + _ASSETS_DIR_URL

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
  .score-wrap { width:100%; height:100%; overflow:auto; display:flex; flex-direction:column; justify-content:center; align-items:center; }
  svg { display:block; width:100%; height:auto; }
  .hint { color:#666; font-size:11px; text-align:center; padding:6px; }
  ::-webkit-scrollbar { width:6px; height:6px; }
  ::-webkit-scrollbar-track { background:#1a1a2e; }
  ::-webkit-scrollbar-thumb { background:#3a3a5e; border-radius:3px; }
</style>
</head>
<body>
<div id="hint" class="hint">谱面加载中…</div>
<div class="score-wrap" id="wrap-tr" style="display:none;"><svg id="treble-svg"></svg></div>
<div class="score-wrap" id="wrap-jp" style="display:none;"><svg id="jp-svg"></svg></div>
<script src="vexflow.js"></script>
<script>
"use strict";

// ===== 全局状态 =====
let _state = { notes: [], clef: 'treble', key: 'C', meter: '4/4', bpm: 120 };
let _mode = 'treble';
let _highlightIdx = -1;
let _highlightColor = '#fbbf24';
let _vfRefs = [];   // [{staveNote, svgX, noteIdx}]
let _jpRefs = [];   // [{el, svgX, noteIdx}]
let _followBeat = -1;
const FOLLOW_WINDOW = { before: 0, after: 2 };
const _trackPalette = ['#60a5fa', '#a78bfa', '#34d399', '#f472b6',
                       '#fbbf24', '#38bdf8', '#fb923c', '#e879f9'];

const { Renderer, Stave, StaveNote, GhostNote, Formatter, Voice } = Vex.Flow;

const pitchMap = {0:'c',1:'c#',2:'d',3:'d#',4:'e',5:'f',6:'f#',7:'g',8:'g#',9:'a',10:'a#',11:'b'};
const midiToVf = (m) => pitchMap[m % 12] + '/' + (Math.floor(m / 12) - 1);

function beatsToVf(b) {
  const beats = Math.max(0.25, b);
  if (Math.abs(beats - 4.0)  < 0.05) return { dur: 'w', dots: 0 };
  if (Math.abs(beats - 3.0)  < 0.05) return { dur: 'h', dots: 1 };
  if (Math.abs(beats - 2.0)  < 0.05) return { dur: 'h', dots: 0 };
  if (Math.abs(beats - 1.5)  < 0.05) return { dur: 'q', dots: 1 };
  if (Math.abs(beats - 1.0)  < 0.05) return { dur: 'q', dots: 0 };
  if (Math.abs(beats - 0.75) < 0.05) return { dur: '8', dots: 1 };
  if (Math.abs(beats - 0.5)  < 0.05) return { dur: '8', dots: 0 };
  return { dur: '16', dots: 0 };
}

// ===== 轨道分组与小节切分 =====
function trackOrder(notes) {
  const order = [];
  for (const n of notes) {
    const t = n.track || 'main';
    if (!order.includes(t)) order.push(t);
  }
  return order;
}

function chunkMeasuresFor(notes, bpm, meter) {
  const beatsPerBar = parseInt(meter.split('/')[0], 10) || 4;
  const items = notes.map((n, i) => ({
    idx: i, midi: n.midi,
    dur: (n.duration || 0.5) / 60.0 * bpm,
    start: (n.time || 0) / 60.0 * bpm,
  }));
  items.sort((a, b) => a.start - b.start || a.idx - b.idx);

  const measures = [];
  for (const it of items) {
    const barIdx = Math.floor(it.start / beatsPerBar);
    let bar = measures[measures.length - 1];
    if (!bar || bar.barIdx !== barIdx) {
      bar = { barIdx, chords: [] };
      measures.push(bar);
    }
    const chord = bar.chords.find(c => Math.abs(c.startBeat - it.start) < 0.01);
    if (chord) {
      chord.midis.push(it.midi);
      chord.dur = Math.max(chord.dur, it.dur);
      chord.tracks.push(it.track);
    } else {
      bar.chords.push({ startBeat: it.start, midis: [it.midi], dur: it.dur,
                        idx: it.idx, tracks: [it.track] });
    }
  }
  return { measures, beatsPerBar };
}

function chunkAll(notes, bpm, meter) {
  return chunkMeasuresFor(notes, bpm, meter).measures;
}

// ===== 休止符补齐 =====
const REST_DUR = { w: 4, h: 2, q: 1, 8: 0.5, 16: 0.25 };
function restForBeats(rb) {
  const order = ['w', 'h', 'q', '8', '16'];
  const parts = [];
  let rest = Math.round(rb * 100) / 100;
  for (const code of order) {
    const v = REST_DUR[code];
    while (rest + 0.001 >= v) { parts.push(code); rest = Math.round((rest - v) * 100) / 100; }
    if (rest <= 0) break;
  }
  return parts;
}

// ===== 深色主题提亮 =====
const DARK_COLORS = new Set(['black', '#000', '#000000', '#444', '#333', '#999999']);
const LIGHT_FILL = '#dfe6f0';
function lightenSvg(root) {
  const els = root.querySelectorAll('*');
  for (const el of els) {
    for (const attr of ['fill', 'stroke']) {
      const v = el.getAttribute(attr);
      if (v && DARK_COLORS.has(v.toLowerCase())) el.setAttribute(attr, LIGHT_FILL);
    }
  }
}

function pickClefFor(notes) {
  if (!notes.length) return _state.clef;
  const avg = notes.reduce((s, n) => s + n.midi, 0) / notes.length;
  return avg < 55 ? 'bass' : 'treble';
}

function followBarRange() {
  if (_followBeat < 0) return null;
  const beatsPerBar = parseInt(_state.meter.split('/')[0], 10) || 4;
  const curBar = Math.floor(_followBeat / beatsPerBar);
  return [Math.max(0, curBar - FOLLOW_WINDOW.before),
          curBar + FOLLOW_WINDOW.after];
}

function measureInRange(bar, range) {
  return !range || (bar >= range[0] && bar <= range[1]);
}

// ===== 统一几何 —— 唯一参数 S 控制谱面真实像素大小 =====
// S = spacing_between_lines_px：glyph 在 SVG 里的真实像素，无缩放放大
function geom(range) {
  const S = 6;              // VexFlow spacing_between_lines_px：4=小, 5=中, 6=中+, 8=大, 10=默认
  const STEM = 3.5 * S;     // stem 默认长度公式
  const STAVE_H = 4 * S;
  const PAD_H = Math.max(3, Math.round(S));
  const LINE_H = PAD_H + STAVE_H + STEM * 2 + Math.max(2, Math.round(S/2));
  const colsPerLine = range ? FOLLOW_WINDOW.before + 1 + FOLLOW_WINDOW.after : 8;
  const CLEF_W = Math.max(18, Math.round(28 * (S/6)));
  // MEAS_W 算成：CLEF + cols*MEAS + right = 容器实际宽度（SVG 物理铺满，不缩放）
  // 先拿到容器宽度；拿不到时先用占位，render 里再设
  const wrapW = document.getElementById('wrap-tr').clientWidth || 720;
  const rightPad = Math.max(12, Math.round(18 * (S/6)));
  const MEAS_W = Math.max(30, Math.floor((wrapW - CLEF_W - rightPad) / colsPerLine));
  return { S, PAD_H, MEAS_W, CLEF_W, LINE_H, colsPerLine, rightPad };
}

// ===== 五线谱渲染 =====
function renderTreble() {
  const svg = document.getElementById('treble-svg');
  svg.innerHTML = '';
  _vfRefs = [];

  const tracks = trackOrder(_state.notes);
  if (tracks.length === 0) return;

  const beatsPerBar = parseInt(_state.meter.split('/')[0], 10) || 4;
  const allMeasures = chunkAll(_state.notes, _state.bpm, _state.meter);
  if (allMeasures.length === 0) return;

  const range = followBarRange();
  const g = geom(range);

  const trackRows = tracks.map(tr => {
    const trNotes = _state.notes.filter(n => (n.track || 'main') === tr);
    const { measures } = chunkMeasuresFor(trNotes, _state.bpm, _state.meter);
    const vis = measures.filter(m => measureInRange(m.barIdx, range));
    return Math.max(1, Math.ceil(vis.length / g.colsPerLine));
  });
  const totalRows = trackRows.reduce((s, x) => s + x, 0);
  const totalH = g.PAD_H + totalRows * g.LINE_H + Math.round(12 * (g.S/6));
  const wrapW = document.getElementById('wrap-tr').clientWidth || 720;
  // 居中：SVG 宽度 = 容器 - 两侧留白，靠 margin:0 auto 居中
  const sidePad = Math.round(wrapW * 0.06);
  const totalW = wrapW - sidePad * 2;

  const r = new Renderer(svg, Renderer.Backends.SVG);
  r.resize(totalW, totalH);
  svg.setAttribute('viewBox', '0 0 ' + totalW + ' ' + totalH);
  svg.style.width = totalW + 'px';
  svg.style.height = totalH + 'px';
  svg.style.margin = '0 auto';
  svg.removeAttribute('height');
  const ctx = r.getContext();

  const trackLabelFont = Math.round(9 * (g.S/10));

  let yCursor = g.PAD_H;
  tracks.forEach((tr, ti) => {
    const trNotes = _state.notes.filter(n => (n.track || 'main') === tr);
    const { measures } = chunkMeasuresFor(trNotes, _state.bpm, _state.meter);
    if (measures.length === 0) return;
    const clef = pickClefFor(trNotes);
    const vis = measures.filter(m => measureInRange(m.barIdx, range));
    if (vis.length === 0) return;

    const rows = [];
    for (let s = 0; s < vis.length; s += g.colsPerLine) rows.push(vis.slice(s, s + g.colsPerLine));

    rows.forEach((rowMeasures, ri) => {
      const y0 = yCursor;
      rowMeasures.forEach((bar, bi) => {
        const x = g.CLEF_W + bi * g.MEAS_W;
        const st = new Stave(x, y0, g.MEAS_W, { spacing_between_lines_px: g.S });
        if (bi === 0) {
          st.addClef(clef).addKeySignature(_state.key);
          if (ri === 0 && ti === 0) st.addTimeSignature(_state.meter);
        }
        if (range && bar.barIdx === range[0] + FOLLOW_WINDOW.before) {
          const NS2 = 'http://www.w3.org/2000/svg';
          const rect = document.createElementNS(NS2, 'rect');
          rect.setAttribute('x', x + 2);
          rect.setAttribute('y', y0 - Math.round(4 * (g.S/10)));
          rect.setAttribute('width', g.MEAS_W - 4);
          rect.setAttribute('height', (4 * g.S) + Math.round(8 * (g.S/6)));
          rect.setAttribute('fill', '#3b82f6');
          rect.setAttribute('opacity', '0.18');
          rect.setAttribute('rx', '4');
          svg.insertBefore(rect, svg.firstChild);
        }
        st.setContext(ctx).draw();

        const barStart = bar.barIdx * beatsPerBar;
        const tickables = [];
        const chordToVf = new Map();
        const sorted = bar.chords.slice().sort((a, b) => a.startBeat - b.startBeat);
        let cursor = barStart;
        const leadingGap = sorted.length ? Math.max(0, sorted[0].startBeat - barStart) : beatsPerBar;
        if (leadingGap > 0.09) for (const code of restForBeats(leadingGap)) tickables.push(new GhostNote({ duration: code }));

        for (const ch of sorted) {
          const { dur, dots } = beatsToVf(ch.dur);
          const vfN = new StaveNote({
            keys: ch.midis.slice().sort((a, b) => a - b).map(midiToVf),
            duration: dur, dots: dots,
            clef: clef, auto_stem: true,
          });
          if (ch.idx === _highlightIdx) vfN.setStyle({ fillStyle: _highlightColor, strokeStyle: _highlightColor });
          tickables.push(vfN);
          chordToVf.set(ch, vfN);
          cursor = Math.max(cursor, ch.startBeat + ch.dur);
        }
        const tailGap = Math.max(0, barStart + beatsPerBar - cursor);
        if (tailGap > 0.09) for (const code of restForBeats(tailGap)) tickables.push(new GhostNote({ duration: code }));

        const voice = new Voice({ num_beats: beatsPerBar, beat_value: 4 });
        voice.setStrict(false);
        voice.addTickables(tickables);
        try {
          new Formatter().joinVoices([voice]).formatToStave([voice], st);
          voice.draw(ctx, st);
        } catch (e) { console.log('bar render error:', e.message || e); }

        bar.chords.forEach((ch, ci) => {
          const vfN = chordToVf.get(ch);
          if (!vfN) return;
          _vfRefs.push({ staveNote: vfN, svgX: x + Math.round(22 * (g.S/10)) + ci * Math.round(14 * (g.S/10)),
                         noteIdx: ch.idx, track: tr, line: yi(ri, ti, trackRows) });
        });
      });
      if (ri === 0) {
        ctx.save();
        ctx.setFont('sans-serif', trackLabelFont);
        ctx.setFillStyle(_trackPalette[ti % _trackPalette.length]);
        ctx.fillText(tr.length > 6 ? tr.slice(0, 6) + '…' : tr, 2, y0 + Math.round(14 * (g.S/10)));
        ctx.restore();
      }
      yCursor += g.LINE_H;
    });
  });
  lightenSvg(svg);
}

function yi(ri, ti, trackRows) {
  let line = 0;
  for (let i = 0; i < ti; i++) line += trackRows[i];
  return line + ri;
}

// ===== 简谱渲染 =====
function renderJianpu() {
  const svg = document.getElementById('jp-svg');
  svg.innerHTML = '';
  _jpRefs = [];

  const tracks = trackOrder(_state.notes);
  if (tracks.length === 0) return;

  const beatsPerBar = parseInt(_state.meter.split('/')[0], 10) || 4;
  const allMeasures = chunkAll(_state.notes, _state.bpm, _state.meter);
  if (allMeasures.length === 0) return;

  const NS = 'http://www.w3.org/2000/svg';
  const range = followBarRange();
  const g = geom(range);
  const jpMap = {0:'1',1:'#1',2:'2',3:'#2',4:'3',5:'4',6:'#4',7:'5',8:'#5',9:'6',10:'#6',11:'7'};

  const trackRows = tracks.map(tr => {
    const trNotes = _state.notes.filter(n => (n.track || 'main') === tr);
    const { measures } = chunkMeasuresFor(trNotes, _state.bpm, _state.meter);
    const vis = measures.filter(m => measureInRange(m.barIdx, range));
    return Math.max(1, Math.ceil(vis.length / g.colsPerLine));
  });
  const totalRows = trackRows.reduce((s, x) => s + x, 0);
  const totalH = g.PAD_H + totalRows * g.LINE_H + Math.round(10 * (g.S/6));
  const wrapW = document.getElementById('wrap-jp').clientWidth || 720;
  const sidePad = Math.round(wrapW * 0.06);
  const totalW = wrapW - sidePad * 2;

  svg.setAttribute('viewBox', '0 0 ' + totalW + ' ' + totalH);
  svg.style.width = totalW + 'px';
  svg.style.height = totalH + 'px';
  svg.style.margin = '0 auto';
  svg.removeAttribute('height');

  const jpFont = Math.round(22 * (g.S/10));
  const jpLineH = g.LINE_H;
  const trackLabelFont = Math.round(9 * (g.S/10));

  let yCursor = g.PAD_H;
  tracks.forEach((tr, ti) => {
    const trNotes = _state.notes.filter(n => (n.track || 'main') === tr);
    const { measures } = chunkMeasuresFor(trNotes, _state.bpm, _state.meter);
    if (measures.length === 0) return;
    const vis = measures.filter(m => measureInRange(m.barIdx, range));
    if (vis.length === 0) return;

    const rows = [];
    for (let s = 0; s < vis.length; s += g.colsPerLine) rows.push(vis.slice(s, s + g.colsPerLine));

    rows.forEach((rowMeasures, ri) => {
      const lineY = yCursor;
      const baseY = lineY + Math.round(22 * (g.S/10));
      rowMeasures.forEach((bar, bi) => {
        const barStart = bar.barIdx * beatsPerBar;
        const lx = g.CLEF_W + bi * g.MEAS_W - Math.round(2 * (g.S/10));
        addLine(svg, NS, lx, lineY, lx, lineY + jpLineH, '#3a3a5e', Math.round(1.5 * (g.S/10)));
        if (bi === 0 && ri === 0) {
          const t = addText(svg, NS, 4, baseY, tr.length > 5 ? tr.slice(0, 5) : tr, 0, trackLabelFont, '#888');
          t.setAttribute('text-anchor', 'start');
        }
        const x0 = g.CLEF_W + bi * g.MEAS_W + Math.round(6 * (g.S/10));
        for (const ch of bar.chords) {
          const localBeat = ch.startBeat - barStart;
          const cx = x0 + localBeat * (g.MEAS_W - Math.round(14 * (g.S/10))) / beatsPerBar;
          const sortedMidis = ch.midis.slice().sort((a, b) => a - b);
          const noteStep = Math.round(14 * (g.S/10));
          sortedMidis.forEach((midi, ki) => {
            const digit = jpMap[midi % 12] || '?';
            const oct = Math.floor(midi / 12) - 5;
            const y = baseY - (sortedMidis.length - 1 - ki) * noteStep;
            const el = addText(svg, NS, cx, y, digit, oct, jpFont);
            el.dataset.idx = ch.idx;
            el.classList.add('jp-note');
            const durBeats = ch.dur;
            const uY = y + Math.round(8 * (g.S/10));
            if (durBeats < 0.4) addUnderline(svg, NS, cx, uY, 2, (g.S/10));
            else if (durBeats < 0.8) addUnderline(svg, NS, cx, uY, 1, (g.S/10));
            _jpRefs.push({ el, svgX: cx, svgY: lineY, noteIdx: ch.idx, track: tr,
                           line: yi(ri, ti, trackRows) });
          });
        }
      });
      yCursor += jpLineH;
    });
  });
  if (_highlightIdx >= 0) applyJpHighlight();
}

// ---- SVG helpers ----
function addText(svg, NS, x, y, digit, oct, size, color) {
  const g = document.createElementNS(NS, 'g');
  const t = document.createElementNS(NS, 'text');
  t.setAttribute('x', x); t.setAttribute('y', y);
  t.setAttribute('text-anchor', 'middle');
  t.setAttribute('font-size', size);
  t.setAttribute('font-family', 'sans-serif');
  t.setAttribute('fill', color || '#e8e8e8');
  t.setAttribute('font-weight', 'bold');
  t.textContent = digit;
  g.appendChild(t);
  const r = Math.max(1.6, Math.round(size * 0.14));
  const octStep = Math.max(4, Math.round(size * 0.28));
  if (oct > 0) {
    for (let d = 0; d < oct; d++) {
      const c = document.createElementNS(NS, 'circle');
      c.setAttribute('cx', x); c.setAttribute('cy', y - size * 0.85 - d * octStep);
      c.setAttribute('r', r); c.setAttribute('fill', color || '#e8e8e8');
      g.appendChild(c);
    }
  } else if (oct < 0) {
    for (let d = 0; d < -oct; d++) {
      const c = document.createElementNS(NS, 'circle');
      c.setAttribute('cx', x); c.setAttribute('cy', y + size * 0.35 + d * octStep);
      c.setAttribute('r', r); c.setAttribute('fill', color || '#e8e8e8');
      g.appendChild(c);
    }
  }
  svg.appendChild(g);
  return t;
}
function addUnderline(svg, NS, cx, y, count, sc) {
  const w = Math.max(4, Math.round(9 * sc));
  const gap = Math.max(2, Math.round(4 * sc));
  const sw = Math.max(1, Math.round(1.6 * sc));
  for (let i = 0; i < count; i++) {
    const l = document.createElementNS(NS, 'line');
    l.setAttribute('x1', cx - w); l.setAttribute('x2', cx + w);
    l.setAttribute('y1', y + i * gap); l.setAttribute('y2', y + i * gap);
    l.setAttribute('stroke', '#888'); l.setAttribute('stroke-width', sw);
    svg.appendChild(l);
  }
}
function addLine(svg, NS, x1, y1, x2, y2, color, w) {
  const l = document.createElementNS(NS, 'line');
  l.setAttribute('x1', x1); l.setAttribute('y1', y1);
  l.setAttribute('x2', x2); l.setAttribute('y2', y2);
  l.setAttribute('stroke', color); l.setAttribute('stroke-width', w);
  svg.appendChild(l);
}

// ===== 高亮 =====
function applyVfHighlight() { renderTreble(); }
function applyJpHighlight() {
  const color = _highlightColor;
  for (const ref of _jpRefs) {
    const on = ref.noteIdx === _highlightIdx;
    ref.el.setAttribute('fill', on ? color : '#e8e8e8');
    ref.el.setAttribute('font-size', on ? Math.round(26) : Math.round(22));
  }
  scrollToNote(ref => ref.noteIdx === _highlightIdx);
}
function applyHighlight() {
  if (_mode === 'treble') applyVfHighlight();
  else applyJpHighlight();
}
function scrollToNote(matchFn) {
  const list = _mode === 'treble' ? _vfRefs : _jpRefs;
  const ref = list.find(matchFn);
  if (!ref) return;
  const wrap = _mode === 'treble' ? document.getElementById('wrap-tr') : document.getElementById('wrap-jp');
  wrap.scrollTop = Math.max(0, ref.line * (Math.round(52 * 0.6)) - 10);
}

// ===== 模式切换 =====
function setMode(mode) {
  _mode = (mode === 'jianpu') ? 'jianpu' : 'treble';
  const showTr = _mode === 'treble';
  document.getElementById('wrap-tr').style.display = showTr ? 'block' : 'none';
  document.getElementById('wrap-jp').style.display = showTr ? 'none' : 'block';
  if (_state.notes.length > 0) {
    if (showTr) renderTreble(); else renderJianpu();
  }
  requestAnimationFrame(() => {
    if (_state.notes.length > 0) {
      if (showTr) renderTreble(); else renderJianpu();
    }
  });
}

// ===== 演奏跟随 =====
function setFollow(beat) {
  _followBeat = beat;
  if (_state.notes.length > 0) {
    if (_mode === 'treble') renderTreble(); else renderJianpu();
  }
}
function clearFollow() {
  _followBeat = -1;
  if (_state.notes.length > 0) {
    if (_mode === 'treble') renderTreble(); else renderJianpu();
  }
}

// ===== 主入口 =====
function render(data) {
  if (!window.Vex || !Vex.Flow) {
    document.getElementById('hint').textContent = 'VexFlow 加载失败 — 请确认 assets/vexflow.js 存在';
    return;
  }
  _state.notes = (data && data.notes) || [];
  _state.clef  = (data && data.clef)  || 'treble';
  _state.key   = (data && data.key)   || 'C';
  _state.meter = (data && data.meter) || '4/4';
  _state.bpm   = (data && data.bpm)   || 120;
  _highlightIdx = -1;
  _followBeat = -1;

  if (_state.notes.length === 0) {
    const hint = document.getElementById('hint');
    hint.style.display = 'block';
    hint.textContent = '选择练习或导入 MIDI 开始';
    document.getElementById('wrap-tr').style.display = 'none';
    document.getElementById('wrap-jp').style.display = 'none';
    return;
  }
  document.getElementById('hint').style.display = 'none';
  if (_mode === 'treble') {
    document.getElementById('wrap-tr').style.display = 'block';
    document.getElementById('wrap-jp').style.display = 'none';
    renderTreble();
  } else {
    document.getElementById('wrap-tr').style.display = 'none';
    document.getElementById('wrap-jp').style.display = 'block';
    renderJianpu();
  }
}

function highlightVf(index, color) {
  _highlightIdx = index;
  _highlightColor = color || '#fbbf24';
  applyHighlight();
}
function clearHighlight() {
  _highlightIdx = -1;
  applyHighlight();
}

// 容器尺寸变化时重渲染
(function () {
  let rszTimer = null;
  window.addEventListener('resize', function () {
    if (rszTimer) clearTimeout(rszTimer);
    rszTimer = setTimeout(function () {
      if (_state.notes.length > 0) {
        if (_mode === 'treble') renderTreble(); else renderJianpu();
      }
    }, 120);
  });
})();
</script>
</body>
</html>
"""