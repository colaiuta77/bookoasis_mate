// BookOasis Mate 메뉴에서 공통 AJAX와 안전한 DOM 출력을 제공합니다.
function bookoasisMateAjax(moduleName, command, data, callback, completeCallback, options) {
  options = options || {};
  $.ajax({
    url: '/' + PACKAGE_NAME + '/ajax/' + moduleName + '/' + command,
    type: 'POST',
    cache: false,
    global: options.global !== false,
    data: data || {},
    dataType: 'json',
    success: function(ret) {
      if (ret.msg) notify(ret.msg, ret.ret || 'info');
      if (callback) callback(ret);
    },
    error: function(xhr) {
      var message = '요청 처리 중 오류가 발생했습니다.';
      if (xhr.responseJSON && xhr.responseJSON.msg) message = xhr.responseJSON.msg;
      if (options.error) options.error(xhr, message);
      if (!options.silent) notify(message, 'danger');
    },
    complete: function(xhr, status) {
      if (completeCallback) completeCallback(xhr, status);
    }
  });
}

function bookoasisMateText(tag, className, text) {
  var node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = text == null ? '' : String(text);
  return node;
}

function bookoasisMateStatus(status) {
  var labels = {healthy: '정상', warning: '주의', error: '오류', unknown: '알 수 없음'};
  var value = status || 'unknown';
  var node = bookoasisMateText('span', 'doctor-status doctor-status-' + value, labels[value] || value);
  return node;
}

function bookoasisMateBytes(value) {
  var number = Number(value || 0);
  if (!number) return '0 B';
  var units = ['B', 'KB', 'MB', 'GB', 'TB'];
  var index = Math.min(Math.floor(Math.log(number) / Math.log(1024)), units.length - 1);
  return (number / Math.pow(1024, index)).toFixed(index ? 1 : 0) + ' ' + units[index];
}

function bookoasisMateClear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

function bookoasisMateEscape(value) {
  var node = document.createElement('div');
  node.textContent = value == null ? '' : String(value);
  return node.innerHTML;
}

var bookoasisMateBookActionItem = null;
var bookoasisMateBookActionRefresh = null;
var bookoasisMateMetadataPlugins = null;

function bookoasisMateButton(text, className) {
  var button = bookoasisMateText('button', className || '', text);
  button.type = 'button';
  return button;
}

