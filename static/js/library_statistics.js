// 저장된 라이브러리 통계와 ECharts 카드를 표시하고 별도 갱신을 요청합니다.
function bookoasisMateStatisticsOption(kind, rows) {
  rows = rows || [];
  var storage = kind.indexOf('storage') === 0;
  if (storage) kind = {storage:'bar', storagePie:'pie', storageTree:'treemap'}[kind];
  var option = {animation:false, aria:{enabled:true},
    color:['#7353ba', '#38a3a5', '#6096ba', '#e9c46a', '#e76f51', '#90be6d', '#f284b6'],
    tooltip:{trigger:'item', renderMode:'richText', confine:true}, textStyle:{color:'#718096'}, series:[]};
  var data = rows.map(function(row) { return {name:row.label || row.period || '', value:Number(row.count || 0)}; });
  if (kind === 'pie' || kind === 'treemap') {
    option.series = [{type:kind, radius:['42%', '70%'], data:data,
      label:{show:true, overflow:'truncate', width:90}, breadcrumb:{show:false}}];
  } else if (kind === 'gauge') {
    option.series = [{type:'gauge', min:0, max:100, progress:{show:true},
      detail:{formatter:'{value}%', fontSize:24}, data:[{value:Number((rows[0] || {}).count || 0)}]}];
  } else if (kind === 'chord') {
    var names = Array.from(new Set(rows.reduce(function(all, row) { return all.concat([row.source, row.target]); }, [])));
    option.series = [{type:'chord', radius:['22%', '65%'], data:names.map(function(name) { return {name:name}; }),
      links:rows, emphasis:{focus:'adjacency'}, label:{show:true}, lineStyle:{opacity:0.5}}];
  } else if (kind === 'heatmap') {
    var fields = Array.from(new Set(rows.map(function(row) { return row.label; })));
    var libraries = Array.from(new Set(rows.map(function(row) { return row.library; })));
    option.grid = {left:110, right:30, top:20, bottom:85};
    option.xAxis = {type:'category', data:fields, axisLabel:{rotate:35}};
    option.yAxis = {type:'category', data:libraries, axisLabel:{width:95, overflow:'truncate'}};
    option.visualMap = {min:0, max:100, bottom:0, left:'center', orient:'horizontal', inRange:{color:['#f6b5ac', '#f3d57b', '#38a3a5']}};
    option.series = [{type:'heatmap', data:rows.map(function(row) { return [fields.indexOf(row.label), libraries.indexOf(row.library), row.value]; })}];
    if (libraries.length > 10) option.dataZoom = [{type:'slider', yAxisIndex:0, start:0, end:1000 / libraries.length}];
  } else if (kind === 'stack') {
    var periods = Array.from(new Set(rows.map(function(row) { return row.period; }))).sort().slice(-60);
    var formats = Array.from(new Set(rows.map(function(row) { return row.label; })));
    option.grid = {left:55, right:20, top:40, bottom:55};
    option.legend = {type:'scroll', textStyle:{color:'#718096'}};
    option.xAxis = {type:'category', data:periods};
    option.yAxis = {type:'value', max:100, axisLabel:{formatter:'{value}%'}};
    option.series = formats.map(function(format) { return {name:format, type:'bar', stack:'format', data:periods.map(function(period) {
      var selected = rows.filter(function(row) { return row.period === period; });
      var total = selected.reduce(function(sum, row) { return sum + row.count; }, 0);
      var amount = selected.filter(function(row) { return row.label === format; }).reduce(function(sum, row) { return sum + row.count; }, 0);
      return total ? Math.round(amount / total * 10000) / 100 : 0;
    })}; });
  } else {
    var horizontal = kind !== 'line';
    option.grid = {left:horizontal ? 125 : 55, right:25, top:20, bottom:55};
    var category = {type:'category', data:data.map(function(row) { return row.name; }), inverse:horizontal,
      axisLabel:{width:110, overflow:'truncate'}};
    option.xAxis = horizontal ? {type:'value'} : category;
    option.yAxis = horizontal ? category : {type:'value'};
    option.series = [{type:horizontal ? 'bar' : 'line', data:data.map(function(row) { return row.value; }), smooth:!horizontal}];
    if (horizontal && data.length > 10) option.dataZoom = [{type:'slider', yAxisIndex:0, start:0, end:1000 / data.length}, {type:'inside', yAxisIndex:0}];
  }
  if (storage) {
    option.tooltip.formatter = function(item) { return item.name + '\n' + bookoasisMateBytes(Number(item.value || 0)); };
    if (option.xAxis) option.xAxis.axisLabel = {formatter:bookoasisMateBytes};
  }
  return option;
}

