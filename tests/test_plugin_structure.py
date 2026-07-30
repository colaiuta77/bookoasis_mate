# FlaskFarm 플러그인의 배포 구조와 메뉴 선언을 검증합니다.
import unittest
from pathlib import Path


class PluginStructureTest(unittest.TestCase):
    def setUp(self):
        self.root = Path(__file__).resolve().parents[1]

    def test_required_files_exist(self):
        required = [
            "__init__.py",
            ".gitignore",
            "setup.py",
            "info.yaml",
            "mod_main.py",
            "mod_migration.py",
            "mod_database_migration.py",
            "mod_gdrive_scan.py",
            "mod_setting.py",
            "model_gdrive_scan.py",
            "gdrive_scan.py",
            "mate_engine.py",
            "category_migration.py",
            "kavita_migration.py",
            "bookoasis_package_import.py",
            "database_migration_worker.py",
            "series_gap.py",
            "cover_inspector.py",
            "bookoasis_logs.py",
            "templates/bookoasis_mate_main_dashboard.html",
            "templates/bookoasis_mate_main_issues.html",
            "templates/bookoasis_mate_main_scanner.html",
            "templates/bookoasis_mate_main_logs.html",
            "templates/bookoasis_mate_main_gaps.html",
            "templates/bookoasis_mate_main_covers.html",
            "templates/bookoasis_mate_main_orphan_covers.html",
            "templates/bookoasis_mate_main_history.html",
            "templates/bookoasis_mate_main_manual.html",
            "templates/bookoasis_mate_setting.html",
            "templates/bookoasis_mate_migration_setting.html",
            "templates/bookoasis_mate_migration_status.html",
            "templates/bookoasis_mate_migration_manual.html",
            "templates/bookoasis_mate_database_migration_setting.html",
            "templates/bookoasis_mate_database_migration_status.html",
            "templates/bookoasis_mate_database_migration_manual.html",
            "templates/bookoasis_mate_gdrive_scan_setting.html",
            "templates/bookoasis_mate_gdrive_scan_list.html",
            "templates/bookoasis_mate_gdrive_scan_manual.html",
            "scripts/gd_poller_ff_bridge.py",
        ]
        self.assertEqual([], [path for path in required if not (self.root / path).is_file()])

    def test_info_matches_folder_and_version(self):
        text = (self.root / "info.yaml").read_text(encoding="utf-8")

        self.assertEqual("bookoasis_mate", self.root.name)
        self.assertIn('package_name: "bookoasis_mate"', text)
        self.assertIn('version: "1.0.2"', text)
        self.assertIn("colaiuta77/bookoasis_mate", text)
        readme = (self.root / "README.md").read_text(encoding="utf-8")
        self.assertIn("**FF용 플러그인이며", readme)
        self.assertNotIn("FlaskFarm(FF)", readme)
        self.assertIn("v1.0.2 (2026-07-29)", readme)

    def test_public_repository_excludes_local_development_and_runtime_files(self):
        ignore = (self.root / ".gitignore").read_text(encoding="utf-8")

        for pattern in (
            "__pycache__/",
            "*.py[cod]",
            ".env",
            "*.db",
            "*.log",
            "*.pem",
            "*.zip",
            "plan.md",
            "checklist.md",
            "context-notes.md",
        ):
            self.assertIn(pattern, ignore)

    def test_setup_declares_requested_menus_and_read_only_engine(self):
        setup = (self.root / "setup.py").read_text(encoding="utf-8")
        engine = (self.root / "mate_engine.py").read_text(encoding="utf-8")

        for label in ("상태 요약", "문제 도서", "스캔 상태", "BookOasis 로그", "시리즈 누락", "표지 검사", "고아 표지파일 정리", "검사 이력", "카테고리 이관", "복사 설정", "복사 상태", "매뉴얼", "Google Drive 연동", "변경 이벤트", "설정", "로그"):
            self.assertIn(label, setup)
        self.assertIn("?mode=ro", engine)
        self.assertIn("PRAGMA query_only = ON", engine)
        self.assertNotIn("PRAGMA journal_mode =", engine)
        self.assertNotIn("julianday(\\'now\\')", engine)
        self.assertIn("P.history_model = None", setup)
        self.assertIn("P.gdrive_scan_model = None", setup)

    def test_library_diagnosis_manual_explains_storage_rules(self):
        setup = (self.root / "setup.py").read_text(encoding="utf-8")
        module = (self.root / "mod_main.py").read_text(encoding="utf-8")
        manual = (
            self.root / "templates/bookoasis_mate_main_manual.html"
        ).read_text(encoding="utf-8")

        self.assertIn('{"uri": "manual", "name": "매뉴얼"}', setup)
        self.assertIn('"manual"', module)
        for label in (
            "상태 요약",
            "문제 도서",
            "스캔 상태",
            "시리즈 누락",
            "표지 검사",
            "고아 표지파일 정리",
            "페이지 수 미기록",
            "파일 크기 미기록",
            "Lazy Scanner",
            "메타데이터 잠금",
            "scanner/tasks.py",
            "book_info_service.py",
            "<strong>설명</strong>",
            "<strong>동작 방식</strong>",
            "Dry Run",
            "CSV",
            "JSON",
        ):
            self.assertIn(label, manual)

    def test_gdrive_scan_pages_use_wrapper_persistent_queue_and_background_worker(self):
        setup = (self.root / "setup.py").read_text(encoding="utf-8")
        module = (self.root / "mod_gdrive_scan.py").read_text(encoding="utf-8")
        model = (self.root / "model_gdrive_scan.py").read_text(encoding="utf-8")
        manual = (
            self.root / "templates/bookoasis_mate_gdrive_scan_manual.html"
        ).read_text(encoding="utf-8")
        listing = (
            self.root / "templates/bookoasis_mate_gdrive_scan_list.html"
        ).read_text(encoding="utf-8")

        self.assertIn('"uri": "gdrive_scan"', setup)
        self.assertIn('name="gdrive_scan"', module)
        self.assertIn("def process_api", module)
        self.assertIn('sub != "event"', module)
        self.assertIn("gdrive_scan_buffer_seconds", module)
        self.assertIn("threading.Thread", module)
        self.assertIn("recover_processing", module)
        self.assertIn("__tablename__ = \"gdrive_scan_event\"", model)
        self.assertIn("CommandDispatcher", manual)
        self.assertIn("gd_poller_ff_bridge.py", manual)
        self.assertIn("--apikey FLASKFARM_API_KEY", manual)
        self.assertIn("pollers[].dispatchers", manual)
        self.assertIn("탭이 아닌 공백", manual)
        self.assertIn("프로세스 목록에 노출", manual)
        self.assertNotIn("# docker-compose.yml", manual)
        self.assertNotIn("buffer_interval:", manual)
        self.assertIn("동일 보관함 중복 제거", manual)
        self.assertIn("global:showLoading === true", listing)
        self.assertIn("gdrive_event_library", listing)
        self.assertIn("gdrive_event_status", listing)
        self.assertIn("gdrive_event_action", listing)
        self.assertIn("gdrive_event_pagination", listing)
        self.assertIn("'cleanup'", listing)
        self.assertIn("'clear'", listing)
        self.assertIn("gdrive_scan_retention_days", module)
        self.assertIn("gdrive_scan_auto_cleanup", module)
        self.assertIn("TERMINAL_STATUSES", model)
        self.assertIn("cleanup_terminal", model)

    def test_kavita_insert_sql_is_python_310_compatible(self):
        source = (self.root / "kavita_migration.py").read_text(encoding="utf-8")

        self.assertIn('quoted_fields = ", ".join(', source)
        self.assertNotIn("f'\\\"{field}\\\"'", source)

    def test_category_migration_pages_separate_settings_status_and_manual(self):
        setup = (self.root / "setup.py").read_text(encoding="utf-8")
        module = (self.root / "mod_migration.py").read_text(encoding="utf-8")
        setting = (self.root / "templates/bookoasis_mate_migration_setting.html").read_text(encoding="utf-8")
        status = (self.root / "templates/bookoasis_mate_migration_status.html").read_text(encoding="utf-8")
        manual = (self.root / "templates/bookoasis_mate_migration_manual.html").read_text(encoding="utf-8")

        self.assertIn('"uri": "migration"', setup)
        self.assertIn('name="migration"', module)
        self.assertIn('id="migration_mode_export"', setting)
        self.assertIn('id="migration_mode_import"', setting)
        self.assertIn("migration_package_inspect_btn", setting)
        self.assertIn("migration_backup_before_import", setting)
        self.assertIn("migration_import_mode_merge", setting)
        self.assertIn("migration_merge_library_select", setting)
        self.assertIn('id="migration_import_name_group"', setting)
        self.assertIn("$('#migration_import_name').val('');", setting)
        self.assertIn("migration_start_btn", status)
        self.assertIn("migration_stop_btn", status)
        self.assertIn("migration_progress_bar", status)
        self.assertIn("migration_result", status)
        self.assertIn("migrationStartPolling", status)
        self.assertIn("migrationStopPolling", status)
        self.assertIn("{global:false}", status)
        self.assertIn("신규 카테고리", manual)
        self.assertIn("기존 카테고리에 병합", manual)
        self.assertIn("--merge-to", manual)

    def test_database_migration_pages_have_both_sources_and_safe_controls(self):
        setup = (self.root / "setup.py").read_text(encoding="utf-8")
        module = (self.root / "mod_database_migration.py").read_text(
            encoding="utf-8"
        )
        category_module = (self.root / "mod_migration.py").read_text(encoding="utf-8")
        setting = (
            self.root / "templates/bookoasis_mate_database_migration_setting.html"
        ).read_text(encoding="utf-8")
        status = (
            self.root / "templates/bookoasis_mate_database_migration_status.html"
        ).read_text(encoding="utf-8")
        manual = (
            self.root / "templates/bookoasis_mate_database_migration_manual.html"
        ).read_text(encoding="utf-8")

        self.assertIn('"uri": "database_migration"', setup)
        self.assertIn('"name": "통DB 이관"', setup)
        self.assertIn('name="database_migration"', module)
        self.assertNotIn("kavita_", category_module)
        for label in ("이관 설정", "이관 상태", "이관 매뉴얼"):
            self.assertIn(label, setup)
        for command in (
            "packages",
            "inspect_database",
            "inspect",
            "start",
            "status",
            "stop",
        ):
            self.assertIn(command, module)
        self.assertIn("globalSelectLocalFile", setting)
        self.assertIn("globalSelectLocalFolder", setting)
        self.assertIn("database_migration_source", setting)
        self.assertIn('type="hidden" id="database_migration_source"', setting)
        self.assertNotIn('<select id="database_migration_source"', setting)
        self.assertIn('id="database_migration_mode_kavita"', setting)
        self.assertIn('id="database_migration_mode_bookoasis"', setting)
        self.assertIn('<h4>공통 설정</h4>', setting)
        self.assertIn('id="kavita_source_fields" class="doctor-section doctor-card"', setting)
        self.assertIn('id="bookoasis_export_panel" class="doctor-section doctor-card"', setting)
        self.assertIn('id="bookoasis_import_panel" class="doctor-section doctor-card"', setting)
        self.assertIn("databaseMigrationToggleSource();", setting)
        self.assertIn("database_migration_target_db_type", setting)
        self.assertIn('name="kavita_target_db_type"', setting)
        self.assertNotIn("database_migration_target_db_type:targetDb", setting)
        self.assertNotIn(
            'model.get("database_migration_target_db_type")',
            (self.root / "mate_service.py").read_text(encoding="utf-8"),
        )
        self.assertIn("select_database_migration_work_dir_btn", setting)
        self.assertIn("select_kavita_mapping_source_btn", setting)
        self.assertIn("select_kavita_mapping_target_btn", setting)
        self.assertIn("add_kavita_path_mapping_btn", setting)
        self.assertIn("kavita_dry_run", setting)
        self.assertIn("kavita_user_mappings", setting)
        self.assertIn("kavita_user_mapping_selectors", setting)
        self.assertIn("databaseMigrationRenderKavitaUsers", setting)
        self.assertIn("suggested_user_mappings", setting)
        self.assertIn("effective_path_mappings", setting)
        self.assertIn("path_mappings_auto_applied", setting)
        self.assertIn("DB 검사·자동 설정", setting)
        self.assertIn("databaseMigrationHasUsefulPathMappings", setting)
        self.assertIn("databaseMigrationAppendPaths", setting)
        self.assertIn("Kavita 원본 경로", setting)
        self.assertIn("기존 BookOasis 보관함 경로", setting)
        self.assertIn("신규 도서 생성", setting)
        self.assertIn("bookoasis_db_package_path", setting)
        self.assertIn("bookoasis_cover_package_path", setting)
        self.assertIn("bookoasis_package_action", setting)
        self.assertIn("bookoasis_export_name", setting)
        self.assertIn("bookoasis_package_mode_export", setting)
        self.assertIn("bookoasis_package_mode_import", setting)
        self.assertIn("bookoasis_export_panel", setting)
        self.assertIn("bookoasis_import_panel", setting)
        self.assertIn("bookoasis_package_path_mappings", setting)
        self.assertIn("bookoasis_package_dry_run", setting)
        self.assertIn("bookoasis_db_package_select", setting)
        self.assertIn("bookoasis_cover_package_select", setting)
        self.assertIn("bookoasis_package_refresh_btn", setting)
        self.assertIn("databaseMigrationLoadPackages", setting)
        self.assertIn("'database_migration', 'packages'", setting)
        self.assertIn(
            "bookoasisMateAjax('database_migration', 'inspect_database'",
            setting,
        )
        self.assertIn("/GDRIVE/READING/...", setting)
        self.assertIn("bookoasis_confirm_stopped", status)
        self.assertIn(
            "bookoasisMateAjax('database_migration', 'inspect'", setting
        )
        self.assertIn("database_migration_progress_bar", status)
        self.assertIn(
            "bookoasisMateAjax('database_migration', 'status'", status
        )
        self.assertIn("global:false", status)
        self.assertIn("var completed = !running && !!data.result", status)
        self.assertIn("(completed ? '완료' : '대기중')", status)
        self.assertIn("databaseMigrationRecoverStartRequest", status)
        self.assertIn("result.package_action === 'export'", status)
        self.assertIn("database_package_path", status)
        self.assertIn("cover_package_path", status)
        self.assertIn("시작 요청 응답은 끊겼지만 통DB 이관 작업은 정상 실행 중입니다.", status)
        self.assertIn("silent:true", status)
        self.assertNotIn("setInterval(databaseMigrationRefresh", status)
        self.assertIn("databaseMigrationStatusRequestActive", status)
        self.assertIn("databaseMigrationScheduleRefresh", status)
        self.assertIn("Math.min(10000", status)
        self.assertIn("databaseMigrationStatusOutageNotified", status)
        self.assertIn("통DB 이관 상태 조회 연결이 복구되었습니다.", status)
        self.assertIn("css/bookoasis_mate.css", manual)
        self.assertEqual(3, manual.count("doctor-section doctor-card"))
        self.assertNotIn("핵심 내용", manual)
        self.assertNotIn("대상 보관함을 만들고 전체 스캔", manual)
        self.assertIn("신규 생성·기존 갱신·경로 미일치 도서 수", manual)
        self.assertIn("사용자 계정 자체는 만들지 않으며", manual)
        self.assertIn("<h4>주의 사항</h4>", manual)
        self.assertIn("압축을 직접 해제하지 않습니다", manual)
        self.assertIn("기존 데이터와 병합하지 않습니다", manual)
        self.assertIn("원본 DB 물리 경로", (
            self.root / "templates/bookoasis_mate_migration_setting.html"
        ).read_text(encoding="utf-8"))

    def test_adult_database_path_is_visible_only_when_enabled(self):
        template = (self.root / "templates/bookoasis_mate_setting.html").read_text(encoding="utf-8")
        dashboard = (
            self.root / "templates/bookoasis_mate_main_dashboard.html"
        ).read_text(encoding="utf-8")

        self.assertEqual(2, template.count("use_collapse('adult_enabled');"))
        self.assertNotIn("use_collapse('adult_enabled', true)", template)
        self.assertIn("cover_min_file_size_kb", template)
        self.assertIn("cover_min_aspect_percent", template)
        self.assertIn("bookoasis_username", template)
        self.assertIn("bookoasis_password", template)
        self.assertIn("bookoasis_log_dir", template)
        self.assertIn("bookoasis_root_path", template)
        self.assertIn("cover_root_custom", template)
        self.assertIn("Reverse proxy 사용 시 예: https://yourdomain.com", template)
        self.assertIn(
            "BookOasis 설치 시 .env에 설정한 WEBHOOK_TOKEN과 같은 값을 입력합니다.",
            template,
        )
        self.assertIn(
            "너무 짧으면 API 연결이 실패할 수 있습니다. 기본값 30초",
            template,
        )
        self.assertIn("general_db_size_btn", template)
        self.assertIn("general_db_libraries_btn", template)
        self.assertIn("adult_db_size_btn", template)
        self.assertIn("adult_db_libraries_btn", template)
        self.assertNotIn('id="quick_check_btn"', template)
        self.assertIn('id="quick_check_btn"', dashboard)
        self.assertIn("bookoasisMateAjax('main', 'quick_check'", dashboard)

    def test_path_settings_use_flaskfarm_file_and_folder_selectors(self):
        setting = (self.root / "templates/bookoasis_mate_setting.html").read_text(encoding="utf-8")
        migration = (self.root / "templates/bookoasis_mate_migration_setting.html").read_text(encoding="utf-8")

        self.assertIn("select_bookoasis_root_path_btn", setting)
        for removed_button_id in (
            "select_bookoasis_log_dir_btn",
            "select_general_db_path_btn",
            "select_adult_db_path_btn",
        ):
            self.assertNotIn(removed_button_id, setting)
        self.assertIn("select_cover_root_path_btn", setting)
        self.assertIn("updateCoverRootMode", setting)
        self.assertIn("globalSelectLocalFolder", setting)
        self.assertNotIn("globalSelectLocalFile", setting)
        self.assertIn("db/media_general.db", setting)
        self.assertIn("db/media_adult.db", setting)
        self.assertIn("select_migration_work_dir_btn", migration)
        self.assertIn("add_migration_import_target_path_btn", migration)
        self.assertIn("globalSelectLocalFolder", migration)
        self.assertIn("paths.indexOf(path) < 0", migration)

    def test_scanner_and_log_pages_poll_without_global_loading(self):
        scanner = (self.root / "templates/bookoasis_mate_main_scanner.html").read_text(encoding="utf-8")
        logs = (self.root / "templates/bookoasis_mate_main_logs.html").read_text(encoding="utf-8")

        self.assertIn('id="live_queue_card"', scanner)
        self.assertIn('id="scanner_auto_refresh"', scanner)
        self.assertIn("setTimeout(function() { loadScanner(false); }, 2000)", scanner)
        self.assertIn("lazy_progress", scanner)
        self.assertIn("global:showLoading === true", scanner)
        self.assertIn('id="clear_queue_btn"', scanner)
        self.assertIn("cancel_library_scan", scanner)
        self.assertIn("scan_library_covers", scanner)
        self.assertIn("cancel_scan_queue_task", scanner)
        self.assertIn("실행 중 작업은 유지하고", scanner)
        self.assertIn('id="log_output"', logs)
        self.assertIn("log_catalog", logs)
        self.assertIn("log_tail", logs)
        self.assertIn("cursor_identity", logs)
        self.assertIn("textContent", logs)

    def test_dashboard_explains_summary_values_and_database_warning(self):
        template = (self.root / "templates/bookoasis_mate_main_dashboard.html").read_text(encoding="utf-8")
        style = (self.root / "static/css/bookoasis_mate.css").read_text(encoding="utf-8")
        engine = (self.root / "mate_engine.py").read_text(encoding="utf-8")

        self.assertIn("doctorSummaryOverview", template)
        self.assertIn("doctor-overview-donut", template)
        self.assertIn("전체 도서", template)
        self.assertIn("페이지 수 미기록", template)
        self.assertIn("파일 크기 미기록", template)
        self.assertIn("toLocaleString('ko-KR')", template)
        self.assertIn("주의 판정 이유", template)
        self.assertIn("DB 파일 손상 판정이 아니라", template)
        self.assertIn("스캔 실패는 최근 7일", template)
        self.assertIn("status_reasons", engine)
        self.assertIn(".doctor-overview-donut", style)
        self.assertIn("conic-gradient", style)
        self.assertIn(".doctor-overview-groups", style)
        self.assertIn(".doctor-db-reason-warning", style)

    def test_cover_inspection_threshold_defaults_exist(self):
        main = (self.root / "mod_main.py").read_text(encoding="utf-8")

        self.assertIn('"cover_min_file_size_kb": "5"', main)
        self.assertIn('"cover_min_aspect_percent": "35"', main)

    def test_series_gap_page_has_library_filter_and_progress_status(self):
        template = (self.root / "templates/bookoasis_mate_main_gaps.html").read_text(encoding="utf-8")
        style = (self.root / "static/css/bookoasis_mate.css").read_text(encoding="utf-8")

        self.assertIn('id="library_id"', template)
        self.assertIn('id="gap_progress"', template)
        self.assertIn("library_id:$('#library_id').val()", template)
        self.assertIn("setGapLoading", template)
        self.assertIn("doctor-gap-table", template)
        self.assertIn("doctor-gap-meta", template)
        self.assertIn("신뢰도 높음", template)
        self.assertIn('id="gap_export_btn"', template)
        self.assertIn("gaps_export", template)
        self.assertIn("URL.createObjectURL", template)
        self.assertIn("CSV 생성 중", template)
        self.assertNotIn("<th>분석</th><th>신뢰도</th>", template)
        self.assertIn(".doctor-gap-table { min-width:960px; table-layout:fixed; }", style)
        self.assertIn(".doctor-gap-library { white-space:nowrap; }", style)

    def test_cover_page_has_library_filter(self):
        template = (self.root / "templates/bookoasis_mate_main_covers.html").read_text(encoding="utf-8")

        self.assertIn('id="cover_library_id"', template)
        self.assertIn('id="cover_progress"', template)
        self.assertIn("library_id:$('#cover_library_id').val()", template)
        self.assertIn("loadCoverLibraries", template)
        self.assertIn("setCoverLoading", template)
        self.assertIn("force:force ? 'true' : 'false'", template)
        self.assertIn("loadCovers(1, true)", template)
        self.assertIn("$(document).ready(function() { resetCoverResults(); loadCoverLibraries(); });", template)

        for value in ("missing", "resolution", "file_size", "aspect"):
            self.assertIn(f'value="{value}"', template)
        self.assertNotIn('value="http"', template)
        self.assertNotIn('value="file"', template)
        self.assertNotIn("cover_duplicate_btn", template)
        self.assertNotIn("cover_orphan_btn", template)
        self.assertNotIn("cover_duplicates", template)
        self.assertNotIn("cover_orphans", template)

    def test_orphan_cover_cleanup_page_has_filters_dry_run_and_progress_log(self):
        template = (self.root / "templates/bookoasis_mate_main_orphan_covers.html").read_text(encoding="utf-8")
        main = (self.root / "mod_main.py").read_text(encoding="utf-8")
        engine = (self.root / "mate_engine.py").read_text(encoding="utf-8")

        self.assertIn('id="orphan_db_type"', template)
        self.assertIn('id="orphan_library_id"', template)
        self.assertIn('id="orphan_dry_run"', template)
        self.assertIn("data-toggle=\"toggle\" checked", template)
        self.assertIn("orphan_cleanup_start", template)
        self.assertIn("orphan_cleanup_status", template)
        self.assertIn("orphan_cleanup_stop", template)
        self.assertIn("Dry Run이 꺼져 있습니다.", template)
        self.assertIn("confirm_delete:dryRun ? 'false' : 'true'", template)
        self.assertIn('id="orphan_status_text"', template)
        self.assertIn('id="orphan_log"', template)
        self.assertIn('"orphan_covers"', main)
        self.assertNotIn("cover_orphans", main)
        self.assertNotIn("cover_duplicates", main)
        self.assertNotIn("duplicate_cover_references", engine)

    def test_issue_page_has_library_filter_without_cover_issue(self):
        template = (self.root / "templates/bookoasis_mate_main_issues.html").read_text(encoding="utf-8")

        self.assertIn('id="issue_library_id"', template)
        self.assertIn("library_id:$('#issue_library_id').val()", template)
        self.assertIn("loadIssueLibraries", template)
        self.assertNotIn('value="cover"', template)
        self.assertIn("bookoasisMateBindBookActions", template)
        self.assertIn("openIssueJson", template)
        self.assertIn('id="issue_json_modal"', template)
        self.assertIn("JSON.stringify(item.diagnostics", template)
        self.assertIn("'시리즈 · ' + item.series_name", template)

    def test_book_action_ui_is_shared_by_issue_cover_and_gap_pages(self):
        script = (self.root / "static/js/bookoasis_mate.js").read_text(encoding="utf-8")
        style = (self.root / "static/css/bookoasis_mate.css").read_text(encoding="utf-8")
        covers = (self.root / "templates/bookoasis_mate_main_covers.html").read_text(encoding="utf-8")
        issues = (self.root / "templates/bookoasis_mate_main_issues.html").read_text(encoding="utf-8")
        gaps = (self.root / "templates/bookoasis_mate_main_gaps.html").read_text(encoding="utf-8")

        self.assertIn("bookoasisMateBindBookActions", script)
        self.assertIn("bookoasisMateOpenSelectedBookDetail", script)
        self.assertIn("BookOasis에서 상세 보기", script)
        self.assertIn("/#detail?", script)
        self.assertIn("repBookId=", script)
        self.assertIn('doctor_bookoasis_url', covers)
        self.assertIn('doctor_bookoasis_url', issues)
        self.assertIn('doctor_bookoasis_url', gaps)
        self.assertIn("bookoasisMateOpenMetadataSearch", script)
        self.assertIn("metadata_plugins", script)
        self.assertIn("metadata_search", script)
        self.assertIn("metadata_apply", script)
        self.assertIn("book_scan", script)
        self.assertIn(".doctor-action-menu", style)
        self.assertIn("bookoasisMateBindBookActions", covers)
        self.assertIn("bookoasisMateBindBookActions", gaps)
        self.assertIn("item.cover_url", gaps)

    def test_top_level_menu_uses_requested_order(self):
        setup = (self.root / "setup.py").read_text(encoding="utf-8")

        setting = setup.index('{"uri": "setting", "name": "설정"}')
        diagnosis = setup.index('"name": "라이브러리 진단"')
        migration = setup.index('"name": "카테고리 이관"')
        database_migration = setup.index('"name": "통DB 이관"')
        log = setup.index('{"uri": "log", "name": "로그"}')
        self.assertLess(setting, diagnosis)
        self.assertLess(diagnosis, migration)
        self.assertLess(migration, database_migration)
        self.assertLess(database_migration, log)

        diagnosis_menu = setup[diagnosis:migration]
        bookoasis_log = diagnosis_menu.index('{"uri": "logs", "name": "BookOasis 로그"}')
        diagnosis_manual = diagnosis_menu.index('{"uri": "manual", "name": "매뉴얼"}')
        self.assertLess(bookoasis_log, diagnosis_manual)
        self.assertEqual(
            diagnosis_menu[bookoasis_log:].splitlines()[1].strip(),
            '{"uri": "manual", "name": "매뉴얼"},',
        )


if __name__ == "__main__":
    unittest.main()