function bookoasisMateEnsureBookActionUi() {
  if (document.getElementById('doctor-book-action-menu')) return;

  var menu = document.createElement('div');
  menu.id = 'doctor-book-action-menu';
  menu.className = 'doctor-action-menu';
  menu.setAttribute('role', 'menu');
  var detailButton = bookoasisMateButton('↗ BookOasis에서 상세 보기', 'doctor-action-menu-item');
  var scanButton = bookoasisMateButton('↻ 개별 도서 재스캔', 'doctor-action-menu-item');
  var metadataButton = bookoasisMateButton('⌕ 플러그인 메타데이터 검색', 'doctor-action-menu-item');
  detailButton.addEventListener('click', function(event) {
    event.stopPropagation();
    bookoasisMateHideBookActionMenu();
    bookoasisMateOpenSelectedBookDetail();
  });
  scanButton.addEventListener('click', function(event) {
    event.stopPropagation();
    bookoasisMateHideBookActionMenu();
    bookoasisMateScanSelectedBook();
  });
  metadataButton.addEventListener('click', function(event) {
    event.stopPropagation();
    bookoasisMateHideBookActionMenu();
    bookoasisMateOpenMetadataSearch();
  });
  menu.addEventListener('click', function(event) { event.stopPropagation(); });
  menu.appendChild(detailButton);
  menu.appendChild(scanButton);
  menu.appendChild(metadataButton);
  document.body.appendChild(menu);

  var modal = document.createElement('div');
  modal.id = 'doctor-metadata-modal';
  modal.className = 'doctor-action-modal';
  modal.setAttribute('aria-hidden', 'true');
  var dialog = document.createElement('div');
  dialog.className = 'doctor-action-dialog';
  dialog.setAttribute('role', 'dialog');
  dialog.setAttribute('aria-modal', 'true');
  dialog.setAttribute('aria-labelledby', 'doctor-metadata-title');

  var header = document.createElement('div');
  header.className = 'doctor-action-dialog-header';
  var title = bookoasisMateText('h4', '', '플러그인 메타데이터 검색');
  title.id = 'doctor-metadata-title';
  var closeButton = bookoasisMateButton('×', 'doctor-action-close');
  closeButton.setAttribute('aria-label', '닫기');
  closeButton.addEventListener('click', bookoasisMateCloseMetadataSearch);
  header.appendChild(title);
  header.appendChild(closeButton);

  var target = bookoasisMateText('div', 'doctor-action-target', '');
  target.id = 'doctor-metadata-target';
  var controls = document.createElement('div');
  controls.className = 'doctor-action-controls';
  var source = document.createElement('select');
  source.id = 'doctor-metadata-source';
  source.className = 'form-control form-control-sm';
  var query = document.createElement('input');
  query.id = 'doctor-metadata-query';
  query.className = 'form-control form-control-sm';
  query.placeholder = '도서 제목 또는 작가';
  query.addEventListener('keydown', function(event) {
    if (event.key === 'Enter') {
      event.preventDefault();
      bookoasisMateRunMetadataSearch();
    }
  });
  var searchButton = bookoasisMateButton('검색', 'btn btn-sm btn-primary');
  searchButton.id = 'doctor-metadata-search-btn';
  searchButton.addEventListener('click', bookoasisMateRunMetadataSearch);
  controls.appendChild(source);
  controls.appendChild(query);
  controls.appendChild(searchButton);

  var status = bookoasisMateText('div', 'doctor-action-status', '');
  status.id = 'doctor-metadata-status';
  var results = document.createElement('div');
  results.id = 'doctor-metadata-results';
  results.className = 'doctor-metadata-results';

  dialog.appendChild(header);
  dialog.appendChild(target);
  dialog.appendChild(controls);
  dialog.appendChild(status);
  dialog.appendChild(results);
  modal.appendChild(dialog);
  modal.addEventListener('click', function(event) {
    if (event.target === modal) bookoasisMateCloseMetadataSearch();
  });
  document.body.appendChild(modal);

  document.addEventListener('click', bookoasisMateHideBookActionMenu);
  document.addEventListener('keydown', function(event) {
    if (event.key === 'Escape') {
      bookoasisMateHideBookActionMenu();
      bookoasisMateCloseMetadataSearch();
    }
  });
}

function bookoasisMateHideBookActionMenu() {
  var menu = document.getElementById('doctor-book-action-menu');
  if (menu) menu.style.display = 'none';
}

function bookoasisMateShowBookActionMenu(event, item, refreshCallback) {
  bookoasisMateEnsureBookActionUi();
  bookoasisMateBookActionItem = item;
  bookoasisMateBookActionRefresh = refreshCallback || null;
  var menu = document.getElementById('doctor-book-action-menu');
  menu.style.display = 'block';
  menu.style.left = Math.max(8, event.clientX) + 'px';
  menu.style.top = Math.max(8, event.clientY) + 'px';
  var rect = menu.getBoundingClientRect();
  if (rect.right > window.innerWidth - 8) menu.style.left = Math.max(8, window.innerWidth - rect.width - 8) + 'px';
  if (rect.bottom > window.innerHeight - 8) menu.style.top = Math.max(8, window.innerHeight - rect.height - 8) + 'px';
}

