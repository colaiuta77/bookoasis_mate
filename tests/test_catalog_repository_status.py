# 저장소 접근 실패와 메타데이터 파일 누락을 구분하는지 검증합니다.
import io
import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from plugin_manager import BookOasisPluginManager, PluginManagerError


class RepositoryStatusTest(unittest.TestCase):
    def test_repository_http_status_is_structured(self):
        item = {'id': 'example', 'repository': 'https://github.com/owner/example', 'ref': 'main'}
        for code, expected in [(404, 'missing'), (401, 'auth_required'), (403, 'forbidden'), (500, 'temporary_error')]:
            with self.subTest(code=code), patch('plugin_manager.urlopen', side_effect=HTTPError(item['repository'], code, 'error', {}, io.BytesIO())):
                state = BookOasisPluginManager._repository_status(item)
                self.assertEqual(state['repository_status'], expected)
        with patch('plugin_manager.urlopen', side_effect=TimeoutError()):
            self.assertEqual(BookOasisPluginManager._repository_status(item)['repository_status'], 'temporary_error')

    def test_missing_is_cached_hidden_and_install_blocked_but_local_retained(self):
        with tempfile.TemporaryDirectory() as root:
            manager = BookOasisPluginManager()
            item = {'id': 'example', 'repository': 'https://github.com/owner/example', 'ref': 'main', 'catalog_version': '1.0'}
            manager.CATALOG = [item]
            settings = {'plugin_manager_plugins_path': str(Path(root) / 'plugins'), 'plugin_manager_work_dir': str(Path(root) / 'work')}
            with patch.object(manager, '_repository_status', return_value={'repository_status': 'missing', 'repository_http_status': 404}):
                self.assertEqual(manager.catalog(settings, refresh_remote=True), [])
            self.assertEqual(manager.catalog(settings), [])
            with self.assertRaises(PluginManagerError):
                manager.start_catalog_install('example', settings)
            with patch.object(manager, '_installed_catalog', return_value={'example': {'installed_version': '0.9'}}):
                installed = manager.catalog(settings)[0]
                self.assertTrue(installed['installed'])
                self.assertEqual(installed['repository_status'], 'missing')
                self.assertFalse(installed['update_available'])

    def test_discovery_failure_preserves_previous_cache(self):
        with tempfile.TemporaryDirectory() as root:
            manager = BookOasisPluginManager()
            settings = {'plugin_manager_work_dir': root}
            path = manager._discovery_cache_path(settings, create=True)
            manager._discovery.write_cache(path, {'items': [{'id': 'kept'}]})
            with patch.object(manager._discovery, 'discover', side_effect=TimeoutError()):
                manager._run_discovery_refresh(settings)
            self.assertEqual(manager._discovery.read_cache(path)['items'], [{'id': 'kept'}])
            self.assertEqual(manager.discovery_status()['status'], 'failed')
            with patch.object(manager._discovery, 'discover', return_value={'items': []}):
                manager._run_discovery_refresh(settings)
            self.assertEqual(manager._discovery.read_cache(path)['items'], [])
