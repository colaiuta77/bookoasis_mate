# 원격 DB 엔진 및 커버 경로 계약의 불일치 판정을 검증합니다.
import unittest
import io
import json
from unittest.mock import Mock, patch
from bookoasis_client import BookOasisClient
from integration_checks import evaluate_integration


class IntegrationChecksTest(unittest.TestCase):
    def test_mismatch_unknown_and_explicit_cover_mapping(self):
        remote = {'success': True, 'root': '/remote/covers', 'migration_status': 'idle'}
        result = evaluate_integration({'cover_root_path': '/data/covers'}, 'sqlite', {'success': True, 'engine': 'mariadb'}, remote)
        self.assertEqual(result['db_engine']['status'], 'mismatch')
        self.assertEqual(result['cover_storage']['status'], 'mapping_required')
        self.assertEqual(evaluate_integration({'cover_root_path': '/remote/covers'}, 'sqlite', {}, remote)['cover_storage']['status'], 'mapping_required')
        result = evaluate_integration({'cover_root_path': '/data/covers', 'cover_storage_remote_root': '/remote/covers'}, 'mariadb', {}, remote)
        self.assertEqual(result['db_engine']['status'], 'unknown')
        self.assertEqual(result['cover_storage']['status'], 'mapped')
        remote['migration_status'] = 'running'
        self.assertEqual(evaluate_integration({}, 'sqlite', {}, remote)['cover_storage']['status'], 'moving')

    def test_client_returns_only_contract_fields_and_uses_header_token(self):
        client = BookOasisClient('http://bookoasis:5930')
        client._admin_request = Mock(side_effect=[{'success': True, 'settings': {'COVER_STORAGE_ROOT': '/covers-new', 'WEBHOOK_TOKEN': 'secret'}}, {'success': True, 'status': {'status': 'running'}}])
        self.assertEqual(client.cover_storage_info(), {'success': True, 'root': '/covers-new', 'migration_status': 'running'})
        response = io.BytesIO(json.dumps({'success': True, 'engine': 'mariadb'}).encode())
        with patch('bookoasis_client.urlopen', return_value=response) as request:
            self.assertEqual(client.remote_db_engine('private-token'), {'success': True, 'engine': 'mariadb'})
            actual = request.call_args.args[0]
            self.assertNotIn('private-token', actual.full_url)
            self.assertEqual(actual.get_header('X-webhook-token'), 'private-token')
        with patch('bookoasis_client.urlopen', side_effect=TimeoutError()):
            self.assertFalse(client.remote_db_engine('private-token')['success'])