(function() {
  'use strict';
  var charts = [], observer = null, timer = null, generation = 0, resultKey = '', busy = false;
  function el(id) { return document.getElementById(id); }
  function dispose() {
    if (observer) observer.disconnect();
    observer = null;
    charts.forEach(function(chart) { chart.dispose(); }); charts = [];
  }
  function card(title, kind, rows) {
    var article = bookoasisMateText('article', 'doctor-card doctor-statistics-chart-card', '');
    article.appendChild(bookoasisMateText('h4', '', title));
    el('statistics_charts').appendChild(article);
    if (!rows || !rows.length) {
      article.appendChild(bookoasisMateText('p', 'doctor-empty', '표시할 데이터가 없습니다. 이전 결과라면 통계를 갱신해 주세요.'));
    } else {
      var canvas = bookoasisMateText('div', 'doctor-statistics-echart', '');
      canvas.setAttribute('role', 'img'); canvas.setAttribute('aria-label', title); article.appendChild(canvas);
      var draw = function() {
        var chart = echarts.init(canvas, null, {renderer:'canvas'});
        chart.setOption(bookoasisMateStatisticsOption(kind, rows)); charts.push(chart);
      };
      if (typeof IntersectionObserver !== 'undefined') {
        canvas._drawStatistics = draw;
        if (!observer) observer = new IntersectionObserver(function(entries) {
          entries.forEach(function(entry) {
            if (entry.isIntersecting) { observer.unobserve(entry.target); entry.target._drawStatistics(); delete entry.target._drawStatistics; }
          });
        }, {rootMargin:'200px'});
        observer.observe(canvas);
      } else draw();
    }
  }
  function render(result) {
    var key = [result.db_type, result.library_id, result.generated_at].join('|');
    if (key === resultKey) return;
    resultKey = key; dispose(); el('statistics_result').hidden = false;
    bookoasisMateClear(el('statistics_charts')); bookoasisMateClear(el('statistics_kpis'));
    el('statistics_result_title').textContent = (result.library_name || '전체 보관함') + ' 통계';
    el('statistics_result_meta').textContent = String(result.engine || '').toUpperCase() + ' · 집계 ' + (result.generated_at || '').replace('T', ' ');
    var summary = result.summary || {}, extra = result.charts || {}, media = result.media_kind || 'book';
    [['자료', summary.total_items], ['시리즈', summary.total_series], ['저자', summary.total_authors],
      ['출판사', summary.total_publishers], ['트랙', summary.total_tracks], ['에피소드', summary.total_episodes],
      ['올해 추가', summary.added_this_year], ['보관함', (result.libraries || []).length],
      ['출시 연도', (result.publication_years || []).length], ['저장 공간', bookoasisMateBytes(summary.storage_bytes || 0)],
      ['재생 시간', summary.total_duration == null ? null : (summary.total_duration / 3600).toFixed(1) + '시간']].forEach(function(pair) {
      if (pair[1] == null) return;
      var box = bookoasisMateText('div', 'doctor-statistics-kpi', '');
      box.appendChild(bookoasisMateText('span', '', pair[0]));
      box.appendChild(bookoasisMateText('strong', '', typeof pair[1] === 'number' ? pair[1].toLocaleString('ko-KR') : pair[1]));
      el('statistics_kpis').appendChild(box);
    });
    card('파일 형식 분포', 'pie', result.formats);
    card('메타데이터 평균 완성도', 'gauge', extra.metadata_average == null ? [] : [{count:extra.metadata_average}]);
    card('메타데이터 점수 분포', 'bar', result.metadata_scores);
    card('메타데이터 누락 현황', 'bar', result.metadata_missing);
    card('보관함별 자료 수', 'bar', (result.libraries || []).map(function(row) { return {label:row.name, count:row.count}; }));
    var libraryNames = {}; (result.libraries || []).forEach(function(row) { libraryNames[row.id] = row.name; });
    card('보관함별 메타데이터 완성도', 'heatmap', (extra.metadata_heatmap || []).map(function(row) {
      return {library:libraryNames[row.library_id] || ('보관함 #' + row.library_id), label:row.label, value:row.value};
    }));
    card('포맷별 저장 공간', 'storagePie', (result.formats || []).map(function(row) { return {label:row.label, count:row.size_bytes || 0}; }));
    if (media !== 'audiobook') { card('장르 분포', 'treemap', result.genres); card('장르 연관성', 'chord', extra.genre_links); }
    if (media === 'book') card('태그 분포', 'treemap', result.tags);
    if (media !== 'video') { card('상위 저자', 'bar', extra.authors); card('상위 출판사', 'bar', extra.publishers); }
    if (media === 'book') card('상위 시리즈', 'bar', extra.series);
    card(media === 'book' ? '페이지 수 분포' : media === 'audiobook' ? '트랙 수 분포' : '에피소드 수 분포', 'bar', extra.lengths);
    card('자료 추가 추이', 'line', (result.added_over_time || []).map(function(row) { return {label:row.period, count:row.count}; }));
    card('출판·출시 연도 타임라인', 'line', result.publication_years);
    var decades = {}; (result.publication_years || []).forEach(function(row) {
      var label = Math.floor(Number(row.label) / 10) * 10 + '년대'; decades[label] = (decades[label] || 0) + row.count;
    });
    card('출판·출시 시대', 'bar', Object.keys(decades).sort().map(function(label) { return {label:label, count:decades[label]}; }));
    card('포맷 비중 변화', 'stack', extra.format_timeline);
    var largest = (result.largest_items || []).filter(function(row) { return row.size_bytes > 0; }).map(function(row) { return {label:row.title, count:row.size_bytes}; });
    card('용량이 큰 항목 · 상위 50개', 'storage', largest.slice(0, 50));
    card('용량이 큰 항목 · Treemap', 'storageTree', largest);
    var progress = result.progress || {};
    card('전체 사용자 진행 상태 · DB 반영 기준', 'bar', [{label:'시작 전', count:progress.not_started}, {label:'진행 중', count:progress.in_progress}, {label:'완료', count:progress.completed}]);
  }
  function schedule() { clearTimeout(timer); timer = setTimeout(load, 5000); }
  function load() {
    clearTimeout(timer);
    var token = ++generation, db = el('statistics_db_type').value, lib = el('statistics_library').value;
    bookoasisMateAjax('main', 'statistics_status', {db_type:db, library_id:lib}, function(ret) {
      if (token !== generation) return;
      var data = ret.data || {}; busy = !!data.busy;
      el('statistics_start').disabled = busy; el('statistics_stop').disabled = data.is_working !== 'run';
      el('statistics_status_message').textContent = data.message || '';
      el('statistics_status_title').textContent = {run:'백그라운드 갱신 중', fail:'갱신 실패 · 이전 결과 유지', stopped:'갱신 중지 · 이전 결과 유지'}[data.is_working] || (data.result ? '저장된 통계' : '첫 집계 대기');
      el('statistics_error').textContent = data.error || (ret.ret !== 'success' ? ret.msg || '조회 실패' : '');
      el('statistics_progress_bar').style.width = Math.min(100, Math.max(0, data.progress_percent || 0)) + '%';
      var select = el('statistics_library'); bookoasisMateClear(select);
      var all = bookoasisMateText('option', '', '전체 보관함'); all.value = ''; select.appendChild(all);
      (data.libraries || []).forEach(function(row) { var opt = bookoasisMateText('option', '', row.name); opt.value = String(row.id); select.appendChild(opt); });
      select.value = lib; if (select.value !== lib) select.value = '';
      if (data.result && typeof echarts !== 'undefined') render(data.result);
      else { dispose(); resultKey = ''; el('statistics_result').hidden = true; }
      schedule();
    }, null, {global:false, silent:true, error:function(xhr, message) {
      if (token !== generation) return;
      el('statistics_error').textContent = message || '통계 조회 실패'; schedule();
    }});
  }
  $(function() {
    if (typeof echarts === 'undefined') { el('statistics_error').textContent = 'ECharts 번들을 불러오지 못했습니다.'; return; }
    el('statistics_db_type').addEventListener('change', function() { el('statistics_library').value = ''; dispose(); resultKey = ''; el('statistics_result').hidden = true; load(); });
    el('statistics_library').addEventListener('change', function() { dispose(); resultKey = ''; el('statistics_result').hidden = true; load(); });
    el('statistics_start').addEventListener('click', function() {
      if (busy) return;
      el('statistics_start').disabled = true;
      bookoasisMateAjax('main', 'statistics_start', {db_type:el('statistics_db_type').value}, load, null, {global:false, error:load});
    });
    el('statistics_stop').addEventListener('click', function() { bookoasisMateAjax('main', 'statistics_stop', {}, load, null, {global:false}); });
    load();
  });
  window.addEventListener('resize', function() { charts.forEach(function(chart) { chart.resize(); }); });
  window.addEventListener('beforeunload', function() { clearTimeout(timer); generation++; dispose(); });
})();
