import pytest
from unittest.mock import patch, MagicMock

from pr_agent.algo.git_patch_processing import decode_if_bytes


class TestDecodeIfBytes:
    """Test suite for the decode_if_bytes function."""

    def test_utf8_bytes_decode_successfully(self):
        """Test that UTF-8 encoded bytes decode successfully."""
        text = "Hello, World! 日本語テスト"
        utf8_bytes = text.encode('utf-8')
        result = decode_if_bytes(utf8_bytes)
        assert result == text

    def test_string_input_returned_as_is(self):
        """Test that string input is returned unchanged."""
        text = "Already a string"
        result = decode_if_bytes(text)
        assert result == text

    def test_empty_bytes_return_empty_string(self):
        """Test that empty bytes return empty string."""
        result = decode_if_bytes(b"")
        assert result == ""

    def test_none_input_returned_as_is(self):
        """Test that None input is returned as None."""
        result = decode_if_bytes(None)
        assert result is None

    def test_bytearray_input(self):
        """Test that bytearray input is handled correctly."""
        text = "Test bytearray"
        byte_array = bytearray(text.encode('utf-8'))
        result = decode_if_bytes(byte_array)
        assert result == text

    def test_shift_jis_bytes_with_charset_normalizer(self):
        """Test that Shift-JIS encoded bytes are detected and decoded."""
        # Japanese text encoded in Shift-JIS
        text = "日本語テスト"
        shift_jis_bytes = text.encode('shift-jis')

        result = decode_if_bytes(shift_jis_bytes)
        assert result == text

    def test_cp932_bytes_with_charset_normalizer(self):
        """Test that CP932 (Windows Japanese) encoded bytes are detected and decoded."""
        # Japanese text with special characters encoded in CP932
        text = "株式会社テスト～①②③"
        cp932_bytes = text.encode('cp932')

        result = decode_if_bytes(cp932_bytes)
        assert result == text

    def test_euc_jp_bytes_returns_string(self):
        """Test that EUC-JP encoded bytes are decoded to a string."""
        # Note: charset-normalizer may detect EUC-JP as a different encoding
        # but should still return a valid string
        text = "日本語テスト"
        euc_jp_bytes = text.encode('euc-jp')

        result = decode_if_bytes(euc_jp_bytes)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_latin1_bytes_returns_string(self):
        """Test that Latin-1 encoded bytes are decoded to a string."""
        # Note: charset-normalizer may detect as a compatible encoding
        text = "Café résumé naïve"
        latin1_bytes = text.encode('latin-1')

        result = decode_if_bytes(latin1_bytes)
        assert isinstance(result, str)
        assert len(result) > 0

    def test_fallback_when_charset_normalizer_fails(self):
        """Test fallback to manual encoding list when charset-normalizer fails."""
        text = "日本語テスト"
        shift_jis_bytes = text.encode('shift-jis')

        # Mock charset_normalizer.from_bytes to fail
        with patch('charset_normalizer.from_bytes') as mock_from_bytes:
            mock_from_bytes.side_effect = Exception("Detection failed")
            result = decode_if_bytes(shift_jis_bytes)
            # Should still decode using fallback list
            assert result == text

    def test_fallback_when_charset_normalizer_returns_none(self):
        """Test fallback when charset-normalizer returns None."""
        text = "日本語テスト"
        shift_jis_bytes = text.encode('shift-jis')

        with patch('charset_normalizer.from_bytes') as mock_from_bytes:
            mock_result = MagicMock()
            mock_result.best.return_value = None
            mock_from_bytes.return_value = mock_result
            result = decode_if_bytes(shift_jis_bytes)
            # Should still decode using fallback list
            assert result == text

    def test_any_bytes_return_string(self):
        """Test that any byte sequence returns a string (iso-8859-1 can decode anything)."""
        # Any byte sequence can be decoded by iso-8859-1 in the fallback list
        random_bytes = bytes([0x80, 0x81, 0x82, 0xff, 0xfe])

        result = decode_if_bytes(random_bytes)
        # iso-8859-1 can decode any byte sequence
        assert isinstance(result, str)

    def test_mixed_content_sql_file_simulation(self):
        """Test decoding of SQL file content with Japanese comments (simulating real use case)."""
        # Simulate SQL file with Japanese comments in Shift-JIS
        sql_content = """
-- テスト用SQLファイル
SELECT * FROM users WHERE name = '田中';
-- コメント: ユーザー情報を取得
"""
        shift_jis_bytes = sql_content.encode('shift-jis')

        result = decode_if_bytes(shift_jis_bytes)
        assert "テスト用SQLファイル" in result
        assert "SELECT * FROM users" in result
        assert "田中" in result

    def test_large_shift_jis_file(self):
        """Test decoding of larger Shift-JIS content (simulating real SQL files)."""
        # Simulate a larger SQL file with mixed content
        lines = []
        for i in range(100):
            lines.append(f"-- コメント行 {i}: テスト用のSQLファイルです")
            lines.append(f"SELECT * FROM table_{i} WHERE id = {i};")
        sql_content = "\n".join(lines)
        shift_jis_bytes = sql_content.encode('shift-jis')

        result = decode_if_bytes(shift_jis_bytes)
        assert "コメント行 0" in result
        assert "コメント行 99" in result
        assert "SELECT * FROM table_50" in result
