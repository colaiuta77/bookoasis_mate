// 저장된 라이브러리 통계와 ECharts 카드를 표시하고 별도 갱신을 요청합니다.
function bookoasisMateStatisticsOption(kind, rows, title, nodes) {
  rows = rows || [];
  var storageTree = kind === 'storageTree';
  var storage = kind.indexOf('storage') === 0;
  if (storage) kind = {storage:'bar', storagePie:'pie', storageTree:'treemap'}[kind];
  var option = {animation:false, aria:{enabled:true},
    color:['#7353ba', '#38a3a5', '#6096ba', '#e9c46a', '#e76f51', '#90be6d', '#f284b6'],
    tooltip:{trigger:'item', renderMode:'richText', confine:true}, textStyle:{color:'#718096'}, series:[]};
  var data = rows.map(function(row) { return {name:row.label || row.period || '', value:Number(row.count || 0)}; });
  if (storageTree) {
    data = data.map(function(item, index) { item.id = String(rows[index].id == null ? index : rows[index].id); return item; });
    if (nodes !== 'size') {
      var groups = new Map();
      data.forEach(function(item, index) {
        var format = String(rows[index].format || '기타').trim().toLowerCase() || '기타';
        if (!groups.has(format)) groups.set(format, {name:format, value:0, children:[]});
        groups.get(format).children.push(item); groups.get(format).value += item.value;
      });
      data = Array.from(groups.values());
    }
    data.sort(function(a, b) { return b.value - a.value; });
  }
  if (kind === 'pie') {
    option.legend = {type:'scroll', bottom:0, left:'center', selectedMode:true, textStyle:{color:'#718096'}};
    option.series = [{type:'pie', radius:['42%', '70%'], center:['50%', '44%'], data:data,
      label:{show:false}, emphasis:{label:{show:true}}}];
  } else if (kind === 'treemap') {
    option.series = [{type:'treemap', name:title || '전체', data:data, left:0, right:0, top:0, bottom:34,
      roam:false, nodeClick:'zoomToNode', label:{show:true, overflow:'truncate'},
      breadcrumb:{show:true, bottom:0, height:24, emptyItemWidth:28},
      itemStyle:{borderColor:'#fff', borderWidth:1, gapWidth:2}}];
    if (storageTree) {
      option.series[0].sort = 'desc';
      option.series[0].upperLabel = {show:nodes !== 'size', height:24};
      option.series[0].label.formatter = function(item) { return item.name + '\n' + bookoasisMateBytes(item.value); };
    }
  } else if (kind === 'calendar') {
    option.tooltip.formatter = function(item) { return item.value[0] + '\n' + item.value[1] + '권'; };
    option.visualMap = {type:'piecewise', orient:'horizontal', left:'center', top:10,
      pieces:[{value:0,label:'기록 없음',color:'#eef0f3'},{value:1,label:'1권',color:'#ddd3fa'},
        {min:2,max:3,label:'2–3권',color:'#bda6f4'},{min:4,max:6,label:'4–6권',color:'#9470e3'},{min:7,label:'7권 이상',color:'#38bdf8'}]};
    option.calendar = {range:String(nodes), top:90, left:40, right:25, cellSize:['auto',18],
      yearLabel:{show:false}, monthLabel:{nameMap:'cn', formatter:'{MM}월'},
      dayLabel:{firstDay:1,nameMap:['일','월','화','수','목','금','토']}, itemStyle:{borderWidth:1,borderColor:'#fff'}};
    option.series = [{type:'heatmap', coordinateSystem:'calendar', data:rows}];
  } else if (kind === 'gauge') {
    option.series = [{type:'gauge', min:0, max:100, progress:{show:true},
      detail:{formatter:'{value}%', fontSize:24}, data:[{value:Number((rows[0] || {}).count || 0)}]}];
  } else if (kind === 'chord') {
    var names = Array.from(new Set(rows.reduce(function(all, row) { return all.concat([row.source, row.target]); }, [])));
    option.series = [{type:'chord', clockwise:false, radius:['26%', '78%'],
      data:nodes && nodes.length ? nodes : names.map(function(name) { return {name:name}; }),
      links:rows, emphasis:{focus:'adjacency'}, label:{show:true, fontSize:10, width:85, overflow:'truncate'}, lineStyle:{color:'target', opacity:0.55}}];
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
  if (option.dataZoom) {
    option.dataZoom = [{type:'slider', yAxisIndex:0, start:0, end:option.dataZoom[0].end,
      width:10, right:2, top:option.grid.top, bottom:option.grid.bottom, zoomLock:true,
      showDataShadow:false, showDetail:false, brushSelect:false, handleSize:0, moveHandleSize:0},
      {type:'inside', yAxisIndex:0, minSpan:option.dataZoom[0].end, maxSpan:option.dataZoom[0].end,
        zoomOnMouseWheel:false, moveOnMouseWheel:true, moveOnMouseMove:true}];
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
  var cards = [], summaryCards = [], layout = {order:[], hidden:[]}, layoutDb = '', cardGrid = null, lastDrag = 0, calendarGeneration = 0;
  function el(id) { return document.getElementById(id); }
  function layoutKey() { return 'bookoasis-mate-statistics-layout-v1-' + layoutDb; }
  function layoutEnabled(enabled) {
    el('statistics_settings_open').disabled = !enabled; el('statistics_layout_reset').disabled = !enabled;
  }
  function readLayout() {
    try {
      var saved = JSON.parse(localStorage.getItem(layoutKey()) || '{}') || {};
      return {order:Array.isArray(saved.order) ? saved.order.filter(function(id) { return typeof id === 'string'; }) : [],
        hidden:Array.isArray(saved.hidden) ? saved.hidden.filter(function(id) { return typeof id === 'string'; }) : []};
    } catch (error) { return {order:[], hidden:[]}; }
  }
  function saveLayout() {
    try { localStorage.setItem(layoutKey(), JSON.stringify(layout)); el('statistics_layout_message').textContent = '현재 브라우저에 저장했습니다.'; }
    catch (error) { el('statistics_layout_message').textContent = '브라우저 저장을 사용할 수 없어 이번 화면에만 적용합니다.'; }
  }
  function orderedCards() {
    var ids = Array.from(new Set(layout.order.concat(cards.map(function(item) { return item.id; }))));
    return ids.map(function(id) { return cards.find(function(item) { return item.id === id; }); }).filter(Boolean);
  }
  function applyLayout() {
    destroyGrid();
    summaryCards.forEach(function(item) { item.node.hidden = layout.hidden.indexOf(item.id) !== -1; });
    el('statistics_kpis').hidden = !summaryCards.some(function(item) { return !item.node.hidden; });
    orderedCards().forEach(function(item) {
      item.node.hidden = layout.hidden.indexOf(item.id) !== -1;
      el('statistics_charts').appendChild(item.node);
    });
    el('statistics_cards_empty').hidden = !cards.length || cards.some(function(item) { return !item.node.hidden; });
    initGrid();
    requestAnimationFrame(function() {
      el('statistics_charts').querySelectorAll('.doctor-statistics-echart').forEach(function(canvas) {
        if (canvas._redrawStatistics) canvas._redrawStatistics();
      });
      charts.forEach(function(chart) { if (chart.getDom().offsetWidth) chart.resize(); });
    });
  }
  function destroyGrid() {
    if (cardGrid) { cardGrid.destroy(); cardGrid = null; }
    el('statistics_charts').classList.remove('statistics-muuri');
  }
  function initGrid() {
    if (typeof Muuri === 'undefined') return;
    var container = el('statistics_charts'), reduced = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    container.classList.add('statistics-muuri');
    cardGrid = new Muuri(container, {
      items:'.doctor-statistics-chart-card:not([hidden])', dragEnabled:true, dragHandle:'.statistics-card-handle',
      layout:{fillGaps:false, rounding:true}, layoutDuration:reduced ? 0 : 260, layoutEasing:'ease',
      dragStartPredicate:{distance:4, delay:0}, dragSortInterval:45,
      dragRelease:{duration:reduced ? 0 : 220, easing:'ease'}
    });
    cardGrid.on('dragStart', function() { lastDrag = Date.now(); });
    cardGrid.on('dragEnd', function() { lastDrag = Date.now(); });
    cardGrid.on('dragReleaseEnd', function() {
      if (!cardGrid) return;
      var visible = cardGrid.getItems().map(function(item) { return item.getElement().dataset.cardId; }), index = 0;
      layout.order = orderedCards().map(function(item) { return item.node.hidden ? item.id : visible[index++]; });
      cardGrid.synchronize(); saveLayout(); renderSettings();
    });
    cardGrid.on('layoutEnd', function() { charts.forEach(function(chart) { if (chart.getDom().offsetWidth) chart.resize(); }); });
  }
  function moveCard(id, target) {
    var order = orderedCards().map(function(item) { return item.id; }), from = order.indexOf(id);
    if (from < 0 || target < 0 || target >= order.length) return;
    order.splice(from, 1); order.splice(target, 0, id); layout.order = order;
    saveLayout(); applyLayout(); renderSettings();
  }
  function renderSettings() {
    var focused = document.activeElement && document.activeElement.getAttribute('aria-label');
    var focusedRow = document.activeElement && document.activeElement.closest('.statistics-setting-row');
    var focusedId = focusedRow && focusedRow.dataset.settingId;
    bookoasisMateClear(el('statistics_card_settings'));
    summaryCards.concat(orderedCards()).forEach(function(item) {
      var ordered = orderedCards(), index = ordered.indexOf(item);
      var row = bookoasisMateText('div', 'statistics-setting-row', '');
      row.dataset.settingId = item.id;
      var label = bookoasisMateText('label', 'doctor-plugin-switch', ''), check = document.createElement('input');
      check.type = 'checkbox'; check.checked = !item.node.hidden; check.setAttribute('role', 'switch'); check.setAttribute('aria-label', item.title);
      var state = bookoasisMateText('span', 'statistics-toggle-state', check.checked ? 'On' : 'Off'); state.setAttribute('aria-hidden', 'true');
      check.addEventListener('change', function() {
        layout.hidden = layout.hidden.filter(function(id) { return id !== item.id; });
        if (!check.checked) layout.hidden.push(item.id);
        state.textContent = check.checked ? 'On' : 'Off';
        saveLayout(); applyLayout();
      });
      label.appendChild(check);
      var track = bookoasisMateText('span', 'doctor-plugin-switch-track', ''); track.setAttribute('aria-hidden', 'true');
      label.appendChild(track); label.appendChild(state); label.appendChild(document.createTextNode(item.title)); row.appendChild(label);
      (index < 0 ? [] : [-1, 1]).forEach(function(step) {
        var button = bookoasisMateText('button', 'btn btn-sm btn-outline-secondary', step < 0 ? '↑' : '↓');
        button.type = 'button'; button.disabled = index + step < 0 || index + step >= ordered.length;
        button.setAttribute('aria-label', item.title + (step < 0 ? ' 위로 이동' : ' 아래로 이동'));
        button.addEventListener('click', function() { moveCard(item.id, index + step); }); row.appendChild(button);
      });
      el('statistics_card_settings').appendChild(row);
    });
    if (focused) {
      var controls = Array.from(el('statistics_card_settings').querySelectorAll('[aria-label]'));
      var next = controls.find(function(node) { return node.getAttribute('aria-label') === focused && !node.disabled; });
      if (!next && focusedId) next = controls.find(function(node) {
        return node.getAttribute('role') === 'switch' && node.closest('.statistics-setting-row').dataset.settingId === focusedId;
      });
      if (next) next.focus();
    }
  }
  function dispose() {
    calendarGeneration++;
    destroyGrid();
    if (observer) observer.disconnect();
    observer = null;
    charts.forEach(function(chart) { chart.dispose(); }); charts = [];
  }
  function card(id, title, kind, rows, nodes) {
    var article = bookoasisMateText('article', 'doctor-statistics-chart-card', '');
    var content = bookoasisMateText('div', 'doctor-card statistics-card-content', ''); article.appendChild(content);
    article.dataset.cardId = id;
    if (kind === 'treemap' || kind === 'storageTree' || kind === 'heatmap' || kind === 'calendar') article.classList.add('statistics-card-wide');
    var header = bookoasisMateText('div', 'statistics-card-header', '');
    header.appendChild(bookoasisMateText('h4', '', title));
    var handle = bookoasisMateText('button', 'statistics-card-handle', '⠿');
    handle.type = 'button';
    handle.setAttribute('aria-label', title + ' 이동 설정'); handle.title = '드래그로 이동 · 클릭하면 카드 설정';
    handle.addEventListener('click', function() { if (Date.now() - lastDrag < 400) return; renderSettings(); el('statistics_settings').showModal(); });
    header.appendChild(handle); content.appendChild(header);
    cards.push({id:id, title:title, node:article}); article.hidden = layout.hidden.indexOf(id) !== -1;
    el('statistics_charts').appendChild(article);
    if ((!rows || !rows.length) && kind !== 'calendar') {
      content.appendChild(bookoasisMateText('p', 'doctor-empty', '표시할 데이터가 없습니다. 이전 결과라면 통계를 갱신해 주세요.'));
    } else {
      var canvas = bookoasisMateText('div', 'doctor-statistics-echart', '');
      canvas.setAttribute('role', 'img'); canvas.setAttribute('aria-label', title); content.appendChild(canvas);
      var chart = null;
      var draw = function() {
        if (!rows.length) return;
        if (!canvas.offsetWidth) { canvas._redrawStatistics = draw; return; }
        if (!chart) { chart = echarts.init(canvas, null, {renderer:'canvas'}); charts.push(chart); }
        chart.setOption(bookoasisMateStatisticsOption(kind, rows, title, nodes), true);
        delete canvas._drawStatistics;
        delete canvas._redrawStatistics;
      };
      if (typeof IntersectionObserver !== 'undefined') {
        canvas._drawStatistics = draw;
        if (!observer) observer = new IntersectionObserver(function(entries) {
          entries.forEach(function(entry) {
            if (entry.isIntersecting) { observer.unobserve(entry.target); if (entry.target._drawStatistics) entry.target._drawStatistics(); }
          });
        }, {rootMargin:'200px'});
        observer.observe(canvas);
      } else draw();
      if (kind === 'storageTree') {
        var mode = document.createElement('select'); mode.className = 'statistics-card-select'; mode.setAttribute('aria-label', '용량 Treemap 보기');
        [['format','포맷별'],['size','용량순']].forEach(function(pair) {
          var option = bookoasisMateText('option', '', pair[1]); option.value = pair[0]; mode.appendChild(option);
        });
        header.insertBefore(mode, handle);
        mode.addEventListener('change', function() { nodes = mode.value; draw(); });
      }
      if (kind === 'calendar') {
        canvas.classList.add('statistics-calendar-chart');
        var scroll = bookoasisMateText('div', 'statistics-calendar-scroll', ''); content.appendChild(scroll); scroll.appendChild(canvas);
        var message = bookoasisMateText('p', 'doctor-muted', '사용자를 선택하면 올해 독서 기록을 표시합니다.'); content.insertBefore(message, scroll);
        var users = document.createElement('select'); users.className = 'statistics-card-select'; users.setAttribute('aria-label', '독서 달력 사용자');
        var placeholder = bookoasisMateText('option', '', '사용자 선택'); placeholder.value = ''; users.appendChild(placeholder);
        users.disabled = true; header.insertBefore(users, handle);
        var epoch = calendarGeneration, request = 0, db = el('statistics_db_type').value, lib = el('statistics_library').value;
        var current = function() { return epoch === calendarGeneration && article.isConnected; };
        bookoasisMateAjax('main', 'statistics_users', {db_type:db}, function(ret) {
          if (!current()) return;
          if (ret.ret !== 'success') { message.textContent = ret.msg || '사용자 목록 조회 실패'; return; }
          (ret.data || []).forEach(function(user) {
            var option = bookoasisMateText('option', '', user.username || ('사용자 #' + user.id)); option.value = String(user.id); users.appendChild(option);
          });
          users.disabled = false;
          if (!ret.data.length) message.textContent = '선택할 사용자가 없습니다.';
        }, null, {global:false, silent:true, error:function() { if (current()) message.textContent = '사용자 목록 조회 실패'; }});
        users.addEventListener('change', function() {
          var token = ++request; rows = []; if (chart) chart.clear();
          message.textContent = users.value ? '독서 기록 조회 중…' : '사용자를 선택하면 올해 독서 기록을 표시합니다.';
          if (!users.value) return;
          bookoasisMateAjax('main', 'statistics_reading_calendar', {db_type:db, library_id:lib, user_id:users.value}, function(ret) {
            if (!current() || token !== request) return;
            if (ret.ret !== 'success') { message.textContent = ret.msg || '독서 기록 조회 실패'; return; }
            rows = ret.data.days || []; nodes = ret.data.year;
            message.textContent = nodes + '년 · ' + rows.filter(function(day) { return day[1] > 0; }).length + '일 읽음'; draw();
          }, null, {global:false, silent:true, error:function() {
            if (current() && token === request) message.textContent = '독서 기록 조회 실패';
          }});
        });
      }
    }
  }
  function render(result) {
    if (cardGrid && cardGrid.getItems().some(function(item) { return item.isDragging() || item.isReleasing(); })) return;
    var key = [result.db_type, result.library_id, result.generated_at].join('|');
    if (key === resultKey) return;
    resultKey = key; dispose(); cards = []; summaryCards = []; layoutDb = result.db_type; layout = readLayout(); el('statistics_result').hidden = false;
    bookoasisMateClear(el('statistics_charts')); bookoasisMateClear(el('statistics_kpis'));
    el('statistics_result_title').textContent = (result.library_name || '전체 보관함') + ' 통계';
    el('statistics_result_meta').textContent = String(result.engine || '').toUpperCase() + ' · 집계 ' + (result.generated_at || '').replace('T', ' ');
    var summary = result.summary || {}, extra = result.charts || {}, media = result.media_kind || 'book';
    [['자료', summary.total_items, 'items'], ['시리즈', summary.total_series, 'series'], ['저자', summary.total_authors, 'authors'],
      ['출판사', summary.total_publishers, 'publishers'], ['트랙', summary.total_tracks, 'tracks'], ['에피소드', summary.total_episodes, 'episodes'],
      ['올해 추가', summary.added_this_year, 'added'], ['보관함', (result.libraries || []).length, 'libraries'],
      ['출시 연도', (result.publication_years || []).length, 'years'], ['저장 공간', bookoasisMateBytes(summary.storage_bytes || 0), 'storage'],
      ['재생 시간', summary.total_duration == null ? null : (summary.total_duration / 3600).toFixed(1) + '시간', 'duration']].forEach(function(pair) {
      if (pair[1] == null) return;
      var box = bookoasisMateText('div', 'doctor-statistics-kpi', '');
      box.dataset.summaryId = pair[2];
      summaryCards.push({id:'summary-' + pair[2], title:'요약 · ' + pair[0], node:box});
      box.appendChild(bookoasisMateText('span', '', pair[0]));
      box.appendChild(bookoasisMateText('strong', '', typeof pair[1] === 'number' ? pair[1].toLocaleString('ko-KR') : pair[1]));
      el('statistics_kpis').appendChild(box);
    });
    card('formats', '파일 형식 분포', 'pie', result.formats);
    card('metadata-average', '메타데이터 평균 완성도', 'gauge', extra.metadata_average == null ? [] : [{count:extra.metadata_average}]);
    card('metadata-scores', '메타데이터 점수 분포', 'bar', result.metadata_scores);
    card('metadata-missing', '메타데이터 누락 현황', 'bar', result.metadata_missing);
    card('libraries', '보관함별 자료 수', 'bar', (result.libraries || []).map(function(row) { return {label:row.name, count:row.count}; }));
    var libraryNames = {}; (result.libraries || []).forEach(function(row) { libraryNames[row.id] = row.name; });
    card('metadata-heatmap', '보관함별 메타데이터 완성도', 'heatmap', (extra.metadata_heatmap || []).map(function(row) {
      return {library:libraryNames[row.library_id] || ('보관함 #' + row.library_id), label:row.label, value:row.value};
    }));
    card('storage-formats', '포맷별 저장 공간', 'storagePie', (result.formats || []).map(function(row) { return {label:row.label, count:row.size_bytes || 0}; }));
    if (media !== 'audiobook') { card('genres', '장르 분포', 'treemap', result.genres); card('genre-links', '장르 연관성', 'chord', extra.genre_links, extra.genre_nodes); }
    if (media === 'book') card('tags', '태그 분포', 'treemap', result.tags);
    if (media !== 'video') { card('authors', '상위 저자', 'bar', extra.authors); card('publishers', '상위 출판사', 'bar', extra.publishers); }
    if (media === 'book') card('series', '상위 시리즈', 'bar', extra.series);
    card('lengths', media === 'book' ? '페이지 수 분포' : media === 'audiobook' ? '트랙 수 분포' : '에피소드 수 분포', 'bar', extra.lengths);
    card('added', '자료 추가 추이', 'line', (result.added_over_time || []).map(function(row) { return {label:row.period, count:row.count}; }));
    card('years', '출판·출시 연도 타임라인', 'line', result.publication_years);
    var decades = {}; (result.publication_years || []).forEach(function(row) {
      var label = Math.floor(Number(row.label) / 10) * 10 + '년대'; decades[label] = (decades[label] || 0) + row.count;
    });
    card('decades', '출판·출시 시대', 'bar', Object.keys(decades).sort().map(function(label) { return {label:label, count:decades[label]}; }));
    card('format-timeline', '포맷 비중 변화', 'stack', extra.format_timeline);
    var largest = (result.largest_items || []).filter(function(row) { return row.size_bytes > 0; }).map(function(row) { return {id:row.id, label:row.title, count:row.size_bytes, format:row.format}; });
    card('largest', '용량이 큰 항목 · 상위 50개', 'storage', largest.slice(0, 50));
    card('largest-tree', '용량이 큰 항목 · Treemap', 'storageTree', largest);
    if (media === 'book') card('reading-calendar', '도서 읽은 날', 'calendar', []);
    var progress = result.progress || {};
    card('progress', '전체 사용자 진행 상태 · DB 반영 기준', 'bar', [{label:'시작 전', count:progress.not_started}, {label:'진행 중', count:progress.in_progress}, {label:'완료', count:progress.completed}]);
    applyLayout(); renderSettings();
  }
  function schedule() { clearTimeout(timer); timer = setTimeout(load, 5000); }
  function load() {
    clearTimeout(timer);
    var token = ++generation, db = el('statistics_db_type').value, lib = el('statistics_library').value;
    bookoasisMateAjax('main', 'statistics_status', {db_type:db, library_id:lib}, function(ret) {
      if (token !== generation) return;
      var data = ret.data || {}; busy = !!data.busy;
      layoutEnabled(!!data.result);
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
      else { dispose(); cards = []; summaryCards = []; renderSettings(); resultKey = ''; el('statistics_result').hidden = true; }
      schedule();
    }, null, {global:false, silent:true, error:function(xhr, message) {
      if (token !== generation) return;
      el('statistics_error').textContent = message || '통계 조회 실패'; schedule();
    }});
  }
  $(function() {
    if (typeof echarts === 'undefined') { el('statistics_error').textContent = 'ECharts 번들을 불러오지 못했습니다.'; return; }
    el('statistics_settings_open').addEventListener('click', function() { renderSettings(); el('statistics_settings').showModal(); });
    el('statistics_settings_close').addEventListener('click', function() { el('statistics_settings').close(); });
    el('statistics_settings').addEventListener('click', function(event) { if (event.target === this) this.close(); });
    el('statistics_layout_reset').addEventListener('click', function() {
      layout = {order:[], hidden:[]}; saveLayout(); applyLayout(); renderSettings();
    });
    function changeScope() { layoutEnabled(false); dispose(); cards = []; summaryCards = []; renderSettings(); resultKey = ''; el('statistics_result').hidden = true; load(); }
    el('statistics_db_type').addEventListener('change', function() { el('statistics_library').value = ''; changeScope(); });
    el('statistics_library').addEventListener('change', changeScope);
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
