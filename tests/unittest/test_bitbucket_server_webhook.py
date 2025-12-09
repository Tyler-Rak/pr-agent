import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pr_agent.servers.bitbucket_server_webhook import (
    active_review_tasks,
    active_review_tasks_lock,
)


class TestBitbucketServerWebhook:
    """Tests for Bitbucket Server webhook parallel endpoint"""

    @pytest.mark.asyncio
    async def test_task_tracking_lifecycle(self):
        """Test that tasks are properly tracked through their lifecycle"""
        # Clear any existing tasks
        async with active_review_tasks_lock:
            active_review_tasks.clear()

        # Simulate task lifecycle
        task_id = 12345
        pr_url = "https://git.rakuten-it.com/projects/TEST/repos/test-repo/pull-requests/1"

        # Task created
        async with active_review_tasks_lock:
            active_review_tasks[task_id] = {
                "pr_url": pr_url,
                "created_at": time.time(),
                "status": "waiting",
                "wait_start": time.time(),
            }
            queue_depth = len(active_review_tasks)

        assert queue_depth == 1
        assert active_review_tasks[task_id]["status"] == "waiting"
        assert active_review_tasks[task_id]["pr_url"] == pr_url

        # Task processing
        async with active_review_tasks_lock:
            active_review_tasks[task_id]["status"] = "processing"
            active_review_tasks[task_id]["processing_start"] = time.time()

        assert active_review_tasks[task_id]["status"] == "processing"
        assert "processing_start" in active_review_tasks[task_id]

        # Task completed
        async with active_review_tasks_lock:
            active_review_tasks.pop(task_id, None)
            remaining = len(active_review_tasks)

        assert remaining == 0
        assert task_id not in active_review_tasks

    @pytest.mark.asyncio
    async def test_multiple_concurrent_tasks(self):
        """Test tracking multiple concurrent tasks"""
        # Clear any existing tasks
        async with active_review_tasks_lock:
            active_review_tasks.clear()

        # Simulate 3 concurrent tasks
        task_ids = [1001, 1002, 1003]
        pr_urls = [
            "https://git.rakuten-it.com/projects/TEST/repos/test-repo/pull-requests/1",
            "https://git.rakuten-it.com/projects/TEST/repos/test-repo/pull-requests/2",
            "https://git.rakuten-it.com/projects/TEST/repos/test-repo/pull-requests/3",
        ]

        # Create tasks
        for task_id, pr_url in zip(task_ids, pr_urls):
            async with active_review_tasks_lock:
                active_review_tasks[task_id] = {
                    "pr_url": pr_url,
                    "created_at": time.time(),
                    "status": "waiting",
                    "wait_start": time.time(),
                }

        # Verify all tasks are tracked
        async with active_review_tasks_lock:
            assert len(active_review_tasks) == 3
            waiting_count = len([t for t in active_review_tasks.values() if t["status"] == "waiting"])
            assert waiting_count == 3

        # Move first task to processing
        async with active_review_tasks_lock:
            active_review_tasks[task_ids[0]]["status"] = "processing"
            active_review_tasks[task_ids[0]]["processing_start"] = time.time()
            active_count = len([t for t in active_review_tasks.values() if t["status"] == "processing"])
            waiting_count = len([t for t in active_review_tasks.values() if t["status"] == "waiting"])

        assert active_count == 1
        assert waiting_count == 2

        # Complete first task
        async with active_review_tasks_lock:
            active_review_tasks.pop(task_ids[0], None)
            remaining = len(active_review_tasks)

        assert remaining == 2

        # Cleanup
        async with active_review_tasks_lock:
            active_review_tasks.clear()

    @pytest.mark.asyncio
    async def test_task_status_counts(self):
        """Test accurate counting of active vs waiting tasks"""
        # Clear any existing tasks
        async with active_review_tasks_lock:
            active_review_tasks.clear()

        # Create 5 tasks: 2 processing, 3 waiting
        async with active_review_tasks_lock:
            for i in range(5):
                status = "processing" if i < 2 else "waiting"
                active_review_tasks[i] = {
                    "pr_url": f"https://git.rakuten-it.com/projects/TEST/repos/test-repo/pull-requests/{i}",
                    "created_at": time.time(),
                    "status": status,
                    "wait_start": time.time(),
                }

            active_count = len([t for t in active_review_tasks.values() if t["status"] == "processing"])
            waiting_count = len([t for t in active_review_tasks.values() if t["status"] == "waiting"])

        assert active_count == 2
        assert waiting_count == 3
        assert len(active_review_tasks) == 5

        # Cleanup
        async with active_review_tasks_lock:
            active_review_tasks.clear()

    @pytest.mark.asyncio
    async def test_task_failure_tracking(self):
        """Test that failed tasks are properly marked"""
        # Clear any existing tasks
        async with active_review_tasks_lock:
            active_review_tasks.clear()

        task_id = 9999
        pr_url = "https://git.rakuten-it.com/projects/TEST/repos/test-repo/pull-requests/999"

        # Create task
        async with active_review_tasks_lock:
            active_review_tasks[task_id] = {
                "pr_url": pr_url,
                "created_at": time.time(),
                "status": "waiting",
                "wait_start": time.time(),
            }

        # Mark as failed
        async with active_review_tasks_lock:
            if task_id in active_review_tasks:
                active_review_tasks[task_id]["status"] = "failed"

        assert active_review_tasks[task_id]["status"] == "failed"

        # Cleanup (simulating finally block)
        async with active_review_tasks_lock:
            status = active_review_tasks.get(task_id, {}).get("status", "unknown")
            active_review_tasks.pop(task_id, None)

        assert status == "failed"
        assert task_id not in active_review_tasks

        # Cleanup
        async with active_review_tasks_lock:
            active_review_tasks.clear()
