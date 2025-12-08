import os
import tempfile
import pytest
import time

# IMPORTANT: Import config_loader before log to avoid circular dependency
from pr_agent.config_loader import get_settings
from pr_agent.log import setup_logger, get_logger, LoggingFormat


class TestFileLogging:
    """Test file logging functionality with configurable LOG_FILE"""

    def test_file_logging_with_env_var(self):
        """Test that logs are written to file when LOG_FILE env var is set"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.log', delete=False) as f:
            log_file = f.name

        try:
            # Set LOG_FILE environment variable
            os.environ['LOG_FILE'] = log_file

            # Setup logger
            setup_logger(level="INFO", fmt=LoggingFormat.CONSOLE)

            # Log test messages
            get_logger().info("Test info message")
            get_logger().error("Test error message")

            # Wait for async write to complete
            time.sleep(0.2)

            # Verify file was created and contains messages
            assert os.path.exists(log_file), f"Log file {log_file} was not created"

            with open(log_file, 'r') as f:
                content = f.read()
                assert "Test info message" in content, "Info message not found in log file"
                assert "Test error message" in content, "Error message not found in log file"
                assert "INFO" in content, "Log level not found"
                assert "ERROR" in content, "Error level not found"

        finally:
            # Cleanup
            if 'LOG_FILE' in os.environ:
                del os.environ['LOG_FILE']
            if os.path.exists(log_file):
                os.unlink(log_file)

    def test_log_file_format(self):
        """Test that log file uses correct format: timestamp level [module] message"""
        with tempfile.NamedTemporaryFile(mode='w', suffix='.log', delete=False) as f:
            log_file = f.name

        try:
            # Set LOG_FILE environment variable
            os.environ['LOG_FILE'] = log_file

            # Setup logger
            setup_logger(level="INFO", fmt=LoggingFormat.CONSOLE)

            # Log a test message
            get_logger().info("Format validation message")

            # Wait for async write
            time.sleep(0.2)

            # Read and verify format
            with open(log_file, 'r') as f:
                content = f.read()

                # Verify format components
                assert "Format validation message" in content, "Message not found"
                assert "[" in content and "]" in content, "Module markers not found"

                # Verify timestamp format (YYYY-MM-DD HH:mm:ss)
                import re
                timestamp_pattern = r'\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}'
                assert re.search(timestamp_pattern, content), f"Timestamp not in expected format. Content: {content}"

                # Verify level is padded to 8 characters
                assert "INFO    " in content, "Log level not properly formatted"

        finally:
            # Cleanup
            if 'LOG_FILE' in os.environ:
                del os.environ['LOG_FILE']
            if os.path.exists(log_file):
                os.unlink(log_file)

    def test_no_file_logging_without_env_var(self):
        """Test that no file is created when LOG_FILE is not set"""
        # Ensure LOG_FILE is not set
        if 'LOG_FILE' in os.environ:
            del os.environ['LOG_FILE']

        # Setup logger without LOG_FILE
        setup_logger(level="INFO", fmt=LoggingFormat.CONSOLE)

        # Log a message
        get_logger().info("This should only go to stdout")

        # Verify no unexpected log file was created in current directory
        # (This is a basic check; a more thorough test would check all possible locations)
        assert not os.path.exists("pr-agent.log"), "Unexpected log file created"
