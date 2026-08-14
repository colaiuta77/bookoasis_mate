// BookOasis 라이브러리 통계의 백그라운드 상태와 Chart.js 차트를 관리합니다.
(function() {
  'use strict';

  var charts = {};
  var deferredCharts = {};
  var chartObserver = null;
  var pollTimer = null;
  var currentState = 'wait';
  var renderedResultKey = '';
  var palette = ['#4f46e5', '#7c3aed', '#0ea5e9', '#14b8a6', '#22c55e', '#eab308', '#f97316', '#ef4444', '#ec4899', '#64748b', '#8b5cf6', '#06b6d4', '#84cc16', '#f59e0b', '#dc2626'];

  function number(value) {
    return Number(value || 0).toLocaleString('ko-KR');
  }

  function bytes(value) {
    return bookoasisMateBytes(Number(value || 0));
  }

  function gigabytes(value) {
    var amount = Number(value || 0) / (1024 * 1024 * 1024);
    return amount.toLocaleString('ko-KR', {
      maximumFractionDigits: amount > 0 && amount < 1 ? 2 : 1
    }) + ' GB';
  }

  function duration(value) {
    var seconds = Math.max(0, Number(value || 0));
    if (!seconds) return '0분';
    var hours = Math.floor(seconds / 3600);
    var minutes = Math.floor((seconds % 3600) / 60);
    return hours ? number(hours) + '시간 ' + minutes + '분' : number(minutes) + '분';
  }

  function colors(count) {
    var output = [];
    for (var index = 0; index < count; index += 1) output.push(palette[index % palette.length]);
    return output;
  }

  function destroyChart(id) {
    if (!charts[id]) return;
    charts[id].destroy();
    delete charts[id];
  }

  function destroyAllCharts() {
    Object.keys(charts).forEach(destroyChart);
  }

  function clearDeferredCharts() {
    deferredCharts = {};
    if (chartObserver) chartObserver.disconnect();
    chartObserver = null;
  }

  function chartTextColor() {
    return window.matchMedia && window.matchMedia('(prefers-color-scheme: dark)').matches ? '#d1d5db' : '#4b5563';
  }

  function renderChart(id, emptyId, rows, config) {
    destroyChart(id);
    var canvas = document.getElementById(id);
    var empty = document.getElementById(emptyId);
    var usable = (rows || []).filter(function(item) { return Number(config.value(item) || 0) > 0; });
    if (!canvas || typeof Chart === 'undefined' || !usable.length) {
      if (canvas) canvas.style.display = 'none';
      if (empty) empty.style.display = '';
      return;
    }
    canvas.style.display = '';
    if (empty) empty.style.display = 'none';
    var labels = usable.map(function(item) { return config.label(item); });
    var values = usable.map(function(item) { return Number(config.value(item) || 0); });
    var chartColors = config.colors || colors(usable.length);
    var textColor = chartTextColor();
    var options = {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      indexAxis: config.indexAxis || 'x',
      plugins: {
        legend: {display: config.legend !== false, position: 'right', labels: {color: textColor, boxWidth: 12}},
        tooltip: {
          callbacks: {
            label: function(context) {
              var value = context.raw || 0;
              return ' ' + (config.tooltip ? config.tooltip(value) : number(value));
            }
          }
        }
      },
      scales: config.scales === false ? undefined : {
        x: {beginAtZero: true, ticks: {color: textColor, precision: 0, callback: config.xTick}, grid: {color: 'rgba(148,163,184,.18)'}},
        y: {beginAtZero: true, ticks: {color: textColor, precision: 0}, grid: {color: 'rgba(148,163,184,.18)'}}
      }
    };
    charts[id] = new Chart(canvas.getContext('2d'), {
      type: config.type || 'bar',
      data: {
        labels: labels,
        datasets: [{
          label: config.datasetLabel || '',
          data: values,
          backgroundColor: chartColors,
          borderColor: config.type === 'line' ? palette[0] : chartColors,
          borderWidth: config.type === 'line' ? 2 : 1,
          fill: config.type === 'line',
          tension: config.type === 'line' ? 0.25 : 0
        }]
      },
      options: options
    });
  }

  function deferChart(id, emptyId, rows, config) {
    var canvas = document.getElementById(id);
    var card = canvas ? canvas.closest('.doctor-statistics-chart-card') : null;
    if (!canvas || !card || typeof IntersectionObserver === 'undefined') {
      renderChart(id, emptyId, rows, config);
      return;
    }
    deferredCharts[id] = {
      emptyId: emptyId,
      rows: rows,
      config: config
    };
    if (!chartObserver) {
      chartObserver = new IntersectionObserver(function(entries) {
        entries.forEach(function(entry) {
          if (!entry.isIntersecting) return;
          var targetCanvas = entry.target.querySelector('canvas');
          var deferred = targetCanvas ? deferredCharts[targetCanvas.id] : null;
          if (!deferred) return;
          delete deferredCharts[targetCanvas.id];
          chartObserver.unobserve(entry.target);
          renderChart(targetCanvas.id, deferred.emptyId, deferred.rows, deferred.config);
        });
      }, {rootMargin: '320px 0px'});
    }
    chartObserver.observe(card);
  }

  function appendKpi(container, label, value, note) {
    var card = bookoasisMateText('div', 'doctor-statistics-kpi', '');
    card.appendChild(bookoasisMateText('span', '', label));
    card.appendChild(bookoasisMateText('strong', '', value));
    if (note) card.appendChild(bookoasisMateText('small', '', note));
    container.appendChild(card);
  }

  function renderKpis(result) {
    var summary = result.summary || {};
    var container = document.getElementById('statistics_kpis');
    bookoasisMateClear(container);
    appendKpi(container, result.media_kind === 'audiobook' ? '오디오북' : '도서', number(summary.total_items) + (result.media_kind === 'audiobook' ? '개' : '권'));
    if (result.media_kind === 'audiobook') {
      appendKpi(container, '트랙', number(summary.total_tracks) + '개');
      appendKpi(container, '총 재생 시간', duration(summary.total_duration));
    } else {
      appendKpi(container, '시리즈', number(summary.total_series) + '개');
    }
    appendKpi(container, '저자', number(summary.total_authors) + '명');
    appendKpi(container, '출판사', number(summary.total_publishers) + '개');
    appendKpi(container, '저장 용량', bytes(summary.storage_bytes));
    appendKpi(container, '올해 등록', number(summary.added_this_year) + (result.media_kind === 'audiobook' ? '개' : '권'));
  }

  function renderLargest(result) {
    var rows = (result.largest_items || []).filter(function(item) { return Number(item.size_bytes || 0) > 0; });
    var body = document.getElementById('statistics_largest_rows');
    bookoasisMateClear(body);
    rows.forEach(function(item, index) {
      var row = document.createElement('tr');
      row.appendChild(bookoasisMateText('td', '', String(index + 1)));
      row.appendChild(bookoasisMateText('td', '', item.title || '제목 없음'));
      row.appendChild(bookoasisMateText('td', '', item.series_name || '-'));
      row.appendChild(bookoasisMateText('td', '', item.library_name || '-'));
      row.appendChild(bookoasisMateText('td', 'doctor-statistics-size', bytes(item.size_bytes)));
      body.appendChild(row);
    });
    document.querySelector('#statistics_largest_rows').closest('.doctor-table-wrap').style.display = rows.length ? '' : 'none';
    document.getElementById('statistics_largest_empty').style.display = rows.length ? 'none' : '';
  }

  function renderResult(result) {
    if (!result) return;
    var resultKey = [
      result.engine || '',
      result.database || '',
      result.library_id || '',
      result.generated_at || '',
      result.duration_ms == null ? '' : result.duration_ms
    ].join('|');
    if (resultKey === renderedResultKey) return;
    renderedResultKey = resultKey;
    clearDeferredCharts();
    document.getElementById('statistics_result').style.display = '';
    var mediaLabel = result.media_kind === 'audiobook' ? '오디오북' : '도서';
    document.getElementById('statistics_result_title').textContent = (result.library_name || '전체 보관함') + ' ' + mediaLabel + ' 통계';
    document.getElementById('statistics_result_meta').textContent = [
      String(result.engine || '').toUpperCase(),
      result.database || '',
      result.generated_at ? '생성 ' + result.generated_at.replace('T', ' ') : '',
      result.duration_ms != null ? (Number(result.duration_ms) / 1000).toFixed(1) + '초' : ''
    ].filter(Boolean).join(' · ');
    document.getElementById('statistics_progress_title').textContent = result.media_kind === 'audiobook' ? '청취 상태' : '독서 상태';
    document.getElementById('statistics_year_title').textContent = result.media_kind === 'audiobook' ? '공개 연도 분포' : '출간 연도 분포';
    document.getElementById('statistics_genres_card').style.display = result.media_kind === 'audiobook' ? 'none' : '';
    document.getElementById('statistics_tags_card').style.display = result.media_kind === 'audiobook' ? 'none' : '';

    renderKpis(result);
    renderChart('statistics_format_count', 'statistics_format_empty', result.formats, {
      type: 'doughnut', scales: false, datasetLabel: '자료 수',
      label: function(item) { return item.label; }, value: function(item) { return item.count; }
    });
    renderChart('statistics_format_size', 'statistics_format_size_empty', result.formats, {
      type: 'bar', indexAxis: 'y', legend: false, datasetLabel: '용량',
      label: function(item) { return item.label; }, value: function(item) { return item.size_bytes; },
      xTick: gigabytes, tooltip: bytes
    });
    deferChart('statistics_libraries', 'statistics_libraries_empty', result.libraries, {
      type: 'bar', legend: false, datasetLabel: '자료 수',
      label: function(item) { return item.name; }, value: function(item) { return item.count; }
    });
    deferChart('statistics_metadata_score', 'statistics_metadata_score_empty', result.metadata_scores, {
      type: 'bar', legend: false, datasetLabel: '자료 수',
      label: function(item) { return item.label; }, value: function(item) { return item.count; }
    });
    deferChart('statistics_metadata_missing', 'statistics_metadata_missing_empty', result.metadata_missing, {
      type: 'bar', indexAxis: 'y', legend: false, datasetLabel: '누락 수',
      label: function(item) { return item.label; }, value: function(item) { return item.count; }
    });
    var progress = result.progress || {};
    deferChart('statistics_progress', 'statistics_progress_empty', [
      {label: '미시작', count: progress.not_started},
      {label: '진행 중', count: progress.in_progress},
      {label: '완료', count: progress.completed}
    ], {
      type: 'doughnut', scales: false, datasetLabel: '자료 수', colors: ['#94a3b8', '#3b82f6', '#22c55e'],
      label: function(item) { return item.label; }, value: function(item) { return item.count; }
    });
    deferChart('statistics_genres', 'statistics_genres_empty', result.genres, {
      type: 'bar', indexAxis: 'y', legend: false, datasetLabel: '도서 수',
      label: function(item) { return item.label; }, value: function(item) { return item.count; }
    });
    deferChart('statistics_tags', 'statistics_tags_empty', result.tags, {
      type: 'bar', indexAxis: 'y', legend: false, datasetLabel: '도서 수',
      label: function(item) { return item.label; }, value: function(item) { return item.count; }
    });
    deferChart('statistics_timeline', 'statistics_timeline_empty', result.added_over_time, {
      type: 'line', legend: false, datasetLabel: '등록 수', colors: [palette[0]],
      label: function(item) { return item.period; }, value: function(item) { return item.count; }
    });
    deferChart('statistics_years', 'statistics_years_empty', result.publication_years, {
      type: 'bar', legend: false, datasetLabel: '자료 수',
      label: function(item) { return item.label; }, value: function(item) { return item.count; }
    });
    renderLargest(result);
  }

  function statusLabel(state) {
    return {wait: '대기중', run: '분석 중', done: '완료', fail: '실패', stopped: '중지됨'}[state] || state || '대기중';
  }

  function renderStatus(status) {
    status = status || {};
    var state = status.is_working || 'wait';
    currentState = state;
    var percent = Math.max(0, Math.min(100, Number(status.progress_percent || 0)));
    document.getElementById('statistics_status_title').textContent = statusLabel(state) + (status.library_name ? ' · ' + status.library_name : '');
    document.getElementById('statistics_status_message').textContent = status.message || '';
    document.getElementById('statistics_elapsed').textContent = status.elapsed_seconds ? Number(status.elapsed_seconds).toFixed(1) + '초' : '';
    document.getElementById('statistics_progress_bar').style.width = percent + '%';
    document.getElementById('statistics_error').textContent = status.error || '';
    document.getElementById('statistics_error').style.display = status.error ? '' : 'none';
    document.getElementById('statistics_start').disabled = state === 'run' || document.getElementById('statistics_library').disabled;
    document.getElementById('statistics_stop').disabled = state !== 'run';
    if (status.result) renderResult(status.result);
    if (state === 'run' || status.validation_pending) schedulePoll();
    else clearPoll();
  }

  function clearPoll() {
    if (pollTimer) window.clearTimeout(pollTimer);
    pollTimer = null;
  }

  function schedulePoll() {
    clearPoll();
    pollTimer = window.setTimeout(loadStatus, 1500);
  }

  function loadStatus() {
    bookoasisMateAjax('main', 'statistics_status', {}, function(ret) {
      if (ret.ret === 'success') renderStatus(ret.data || {});
    }, null, {global: false, silent: true, error: schedulePoll});
  }

  function loadCatalog() {
    var dbType = document.getElementById('statistics_db_type').value;
    var select = document.getElementById('statistics_library');
    var start = document.getElementById('statistics_start');
    select.disabled = true;
    start.disabled = true;
    document.getElementById('statistics_catalog_note').textContent = '보관함을 조회하고 있습니다.';
    bookoasisMateAjax('main', 'statistics_catalog', {db_type: dbType}, function(ret) {
      var data = ret.data || {};
      bookoasisMateClear(select);
      var all = document.createElement('option');
      all.value = '';
      all.textContent = '전체 보관함';
      select.appendChild(all);
      (data.libraries || []).forEach(function(item) {
        var option = document.createElement('option');
        option.value = item.id;
        option.textContent = item.name + ' (' + item.id + ')';
        select.appendChild(option);
      });
      select.disabled = false;
      start.disabled = currentState === 'run';
      document.getElementById('statistics_catalog_note').textContent = String(data.engine || '').toUpperCase() + ' · ' + number((data.libraries || []).length) + '개 보관함';
    }, null, {
      global: false,
      error: function(unusedXhr, message) {
        document.getElementById('statistics_catalog_note').textContent = message;
      }
    });
  }

  function startAnalysis() {
    clearPoll();
    bookoasisMateAjax('main', 'statistics_start', {
      db_type: document.getElementById('statistics_db_type').value,
      library_id: document.getElementById('statistics_library').value
    }, function(ret) {
      var data = ret.data || {};
      renderStatus(data.status || data);
    }, null, {global: false});
  }

  function stopAnalysis() {
    bookoasisMateAjax('main', 'statistics_stop', {}, function(ret) {
      renderStatus((ret.data || {}).status || {});
    }, null, {global: false});
  }

  $(function() {
    if (typeof Chart === 'undefined') {
      document.getElementById('statistics_error').textContent = 'Chart.js 번들을 불러오지 못했습니다.';
      document.getElementById('statistics_error').style.display = '';
      return;
    }
    document.getElementById('statistics_db_type').addEventListener('change', loadCatalog);
    document.getElementById('statistics_start').addEventListener('click', startAnalysis);
    document.getElementById('statistics_stop').addEventListener('click', stopAnalysis);
    loadCatalog();
    loadStatus();
  });

  window.addEventListener('beforeunload', function() {
    clearPoll();
    clearDeferredCharts();
    destroyAllCharts();
  });
})();