function bookoasisMateBindBookActions(row, item, refreshCallback) {
  bookoasisMateEnsureBookActionUi();
  row.classList.add('doctor-action-row');
  row.tabIndex = 0;
  row.title = '클릭하여 도서 작업 열기';
  row.addEventListener('click', function(event) {
    event.stopPropagation();
    bookoasisMateShowBookActionMenu(event, item, refreshCallback);
  });
  row.addEventListener('keydown', function(event) {
    if (event.key === 'Enter' || event.key === ' ') {
      event.preventDefault();
      var rect = row.getBoundingClientRect();
      bookoasisMateShowBookActionMenu({clientX: rect.left + 24, clientY: rect.top + 24}, item, refreshCallback);
    }
  });
}

function bookoasisMateSelectedDbType() {
  return (bookoasisMateBookActionItem && bookoasisMateBookActionItem.db_type) ||
    ($('#db_type').length ? $('#db_type').val() : 'general');
}

function bookoasisMateBookDetailUrl(item) {
  var config = document.getElementById('doctor_bookoasis_url');
  var baseUrl = config ? String(config.value || '').trim().replace(/\/+$/, '') : '';
  if (!/^https?:\/\/[^/]+/i.test(baseUrl)) return '';
  var seriesName = String((item && (item.series_name || item.title)) || '').trim();
  if (!seriesName) return '';
  var libraryId = item && item.library_id ? String(item.library_id) : 'all';
  var params = [
    'series=' + encodeURIComponent(seriesName),
    'libraryId=' + encodeURIComponent(libraryId)
  ];
  if (item && item.id) params.push('repBookId=' + encodeURIComponent(item.id));
  if (item && item.title) params.push('displayTitle=' + encodeURIComponent(item.title));
  return baseUrl + '/#detail?' + params.join('&');
}

function bookoasisMateOpenSelectedBookDetail() {
  var item = bookoasisMateBookActionItem;
  if (!item) return;
  if (bookoasisMateSelectedDbType() === 'adult') {
    notify('현재 BookOasis 새 창 라우팅은 성인 DB 유형 복원을 지원하지 않습니다.', 'warning');
    return;
  }
  var url = bookoasisMateBookDetailUrl(item);
  if (!url) {
    notify('BookOasis URL 또는 도서 상세 식별정보를 확인해 주세요.', 'warning');
    return;
  }
  window.open(url, '_blank', 'noopener,noreferrer');
}

function bookoasisMateScanSelectedBook() {
  var item = bookoasisMateBookActionItem;
  if (!item || !item.id) return;
  var title = item.title || item.series_name || ('도서 ' + item.id);
  if (!confirm('"' + title + '" 도서를 재스캔하시겠습니까? 표지와 로컬 메타데이터, 페이지 정보가 함께 갱신됩니다.')) return;
  bookoasisMateAjax('main', 'book_scan', {book_id:item.id, db_type:bookoasisMateSelectedDbType()}, function(ret) {
    if (ret.data && ret.data.success && bookoasisMateBookActionRefresh) bookoasisMateBookActionRefresh();
  });
}

function bookoasisMateSetMetadataStatus(message, loading) {
  var status = document.getElementById('doctor-metadata-status');
  if (!status) return;
  status.className = 'doctor-action-status' + (loading ? ' doctor-action-status-loading' : '');
  status.textContent = message || '';
}

function bookoasisMatePopulateMetadataPlugins(plugins) {
  var select = document.getElementById('doctor-metadata-source');
  bookoasisMateClear(select);
  (plugins || []).forEach(function(plugin) {
    var option = document.createElement('option');
    option.value = plugin.id || '';
    var state = '';
    if (plugin.enabled === false || plugin.active === false) state = ' · 비활성';
    else if (plugin.configured === false) state = ' · 필수 설정 필요';
    else if (plugin.update_supported) state = ' · 자체 업데이트 지원';
    option.textContent = (plugin.name || plugin.id || '이름 없는 플러그인') + state;
    option.disabled = plugin.enabled === false || plugin.active === false || plugin.configured === false;
    select.appendChild(option);
  });
  if (!select.options.length) {
    var empty = document.createElement('option');
    empty.value = '';
    empty.textContent = '활성 검색 플러그인 없음';
    select.appendChild(empty);
  } else {
    var selected = Array.prototype.some.call(select.options, function(option) {
      if (option.disabled) return false;
      option.selected = true;
      return true;
    });
    if (!selected) {
      var unavailable = document.createElement('option');
      unavailable.value = '';
      unavailable.textContent = '사용 가능한 검색 플러그인 없음';
      unavailable.selected = true;
      select.insertBefore(unavailable, select.firstChild);
    }
  }
}

