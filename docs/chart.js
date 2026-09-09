/* Candle + volume + moving-average chart on a plain canvas.
   No library: the whole app has to work offline from the service-worker cache,
   and a phone renders a few hundred rects far faster than it parses a charting
   bundle. */
(function (global) {
  'use strict';

  var COLORS = {
    up: '#22c55e', down: '#ef4444',
    ma50: '#38bdf8', ma150: '#f59e0b', ma200: '#a78bfa',
    pivot: '#e6edf3', support: '#ef4444',
    grid: '#24303d', text: '#8b9bb0', volUp: '#1f6f3f', volDown: '#7f2b2b',
    warn: '#f59e0b'
  };

  function niceTicks(min, max, count) {
    var span = max - min;
    if (!isFinite(span) || span <= 0) return [min];
    var raw = span / count;
    var mag = Math.pow(10, Math.floor(Math.log10(raw)));
    var norm = raw / mag;
    var step = (norm >= 5 ? 5 : norm >= 2 ? 2 : 1) * mag;
    var out = [], v = Math.ceil(min / step) * step;
    for (; v <= max + 1e-9; v += step) out.push(v);
    return out;
  }

  /* Group daily bars into weekly candles.

     The moving averages stay the *daily* 50/150/200 SMAs sampled at each
     week's close, not 50/150/200-week averages: they are the same lines the
     Stage 2 template is judged on, just drawn at weekly resolution. Switching
     them to week counts would show something the screen never tested. */
  function aggregateWeekly(series) {
    var n = series.c.length;
    var out = { d: [], o: [], h: [], l: [], c: [], v: [] };
    var maKeys = ['ma50', 'ma150', 'ma200'].filter(function (k) { return series[k]; });
    maKeys.forEach(function (k) { out[k] = []; });
    // Lets caller remap daily bar indices (contraction markers) onto weeks.
    var dayToWeek = new Array(n);

    var key = null;
    for (var i = 0; i < n; i++) {
      var k = mondayOf(series.d[i]);
      if (k !== key) {
        key = k;
        out.d.push(series.d[i]);
        out.o.push(series.o[i]);
        out.h.push(series.h[i]);
        out.l.push(series.l[i]);
        out.c.push(series.c[i]);
        out.v.push(series.v[i] || 0);
        maKeys.forEach(function (mk) { out[mk].push(series[mk][i]); });
      } else {
        var j = out.d.length - 1;
        out.d[j] = series.d[i];                                  // label on the week's last session
        if (series.h[i] > out.h[j]) out.h[j] = series.h[i];
        if (series.l[i] < out.l[j]) out.l[j] = series.l[i];
        out.c[j] = series.c[i];
        out.v[j] += series.v[i] || 0;
        maKeys.forEach(function (mk) { out[mk][j] = series[mk][i]; });
      }
      dayToWeek[i] = out.d.length - 1;
    }
    out.dayToWeek = dayToWeek;
    return out;
  }

  /* ISO date of the Monday starting the week that contains `iso`. */
  function mondayOf(iso) {
    var d = new Date(iso + 'T00:00:00Z');
    var dow = (d.getUTCDay() + 6) % 7;        // Monday = 0
    d.setUTCDate(d.getUTCDate() - dow);
    return d.toISOString().slice(0, 10);
  }

  function fmtPrice(v) {
    if (v >= 1000) return v.toFixed(0);
    if (v >= 100) return v.toFixed(1);
    return v.toFixed(2);
  }

  /**
   * @param canvas  target canvas element
   * @param series  {d,o,h,l,c,v,ma50,ma150,ma200} arrays, oldest first
   * @param opts    {bars, pivot, support, contractions}
   */
  function draw(canvas, series, opts) {
    opts = opts || {};
    var dpr = Math.min(global.devicePixelRatio || 1, 2);
    var cssW = canvas.clientWidth || 320;
    var cssH = canvas.clientHeight || 300;
    canvas.width = Math.round(cssW * dpr);
    canvas.height = Math.round(cssH * dpr);
    var ctx = canvas.getContext('2d');
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);

    var total = series.c.length;
    var n = Math.min(opts.bars || total, total);
    var from = total - n;

    // Layout: price pane on top, volume pane beneath.
    var padL = 6, padR = 46, padT = 10, padB = 18;
    var volH = Math.round(cssH * 0.18);
    var priceH = cssH - padT - padB - volH - 6;
    var plotW = cssW - padL - padR;

    var slice = function (arr) { return arr.slice(from); };
    var highs = slice(series.h), lows = slice(series.l),
        opens = slice(series.o), closes = slice(series.c), vols = slice(series.v);

    var maKeys = ['ma50', 'ma150', 'ma200'].filter(function (k) { return series[k]; });
    var mas = {};
    maKeys.forEach(function (k) { mas[k] = slice(series[k]); });

    var lo = Infinity, hi = -Infinity;
    for (var i = 0; i < n; i++) {
      if (lows[i] != null && lows[i] < lo) lo = lows[i];
      if (highs[i] != null && highs[i] > hi) hi = highs[i];
    }
    // Including the long averages in the scale is right for a wide view, but
    // ruins a zoomed one: with price at 190 and the 200MA at 150, the base
    // gets squashed into the top eighth of the pane. When fitting to the base,
    // scale to the candles and the levels, and let the averages run off.
    if (!opts.fitPriceOnly) {
      maKeys.forEach(function (k) {
        for (var j = 0; j < n; j++) {
          var v = mas[k][j];
          if (v == null) continue;
          if (v < lo) lo = v; if (v > hi) hi = v;
        }
      });
    }
    [opts.pivot, opts.support].forEach(function (v) {
      if (v == null) return;
      if (v < lo) lo = v; if (v > hi) hi = v;
    });
    if (!isFinite(lo) || !isFinite(hi) || hi <= lo) { lo = 0; hi = 1; }
    var pad = (hi - lo) * 0.06;
    lo -= pad; hi += pad;

    var y = function (p) { return padT + priceH - (p - lo) / (hi - lo) * priceH; };
    var slot = plotW / n;
    var bw = Math.max(1, Math.min(9, slot * 0.68));
    var x = function (i) { return padL + slot * (i + 0.5); };

    // --- grid + price axis
    ctx.font = '10px -apple-system, sans-serif';
    ctx.textBaseline = 'middle';
    niceTicks(lo, hi, 4).forEach(function (t) {
      var yy = Math.round(y(t)) + 0.5;
      ctx.strokeStyle = COLORS.grid; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(padL + plotW, yy); ctx.stroke();
      ctx.fillStyle = COLORS.text; ctx.textAlign = 'left';
      ctx.fillText(fmtPrice(t), padL + plotW + 5, yy);
    });

    // --- shade each contraction so the tightening is visible at a glance
    var lastLabelRight = -Infinity, labelRow = 0;
    (opts.contractions || []).forEach(function (c, idx) {
      var a = c.high_idx - from, b = c.low_idx - from;
      if (b < 0 || a > n) return;
      a = Math.max(a, 0); b = Math.min(b, n - 1);
      var x0 = padL + slot * a, x1 = padL + slot * (b + 1);
      // Amber for a consolidation wider than the one before it: the base
      // loosened there, which is what stops it being a textbook VCP.
      var shade = Math.min(0.05 + idx * 0.025, 0.16);
      ctx.fillStyle = c.widened ? 'rgba(245,158,11,' + (shade + 0.06) + ')'
                                : 'rgba(56,189,248,' + shade + ')';
      ctx.fillRect(x0, padT, Math.max(x1 - x0, 1), priceH);
      // Label each consolidation with its number and how deep it was - the
      // shrinking sequence is the whole point of the pattern.
      var mid = (x0 + x1) / 2;
      ctx.textAlign = 'center';
      ctx.font = 'bold 10px -apple-system, sans-serif';
      var label = c.depth_pct != null ? c.depth_pct.toFixed(1) + '%' : '';
      var width = Math.max(ctx.measureText(label).width, 16);
      // Narrow neighbouring bands would print their labels on top of each
      // other, so stagger onto a second line instead.
      labelRow = (mid - width / 2 < lastLabelRight + 3) ? (labelRow + 1) % 2 : 0;
      lastLabelRight = mid + width / 2;
      var top = padT + 9 + labelRow * 24;
      // The pivot line often runs straight through this text, so lay a chip
      // behind it.
      var chipW = Math.max(width, 20) + 8;
      ctx.fillStyle = 'rgba(11,15,20,.72)';
      ctx.fillRect(mid - chipW / 2, top - 9, chipW, label ? 23 : 11);
      ctx.fillStyle = COLORS.text;
      ctx.fillText('T' + c.index, mid, top);
      if (label) {
        ctx.fillStyle = c.widened ? COLORS.warn : COLORS.up;
        ctx.fillText(label, mid, top + 12);
      }
      ctx.font = '10px -apple-system, sans-serif';
    });

    // --- volume pane
    var vMax = 0;
    for (var k = 0; k < n; k++) if (vols[k] > vMax) vMax = vols[k];
    var volTop = padT + priceH + 6;
    for (var m = 0; m < n; m++) {
      var vh = vMax ? (vols[m] / vMax) * volH : 0;
      ctx.fillStyle = closes[m] >= opens[m] ? COLORS.volUp : COLORS.volDown;
      ctx.fillRect(x(m) - bw / 2, volTop + volH - vh, bw, Math.max(vh, 0.5));
    }

    // --- candles
    for (var q = 0; q < n; q++) {
      var up = closes[q] >= opens[q];
      ctx.strokeStyle = ctx.fillStyle = up ? COLORS.up : COLORS.down;
      var cx = Math.round(x(q)) + 0.5;
      ctx.lineWidth = 1;
      ctx.beginPath(); ctx.moveTo(cx, y(highs[q])); ctx.lineTo(cx, y(lows[q])); ctx.stroke();
      var yo = y(opens[q]), yc = y(closes[q]);
      var top = Math.min(yo, yc), h = Math.max(Math.abs(yc - yo), 1);
      ctx.fillRect(cx - bw / 2, top, bw, h);
    }

    // --- moving averages
    ctx.save();
    ctx.beginPath();
    ctx.rect(padL, padT, plotW, priceH);
    ctx.clip();
    maKeys.forEach(function (key) {
      ctx.strokeStyle = COLORS[key]; ctx.lineWidth = 1.4;
      ctx.beginPath();
      var started = false;
      for (var i2 = 0; i2 < n; i2++) {
        var v2 = mas[key][i2];
        if (v2 == null) { started = false; continue; }
        var px = x(i2), py = y(v2);
        if (started) ctx.lineTo(px, py); else { ctx.moveTo(px, py); started = true; }
      }
      ctx.stroke();
    });
    ctx.restore();

    // --- pivot and support lines
    function level(value, color, label) {
      if (value == null) return;
      var yy = Math.round(y(value)) + 0.5;
      ctx.save();
      ctx.strokeStyle = color; ctx.lineWidth = 1; ctx.setLineDash([4, 3]);
      ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(padL + plotW, yy); ctx.stroke();
      ctx.restore();
      // Chip behind the label so it stays readable over candles.
      ctx.font = '9px -apple-system, sans-serif';
      var tw = ctx.measureText(label).width;
      ctx.fillStyle = 'rgba(11,15,20,.82)';
      ctx.fillRect(padL + 1, yy - 12, tw + 8, 11);
      ctx.fillStyle = color; ctx.textAlign = 'left';
      ctx.fillText(label, padL + 5, yy - 6.5);
      ctx.font = '10px -apple-system, sans-serif';
    }
    level(opts.pivot, COLORS.pivot, 'PIVOT ' + fmtPrice(opts.pivot || 0));
    level(opts.support, COLORS.support, 'STOP ' + fmtPrice(opts.support || 0));

    // --- date axis: first, middle, last
    ctx.fillStyle = COLORS.text; ctx.font = '9px -apple-system, sans-serif';
    var dates = series.d.slice(from);
    [[0, 'left'], [Math.floor(n / 2), 'center'], [n - 1, 'right']].forEach(function (p) {
      var i3 = p[0];
      if (!dates[i3]) return;
      ctx.textAlign = p[1];
      var xx = p[1] === 'left' ? padL : p[1] === 'right' ? padL + plotW : padL + plotW / 2;
      ctx.fillText(dates[i3].slice(2), xx, cssH - 7);
    });
  }

  global.VCPChart = { draw: draw, COLORS: COLORS, aggregateWeekly: aggregateWeekly };
})(window);
