// 코어 문제 카드와 실제 스캔 이력을 읽기 전용으로 표시합니다.
var coreScanData = null;
var coreScanRequestRunning = false;
var coreScanUpdatedAt = 0;
var coreScanDb = '';

function bookoasisMateCoreError(data) {
  data = data || {};
  if (data.http_status === 401 || data.http_status === 403) return '관리자 권한 확인';
  if (data.http_status === 404 || data.http_status === 405) return '코어 API 미지원';
  if (!data.http_status && data.supported === false && !data.message) return '코어 API 미지원';
  return data.message || '조회 실패 · 현재 상태를 확인할 수 없습니다.';
}

function bookoasisMateProblemMatches(item, dbType, libraryId) {
  var target = item.target || {};
  var scopes = target.cards || [item.card || target];
  return scopes.some(function(scope) {
    return (!scope.db_type || scope.db_type === dbType) &&
      (!libraryId || !scope.library_id || String(scope.library_id) === String(libraryId));
  });
}

function bookoasisMateScanResult(item) {
  var summary = item.result_summary;
  if (!summary || typeof summary !== 'object') return '결과 요약 미제공';
  var parts = [];
  [['new_books', '신규', '권'], ['succeeded', '성공', '권'], ['books', '대상', '권'], ['errors', '오류', '건']].forEach(function(field) {
    if (typeof summary[field[0]] === 'number') parts.push(field[1] + ' ' + summary[field[0]] + field[2]);
  });
  return parts.join(' · ') || '결과 요약 미제공';
}

function renderCoreScanStatus() {
  if (!coreScanData) return;
  var dbType = $('#db_type').val(), libraryId = $('#core_library_filter').val();
  var problems = coreScanData.problems || {}, history = coreScanData.history || {};
  var root = document.getElementById('core_problem_rows');
  bookoasisMateClear(root);
  var names = {file_missing:'파일 없음 · 휴지통으로 이동됨', mass_missing:'대량 이동/삭제 의심 · 처리 보류',
    remote_unavailable:'원격 드라이브 연결 끊김', cover_missing:'표지 추출 실패', file_corrupt:'파일 손상',
    metadata_invalid:'도서 정보 이상', system_task_failed:'백그라운드 작업 실패', user_report:'사용자 신고'};
  var shown = (problems.items || []).filter(function(item) { return bookoasisMateProblemMatches(item, dbType, libraryId); });
  if (!problems.success || !shown.length) root.appendChild(bookoasisMateText('p', 'doctor-muted',
    problems.success ? '현재 필터에 해당하는 코어 문제·경고가 없습니다.' : bookoasisMateCoreError(problems)));
  shown.forEach(function(item) {
    var card = item.card || {}, target = item.target || {};
    var code = card.code || String(item.title_key || '').split('.')[2];
    var row = bookoasisMateText('div', 'doctor-card', '');
    row.appendChild(bookoasisMateText('strong', item.severity === 'action_required' ? 'doctor-error' : '',
      (item.severity === 'action_required' ? '[조치 필요] ' : '[참고] ') + (item.title || names[code] || '코어 문제 · ' + (code || item.source || '미분류'))));
    var scopes = target.cards || [card];
    var scopeText = scopes.map(function(scope) {
      return [scope.db_type, scope.library || (scope.library_id ? '보관함 #' + scope.library_id : ''),
        scope.open_count ? scope.open_count + '건' : '', scope.muted ? '알고 있음' : ''].filter(Boolean).join(' · ');
    }).filter(Boolean).join(' / ');
    row.appendChild(bookoasisMateText('div', 'doctor-muted', scopeText || '시스템 공통'));
    if (item.raw_detail) row.appendChild(bookoasisMateText('div', 'doctor-path', item.raw_detail));
    if (code === 'mass_missing') row.appendChild(bookoasisMateText('div', 'doctor-muted', '자동 승인하지 않습니다. BookOasis 알림에서 원인을 확인한 뒤 처리하세요.'));
    if (item.updated_at) row.appendChild(bookoasisMateText('div', 'doctor-muted', '최근 감지 ' + item.updated_at));
    root.appendChild(row);
  });
  var body = document.getElementById('core_history_rows');
  bookoasisMateClear(body);
  var records = (history.items || []).filter(function(item) {
    return item.db_type === dbType && (!libraryId || String(item.library_id) === String(libraryId));
  });
  document.getElementById('core_history_message').textContent = !history.success ? bookoasisMateCoreError(history) :
    !records.length ? '코어가 제공한 최근 20건 중 현재 필터에 해당하는 기록이 없습니다.' : '';
  var triggers = {manual:'수동', cron:'예약', scheduled:'예약', lazy:'Lazy', webhook:'웹훅'};
  var statuses = {completed:'완료', failed:'실패', cancelled:'취소'};
  records.forEach(function(item) {
    var row = document.createElement('tr');
    [item.library_name || ('보관함 #' + item.library_id), item.task_type || '-',
      triggers[item.trigger_type] || item.trigger_type || '미기록', statuses[item.status] || item.status || '-',
      bookoasisMateScanResult(item), item.finished_at || '-', item.error_message || '-'].forEach(function(value) {
      row.appendChild(bookoasisMateText('td', '', value));
    });
    body.appendChild(row);
  });
}

function loadCoreScanStatus(force) {
  var dbType = $('#db_type').val();
  if (coreScanRequestRunning || (!force && coreScanDb === dbType && Date.now() - coreScanUpdatedAt < 10000)) return;
  coreScanRequestRunning = true;
  bookoasisMateAjax('scan', 'core_scan_status', {db_type:dbType}, function(ret) {
    if ($('#db_type').val() !== dbType) return;
    coreScanData = ret.data || {problems:{message:ret.msg}, history:{message:ret.msg}};
    coreScanDb = dbType;
    renderCoreScanStatus();
  }, function() {
    coreScanRequestRunning = false;
    coreScanUpdatedAt = Date.now();
    if ($('#db_type').val() !== dbType) loadCoreScanStatus(true);
  }, {global:false, silent:true, error:function(xhr, message) {
    if ($('#db_type').val() !== dbType) return;
    coreScanData = {problems:{http_status:xhr.status, message:message}, history:{http_status:xhr.status, message:message}};
    renderCoreScanStatus();
  }});
}
