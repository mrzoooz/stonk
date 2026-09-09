/* VCP Screener - list, filtering and the detail sheet.
   Data is a static JSON bundle produced by the nightly GitHub Actions run. */
(function () {
  'use strict';

  var DATA_URL = 'data/screen.json';
  var STAR_KEY = 'vcp.starred';
  var CALC_KEY = 'vcp.calc';
  var CHART_KEY = 'vcp.chart';
  var MISSED_KEY = 'vcp.hideMissed';

  var state = {
    data: null,
    tab: 'ready',
    sort: 'score',
    query: '',
    starred: loadStars(),
    current: null,
    months: 6,
    interval: 'daily',
    collapsed: false,
    expanded: false,
    hideMissed: true
  };

  // Roughly how many bars make up a month at each interval.
  var BARS_PER_MONTH = { daily: 21.7, weekly: 4.35 };

  var el = {};
  ['status', 'list', 'empty', 'tabs', 'search', 'sort', 'refresh', 'detail', 'back',
   'd-symbol', 'd-name', 'd-star', 'chart', 'chart-legend', 'plan', 'd-contractions',
   'd-vcp', 'd-stage2', 'acct', 'riskpct', 'calc-out', 'foot-note', 'range-row',
   'chart-wrap', 'calc-card', 'interval-row', 'chart-controls',
   'chart-panel', 'chart-collapse', 'chart-expand', 'chart-body',
   'd-price', 'd-change', 'chart-price', 'chart-title',
   'hide-missed', 'hide-missed-wrap'
  ].forEach(function (id) { el[id] = document.getElementById(id); });

  // ------------------------------------------------------------- storage
  function loadStars() {
    try { return JSON.parse(localStorage.getItem(STAR_KEY)) || []; }
    catch (e) { return []; }
  }
  function saveStars() {
    try { localStorage.setItem(STAR_KEY, JSON.stringify(state.starred)); } catch (e) {}
  }
  function isStarred(sym) { return state.starred.indexOf(sym) !== -1; }
  function toggleStar(sym) {
    var i = state.starred.indexOf(sym);
    if (i === -1) state.starred.push(sym); else state.starred.splice(i, 1);
    saveStars();
    updateCounts();
  }

  // ------------------------------------------------------------ formatting
  function num(v, nd) {
    if (v == null || isNaN(v)) return '--';
    return Number(v).toFixed(nd == null ? 2 : nd);
  }
  function pct(v, nd) { return v == null || isNaN(v) ? '--' : Number(v).toFixed(nd == null ? 1 : nd) + '%'; }
  function money(v) {
    if (v == null || isNaN(v)) return '--';
    var a = Math.abs(v);
    if (a >= 1e12) return '$' + (v / 1e12).toFixed(1) + 'T';
    if (a >= 1e9) return '$' + (v / 1e9).toFixed(1) + 'B';
    if (a >= 1e6) return '$' + (v / 1e6).toFixed(0) + 'M';
    return '$' + v.toFixed(0);
  }
  // Thresholds come from the scan's config, so the colours can never drift
  // from the rules that produced the numbers.
  function limits() {
    var c = (state.data && state.data.config) || {};
    return {
      maxRisk: c.max_risk_pct == null ? 8 : c.max_risk_pct,
      okRisk: c.preferred_max_risk_pct == null ? 5 : c.preferred_max_risk_pct,
      minRR: c.min_reward_risk == null ? 2 : c.min_reward_risk,
      okRR: c.preferred_reward_risk == null ? 3 : c.preferred_reward_risk
    };
  }

  /* Green at or better than preferred, amber inside the accepted band.

     Classified on the rounded value that is actually displayed: a reward:risk
     of 2.97 shows as "3.0", and colouring that amber against a "3+ is green"
     rule reads as a bug rather than a borderline number. */
  function riskClass(v) {
    var L = limits();
    if (v == null || isNaN(v)) return '';
    var shown = Number(Number(v).toFixed(1));
    if (shown <= L.okRisk) return 'good';
    return shown <= L.maxRisk ? 'warn' : 'bad';
  }
  function rrClass(v) {
    var L = limits();
    if (v == null || isNaN(v)) return '';
    var shown = Number(Number(v).toFixed(1));
    if (shown >= L.okRR) return 'good';
    return shown >= L.minRR ? 'warn' : 'bad';
  }

  var STATUS_LABEL = {
    actionable: 'In buy zone',
    broke_out: 'Missed - pivot already broken',
    extended: 'Extended',
    forming: 'Still forming',
    none: 'No base'
  };

  // ------------------------------------------------------------- rows
  function rowsFor(tab) {
    var d = state.data;
    if (!d) return [];
    if (tab === 'starred') {
      return d.ready.concat(d.watch, d.stage2).filter(function (r) { return isStarred(r.symbol); });
    }
    return d[tab] || [];
  }

  function metricsOf(row) {
    // Full rows carry vcp+stage2; thinned Stage 2 rows carry a flat `metrics`.
    var vcp = (row.vcp && row.vcp.metrics) || {};
    var s2 = (row.stage2 && row.stage2.metrics) || row.metrics || {};
    return { vcp: vcp, s2: s2 };
  }

  function sortRows(rows) {
    var by = state.sort;
    var copy = rows.slice();
    copy.sort(function (a, b) {
      var ma = metricsOf(a), mb = metricsOf(b);
      function g(m, path) {
        var v = path === 'rs' ? m.s2.rs_126d : m.vcp[path];
        return v == null || isNaN(v) ? null : Number(v);
      }
      switch (by) {
        case 'risk': return cmpAsc(g(ma, 'risk_pct'), g(mb, 'risk_pct'));
        case 'rr': return cmpDesc(g(ma, 'reward_risk'), g(mb, 'reward_risk'));
        case 'tight': return cmpAsc(g(ma, 'final_depth_pct'), g(mb, 'final_depth_pct'));
        case 'near': return cmpDesc(g(ma, 'distance_to_pivot_pct'), g(mb, 'distance_to_pivot_pct'));
        case 'rs': return cmpDesc(g(ma, 'rs'), g(mb, 'rs'));
        case 'symbol': return a.symbol.localeCompare(b.symbol);
        default: return (b.score || 0) - (a.score || 0);
      }
    });
    return copy;
  }
  function cmpAsc(a, b) { if (a == null) return 1; if (b == null) return -1; return a - b; }
  function cmpDesc(a, b) { if (a == null) return 1; if (b == null) return -1; return b - a; }

  function filterRows(rows) {
    // A name trading above its pivot is a trade that already went; hiding them
    // keeps the list to what is still entrable.
    if (state.hideMissed) {
      rows = rows.filter(function (r) {
        return !(r.vcp && r.vcp.status === 'broke_out');
      });
    }
    var q = state.query.trim().toUpperCase();
    if (!q) return rows;
    return rows.filter(function (r) {
      return r.symbol.indexOf(q) !== -1 || (r.name || '').toUpperCase().indexOf(q) !== -1;
    });
  }

  // ------------------------------------------------------------- rendering
  function ladder(contractions) {
    if (!contractions || !contractions.length) return '';
    var max = Math.max.apply(null, contractions.map(function (c) { return c.depth_pct; }));
    return '<div class="ladder">' + contractions.map(function (c) {
      var w = Math.max(4, (c.depth_pct / max) * 100);
      return '<div class="rung"><span class="lbl">T' + c.index + '</span>' +
             '<span class="bar" style="width:' + w.toFixed(0) + '%"></span>' +
             '<span class="val">' + c.depth_pct.toFixed(1) + '%</span></div>';
    }).join('') + '</div>';
  }

  function cardHTML(row) {
    var m = metricsOf(row);
    var v = m.vcp, s = m.s2;
    var status = (row.vcp && row.vcp.status) || 'none';
    var hasVcp = v.pivot != null;

    var metrics = hasVcp ? [
      ['Pivot', num(v.pivot), ''],
      ['Stop', num(v.support), ''],
      ['Risk', pct(v.risk_pct), riskClass(v.risk_pct)],
      ['R:R', v.reward_risk == null ? '--' : v.reward_risk.toFixed(1), rrClass(v.reward_risk)]
    ] : [
      ['50MA', num(s.ma50), ''],
      ['150MA', num(s.ma150), ''],
      ['Beta', num(s.beta), ''],
      ['vs SPY', pct(s.rs_126d, 0), s.rs_126d > 0 ? 'good' : '']
    ];

    return '' +
      '<article class="card" data-sym="' + row.symbol + '">' +
        '<div class="card-top">' +
          '<span class="sym">' + row.symbol + '</span>' +
          '<span class="pill ' + status + '">' + (STATUS_LABEL[status] || status) + '</span>' +
          '<span class="price">' + num(row.price) +
            (row.change_pct != null && !isNaN(row.change_pct)
              ? ' <span class="chg ' + (row.change_pct >= 0 ? 'up' : 'down') + '">' +
                (row.change_pct >= 0 ? '+' : '') + row.change_pct.toFixed(1) + '%</span>'
              : '') +
          '</span>' +
          '<button class="star ' + (isStarred(row.symbol) ? 'on' : '') + '" data-star="' + row.symbol +
            '" aria-label="Star">' + (isStarred(row.symbol) ? '★' : '☆') + '</button>' +
        '</div>' +
        '<p class="name">' + escapeHTML(row.name || '') + '</p>' +
        '<div class="metrics">' + metrics.map(function (x) {
          return '<div class="metric"><div class="k">' + x[0] + '</div>' +
                 '<div class="v ' + x[2] + '">' + x[1] + '</div></div>';
        }).join('') + '</div>' +
        ladder(row.vcp && row.vcp.contractions) +
        (row.bucket !== 'ready' && (row.vcp_reason || (row.vcp && row.vcp.reason))
          ? '<p class="reason">' + escapeHTML(row.vcp_reason || row.vcp.reason) + '</p>' : '') +
      '</article>';
  }

  function escapeHTML(s) {
    return String(s).replace(/[&<>"']/g, function (c) {
      return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
    });
  }

  function render() {
    if (!state.data) return;
    var all = rowsFor(state.tab);
    var rows = sortRows(filterRows(all));
    var missed = all.filter(function (r) {
      return r.vcp && r.vcp.status === 'broke_out';
    }).length;
    el['hide-missed-wrap'].hidden = missed === 0 && !state.hideMissed;
    el['hide-missed-wrap'].title = missed + ' name(s) here have already broken their pivot';
    el.list.innerHTML = rows.map(cardHTML).join('');
    el.empty.hidden = rows.length > 0;
    if (!rows.length) {
      el.empty.textContent = state.query
        ? 'Nothing matches "' + state.query + '".'
        : emptyMessage(state.tab);
    }
  }

  function emptyMessage(tab) {
    if (tab === 'starred') return 'No starred names yet. Tap the star on a card to keep it here.';
    if (tab === 'ready') return 'No stock passed every rule in last night’s scan. That is normal - check Watch.';
    if (tab === 'watch') return 'Nothing is close enough to a valid pivot right now.';
    return 'No names passed the Stage 2 template.';
  }

  function updateCounts() {
    var d = state.data;
    if (!d) return;
    document.getElementById('count-ready').textContent = d.ready.length;
    document.getElementById('count-watch').textContent = d.watch.length;
    document.getElementById('count-stage2').textContent = d.stage2.length;
    document.getElementById('count-starred').textContent = rowsFor('starred').length;
  }

  // ------------------------------------------------------------- detail
  function findRow(sym) {
    var d = state.data;
    var all = d.ready.concat(d.watch, d.stage2);
    for (var i = 0; i < all.length; i++) if (all[i].symbol === sym) return all[i];
    return null;
  }

  function openDetail(sym) {
    var row = findRow(sym);
    if (!row) return;
    state.current = row;
    el['d-symbol'].textContent = row.symbol;
    el['d-name'].textContent = row.name || row.exchange || '';

    // Price stays in the header so it is still on screen once you scroll down
    // to the plan and the checklists.
    el['d-price'].textContent = num(row.price);
    var chg = row.change_pct;
    var v0 = (row.vcp && row.vcp.metrics) || {};
    var parts = [];
    if (chg != null && !isNaN(chg)) parts.push((chg >= 0 ? '+' : '') + chg.toFixed(2) + '%');
    parts.push((row.date || '').slice(5) + ' close');
    el['d-change'].textContent = parts.join(' \u00b7 ');
    el['d-change'].className = chg == null || isNaN(chg) ? '' : (chg >= 0 ? 'up' : 'down');

    // Expanding the chart covers the sheet header, so the same figures are
    // repeated in the chart's own bar, where they stay visible.
    el['chart-title'].textContent = row.symbol;
    el['chart-price'].innerHTML =
      '<b>' + num(row.price) + '</b>' +
      '<span class="' + (chg == null || isNaN(chg) ? '' : (chg >= 0 ? 'up' : 'down')) + '">' +
      escapeHTML(parts.join(' \u00b7 ')) + '</span>';
    el['d-star'].textContent = isStarred(sym) ? '★' : '☆';
    el['d-star'].classList.toggle('on', isStarred(sym));
    el.detail.hidden = false;
    document.body.style.overflow = 'hidden';

    renderPlan(row);
    renderContractions(row);
    renderVcpRules(row);
    renderStage2(row);
    updateCalc();

    // A name with no stored series (Stage 2 only) gets no chart and no
    // position-size box - there is no pivot to size against.
    var hasPivot = !!(row.vcp && row.vcp.metrics && row.vcp.metrics.pivot != null);
    el['chart-panel'].hidden = !row.has_series;
    el['calc-card'].hidden = !hasPivot;

    if (row.has_series) {
      el.chart.getContext('2d').clearRect(0, 0, el.chart.width, el.chart.height);
      fetch('data/series/' + encodeURIComponent(sym) + '.json')
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (series) {
          if (!series || state.current !== row) return;
          row._series = series;
          drawChart();
        })
        .catch(function () {});
    }
  }

  function drawChart() {
    var row = state.current;
    if (!row || !row._series) return;
    var v = (row.vcp && row.vcp.metrics) || {};
    // Indices in the payload are already rebased onto the daily series.
    var contractions = (row.vcp && row.vcp.contractions) || [];
    var series = row._series;

    if (state.interval === 'weekly') {
      if (!row._weekly) row._weekly = VCPChart.aggregateWeekly(row._series);
      var map = row._weekly.dayToWeek;
      var lastDay = map.length - 1;
      // Contraction markers are daily indices; move them onto their weeks.
      contractions = contractions.map(function (c) {
        return {
          index: c.index,
          depth_pct: c.depth_pct,
          high_idx: map[Math.min(Math.max(c.high_idx, 0), lastDay)],
          low_idx: map[Math.min(Math.max(c.low_idx, 0), lastDay)]
        };
      });
      series = row._weekly;
    }

    // "Base" frames the consolidations themselves. On a chart covering a long
    // advance the base is a sliver at the right-hand edge, which is exactly
    // the part you need to read.
    var total = series.c.length;
    var bars, fitPriceOnly = false;
    if (state.months === 'base' && contractions.length) {
      var firstBar = Math.max(0, contractions[0].high_idx);
      bars = Math.min(total, Math.round((total - firstBar) * 1.4) + 4);
      bars = Math.max(bars, 24);          // never so tight it is unreadable
      fitPriceOnly = true;
    } else {
      var months = state.months === 'base' ? 6 : state.months;
      bars = Math.round(months * BARS_PER_MONTH[state.interval]);
    }

    VCPChart.draw(el.chart, series, {
      bars: bars,
      pivot: v.pivot,
      support: v.support,
      contractions: contractions,
      fitPriceOnly: fitPriceOnly
    });
    el['chart-legend'].innerHTML =
      (state.months === 'base'
        ? '<span>Base view - scaled to the consolidations, so the longer averages sit off-chart</span>'
        : '') +
      legendItem(VCPChart.COLORS.ma50, '50MA') +
      legendItem(VCPChart.COLORS.ma150, '150MA') +
      legendItem(VCPChart.COLORS.ma200, '200MA') +
      legendItem(VCPChart.COLORS.pivot, 'Pivot') +
      legendItem(VCPChart.COLORS.support, 'Stop');
  }
  function setCollapsed(on) {
    state.collapsed = on;
    el['chart-panel'].classList.toggle('is-collapsed', on);
    el['chart-collapse'].setAttribute('aria-expanded', String(!on));
    if (on && state.expanded) setExpanded(false);
    saveChartPrefs();
    if (!on) drawChart();
  }

  function setExpanded(on) {
    var hasBase = !!(state.current && state.current.vcp &&
                     (state.current.vcp.contractions || []).length);
    if (on && hasBase && state.months !== 'base') {
      state.months = 'base';
      markActive('range-row', 'months', 'base');
      saveChartPrefs();
    }
    state.expanded = on;
    el['chart-panel'].classList.toggle('is-expanded', on);
    // Stop the page behind the overlay from scrolling under a finger.
    document.body.style.overflow = on || state.current ? 'hidden' : '';
    el['chart-expand'].innerHTML = on ? '&times;' : '&#9974;';
    el['chart-expand'].setAttribute('aria-label', on ? 'Close expanded chart' : 'Expand chart');
    // The canvas is sized from its box, so it has to be redrawn after the
    // layout changes rather than scaled.
    requestAnimationFrame(drawChart);
  }

  function saveChartPrefs() {
    try {
      localStorage.setItem(CHART_KEY, JSON.stringify({
        months: state.months, interval: state.interval, collapsed: state.collapsed
      }));
    } catch (e) {}
  }

  function legendItem(color, label) {
    return '<span><i style="background:' + color + '"></i>' + label + '</span>';
  }

  function renderPlan(row) {
    var v = (row.vcp && row.vcp.metrics) || {};
    if (v.pivot == null) {
      el.plan.innerHTML = '<h3>Trade plan</h3><p class="plan-note">' +
        'This name passes the Stage 2 template but has no valid VCP base yet, so there is no pivot to buy through.' +
        (row.vcp_reason ? ' ' + escapeHTML(row.vcp_reason) + '.' : '') + '</p>';
      return;
    }
    var L = limits();
    var cells = [
      ['Buy above', num(v.pivot), ''],
      ['Up to', num(v.max_entry), ''],
      ['Stop', num(v.support), ''],
      ['Target', num(v.target), ''],
      ['Risk', pct(v.risk_pct), riskClass(v.risk_pct)],
      ['Reward:risk', v.reward_risk == null ? '--' : '1 : ' + v.reward_risk.toFixed(1),
       rrClass(v.reward_risk)],
      ['From pivot', pct(v.distance_to_pivot_pct), '']
    ];

    // Spell out anything inside the accepted band but short of preferred, so a
    // wider stop or a thinner payoff is a decision rather than an oversight.
    var caveats = [];
    if (v.risk_pct != null && Number(v.risk_pct.toFixed(1)) > L.okRisk) {
      caveats.push('Risk of ' + pct(v.risk_pct) + ' is above the ' + pct(L.okRisk, 0) +
        ' you want. The stop is wider than ideal, so size the position down or wait' +
        ' for a tighter pivot.');
    }
    if (v.perfect_vcp === false) {
      caveats.push('A pause inside the base was followed by a wider one, so the ' +
        'contraction is not clean. The reference calls that "not a perfect VCP" rather ' +
        'than not a VCP - the pattern stands, but it is a weaker one.');
    }
    if (v.reward_risk != null && Number(v.reward_risk.toFixed(1)) < L.okRR) {
      caveats.push('Reward:risk of 1 : ' + v.reward_risk.toFixed(1) + ' is below the 1 : ' +
        L.okRR + ' you want. It clears the 1 : ' + L.minRR + ' minimum, but the payoff is thin.');
    }

    el.plan.innerHTML = '<h3>Trade plan</h3><div class="plan-grid">' +
      cells.map(function (c) {
        return '<div class="metric"><div class="k">' + c[0] + '</div><div class="v ' + c[2] +
               '">' + c[1] + '</div></div>';
      }).join('') +
      (caveats.length
        ? '<p class="caveat">' + caveats.map(escapeHTML).join(' ') + '</p>' : '') +
      '</div><p class="plan-note">Buy on a break above ' + num(v.pivot) +
      ' on surging volume. The stop sits at the last consolidation low (' + num(v.support) +
      '), which is the low the pattern says should not be broken. Target is the measured move: pivot plus the base’s own depth (' +
      pct(v.base_depth_pct) + ').</p>';
  }

  function renderContractions(row) {
    var cs = (row.vcp && row.vcp.contractions) || [];
    if (!cs.length) { el['d-contractions'].innerHTML = '<p class="plan-note">No consolidation cycles detected.</p>'; return; }
    el['d-contractions'].innerHTML =
      '<table class="t"><thead><tr><th>T</th><th>High</th><th>Low</th><th>Depth</th><th>Bars</th><th>Avg vol</th></tr></thead><tbody>' +
      cs.map(function (c) {
        return '<tr><td>T' + c.index + '</td><td>' + num(c.high) + '</td><td>' + num(c.low) +
               '</td><td>' + c.depth_pct.toFixed(1) + '%</td><td>' + c.bars + '</td><td>' +
               (c.avg_volume >= 1e6 ? (c.avg_volume / 1e6).toFixed(1) + 'M' : Math.round(c.avg_volume / 1e3) + 'K') +
               '</td></tr>';
      }).join('') + '</tbody></table>';
  }

  function renderVcpRules(row) {
    var vcp = row.vcp;
    if (!vcp) { el['d-vcp'].innerHTML = '<p class="plan-note">Not evaluated.</p>'; return; }
    var v = vcp.metrics || {};
    var cfg = state.data.config || {};
    var rules = [
      ['At least ' + (cfg.min_contractions || 2) + ' contractions',
       (v.contraction_count || 0) >= (cfg.min_contractions || 2), (v.contraction_count || 0) + ' found'],
      ['Each T tighter than the last', v.final_depth_pct != null && v.first_depth_pct != null &&
       v.final_depth_pct < v.first_depth_pct, pct(v.first_depth_pct) + ' → ' + pct(v.final_depth_pct)],
      ['Final T within ' + (cfg.final_depth_pct ? cfg.final_depth_pct.join('-') : '2-12') + '%',
       v.final_depth_pct != null && cfg.final_depth_pct &&
       v.final_depth_pct >= cfg.final_depth_pct[0] && v.final_depth_pct <= cfg.final_depth_pct[1],
       pct(v.final_depth_pct)],
      ['Final T volume dried up',
       v.volume_dryup_ratio != null && v.volume_dryup_ratio <= (cfg.dryup_ratio || 0.85),
       v.volume_dryup_ratio == null ? '--'
         : v.volume_dryup_ratio.toFixed(2) + '× its 50d avg'
           + (v.recent_volume_vs_50d != null
              ? ' (last 5d ' + v.recent_volume_vs_50d.toFixed(2) + '×)' : '')],
      ['Support has held', v.bars_since_support != null && v.bars_since_support >= 3,
       (v.bars_since_support || 0) + ' bars since the low'],
      ['Risk at or under ' + pct(cfg.max_risk_pct, 0),
       v.risk_pct != null && v.risk_pct <= (cfg.max_risk_pct || 8),
       pct(v.risk_pct) + (v.risk_pct != null && v.risk_pct > limits().okRisk
         ? ' - over the ' + pct(limits().okRisk, 0) + ' you prefer' : '')],
      ['Reward:risk at least ' + (cfg.min_reward_risk || 2),
       v.reward_risk != null && v.reward_risk >= (cfg.min_reward_risk || 2),
       (v.reward_risk == null ? '--' : '1 : ' + v.reward_risk.toFixed(1)) +
       (v.reward_risk != null && v.reward_risk < limits().okRR
         ? ' - under the 1 : ' + limits().okRR + ' you prefer' : '')],
      ['Pivot not broken yet', v.pivot_broken === false,
       v.high_since_support == null ? '--'
         : 'high since low ' + num(v.high_since_support) + ' vs pivot ' + num(v.pivot)],
      ['Price still near the pivot', vcp.status === 'actionable',
       pct(v.distance_to_pivot_pct) + ' from pivot'],
      ['Base has run long enough',
       v.base_length_bars != null &&
         v.base_length_bars >= (cfg.preferred_base_bars || 63),
       v.base_length_bars == null ? '--'
         : v.base_length_bars + ' sessions (~' + (v.base_length_bars / 21).toFixed(1) +
           ' months), want ' + Math.round((cfg.preferred_base_bars || 63) / 21) + '+'],
      ['Each T at most half the last',
       !!(v.preferred && v.preferred.shrink),
       v.worst_shrink_ratio == null ? '--'
         : 'worst step ' + v.worst_shrink_ratio.toFixed(2) + ' of the previous'],
      ['No widening pause in the base',
       v.perfect_vcp !== false,
       !v.widening_pauses || !v.widening_pauses.length ? 'clean sequence'
         : 'widened after the ' + pct(v.widening_pauses[0].depth_pct) + ' pause on ' +
           v.widening_pauses[0].date +
           (v.widening_pauses.length > 1
             ? ' (+' + (v.widening_pauses.length - 1) + ' more)' : '')],
      ['Volume fell across the base',
       !v.volume_rose_pauses || !v.volume_rose_pauses.length,
       !v.volume_rose_pauses || !v.volume_rose_pauses.length ? 'declining'
         : v.volume_rose_pauses.length + ' pause(s) traded heavier than the next'],
      ['First T within ' + pct(cfg.preferred_first_depth_pct || 30, 0),
       !!(v.preferred && v.preferred.first_depth),
       pct(v.first_depth_pct)]
    ];
    el['d-vcp'].innerHTML = rules.map(checkRow).join('');
  }

  function renderStage2(row) {
    var s2 = row.stage2;
    if (!s2 || !s2.checks) {
      var m = row.metrics || {};
      el['d-stage2'].innerHTML =
        '<p class="plan-note">Passed every Stage 2 rule. 150MA slope ' + pct(m.ma150_slope_pct) +
        ', 200MA slope ' + pct(m.ma200_slope_pct) + ', ' + (m.up_weeks || 0) + ' up weeks vs ' +
        (m.down_weeks || 0) + ' down, beta ' + num(m.beta) + ', ' + money(m.dollar_volume) + ' traded.</p>';
      return;
    }
    el['d-stage2'].innerHTML = s2.checks.map(function (c) {
      return checkRow([c.label, c.passed, c.detail]);
    }).join('');
  }

  function checkRow(r) {
    return '<div class="check ' + (r[1] ? 'pass' : 'fail') + '">' +
           '<span class="mark">' + (r[1] ? '✓' : '✕') + '</span>' +
           '<span class="lbl">' + escapeHTML(r[0]) + '</span>' +
           '<span class="det">' + escapeHTML(r[2] == null ? '' : r[2]) + '</span></div>';
  }

  // ------------------------------------------------------------- calculator
  function updateCalc() {
    var row = state.current;
    if (!row) return;
    var v = (row.vcp && row.vcp.metrics) || {};
    var acct = parseFloat(el.acct.value), riskPct = parseFloat(el.riskpct.value);
    try { localStorage.setItem(CALC_KEY, JSON.stringify({ acct: acct, riskPct: riskPct })); } catch (e) {}
    if (v.pivot == null || !isFinite(acct) || !isFinite(riskPct) || acct <= 0 || riskPct <= 0) {
      el['calc-out'].textContent = 'Enter an account size and the percent of it you are willing to lose on this trade.';
      return;
    }
    var perShare = v.pivot - v.support;
    if (perShare <= 0) { el['calc-out'].textContent = '--'; return; }
    var dollarsAtRisk = acct * riskPct / 100;
    var shares = Math.floor(dollarsAtRisk / perShare);
    var cost = shares * v.pivot;
    el['calc-out'].innerHTML =
      '<strong>' + shares.toLocaleString() + ' shares</strong> at the pivot (' + money(cost) +
      ', ' + (acct ? (cost / acct * 100).toFixed(0) : 0) + '% of the account).<br>' +
      'Loss if stopped out: ' + money(shares * perShare) + '. ' +
      'Gain at the target: ' + money(shares * (v.target - v.pivot)) + '.';
  }

  function closeDetail() {
    if (state.expanded) setExpanded(false);
    el.detail.hidden = true;
    state.current = null;
    document.body.style.overflow = '';
  }

  // ------------------------------------------------------------- data load
  function load(force) {
    el.status.classList.remove('err');
    el.status.textContent = 'Loading…';
    fetch(DATA_URL + (force ? '?t=' + Date.now() : ''), force ? { cache: 'reload' } : {})
      .then(function (r) {
        if (!r.ok) throw new Error('HTTP ' + r.status);
        return r.json();
      })
      .then(function (data) {
        state.data = data;
        updateCounts();
        render();
        var as = data.as_of || '?';
        el.status.textContent = 'Data through ' + as + ' · ' +
          data.universe_size.toLocaleString() + ' symbols scanned';
        el['foot-note'].textContent = 'Scan ran ' + (data.generated_at || '').replace('T', ' ').replace('+00:00', ' UTC') +
          '. Daily bars only - this is a nightly screen, not a live quote feed. Not investment advice.';
      })
      .catch(function (err) {
        el.status.classList.add('err');
        el.status.textContent = 'Could not load results (' + err.message + '). ' +
          'If the nightly scan has not run yet, there is nothing to show.';
        el.empty.hidden = false;
        el.empty.textContent = 'No data yet.';
      });
  }

  // ------------------------------------------------------------- events
  el.tabs.addEventListener('click', function (e) {
    var btn = e.target.closest('.tab');
    if (!btn) return;
    state.tab = btn.dataset.tab;
    Array.prototype.forEach.call(el.tabs.children, function (b) {
      b.classList.toggle('is-active', b === btn);
    });
    window.scrollTo(0, 0);
    render();
  });

  el.list.addEventListener('click', function (e) {
    var star = e.target.closest('[data-star]');
    if (star) {
      e.stopPropagation();
      toggleStar(star.dataset.star);
      render();
      return;
    }
    var card = e.target.closest('.card');
    if (card) openDetail(card.dataset.sym);
  });

  el.search.addEventListener('input', function () { state.query = el.search.value; render(); });
  el['hide-missed'].addEventListener('change', function () {
    state.hideMissed = el['hide-missed'].checked;
    try { localStorage.setItem(MISSED_KEY, state.hideMissed ? '1' : '0'); } catch (e) {}
    render();
  });
  el.sort.addEventListener('change', function () { state.sort = el.sort.value; render(); });
  el.refresh.addEventListener('click', function () { load(true); });
  el.back.addEventListener('click', closeDetail);
  el['d-star'].addEventListener('click', function () {
    if (!state.current) return;
    toggleStar(state.current.symbol);
    el['d-star'].textContent = isStarred(state.current.symbol) ? '★' : '☆';
    el['d-star'].classList.toggle('on', isStarred(state.current.symbol));
    render();
  });
  function wireChartControls(row, key, parse) {
    el[row].addEventListener('click', function (e) {
      var b = e.target.closest('.range');
      if (!b) return;
      state[key] = parse(b);
      Array.prototype.forEach.call(el[row].children, function (x) {
        x.classList.toggle('is-active', x === b);
      });
      saveChartPrefs();
      drawChart();
    });
  }
  wireChartControls('range-row', 'months', function (b) {
    return b.dataset.months === 'base' ? 'base' : parseFloat(b.dataset.months);
  });
  wireChartControls('interval-row', 'interval', function (b) { return b.dataset.interval; });
  el['chart-collapse'].addEventListener('click', function () { setCollapsed(!state.collapsed); });
  el['chart-expand'].addEventListener('click', function () { setExpanded(!state.expanded); });

  [el.acct, el.riskpct].forEach(function (input) {
    input.addEventListener('input', updateCalc);
  });
  window.addEventListener('resize', function () { if (state.current) drawChart(); });
  document.addEventListener('keydown', function (e) {
    if (e.key !== 'Escape') return;
    if (state.expanded) setExpanded(false);
    else if (!el.detail.hidden) closeDetail();
  });

  try {
    var saved = JSON.parse(localStorage.getItem(CALC_KEY) || 'null');
    if (saved) { el.acct.value = saved.acct; el.riskpct.value = saved.riskPct; }
  } catch (e) {}

  try {
    var chartPref = JSON.parse(localStorage.getItem(CHART_KEY) || 'null');
    if (chartPref) {
      if (chartPref.months) state.months = chartPref.months;
      if (chartPref.interval) state.interval = chartPref.interval;
      markActive('range-row', 'months', String(state.months));
      markActive('interval-row', 'interval', state.interval);
      if (chartPref.collapsed) {
        state.collapsed = true;
        el['chart-panel'].classList.add('is-collapsed');
        el['chart-collapse'].setAttribute('aria-expanded', 'false');
      }
    }
  } catch (e) {}

  function markActive(rowId, attr, value) {
    Array.prototype.forEach.call(el[rowId].children, function (b) {
      b.classList.toggle('is-active', b.dataset[attr] === value);
    });
  }

  try {
    var savedMissed = localStorage.getItem(MISSED_KEY);
    if (savedMissed !== null) state.hideMissed = savedMissed === '1';
  } catch (e) {}
  el['hide-missed'].checked = state.hideMissed;

  if ('serviceWorker' in navigator) {
    window.addEventListener('load', function () {
      navigator.serviceWorker.register('sw.js').catch(function () {});
    });
  }

  load(false);
})();
