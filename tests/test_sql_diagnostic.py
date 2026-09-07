# SQL 진단 모드의 허용 목록과 기존 민감 정보 보호를 검증합니다.
import unittest
from types import SimpleNamespace
from unittest.mock import Mock
from sql_query import ReadOnlySqlTool, validate_read_only_sql


class DiagnosticTest(unittest.TestCase):
    def test_diagnostic_allowlist_and_sensitive_guards(self):
        guard = ReadOnlySqlTool._guard_mariadb_sensitive
        for query in (
            "SELECT * FROM information_schema.PROCESSLIST WHERE COMMAND != 'Sleep'",
            "SELECT p.ID FROM `information_schema`.`PROCESSLIST` p",
            "WITH x AS (SELECT * FROM information_schema.INNODB_TRX) SELECT * FROM x",
            "SELECT * FROM performance_schema.threads",
            "SELECT 'DROP TABLE users' AS text_value",
        ):
            with self.subTest(query=query):
                validate_read_only_sql(query)
                guard(query, mode="diagnostic")
        for query in (
            "SELECT * FROM mysql.user", "SELECT * FROM information_schema.USER_PRIVILEGES",
            "SELECT * FROM `settings`", "SELECT `password_hash` FROM `users`",
            "SELECT * FROM books JOIN users u ON u.id=books.id",
            "SELECT GET_LOCK('x', 30)", "SELECT RELEASE_LOCK('x')",
            "SELECT * FROM books LOCK IN SHARE MODE", "SELECT * FROM otherdb.books",
            "SELECT * FROM (otherdb.books)",
        ):
            with self.subTest(query=query), self.assertRaises(ValueError):
                guard(query, mode="diagnostic")
        with self.assertRaises(ValueError):
            guard("SELECT * FROM information_schema.PROCESSLIST")

    def test_sqlite_rejects_diagnostic_before_connecting(self):
        with self.assertRaisesRegex(ValueError, 'MariaDB'):
            ReadOnlySqlTool({'db_engine': 'sqlite'}).execute('general', 'SELECT 1', mode='diagnostic')

    def test_diagnostic_stream_is_bounded_and_transaction_is_read_only(self):
        tool = ReadOnlySqlTool({'db_engine': 'mariadb'})
        connection = Mock()
        cursor = Mock(description=[('ID',)])
        cursor.fetchmany.side_effect = lambda count: [{'ID': i} for i in range(count)]
        connection.execute_stream.return_value = cursor
        tool.database_adapter.connect = Mock(return_value=connection)
        result = tool.execute('general', 'SELECT * FROM information_schema.PROCESSLIST', max_rows=9000, timeout_seconds=99, mode='diagnostic')
        self.assertEqual(result['row_count'], 5000)
        self.assertTrue(result['truncated'])
        cursor.fetchmany.assert_called_once_with(5001)
        connection.execute.assert_any_call('START TRANSACTION READ ONLY')
        connection.execute.assert_any_call('SET SESSION max_statement_time = ?', (30.0,))
        connection.rollback.assert_called_once()
