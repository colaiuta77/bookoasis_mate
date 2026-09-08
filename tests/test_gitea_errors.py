# Gitea 요청 실패의 원인별 안내와 비밀정보 비노출을 검증합니다.
import socket
import ssl
import unittest
from unittest.mock import Mock
from urllib.error import HTTPError, URLError

from plugin_manager import GiteaClient, PluginManagerError


class GiteaErrorTest(unittest.TestCase):
    def test_request_errors_identify_actionable_category_without_secrets(self):
        cases = [
            (401, "/api/v1/user", "인증 실패"),
            (403, "/api/v1/user", "사용자 정보 조회 권한"),
            (403, "/api/v1/repos/search", "저장소 읽기 권한"),
            (404, "/api/v1/user", "API 주소"),
            (404, "/api/v1/repos/a/b", "저장소·ref"),
            (429, "/api/v1/user", "요청 제한"),
            (502, "/api/v1/user", "서버 또는 프록시"),
            (URLError(socket.gaierror("secret-token")), "/api/v1/user", "이름 해석"),
            (URLError(TimeoutError("secret-token")), "/api/v1/user", "응답 시간"),
            (URLError(ssl.SSLCertVerificationError("secret-token")), "/api/v1/user", "SSL"),
            (URLError(ConnectionRefusedError("secret-token")), "/api/v1/user", "연결할 수 없"),
        ]
        for failure, path, expected in cases:
            with self.subTest(failure=failure, path=path):
                if isinstance(failure, int):
                    failure = HTTPError("https://private.example/secret-token", failure, "secret-token", {}, None)
                    self.addCleanup(failure.close)
                client = GiteaClient("https://private.example", "secret-token", opener=Mock(open=Mock(side_effect=failure)))
                with self.assertRaises(PluginManagerError) as caught:
                    client._open(path)
                self.assertIn(expected, str(caught.exception))
                self.assertNotIn("secret-token", str(caught.exception))
                self.assertNotIn("private.example", str(caught.exception))