function bookoasisMateLoadMetadataPlugins() {
  if (bookoasisMateMetadataPlugins) {
    bookoasisMatePopulateMetadataPlugins(bookoasisMateMetadataPlugins);
    var cachedAvailable = bookoasisMateMetadataPlugins.filter(function(plugin) {
      return plugin.enabled !== false && plugin.active !== false && plugin.configured !== false;
    }).length;
    bookoasisMateSetMetadataStatus(
      bookoasisMateMetadataPlugins.length
        ? '사용 가능한 검색 플러그인 ' + cachedAvailable + '개 · 비활성 또는 설정 필요 항목은 선택할 수 없습니다.'
        : '활성화된 검색 플러그인이 없습니다.',
      false
    );
    return;
  }
  bookoasisMateSetMetadataStatus('검색 플러그인을 불러오는 중입니다.', true);
  bookoasisMateAjax('main', 'metadata_plugins', {}, function(ret) {
    if (!ret.data || !ret.data.success) {
      bookoasisMateSetMetadataStatus((ret.data && (ret.data.message || ret.data.error)) || '플러그인 목록을 불러오지 못했습니다.', false);
      return;
    }
    bookoasisMateMetadataPlugins = ret.data.plugins || [];
    bookoasisMatePopulateMetadataPlugins(bookoasisMateMetadataPlugins);
    var available = bookoasisMateMetadataPlugins.filter(function(plugin) {
      return plugin.enabled !== false && plugin.active !== false && plugin.configured !== false;
    }).length;
    bookoasisMateSetMetadataStatus(
      bookoasisMateMetadataPlugins.length
        ? '사용 가능한 검색 플러그인 ' + available + '개 · 비활성 또는 설정 필요 항목은 선택할 수 없습니다.'
        : '활성화된 검색 플러그인이 없습니다.',
      false
    );
  }, function(xhr, status) {
    if (status !== 'success') bookoasisMateSetMetadataStatus('플러그인 목록 요청에 실패했습니다.', false);
  });
}

function bookoasisMateOpenMetadataSearch() {
  var item = bookoasisMateBookActionItem;
  if (!item) return;
  bookoasisMateEnsureBookActionUi();
  var modal = document.getElementById('doctor-metadata-modal');
  document.getElementById('doctor-metadata-target').textContent =
    (item.title || '제목 없음') + ' · ID ' + item.id;
  document.getElementById('doctor-metadata-query').value = item.series_name || item.title || '';
  bookoasisMateClear(document.getElementById('doctor-metadata-results'));
  modal.style.display = 'flex';
  modal.setAttribute('aria-hidden', 'false');
  bookoasisMateLoadMetadataPlugins();
}

function bookoasisMateCloseMetadataSearch() {
  var modal = document.getElementById('doctor-metadata-modal');
  if (!modal) return;
  modal.style.display = 'none';
  modal.setAttribute('aria-hidden', 'true');
}

