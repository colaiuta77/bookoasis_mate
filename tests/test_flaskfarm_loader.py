# FlaskFarm 핵심 계약을 모사해 플러그인 setup 로딩과 모듈 등록을 검증합니다.
import importlib
import json
import logging
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


class _DummyColumn:
    def desc(self):
        return self


class _DummyDb:
    Integer = int
    String = str
    DateTime = object
    Text = str

    @staticmethod
    def Column(*args, **kwargs):
        return _DummyColumn()


class _FailingHistoryDb(_DummyDb):
    @staticmethod
    def Column(*args, **kwargs):
        raise RuntimeError("history model is unavailable")


class _DummyModelBase:
    def save(self):
        return self


class _DummySetting:
    values = {}

    @classmethod
    def get(cls, key):
        return cls.values.get(key)

    @classmethod
    def get_bool(cls, key):
        return str(cls.get(key)).lower() == "true"

    @classmethod
    def to_dict(cls):
        return dict(cls.values)


class _DummyForm(dict):
    def to_dict(self):
        return dict(self)


class _DummyResponse:
    def __init__(self, response=None, mimetype=None, headers=None):
        self.data = response
        self.mimetype = mimetype
        self.headers = dict(headers or {})


class _DummyModuleBase:
    def __init__(self, plugin, first_menu=None, name=None, scheduler_desc=None):
        self.P = plugin
        self.first_menu = first_menu
        self.name = name
        self.scheduler_desc = scheduler_desc


class _DummyPlugin:
    package_name = "bookoasis_mate"
    ModelSetting = _DummySetting
    logger = logging.getLogger("bookoasis_mate_loader_test")

    def __init__(self):
        self.module_list = []

    def set_module_list(self, module_classes):
        self.module_list = [module_class(self) for module_class in module_classes]