function bookoasisMateRunMetadataSearch() {
  var query = document.getElementById('doctor-metadata-query').value.trim();
  var source = document.getElementById('doctor-metadata-source').value;
  if (!query) {
    bookoasisMateSetMetadataStatus('검색어를 입력해 주세요.', false);
    return;
  }
  if (!source) {
    bookoasisMateSetMetadataStatus('검색 플러그인을 선택해 주세요.', false);
    return;
  }
  bookoasisMateSetMetadataStatus('메타데이터를 검색하는 중입니다.', true);
  bookoasisMateClear(document.getElementById('doctor-metadata-results'));
  bookoasisMateAjax('main', 'metadata_search', {
    db_type:bookoasisMateSelectedDbType(),
    query:query,
    source:source
  }, function(ret) {
    if (!ret.data || !ret.data.success) {
      bookoasisMateSetMetadataStatus((ret.data && (ret.data.message || ret.data.error)) || '메타데이터 검색에 실패했습니다.', false);
      return;
    }
    var results = ret.data.results || [];
    bookoasisMateSetMetadataStatus(results.length ? '검색 결과 ' + results.length + '건입니다.' : '검색 결과가 없습니다.', false);
    bookoasisMateRenderMetadataResults(results, source);
  }, function(xhr, status) {
    if (status !== 'success') bookoasisMateSetMetadataStatus('메타데이터 검색 요청에 실패했습니다.', false);
  });
}

function bookoasisMateRenderMetadataResults(results, source) {
  var container = document.getElementById('doctor-metadata-results');
  bookoasisMateClear(container);
  results.forEach(function(item) {
    var card = document.createElement('div');
    card.className = 'doctor-metadata-card';
    var cover = document.createElement('div');
    cover.className = 'doctor-metadata-cover';
    if (/^https?:\/\//i.test(String(item.cover || ''))) {
      var image = document.createElement('img');
      image.src = item.cover;
      image.alt = '';
      image.onerror = function() { this.style.display = 'none'; };
      cover.appendChild(image);
    }
    var info = document.createElement('div');
    info.className = 'doctor-metadata-info';
    info.appendChild(bookoasisMateText('strong', '', item.title || '제목 없음'));
    info.appendChild(bookoasisMateText('div', 'doctor-muted',
      [item.author, item.publisher, item.pubDate].filter(Boolean).join(' · ') || '서지 정보 없음'));
    info.appendChild(bookoasisMateText('p', 'doctor-metadata-description', item.description || '설명 없음'));
    var applyButton = bookoasisMateButton('이 메타데이터 적용', 'btn btn-sm btn-outline-primary');
    applyButton.addEventListener('click', function() { bookoasisMateApplyMetadata(item, source, applyButton); });
    info.appendChild(applyButton);
    card.appendChild(cover);
    card.appendChild(info);
    container.appendChild(card);
  });
}

function bookoasisMateApplyMetadata(metadata, source, button) {
  var item = bookoasisMateBookActionItem;
  if (!item || !item.id) return;
  if (!confirm('선택한 메타데이터를 "' + (item.title || '도서') + '"에 적용하시겠습니까?')) return;
  button.disabled = true;
  bookoasisMateSetMetadataStatus('메타데이터를 적용하는 중입니다.', true);
  bookoasisMateAjax('main', 'metadata_apply', {
    book_id:item.id,
    db_type:bookoasisMateSelectedDbType(),
    source:source,
    item_data:JSON.stringify(metadata)
  }, function(ret) {
    if (!ret.data || !ret.data.success) {
      button.disabled = false;
      bookoasisMateSetMetadataStatus((ret.data && (ret.data.message || ret.data.error)) || '메타데이터 적용에 실패했습니다.', false);
      return;
    }
    bookoasisMateSetMetadataStatus('메타데이터를 적용했습니다.', false);
    if (bookoasisMateBookActionRefresh) bookoasisMateBookActionRefresh();
    setTimeout(bookoasisMateCloseMetadataSearch, 350);
  }, function(xhr, status) {
    if (status !== 'success') {
      button.disabled = false;
      bookoasisMateSetMetadataStatus('메타데이터 적용 요청에 실패했습니다.', false);
    }
  });
}