class FlaskFarmLoaderTest(unittest.TestCase):
    def setUp(self):
        _DummySetting.values = {}

    def _load_package(self, database):
        plugin = types.ModuleType("plugin")
        plugin.PluginModuleBase = _DummyModuleBase
        plugin.ModelBase = _DummyModelBase
        plugin.db = database
        plugin.F = types.SimpleNamespace(app=None, db=None)
        plugin.create_plugin_instance = lambda setting: _DummyPlugin()

        flask = types.ModuleType("flask")
        flask.Response = _DummyResponse
        flask.jsonify = lambda value=None, *args, **kwargs: value
        flask.render_template = lambda name, **kwargs: name

        old_plugin = sys.modules.get("plugin")
        old_flask = sys.modules.get("flask")
        sys.modules["plugin"] = plugin
        sys.modules["flask"] = flask
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        try:
            for name in list(sys.modules):
                if name == "bookoasis_mate" or name.startswith("bookoasis_mate."):
                    del sys.modules[name]
            return importlib.import_module("bookoasis_mate")
        finally:
            sys.path.pop(0)
            if old_plugin is None:
                sys.modules.pop("plugin", None)
            else:
                sys.modules["plugin"] = old_plugin
            if old_flask is None:
                sys.modules.pop("flask", None)
            else:
                sys.modules["flask"] = old_flask

    def test_setup_registers_main_migration_database_gdrive_sql_and_setting_modules(self):
        package = self._load_package(_DummyDb())

        self.assertEqual("bookoasis_mate", package.P.package_name)
        self.assertEqual(
            ["main", "migration", "database_migration", "gdrive_scan", "sql", "setting"],
            [module.name for module in package.P.module_list],
        )
        self.assertEqual("dashboard", package.P.module_list[0].first_menu)
        self.assertEqual("setting", package.P.module_list[1].first_menu)
        self.assertEqual("setting", package.P.module_list[2].first_menu)
        self.assertEqual("setting", package.P.module_list[3].first_menu)
        self.assertIsNone(package.P.module_list[4].first_menu)
        self.assertIsNotNone(package.P.history_model)
        self.assertIsNotNone(package.P.gdrive_scan_model)
        self.assertEqual(
            "30", package.P.module_list[3].db_default["gdrive_scan_retention_days"]
        )
        self.assertEqual(
            "True", package.P.module_list[3].db_default["gdrive_scan_auto_cleanup"]
        )
        self.assertEqual("30", package.P.module_list[0].db_default["api_timeout"])
        self.assertEqual(
            "False",
            package.P.module_list[0].db_default["cover_root_custom"],
        )
        self.assertIn("bookoasis_root_path", package.P.module_list[0].db_default)
        service = package.P.bookoasis_mate_service
        self.assertEqual(30, service.settings_from_mapping({})["api_timeout"])
        self.assertEqual(
            12,
            service.settings_from_mapping({"api_timeout": "12"})["api_timeout"],
        )
        derived = service.settings_from_mapping(
            {"bookoasis_root_path": "/host/volume1/docker/bookoasis"}
        )
        self.assertEqual(
            "/host/volume1/docker/bookoasis/db/media_general.db",
            derived["general_db_path"],
        )
        self.assertEqual(
            "/host/volume1/docker/bookoasis/db/media_adult.db",
            derived["adult_db_path"],
        )
        self.assertEqual(
            "/host/volume1/docker/bookoasis/logs",
            derived["bookoasis_log_dir"],
        )
        self.assertEqual(
            "/host/volume1/docker/bookoasis/covers",
            derived["cover_root_path"],
        )
        custom_cover = service.settings_from_mapping(
            {
                "bookoasis_root_path": "/host/volume1/docker/bookoasis",
                "cover_root_custom": "True",
                "cover_root_path": "/host/custom/covers",
            }
        )
        self.assertTrue(custom_cover["cover_root_custom"])
        self.assertEqual(
            "/host/custom/covers",
            custom_cover["cover_root_path"],
        )
        self.assertEqual(
            "/host/volume1/docker/bookoasis/db/media_general.db",
            custom_cover["general_db_path"],
        )
        _DummySetting.values = {
            "bookoasis_root_path": "/host/volume1/docker/bookoasis",
            "cover_root_custom": "True",
            "cover_root_path": "/host/persisted/covers",
        }
        persisted = service.settings()
        self.assertEqual(
            "/host/persisted/covers",
            persisted["cover_root_path"],
        )
        legacy = service.settings_from_mapping(
            {"general_db_path": "/legacy/media_general.db"}
        )
        self.assertEqual("/legacy/media_general.db", legacy["general_db_path"])

    def test_sql_module_serves_presets_and_executes_read_only_query(self):
        package = self._load_package(_DummyDb())
        module = package.P.module_list[4]
        tool = Mock()
        tool.presets.return_value = [{"id": "users", "name": "사용자 ID 목록"}]
        tool.execute.return_value = {
            "db_type": "general",
            "columns": ["id"],
            "rows": [[3]],
            "row_count": 1,
            "truncated": False,
            "max_rows": 200,
            "elapsed_ms": 1,
        }

        with patch.object(module, "_tool", return_value=tool):
            presets = module.process_ajax(
                "presets", types.SimpleNamespace(form=_DummyForm())
            )
            executed = module.process_ajax(
                "execute",
                types.SimpleNamespace(
                    form=_DummyForm(
                        db_type="general",
                        query="SELECT id FROM users",
                        max_rows="200",
                        timeout_seconds="3",
                    )
                ),
            )

        self.assertEqual("success", presets["ret"])
        self.assertEqual("users", presets["data"][0]["id"])
        self.assertEqual("success", executed["ret"])
        self.assertEqual([[3]], executed["data"]["rows"])
        tool.execute.assert_called_once_with(
            "general",
            "SELECT id FROM users",
            max_rows="200",
            timeout_seconds="3",
        )

    def test_gdrive_event_list_filters_and_cleanup_route_to_model(self):
        package = self._load_package(_DummyDb())
        module = package.P.module_list[3]

        class FakeEventModel:
            list_kwargs = None
            cleanup_kwargs = None

            @classmethod
            def list_page(cls, **kwargs):
                cls.list_kwargs = kwargs
                return {
                    "items": [],
                    "page": 2,
                    "page_size": 20,
                    "pages": 3,
                    "total": 42,
                }

            @classmethod
            def counts(cls):
                return {"queued": 1, "retry": 0, "processing": 0, "completed": 4, "failed": 1}

            @classmethod
            def filter_options(cls):
                return [
                    {
                        "db_type": "general",
                        "library_id": 25,
                        "library_name": "만화",
                    }
                ]

            @classmethod
            def cleanup_terminal(cls, **kwargs):
                cls.cleanup_kwargs = kwargs
                return 7

        package.P.gdrive_scan_model = FakeEventModel
        _DummySetting.values = {
            "gdrive_scan_history_limit": "200",
            "gdrive_scan_retention_days": "30",
            "gdrive_scan_auto_cleanup": "True",
        }
        request = types.SimpleNamespace(
            form=_DummyForm(
                page="2",
                page_size="20",
                order="asc",
                db_type="general",
                library_id="25",
                status="completed",
                action="create",
                search="표지",
            )
        )

        response = module.process_ajax("list", request)

        self.assertEqual("success", response["ret"])
        self.assertEqual(42, response["data"]["total"])
        self.assertEqual(
            {
                "page": "2",
                "page_size": 20,
                "status": "completed",
                "action": "create",
                "db_type": "general",
                "library_id": "25",
                "search": "표지",
                "order": "asc",
            },
            FakeEventModel.list_kwargs,
        )

        cleanup = module.process_ajax(
            "cleanup", types.SimpleNamespace(form=_DummyForm())
        )
        self.assertEqual("success", cleanup["ret"])
        self.assertEqual(
            {"retention_days": 30, "delete_all": False},
            FakeEventModel.cleanup_kwargs,
        )

    def test_gap_pages_and_csv_share_cached_full_analysis(self):
        package = self._load_package(_DummyDb())
        service = package.P.bookoasis_mate_service
        module = package.P.module_list[0]
        analysis_calls = []
        analysis = {
            "items": [
                {
                    "db_type": "general",
                    "library_id": 7,
                    "library_name": "웹툰",
                    "series_name": "=위험한 시리즈",
                    "present": [1, 2, 4],
                    "missing": [3],
                    "book_count": 3,
                    "parsed_count": 3,
                    "unparsed_count": 0,
                    "ambiguous_count": 0,
                    "confidence": "high",
                    "cover_url": "http://bookoasis/covers/7/cover.webp",
                }
            ],
            "total": 1,
            "analyzed_books": 3,
            "duration_ms": 120,
        }

        class FakeEngine:
            def analyze_series_gaps(self, **kwargs):
                analysis_calls.append(kwargs)
                return analysis

        _DummySetting.values = {
            "general_db_path": "/bookoasis/db/media_general.db",
            "page_size": "50",
        }
        with patch.object(service, "engine", return_value=FakeEngine()):
            page = service.gaps(
                db_type="general",
                library_id="7",
                search="위험",
                page=1,
            )
            exported = module.process_ajax(
                "gaps_export",
                types.SimpleNamespace(
                    form=_DummyForm(
                        db_type="general",
                        library_id="7",
                        search="위험",
                    )
                ),
            )

        self.assertEqual(1, len(analysis_calls))
        self.assertFalse(page["cache_hit"])
        self.assertEqual("text/csv", exported.mimetype)
        self.assertEqual("1", exported.headers["X-BookOasis-Export-Count"])
        csv_text = exported.data.decode("utf-8")
        self.assertTrue(csv_text.startswith("\ufeff"))
        self.assertIn("'=위험한 시리즈", csv_text)
        self.assertIn("1-2, 4", csv_text)
        self.assertIn("3", csv_text)
        self.assertNotIn("cover.webp", csv_text)

    def test_database_diagnostics_ajax_routes_to_owning_pages(self):
        package = self._load_package(_DummyDb())
        main_module = package.P.module_list[0]
        setting_module = package.P.module_list[5]
        request = types.SimpleNamespace(form=_DummyForm(db_type="general"))

        with patch.object(
            main_module.service,
            "quick_check",
            return_value={
                "success": True,
                "message": "정상",
                "result": ["ok"],
            },
        ) as mocked_quick_check:
            quick_check = main_module.process_ajax("quick_check", request)
        with patch.object(
            setting_module.service,
            "database_details",
            return_value={
                "success": True,
                "path": "/bookoasis/db/media_general.db",
                "file_size": 1024,
                "libraries": [{"id": 7, "name": "만화"}],
            },
        ) as mocked_details:
            details = setting_module.process_ajax("database_details", request)

        self.assertEqual("success", quick_check["ret"])
        self.assertEqual(["ok"], quick_check["data"]["result"])
        mocked_quick_check.assert_called_once_with("general")
        self.assertEqual("success", details["ret"])
        self.assertEqual(7, details["data"]["libraries"][0]["id"])
        mocked_details.assert_called_once()

    def test_setup_loads_without_optional_history_model(self):
        with self.assertLogs("bookoasis_mate_loader_test", level="ERROR"):
            package = self._load_package(_FailingHistoryDb())

        self.assertEqual(
            ["main", "migration", "database_migration", "gdrive_scan", "sql", "setting"],
            [module.name for module in package.P.module_list],
        )
        self.assertIsNone(package.P.history_model)
        self.assertIsNone(package.P.gdrive_scan_model)

    def test_service_starts_category_export_in_separate_process(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            work_root = root / "migration"
            cover_root = root / "covers"
            work_root.mkdir()
            cover_root.mkdir()
            _DummySetting.values = {
                "general_db_path": str(root / "media_general.db"),
                "cover_root_path": str(cover_root),
                "migration_work_dir": str(work_root),
                "migration_operation": "export",
                "migration_export_db_type": "general",
                "migration_export_library_ids": "1,2",
            }
            package = self._load_package(_DummyDb())
            service = package.P.bookoasis_mate_service

            captured = {}

            def fake_launch(paths, config, status):
                captured.update(
                    {
                        "paths": paths,
                        "config": config,
                        "status": status,
                    }
                )
                return types.SimpleNamespace(pid=4321, poll=lambda: None)

            with patch.object(
                service,
                "_launch_maintenance_worker",
                side_effect=fake_launch,
            ):
                started = service.start_migration()
                status = service.migration_status()

        self.assertTrue(started["started"])
        self.assertEqual("run", status["is_working"])
        self.assertEqual("export", status["operation"])
        self.assertEqual("category_migration", captured["config"]["job_type"])
        self.assertEqual([1, 2], captured["config"]["export_library_ids"])
        self.assertEqual(
            str(root / "media_general.db"),
            captured["config"]["target_general_db"],
        )

    def test_service_routes_existing_category_merge_to_import_engine(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            work_root = root / "migration"
            cover_root = root / "covers"
            work_root.mkdir()
            cover_root.mkdir()
            _DummySetting.values = {
                "general_db_path": str(root / "media_general.db"),
                "cover_root_path": str(cover_root),
                "migration_work_dir": str(work_root),
                "migration_operation": "import",
                "migration_import_package": str(work_root / "sample.oasis.zip"),
                "migration_import_db_type": "general",
                "migration_import_mode": "merge",
                "migration_merge_library_id": "7",
                "migration_import_name": "병합에서는 사용하지 않음",
                "migration_import_target_paths": "/books/one\n/books/two",
                "migration_backup_before_import": "True",
            }
            package = self._load_package(_DummyDb())
            service = package.P.bookoasis_mate_service

            captured = {}

            def fake_launch(paths, config, status):
                captured.update(config)
                return types.SimpleNamespace(pid=4321, poll=lambda: None)

            with patch.object(
                service,
                "_launch_maintenance_worker",
                side_effect=fake_launch,
            ):
                started = service.start_migration()
                status = service.migration_status()

        self.assertTrue(started["started"])
        self.assertEqual("run", status["is_working"])
        self.assertEqual("7", captured["merge_library_id"])
        self.assertEqual("", captured["import_name"])
        self.assertEqual(
            ["/books/one", "/books/two"],
            captured["import_target_paths"],
        )

    def test_migration_ajax_routes_start_status_and_stop(self):
        package = self._load_package(_DummyDb())
        module = package.P.module_list[1]

        with patch.object(
            module.service,
            "start_migration",
            return_value={"started": True, "message": "시작", "status": {"is_working": "run"}},
        ):
            started = module.process_ajax(
                "start",
                types.SimpleNamespace(form={}),
            )
        with patch.object(
            module.service,
            "migration_status",
            return_value={"is_working": "run"},
        ):
            status = module.process_ajax(
                "status",
                types.SimpleNamespace(form={}),
            )
        with patch.object(
            module.service,
            "stop_migration",
            return_value={"requested": True, "message": "중지", "status": {"is_working": "run"}},
        ):
            stopped = module.process_ajax(
                "stop",
                types.SimpleNamespace(form={}),
            )

        self.assertEqual("success", started["ret"])
        self.assertEqual("run", status["data"]["is_working"])
        self.assertEqual("success", stopped["ret"])

    def test_database_migration_ajax_routes_inspect_start_status_and_stop(self):
        package = self._load_package(_DummyDb())
        module = package.P.module_list[2]
        request = types.SimpleNamespace(form={})

        with patch.object(
            module.service,
            "database_migration_packages",
            return_value={
                "databases": [{"path": "/work/db_test.tar.gz"}],
                "covers": [{"path": "/work/covers_test.tar.gz"}],
            },
        ):
            packages = module.process_ajax("packages", request)
        with patch.object(
            module.service,
            "inspect_bookoasis_database_package",
            return_value={
                "source_type": "bookoasis",
                "inspection_scope": "database",
                "books_count": 4,
            },
        ):
            inspected_database = module.process_ajax("inspect_database", request)
        with patch.object(
            module.service,
            "inspect_database_migration",
            return_value={"source_type": "kavita", "books_count": 3},
        ):
            inspected = module.process_ajax("inspect", request)
        with patch.object(
            module.service,
            "start_database_migration",
            return_value={
                "started": True,
                "message": "시작",
                "status": {"is_working": "run"},
            },
        ) as mocked_start:
            started = module.process_ajax("start", request)
        with patch.object(
            module.service,
            "database_migration_status",
            return_value={"is_working": "run"},
        ):
            status = module.process_ajax("status", request)
        with patch.object(
            module.service,
            "stop_database_migration",
            return_value={
                "requested": True,
                "message": "중지",
                "status": {"is_working": "run"},
            },
        ):
            stopped = module.process_ajax("stop", request)

        self.assertEqual(
            "/work/db_test.tar.gz",
            packages["data"]["databases"][0]["path"],
        )
        self.assertEqual(4, inspected_database["data"]["books_count"])
        self.assertEqual(3, inspected["data"]["books_count"])
        self.assertEqual("success", started["ret"])
        self.assertEqual("run", status["data"]["is_working"])
        self.assertEqual("success", stopped["ret"])
        mocked_start.assert_called_once_with(request.form)

    def test_database_migration_preserves_legacy_kavita_target_database(self):
        _DummySetting.values = {
            "database_migration_source": "kavita",
            "kavita_target_db_type": "adult",
        }
        package = self._load_package(_DummyDb())

        config = package.P.bookoasis_mate_service.database_migration_config()

        self.assertEqual("kavita", config["source_type"])
        self.assertEqual("adult", config["target_db_type"])
        self.assertEqual("import", config["bookoasis_package_action"])

    def test_service_starts_kavita_dry_run_in_separate_process(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            kavita_db = root / "kavita.db"
            bookoasis_db = root / "media_general.db"
            cover_root = root / "covers"
            work_root = root / "migration"
            sqlite3.connect(kavita_db).close()
            sqlite3.connect(bookoasis_db).close()
            cover_root.mkdir()
            work_root.mkdir()
            _DummySetting.values = {
                "general_db_path": str(bookoasis_db),
                "cover_root_path": str(cover_root),
                "migration_work_dir": str(work_root),
                "database_migration_source": "kavita",
                "kavita_db_path": str(kavita_db),
                "kavita_target_db_type": "general",
                "kavita_import_covers": "False",
                "kavita_import_progress": "False",
                "kavita_lock_metadata": "True",
                "kavita_backup_before_import": "True",
                "kavita_dry_run": "True",
            }
            package = self._load_package(_DummyDb())
            service = package.P.bookoasis_mate_service

            with patch.object(
                service,
                "_start_database_migration_process",
            ) as mocked_start:
                started = service.start_database_migration()
                status = service.database_migration_status()

        self.assertTrue(started["started"])
        self.assertEqual("run", status["is_working"])
        self.assertEqual("kavita", status["operation"])
        mocked_start.assert_called_once()
        config = mocked_start.call_args.args[0]
        self.assertTrue(config["dry_run"])
        self.assertEqual(str(kavita_db), config["kavita_db_path"])

    def test_service_starts_bookoasis_package_in_separate_process(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            general_db = root / "media_general.db"
            adult_db = root / "media_adult.db"
            covers = root / "covers"
            work_root = root / "migration"
            db_package = work_root / "db_dist_test.tar.gz"
            cover_package = work_root / "covers_dist_test.tar.gz"
            sqlite3.connect(general_db).close()
            sqlite3.connect(adult_db).close()
            covers.mkdir()
            work_root.mkdir()
            db_package.touch()
            cover_package.touch()
            _DummySetting.values = {
                "general_db_path": str(general_db),
                "adult_db_path": str(adult_db),
                "cover_root_path": str(covers),
                "migration_work_dir": str(work_root),
                "database_migration_source": "bookoasis",
                "bookoasis_db_package_path": str(db_package),
                "bookoasis_cover_package_path": str(cover_package),
                "bookoasis_package_path_mappings": "/old/books => /new/books",
                "bookoasis_backup_before_import": "True",
                "bookoasis_package_dry_run": "True",
            }
            package = self._load_package(_DummyDb())
            service = package.P.bookoasis_mate_service

            with patch.object(
                service,
                "_start_database_migration_process",
            ) as mocked_start:
                started = service.start_database_migration()
                status = service.database_migration_status()

        self.assertTrue(started["started"])
        self.assertEqual("run", status["is_working"])
        self.assertEqual("bookoasis", status["operation"])
        mocked_start.assert_called_once()
        config = mocked_start.call_args.args[0]
        self.assertEqual(
            str(db_package),
            config["bookoasis_db_package_path"],
        )
        self.assertEqual(
            str(cover_package),
            config["bookoasis_cover_package_path"],
        )
        self.assertEqual(
            "/old/books => /new/books",
            config["bookoasis_path_mappings"],
        )
        self.assertTrue(config["bookoasis_dry_run"])
        self.assertEqual("import", config["bookoasis_package_action"])

    def test_service_recovers_and_stops_external_kavita_process(self):
        with tempfile.TemporaryDirectory() as tempdir:
            work_root = Path(tempdir)
            _DummySetting.values = {
                "migration_work_dir": str(work_root),
            }
            package = self._load_package(_DummyDb())
            service = package.P.bookoasis_mate_service
            paths = service._database_migration_worker_paths(work_root)
            paths["status"].write_text(
                json.dumps(
                    {
                        "is_working": "run",
                        "operation": "kavita",
                        "worker_pid": 12345,
                        "message": "Kavita 도서를 이관하고 있습니다.",
                        "logs": [],
                    }
                ),
                encoding="utf-8",
            )
            service._database_migration_status_path = paths["status"]
            service._database_migration_stop_path = paths["stop"]

            status = service.database_migration_status()
            stopped = service.stop_database_migration()
            stop_exists = paths["stop"].is_file()

        self.assertEqual("kavita", status["operation"])
        self.assertTrue(stopped["requested"])
        self.assertTrue(stop_exists)

    def test_service_starts_bookoasis_export_without_import_packages(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            work_root = root / "migration"
            work_root.mkdir()
            _DummySetting.values = {
                "migration_work_dir": str(work_root),
                "database_migration_source": "bookoasis",
                "bookoasis_package_action": "export",
                "bookoasis_export_name": "share",
            }
            package = self._load_package(_DummyDb())
            service = package.P.bookoasis_mate_service

            with patch.object(
                service,
                "_start_database_migration_process",
            ) as mocked_start:
                started = service.start_database_migration()
                status = service.database_migration_status()

        self.assertTrue(started["started"])
        self.assertEqual("run", status["is_working"])
        self.assertEqual("export", status["package_action"])
        config = mocked_start.call_args.args[0]
        self.assertEqual("export", config["bookoasis_package_action"])
        self.assertEqual("share", config["bookoasis_export_name"])

    def test_service_requests_all_library_rescans_through_webhook_client(self):
        with tempfile.TemporaryDirectory() as tempdir:
            db_path = Path(tempdir) / "media_general.db"
            connection = sqlite3.connect(db_path)
            connection.execute("CREATE TABLE libraries (id INTEGER PRIMARY KEY, name TEXT)")
            connection.executemany("INSERT INTO libraries VALUES (?, ?)", [(1, "첫째"), (2, "둘째")])
            connection.commit()
            connection.close()
            _DummySetting.values = {
                "general_db_path": str(db_path),
                "bookoasis_url": "http://bookoasis:5930",
                "webhook_token": "secret",
            }
            package = self._load_package(_DummyDb())

            with self.assertLogs("bookoasis_mate_loader_test", level="DEBUG") as captured:
                with patch(
                    "bookoasis_mate.mate_service.BookOasisClient.request_scan",
                    return_value={"success": True, "already_queued": False, "message": "queued"},
                ) as mocked_request:
                    result = package.P.bookoasis_mate_service.request_rescan(all_libraries=True)

            self.assertTrue(result["success"])
            self.assertEqual(2, result["queued"])
            self.assertEqual([1, 2], sorted(call.args[1] for call in mocked_request.call_args_list))
            log_text = "\n".join(captured.output)
            self.assertIn("재스캔 요청 완료", log_text)
            self.assertIn("requested=2", log_text)
            self.assertNotIn("secret", log_text)

    def test_service_runs_book_actions_and_invalidates_cache(self):
        _DummySetting.values = {
            "bookoasis_url": "http://bookoasis:5930",
            "bookoasis_username": "admin",
            "bookoasis_password": "secret-password",
        }
        package = self._load_package(_DummyDb())
        service = package.P.bookoasis_mate_service
        service._cached_report = {"old": True}
        service._cover_issue_cache = {"items": [1]}

        with patch(
            "bookoasis_mate.mate_service.BookOasisClient.scan_book",
            return_value={"success": True, "message": "완료"},
        ):
            result = service.scan_book(3, "general")

        self.assertTrue(result["success"])
        self.assertIsNone(service._cached_report)
        self.assertIsNone(service._cover_issue_cache)

    def test_service_searches_and_applies_metadata_without_logging_credentials(self):
        _DummySetting.values = {
            "bookoasis_url": "http://bookoasis:5930",
            "bookoasis_username": "admin",
            "bookoasis_password": "secret-password",
        }
        package = self._load_package(_DummyDb())
        service = package.P.bookoasis_mate_service

        with self.assertLogs("bookoasis_mate_loader_test", level="DEBUG") as captured:
            with patch(
                "bookoasis_mate.mate_service.BookOasisClient.metadata_plugins",
                return_value={
                    "success": True,
                    "plugins": [{"id": "aladin", "name": "알라딘", "config": {"API_KEY": "plugin-secret"}}],
                },
            ):
                with patch(
                    "bookoasis_mate.mate_service.BookOasisClient.metadata_plugins_manage",
                    return_value={
                        "success": True,
                        "plugins": [{
                            "id": "aladin",
                            "name": "알라딘",
                            "enabled": True,
                            "is_searchable": True,
                            "config_schema": [{"key": "API_KEY", "required": True}],
                            "config": {"API_KEY": "plugin-secret"},
                            "update_manifest": {"enabled": True},
                        }],
                    },
                ):
                    plugins = service.metadata_plugins()
            with patch(
                "bookoasis_mate.mate_service.BookOasisClient.search_metadata",
                return_value={"success": True, "results": [{"title": "검색 결과"}]},
            ):
                searched = service.search_metadata("검색어", "aladin", "general")
            with patch(
                "bookoasis_mate.mate_service.BookOasisClient.apply_metadata",
                return_value={"success": True, "message": "적용 완료"},
            ):
                applied = service.apply_metadata(9, {"title": "검색 결과"}, "aladin", "general")

        self.assertEqual(
            [{
                "id": "aladin",
                "name": "알라딘",
                "enabled": True,
                "configured": True,
                "active": True,
                "update_supported": True,
            }],
            plugins["plugins"],
        )
        self.assertNotIn("plugin-secret", str(plugins))
        self.assertTrue(searched["success"])
        self.assertTrue(applied["success"])
        log_text = "\n".join(captured.output)
        self.assertNotIn("secret-password", log_text)
        self.assertNotIn("검색어", log_text)

    def test_cover_service_paginates_cached_issue_results(self):
        with tempfile.TemporaryDirectory() as cover_root:
            _DummySetting.values = {
                "bookoasis_url": "http://bookoasis:5930",
                "cover_root_path": cover_root,
            }
            package = self._load_package(_DummyDb())
            sources = [
                {
                    "items": [
                        {"id": 1, "cover_path": "", "title": "표지 없음"},
                        {"id": 2, "cover_path": "1/normal.webp", "title": "정상"},
                    ],
                    "total": 5,
                    "page": 1,
                    "page_size": 1000,
                    "pages": 2,
                },
                {
                    "items": [
                        {"id": 3, "cover_path": "1/small.webp", "title": "저해상도"},
                        {"id": 4, "cover_path": "1/missing.webp", "title": "파일 없음"},
                        {"id": 5, "cover_path": "1/tiny.webp", "title": "작은 파일"},
                    ],
                    "total": 5,
                    "page": 2,
                    "page_size": 1000,
                    "pages": 2,
                },
            ]
            statuses = {
                "": {"status": "missing_reference"},
                "1/normal.webp": {"status": "ok"},
                "1/small.webp": {"status": "low_resolution", "issues": ["low_resolution"], "width": 100, "height": 140},
                "1/missing.webp": {"status": "missing_file"},
                "1/tiny.webp": {"status": "low_resolution", "issues": ["low_resolution", "small_file"]},
            }

            with patch(
                "bookoasis_mate.mate_service.BookOasisMateEngine.cover_items",
                side_effect=sources,
            ) as mocked_items:
                with patch(
                    "bookoasis_mate.mate_service.inspect_cover_file",
                    side_effect=lambda root, path, **kwargs: statuses[path],
                ) as mocked_inspect:
                    first = package.P.bookoasis_mate_service.covers(
                        mode="resolution",
                        page=1,
                        page_size=1,
                        force=True,
                    )
                    second = package.P.bookoasis_mate_service.covers(
                        mode="resolution",
                        page=2,
                        page_size=1,
                    )
                    cached_ids = package.P.bookoasis_mate_service.cover_issue_book_ids(
                        mode="resolution",
                    )

        self.assertEqual([3], [item["id"] for item in first["items"]])
        self.assertEqual([5], [item["id"] for item in second["items"]])
        self.assertEqual(2, first["total"])
        self.assertEqual(5, first["source_total"])
        self.assertEqual(2, first["pages"])
        self.assertEqual(5, first["inspected_count"])
        self.assertEqual(2, first["issue_count"])
        self.assertFalse(first["cache_hit"])
        self.assertTrue(second["cache_hit"])
        self.assertEqual([3, 5], cached_ids)
        self.assertEqual(2, mocked_items.call_count)
        self.assertEqual(5, mocked_inspect.call_count)
        self.assertGreaterEqual(first["duration_ms"], 0)

    def test_cover_service_separates_inspection_modes_and_legacy_aliases(self):
        with tempfile.TemporaryDirectory() as cover_root:
            _DummySetting.values = {
                "bookoasis_url": "http://bookoasis:5930",
                "cover_root_path": cover_root,
            }
            package = self._load_package(_DummyDb())
            source = {
                "items": [
                    {"id": 1, "cover_path": "", "title": "표지 없음"},
                    {"id": 2, "cover_path": "1/low.webp", "title": "저해상도"},
                    {"id": 3, "cover_path": "1/small.webp", "title": "작은 파일"},
                    {"id": 4, "cover_path": "1/wide.webp", "title": "비정상 비율"},
                ],
                "total": 4,
                "page": 1,
                "page_size": 1000,
                "pages": 1,
            }
            statuses = {
                "": {"status": "missing_reference"},
                "1/low.webp": {"status": "low_resolution", "issues": ["low_resolution"]},
                "1/small.webp": {"status": "small_file", "issues": ["small_file"]},
                "1/wide.webp": {"status": "abnormal_aspect_ratio", "issues": ["abnormal_aspect_ratio"]},
            }

            with patch("bookoasis_mate.mate_service.BookOasisMateEngine.cover_items", return_value=source):
                with patch(
                    "bookoasis_mate.mate_service.inspect_cover_file",
                    side_effect=lambda root, path, **kwargs: statuses[path],
                ):
                    missing = package.P.bookoasis_mate_service.covers(mode="http", force=True)
                    resolution = package.P.bookoasis_mate_service.covers(mode="file", force=True)
                    file_size = package.P.bookoasis_mate_service.covers(mode="file_size", force=True)
                    aspect = package.P.bookoasis_mate_service.covers(mode="aspect", force=True)

        self.assertEqual("missing", missing["mode"])
        self.assertEqual([1], [item["id"] for item in missing["items"]])
        self.assertEqual("resolution", resolution["mode"])
        self.assertEqual([2], [item["id"] for item in resolution["items"]])
        self.assertEqual([3], [item["id"] for item in file_size["items"]])
        self.assertEqual([4], [item["id"] for item in aspect["items"]])

    def test_main_ajax_logs_start_and_finish(self):
        package = self._load_package(_DummyDb())
        module = package.P.module_list[0]
        request = types.SimpleNamespace(form={"db_type": "general", "search": "숨긴 검색어"})

        with self.assertLogs("bookoasis_mate_loader_test", level="DEBUG") as captured:
            response = module.process_ajax("unsupported", request)

        self.assertEqual(400, response[1])
        log_text = "\n".join(captured.output)
        self.assertIn("AJAX 시작 command=unsupported", log_text)
        self.assertIn("search=true", log_text)
        self.assertNotIn("숨긴 검색어", log_text)
        self.assertIn("AJAX 종료 command=unsupported", log_text)
        self.assertIn("duration_ms=", log_text)

    def test_service_normalizes_exit_pending_when_live_queue_is_active(self):
        package = self._load_package(_DummyDb())
        service = package.P.bookoasis_mate_service
        db_snapshot = {
            "libraries": [{
                "id": 3,
                "name": "DB 보관함",
                "checkpoint_folders": 7,
                "vfs_refresh_before_scan": 0,
                "rclone_rc_configured": False,
            }],
            "tasks": [{
                "id": 10,
                "task_type": "lazy_scan",
                "task_key": "lazy_scan",
                "status": "exit_pending",
                "stage": "RAM 환수 재기동 (배치 #59)",
            }],
        }
        queue_response = {
            "success": True,
            "queue": {
                "running": {
                    "type": "lazy_scan",
                    "key": "lazy_scan",
                    "stage": "RAM 환수 재기동 (배치 #59)",
                    "library_name": "전체 시스템 (Lazy Scanner)",
                },
                "pending": [],
            },
        }

        with patch("bookoasis_mate.mate_service.BookOasisMateEngine.scanner_status", return_value=db_snapshot):
            with patch.object(service, "admin_client") as mocked_admin:
                mocked_admin.return_value.library_schedules.return_value = {
                    "success": True,
                    "libraries": [{
                        "id": 3,
                        "name": "API 보관함",
                        "scan_status": "scanning",
                        "vfs_refresh_before_scan": 1,
                        "rclone_rc_url": "http://user:password@127.0.0.1:5572",
                    }],
                }
                mocked_admin.return_value.queue_status.return_value = queue_response
                with patch(
                    "bookoasis_mate.mate_service.read_lazy_progress",
                    return_value={"done": 12, "total": 50, "percent": 24.0, "filename": "현재.cbz"},
                ):
                    result = service.scanner(include_live=True)

        self.assertEqual("running", result["tasks"][0]["display_status"])
        self.assertTrue(result["tasks"][0]["is_active"])
        self.assertEqual("api", result["library_source"])
        self.assertEqual("API 보관함", result["libraries"][0]["name"])
        self.assertEqual(7, result["libraries"][0]["checkpoint_folders"])
        self.assertTrue(result["libraries"][0]["rclone_rc_configured"])
        self.assertNotIn("rclone_rc_url", result["libraries"][0])
        self.assertEqual("lazy_scan", result["live_queue"]["running"]["type"])
        self.assertEqual(12, result["lazy_progress"]["done"])

    def test_service_prefers_admin_scan_api_when_credentials_exist(self):
        with tempfile.TemporaryDirectory() as tempdir:
            db_path = Path(tempdir) / "media_general.db"
            connection = sqlite3.connect(db_path)
            connection.execute("CREATE TABLE libraries (id INTEGER PRIMARY KEY, name TEXT)")
            connection.execute("INSERT INTO libraries VALUES (1, '첫째')")
            connection.commit()
            connection.close()
            _DummySetting.values = {
                "general_db_path": str(db_path),
                "bookoasis_url": "http://bookoasis:5930",
                "bookoasis_username": "admin",
                "bookoasis_password": "secret-password",
                "webhook_token": "secret",
            }
            package = self._load_package(_DummyDb())
            service = package.P.bookoasis_mate_service

            with patch.object(service, "admin_client") as mocked_admin:
                mocked_admin.return_value.scan_library.return_value = {
                    "success": True,
                    "message": "관리자 API 등록",
                }
                result = service.request_rescan(
                    library_id=1,
                    all_libraries=False,
                )

        self.assertTrue(result["success"])
        self.assertEqual("admin_api", result["source"])
        mocked_admin.return_value.scan_library.assert_called_once_with(
            1,
            "general",
            False,
        )
        mocked_admin.return_value.request_scan.assert_not_called()

    def test_service_falls_back_to_webhook_when_scan_api_is_unsupported(self):
        with tempfile.TemporaryDirectory() as tempdir:
            db_path = Path(tempdir) / "media_general.db"
            connection = sqlite3.connect(db_path)
            connection.execute("CREATE TABLE libraries (id INTEGER PRIMARY KEY, name TEXT)")
            connection.execute("INSERT INTO libraries VALUES (1, '첫째')")
            connection.commit()
            connection.close()
            _DummySetting.values = {
                "general_db_path": str(db_path),
                "bookoasis_url": "http://bookoasis:5930",
                "bookoasis_username": "admin",
                "bookoasis_password": "secret-password",
                "webhook_token": "secret",
            }
            package = self._load_package(_DummyDb())
            service = package.P.bookoasis_mate_service

            with patch.object(service, "admin_client") as mocked_admin:
                mocked_admin.return_value.library_schedules.return_value = {
                    "success": False,
                    "http_status": 404,
                }
                mocked_admin.return_value.scan_library.return_value = {
                    "success": False,
                    "http_status": 404,
                }
                mocked_admin.return_value.request_scan.return_value = {
                    "success": True,
                    "already_queued": False,
                    "message": "웹훅 등록",
                }
                result = service.request_rescan(library_id=1)

        self.assertTrue(result["success"])
        self.assertEqual("webhook_fallback", result["source"])
        mocked_admin.return_value.request_scan.assert_called_once_with(
            "secret",
            1,
            "general",
            False,
        )

    def test_kavita_preview_prefers_permissions_api_users(self):
        _DummySetting.values = {
            "database_migration_source": "kavita",
            "bookoasis_url": "http://bookoasis:5930",
            "bookoasis_username": "admin",
            "bookoasis_password": "secret-password",
        }
        package = self._load_package(_DummyDb())
        service = package.P.bookoasis_mate_service
        engine = Mock()
        engine.inspect.return_value = {
            "books_count": 1,
            "matched_books_count": 0,
            "source_users": ["Reader"],
            "target_users": ["db-user"],
            "suggested_user_mappings": [],
        }

        with patch.object(service, "_database_migration_engine", return_value=engine):
            with patch.object(service, "admin_client") as mocked_admin:
                mocked_admin.return_value.permissions.return_value = {
                    "success": True,
                    "users": [
                        {"id": 1, "username": "reader"},
                        {"id": 2, "username": "admin"},
                    ],
                }
                result = service.inspect_database_migration()

        self.assertEqual("permissions_api", result["target_users_source"])
        self.assertEqual(["admin", "reader"], result["target_users"])
        self.assertEqual(
            ["Reader => reader"],
            result["suggested_user_mappings"],
        )

    def test_main_ajax_routes_bookoasis_log_commands(self):
        package = self._load_package(_DummyDb())
        module = package.P.module_list[0]

        with patch.object(
            module.service,
            "log_catalog",
            return_value={"success": True, "files": [{"name": "lazy_scanner.log"}]},
        ):
            catalog = module.process_ajax("log_catalog", types.SimpleNamespace(form={}))
        with patch.object(
            module.service,
            "log_tail",
            return_value={"success": True, "text": "새 로그\n", "offset": 7},
        ) as mocked_tail:
            tail = module.process_ajax(
                "log_tail",
                types.SimpleNamespace(form={
                    "filename": "lazy_scanner.log",
                    "cursor_identity": "1:2",
                    "cursor_offset": "100",
                    "line_limit": "500",
                }),
            )

        self.assertEqual("lazy_scanner.log", catalog["data"]["files"][0]["name"])
        self.assertEqual("새 로그\n", tail["data"]["text"])
        mocked_tail.assert_called_once_with(
            "lazy_scanner.log",
            cursor_identity="1:2",
            cursor_offset="100",
            line_limit="500",
        )

    def test_main_ajax_routes_book_scan_and_metadata_apply(self):
        package = self._load_package(_DummyDb())
        module = package.P.module_list[0]

        with patch.object(
            module.service,
            "scan_book",
            return_value={"success": True, "message": "재스캔 완료"},
        ) as mocked_scan:
            scan_response = module.process_ajax(
                "book_scan",
                types.SimpleNamespace(form={"book_id": "8", "db_type": "adult"}),
            )
        with patch.object(
            module.service,
            "apply_metadata",
            return_value={"success": True, "message": "적용 완료"},
        ) as mocked_apply:
            apply_response = module.process_ajax(
                "metadata_apply",
                types.SimpleNamespace(form={
                    "book_id": "8",
                    "db_type": "adult",
                    "source": "aladin",
                    "item_data": '{"title":"검색 결과"}',
                }),
            )

        self.assertEqual("success", scan_response["ret"])
        mocked_scan.assert_called_once_with("8", "adult")
        self.assertEqual("success", apply_response["ret"])
        mocked_apply.assert_called_once_with("8", {"title": "검색 결과"}, "aladin", "adult")

    def test_main_ajax_routes_batch_rescan_commands(self):
        package = self._load_package(_DummyDb())
        module = package.P.module_list[0]

        with patch.object(
            module.service,
            "start_batch_rescan",
            return_value={"started": True, "message": "시작", "status": {"is_working": "run"}},
        ) as mocked_start:
            start_response = module.process_ajax(
                "batch_rescan_start",
                types.SimpleNamespace(form={
                    "source": "issues",
                    "db_type": "adult",
                    "library_id": "7",
                    "issue_type": "pages",
                    "mode": "missing",
                    "search": "검색어",
                }),
            )
        with patch.object(
            module.service,
            "batch_rescan_status",
            return_value={"is_working": "run"},
        ):
            status_response = module.process_ajax(
                "batch_rescan_status",
                types.SimpleNamespace(form={}),
            )
        with patch.object(
            module.service,
            "stop_batch_rescan",
            return_value={"requested": True, "message": "중지", "status": {"is_working": "run"}},
        ):
            stop_response = module.process_ajax(
                "batch_rescan_stop",
                types.SimpleNamespace(form={}),
            )

        self.assertEqual("success", start_response["ret"])
        mocked_start.assert_called_once_with(
            source="issues",
            db_type="adult",
            library_id="7",
            issue_type="pages",
            mode="missing",
            search="검색어",
        )
        self.assertEqual("run", status_response["data"]["is_working"])
        self.assertEqual("success", stop_response["ret"])

    def test_service_starts_filtered_batch_rescan_with_sensitive_external_config(self):
        with tempfile.TemporaryDirectory() as tempdir:
            db_path = Path(tempdir) / "media_general.db"
            db_path.write_bytes(b"database")
            _DummySetting.values = {
                "general_db_path": str(db_path),
                "bookoasis_url": "http://bookoasis:5930",
                "bookoasis_username": "admin",
                "bookoasis_password": "secret",
                "api_timeout": "30",
            }
            package = self._load_package(_DummyDb())
            service = package.P.bookoasis_mate_service
            engine = Mock()
            engine.get_target.return_value = types.SimpleNamespace(path=str(db_path))
            engine.issue_book_ids.return_value = [9, 3, 9]
            client = Mock()
            client.login_admin.return_value = {"success": True}
            process = Mock(pid=321)

            with patch.object(service, "engine", return_value=engine):
                with patch.object(service, "admin_client", return_value=client):
                    with patch.object(
                        service,
                        "_launch_maintenance_worker",
                        return_value=process,
                    ) as mocked_launch:
                        result = service.start_batch_rescan(
                            "issues",
                            db_type="general",
                            library_id="2",
                            issue_type="pages",
                            search="대상",
                        )

        self.assertTrue(result["started"])
        self.assertEqual(2, result["status"]["total"])
        engine.issue_book_ids.assert_called_once_with(
            db_type="general",
            library_id="2",
            issue_type="pages",
            search="대상",
        )
        args, kwargs = mocked_launch.call_args
        worker_config = args[1]
        self.assertEqual([3, 9], worker_config["book_ids"])
        self.assertEqual("secret", worker_config["bookoasis_password"])
        self.assertTrue(worker_config["delete_config_after_read"])
        self.assertTrue(kwargs["sensitive_config"])

    def test_main_ajax_routes_scanner_control_commands(self):
        package = self._load_package(_DummyDb())
        module = package.P.module_list[0]
        cases = [
            (
                "cancel_library_scan",
                "cancel_library_scan",
                {"library_id": "7", "db_type": "adult"},
                ("7", "adult"),
            ),
            (
                "scan_library_covers",
                "scan_library_covers",
                {"library_id": "7", "db_type": "adult"},
                ("7", "adult"),
            ),
            (
                "clear_scan_queue",
                "clear_scan_queue",
                {},
                (),
            ),
            (
                "cancel_scan_queue_task",
                "cancel_scan_queue_task",
                {"task_key": "library_scan_adult_7"},
                ("library_scan_adult_7",),
            ),
        ]

        for command, method_name, form, expected_args in cases:
            with self.subTest(command=command):
                with patch.object(
                    module.service,
                    method_name,
                    return_value={"success": True, "message": "완료"},
                ) as mocked_method:
                    response = module.process_ajax(
                        command,
                        types.SimpleNamespace(form=form),
                    )
                self.assertEqual("success", response["ret"])
                mocked_method.assert_called_once_with(*expected_args)

    def test_service_runs_orphan_cover_dry_run_in_background(self):
        with tempfile.TemporaryDirectory() as tempdir:
            root = Path(tempdir)
            db_path = root / "media_general.db"
            cover_root = root / "covers"
            (cover_root / "1").mkdir(parents=True)
            (cover_root / "1" / "used.webp").write_bytes(b"used")
            (cover_root / "1" / "orphan.webp").write_bytes(b"orphan")
            connection = sqlite3.connect(db_path)
            connection.execute("CREATE TABLE libraries (id INTEGER PRIMARY KEY, name TEXT)")
            connection.execute(
                "CREATE TABLE books (id INTEGER PRIMARY KEY, title TEXT, cover_image TEXT, is_deleted INTEGER)"
            )
            connection.execute("INSERT INTO libraries VALUES (1, '테스트 보관함')")
            connection.execute("INSERT INTO books VALUES (1, '사용 도서', '1/used.webp', 0)")
            connection.commit()
            connection.close()
            _DummySetting.values = {
                "general_db_path": str(db_path),
                "cover_root_path": str(cover_root),
            }
            package = self._load_package(_DummyDb())
            service = package.P.bookoasis_mate_service

            started = service.start_orphan_cleanup("general", "1", "true")
            service._orphan_cleanup_process.wait(timeout=5)
            status = service.orphan_cleanup_status()
            with self.assertRaises(ValueError):
                service.start_orphan_cleanup("general", "1", "false", confirm_delete="false")

        self.assertTrue(started["started"])
        self.assertEqual("wait", status["is_working"])
        self.assertTrue(status["dry_run"])
        self.assertEqual(2, status["scanned_count"])
        self.assertEqual(1, status["target_count"])
        self.assertEqual(0, status["deleted_count"])
        self.assertEqual("planned", status["items"][0]["status"])
        self.assertEqual("1/orphan.webp", status["items"][0]["path"])

    def test_main_ajax_routes_orphan_cleanup_commands(self):
        package = self._load_package(_DummyDb())
        module = package.P.module_list[0]

        with patch.object(
            module.service,
            "start_orphan_cleanup",
            return_value={"started": True, "message": "시작", "status": {"is_working": "run"}},
        ) as mocked_start:
            response = module.process_ajax(
                "orphan_cleanup_start",
                types.SimpleNamespace(form={
                    "db_type": "adult",
                    "library_id": "7",
                    "dry_run": "false",
                    "confirm_delete": "true",
                }),
            )
        with patch.object(
            module.service,
            "orphan_cleanup_status",
            return_value={"is_working": "run"},
        ):
            status_response = module.process_ajax(
                "orphan_cleanup_status",
                types.SimpleNamespace(form={}),
            )
        with patch.object(
            module.service,
            "stop_orphan_cleanup",
            return_value={"requested": True, "message": "중지", "status": {"is_working": "run"}},
        ):
            stop_response = module.process_ajax(
                "orphan_cleanup_stop",
                types.SimpleNamespace(form={}),
            )

        self.assertEqual("success", response["ret"])
        mocked_start.assert_called_once_with(
            db_type="adult",
            library_id="7",
            dry_run="false",
            confirm_delete="true",
        )
        self.assertEqual("run", status_response["data"]["is_working"])
        self.assertEqual("success", stop_response["ret"])


if __name__ == "__main__":
    unittest.main()
