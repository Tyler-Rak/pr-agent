import asyncio
import ast
import copy
import json
import os
import re
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

import uvicorn
from fastapi import APIRouter, FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.responses import RedirectResponse
from starlette import status
from starlette.background import BackgroundTasks
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette_context.middleware import RawContextMiddleware

from pr_agent.agent.pr_agent import PRAgent
from pr_agent.algo.utils import update_settings_from_args
from pr_agent.config_loader import get_settings
from pr_agent.git_providers.utils import apply_repo_settings
from pr_agent.log import LoggingFormat, get_logger, setup_logger
from pr_agent.servers.utils import verify_signature

setup_logger(fmt=LoggingFormat.JSON, level=get_settings().get("CONFIG.LOG_LEVEL", "DEBUG"))
router = APIRouter()

# Global semaphore for limiting concurrent reviews
review_semaphore = None

# Task tracking for queue monitoring
active_review_tasks = {}
active_review_tasks_lock = asyncio.Lock()


def get_hostname_from_webhook(data: dict) -> Optional[str]:
    """
    Extract the Bitbucket Server hostname from webhook payload.
    Supports both PR events and repository events.

    Args:
        data: Webhook payload

    Returns:
        Hostname (e.g., "git.rakuten-it.com") or None if not found
    """
    try:
        # PR events: pr:opened, pr:comment:added, etc.
        if "pullRequest" in data:
            repo_link = data["pullRequest"]["toRef"]["repository"]["links"]["self"][0]["href"]
        # Repository events: repo:refs_changed (when not PR-related)
        elif "repository" in data:
            repo_link = data["repository"]["links"]["self"][0]["href"]
        else:
            return None

        # Example: "https://git.rakuten-it.com/projects/TRV/repos/repo"
        hostname = urlparse(repo_link).netloc
        return hostname
    except (KeyError, IndexError, TypeError) as e:
        get_logger().warning(f"Failed to extract hostname from webhook payload: {e}")
        return None


def get_server_config(hostname: Optional[str] = None) -> Dict[str, any]:
    """
    Get server configuration (URL, credentials) for a specific Bitbucket Server instance.

    Supports two modes:
    1. Multi-server: [BITBUCKET_SERVER_INSTANCES] with per-server config
       - URL is constructed from hostname: https://{hostname}
    2. Single-server (legacy): [BITBUCKET_SERVER] with global config
       - URL must be explicitly provided

    Args:
        hostname: Server hostname (e.g., "git.rakuten-it.com")

    Returns:
        Dict with 'url', 'bearer_token', 'username', 'password', 'webhook_secret'
    """
    # Try multi-server configuration
    if hostname:
        instances_config = get_settings().get("BITBUCKET_SERVER_INSTANCES", {})
        if hostname in instances_config:
            server_config = instances_config[hostname]
            get_logger().debug(f"Using multi-server config for {hostname}")

            # URL is always constructed from hostname in multi-server mode
            url = f"https://{hostname}"

            return {
                "url": url,
                "bearer_token": server_config.get("bearer_token"),
                "username": server_config.get("username"),
                "password": server_config.get("password"),
                "webhook_secret": server_config.get("webhook_secret")
            }

    # Fallback to legacy single-server configuration
    get_logger().debug("Using legacy single-server configuration")
    return {
        "url": get_settings().get("BITBUCKET_SERVER.URL"),
        "bearer_token": get_settings().get("BITBUCKET_SERVER.BEARER_TOKEN"),
        "username": get_settings().get("BITBUCKET_SERVER.USERNAME"),
        "password": get_settings().get("BITBUCKET_SERVER.PASSWORD"),
        "webhook_secret": get_settings().get("BITBUCKET_SERVER.WEBHOOK_SECRET")
    }


def handle_request(
    background_tasks: BackgroundTasks, url: str, body: str, log_context: dict
):
    log_context["action"] = body
    log_context["api_url"] = url

    async def inner():
        try:
            with get_logger().contextualize(**log_context):
                await PRAgent().handle_request(url, body)
        except Exception as e:
            get_logger().error(f"Failed to handle webhook: {e}")

    background_tasks.add_task(inner)

def should_process_pr_logic(data) -> bool:
    try:
        pr_data = data.get("pullRequest", {})
        title = pr_data.get("title", "")
        
        from_ref = pr_data.get("fromRef", {})
        source_branch = from_ref.get("displayId", "") if from_ref else ""
        
        to_ref = pr_data.get("toRef", {})
        target_branch = to_ref.get("displayId", "") if to_ref else ""
        
        author = pr_data.get("author", {})
        user = author.get("user", {}) if author else {}
        sender = user.get("name", "") if user else ""
        
        repository = to_ref.get("repository", {}) if to_ref else {}
        project = repository.get("project", {}) if repository else {}
        project_key = project.get("key", "") if project else ""
        repo_slug = repository.get("slug", "") if repository else ""
        
        repo_full_name = f"{project_key}/{repo_slug}" if project_key and repo_slug else ""
        pr_id = pr_data.get("id", None)

        # To ignore PRs from specific repositories
        ignore_repos = get_settings().get("CONFIG.IGNORE_REPOSITORIES", [])
        if repo_full_name and ignore_repos:
            if any(re.search(regex, repo_full_name) for regex in ignore_repos):
                get_logger().info(f"Ignoring PR from repository '{repo_full_name}' due to 'config.ignore_repositories' setting")
                return False

        # To ignore PRs from specific users
        ignore_pr_users = get_settings().get("CONFIG.IGNORE_PR_AUTHORS", [])
        if ignore_pr_users and sender:
            if any(re.search(regex, sender) for regex in ignore_pr_users):
                get_logger().info(f"Ignoring PR from user '{sender}' due to 'config.ignore_pr_authors' setting")
                return False

        # To ignore PRs with specific titles
        if title:
            ignore_pr_title_re = get_settings().get("CONFIG.IGNORE_PR_TITLE", [])
            if not isinstance(ignore_pr_title_re, list):
                ignore_pr_title_re = [ignore_pr_title_re]
            if ignore_pr_title_re and any(re.search(regex, title) for regex in ignore_pr_title_re):
                get_logger().info(f"Ignoring PR with title '{title}' due to config.ignore_pr_title setting")
                return False

        ignore_pr_source_branches = get_settings().get("CONFIG.IGNORE_PR_SOURCE_BRANCHES", [])
        ignore_pr_target_branches = get_settings().get("CONFIG.IGNORE_PR_TARGET_BRANCHES", [])
        if (ignore_pr_source_branches or ignore_pr_target_branches):
            if any(re.search(regex, source_branch) for regex in ignore_pr_source_branches):
                get_logger().info(
                    f"Ignoring PR with source branch '{source_branch}' due to config.ignore_pr_source_branches settings")
                return False
            if any(re.search(regex, target_branch) for regex in ignore_pr_target_branches):
                get_logger().info(
                    f"Ignoring PR with target branch '{target_branch}' due to config.ignore_pr_target_branches settings")
                return False

        # Allow_only_specific_folders
        allowed_folders = get_settings().config.get("allow_only_specific_folders", [])
        if allowed_folders and pr_id and project_key and repo_slug:
            from pr_agent.git_providers.bitbucket_server_provider import BitbucketServerProvider
            # Extract hostname and get server URL
            hostname = get_hostname_from_webhook(data)
            server_config = get_server_config(hostname)
            bitbucket_server_url = server_config.get("url", "")
            pr_url = f"{bitbucket_server_url}/projects/{project_key}/repos/{repo_slug}/pull-requests/{pr_id}"
            provider = BitbucketServerProvider(pr_url=pr_url)
            changed_files = provider.get_files()
            if changed_files:
                # Check if ALL files are outside allowed folders
                all_files_outside = True
                for file_path in changed_files:
                    if any(file_path.startswith(folder) for folder in allowed_folders):
                        all_files_outside = False
                        break
                
                if all_files_outside:
                    get_logger().info(f"Ignoring PR because all files {changed_files} are outside allowed folders {allowed_folders}")
                    return False
    except Exception as e:
        get_logger().error(f"Failed 'should_process_pr_logic': {e}")
        return True # On exception - we continue. Otherwise, we could just end up with filtering all PRs
    return True

@router.post("/")
async def redirect_to_webhook():
    return RedirectResponse(url="/webhook")

@router.post("/webhook")
async def handle_webhook(background_tasks: BackgroundTasks, request: Request):
    log_context = {"server_type": "bitbucket_server"}
    data = await request.json()
    get_logger().info(json.dumps(data))

    # Extract hostname and get server-specific configuration
    hostname = get_hostname_from_webhook(data)
    server_config = get_server_config(hostname)

    webhook_secret = server_config.get("webhook_secret")
    if webhook_secret:
        body_bytes = await request.body()
        if body_bytes.decode('utf-8') == '{"test": true}':
            return JSONResponse(
                status_code=status.HTTP_200_OK, content=jsonable_encoder({"message": "connection test successful"})
            )
        signature_header = request.headers.get("x-hub-signature", None)
        verify_signature(body_bytes, webhook_secret, signature_header)

    pr_id = data["pullRequest"]["id"]
    repository_name = data["pullRequest"]["toRef"]["repository"]["slug"]
    project_name = data["pullRequest"]["toRef"]["repository"]["project"]["key"]
    bitbucket_server = server_config.get("url")

    if not bitbucket_server:
        get_logger().error(f"Could not determine Bitbucket Server URL for hostname: {hostname}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=jsonable_encoder({"error": "Server URL not configured"})
        )

    pr_url = f"{bitbucket_server}/projects/{project_name}/repos/{repository_name}/pull-requests/{pr_id}"

    log_context["api_url"] = pr_url
    log_context["webhook_event_type"] = "pull_request"

    commands_to_run = []

    if (data["eventKey"] == "pr:opened"
            or (data["eventKey"] == "repo:refs_changed" and data.get("pullRequest", {}).get("id", -1) != -1)):  # push event; -1 for push unassigned to a PR: #Check auto commands for creation/updating
        apply_repo_settings(pr_url)
        if not should_process_pr_logic(data):
            get_logger().info(f"PR ignored due to config settings", **log_context)
            return JSONResponse(
                status_code=status.HTTP_200_OK, content=jsonable_encoder({"message": "PR ignored by config"})
            )
        if get_settings().config.disable_auto_feedback:  # auto commands for PR, and auto feedback is disabled
            get_logger().info(f"Auto feedback is disabled, skipping auto commands for PR {pr_url}", **log_context)
            return JSONResponse(
                status_code=status.HTTP_200_OK, content=jsonable_encoder({"message": "PR ignored due to auto feedback not enabled"})
            )
        get_settings().set("config.is_auto_command", True)
        if data["eventKey"] == "pr:opened":
            commands_to_run.extend(_get_commands_list_from_settings('BITBUCKET_SERVER.PR_COMMANDS'))
        else: #Has to be: data["eventKey"] == "pr:from_ref_updated"
            if not get_settings().get("BITBUCKET_SERVER.HANDLE_PUSH_TRIGGER"):
                get_logger().info(f"Push trigger is disabled, skipping push commands for PR {pr_url}", **log_context)
                return JSONResponse(
                    status_code=status.HTTP_200_OK, content=jsonable_encoder({"message": "PR ignored due to push trigger not enabled"})
                )

            get_settings().set("config.is_new_pr", False)
            commands_to_run.extend(_get_commands_list_from_settings('BITBUCKET_SERVER.PUSH_COMMANDS'))
    elif data["eventKey"] == "pr:comment:added":
        commands_to_run.append(data["comment"]["text"])
    else:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=json.dumps({"message": "Unsupported event"}),
        )

    async def inner():
        try:
            await _run_commands_sequentially(commands_to_run, pr_url, log_context)
        except Exception as e:
            get_logger().error(f"Failed to handle webhook: {e}")

    background_tasks.add_task(inner)

    return JSONResponse(
        status_code=status.HTTP_200_OK, content=jsonable_encoder({"message": "success"})
    )


@router.post("/webhook-parallel")
async def handle_webhook_parallel(request: Request):
    """
    Parallel webhook handler for concurrent PR review processing.
    Uses asyncio.create_task() for true parallel execution with semaphore-based concurrency limiting.
    """
    global review_semaphore

    log_context = {"server_type": "bitbucket_server", "parallel": True}
    data = await request.json()
    get_logger().info(json.dumps(data))

    # Check if parallel reviews are enabled
    enable_parallel = get_settings().get("BITBUCKET_APP.ENABLE_PARALLEL_REVIEWS", False)
    if not enable_parallel:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=jsonable_encoder({"error": "Parallel reviews not enabled. Set bitbucket_app.enable_parallel_reviews=true in configuration."})
        )

    # Initialize semaphore if not already done
    if review_semaphore is None:
        max_concurrent = get_settings().get("BITBUCKET_APP.MAX_CONCURRENT_REVIEWS", 5)
        if max_concurrent > 0:
            review_semaphore = asyncio.Semaphore(max_concurrent)
            get_logger().info(f"Initialized review semaphore with max_concurrent_reviews={max_concurrent}")

    # Extract hostname and get server-specific configuration
    hostname = get_hostname_from_webhook(data)
    server_config = get_server_config(hostname)

    # Webhook signature verification
    webhook_secret = server_config.get("webhook_secret")
    if webhook_secret:
        body_bytes = await request.body()
        if body_bytes.decode('utf-8') == '{"test": true}':
            return JSONResponse(
                status_code=status.HTTP_200_OK, content=jsonable_encoder({"message": "connection test successful"})
            )
        signature_header = request.headers.get("x-hub-signature", None)
        verify_signature(body_bytes, webhook_secret, signature_header)

    pr_id = data["pullRequest"]["id"]
    repository_name = data["pullRequest"]["toRef"]["repository"]["slug"]
    project_name = data["pullRequest"]["toRef"]["repository"]["project"]["key"]
    bitbucket_server = server_config.get("url")

    if not bitbucket_server:
        get_logger().error(f"Could not determine Bitbucket Server URL for hostname: {hostname}")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content=jsonable_encoder({"error": "Server URL not configured"})
        )

    pr_url = f"{bitbucket_server}/projects/{project_name}/repos/{repository_name}/pull-requests/{pr_id}"

    log_context["api_url"] = pr_url
    log_context["webhook_event_type"] = "pull_request"

    commands_to_run = []

    if (data["eventKey"] == "pr:opened"
            or (data["eventKey"] == "repo:refs_changed" and data.get("pullRequest", {}).get("id", -1) != -1)):
        apply_repo_settings(pr_url)
        if not should_process_pr_logic(data):
            get_logger().info(f"PR ignored due to config settings", **log_context)
            return JSONResponse(
                status_code=status.HTTP_200_OK, content=jsonable_encoder({"message": "PR ignored by config"})
            )
        if get_settings().config.disable_auto_feedback:
            get_logger().info(f"Auto feedback is disabled, skipping auto commands for PR {pr_url}", **log_context)
            return JSONResponse(
                status_code=status.HTTP_200_OK, content=jsonable_encoder({"message": "PR ignored due to auto feedback not enabled"})
            )
        get_settings().set("config.is_auto_command", True)
        if data["eventKey"] == "pr:opened":
            commands_to_run.extend(_get_commands_list_from_settings('BITBUCKET_SERVER.PR_COMMANDS'))
        else:
            if not get_settings().get("BITBUCKET_SERVER.HANDLE_PUSH_TRIGGER"):
                get_logger().info(f"Push trigger is disabled, skipping push commands for PR {pr_url}", **log_context)
                return JSONResponse(
                    status_code=status.HTTP_200_OK, content=jsonable_encoder({"message": "PR ignored due to push trigger not enabled"})
                )
            get_settings().set("config.is_new_pr", False)
            commands_to_run.extend(_get_commands_list_from_settings('BITBUCKET_SERVER.PUSH_COMMANDS'))
    elif data["eventKey"] == "pr:comment:added":
        comment_text = data["comment"]["text"].strip()
        # Check if comment is /inspect (trigger all 3 commands)
        if comment_text == "/inspect":
            get_logger().info("/inspect command detected, expanding to all commands")
            commands_to_run.extend(_get_commands_list_from_settings('BITBUCKET_SERVER.PR_COMMANDS'))
        elif comment_text.startswith("/"):
            commands_to_run.append(comment_text)
        else:
            # Not a command, ignore
            get_logger().info(f"Ignoring non-command comment: {comment_text[:50]}...", **log_context)
            return JSONResponse(
                status_code=status.HTTP_200_OK,
                content=jsonable_encoder({"message": "Comment ignored (not a command)"})
            )
    else:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content=json.dumps({"message": "Unsupported event"}),
        )

    # Create isolated copies for this task
    commands_copy = copy.deepcopy(commands_to_run)
    log_context_copy = copy.deepcopy(log_context)

    async def inner_parallel():
        """Inner function that runs the commands with semaphore control"""
        task_id = id(asyncio.current_task())
        start_time = time.time()

        try:
            # Register task
            async with active_review_tasks_lock:
                active_review_tasks[task_id] = {
                    "pr_url": pr_url,
                    "created_at": start_time,
                    "status": "waiting",
                    "wait_start": start_time,
                }
                queue_depth = len(active_review_tasks)

            get_logger().info(
                f"Review task created: event=review_task_created task_id={task_id} queue_depth={queue_depth} pr_url={pr_url}",
                **log_context_copy
            )

            if review_semaphore:
                wait_start = time.time()

                async with review_semaphore:
                    wait_time = time.time() - wait_start

                    # Update status and get counts
                    async with active_review_tasks_lock:
                        active_review_tasks[task_id]["status"] = "processing"
                        active_review_tasks[task_id]["processing_start"] = time.time()
                        active_tasks = len([t for t in active_review_tasks.values() if t["status"] == "processing"])
                        waiting_tasks = len([t for t in active_review_tasks.values() if t["status"] == "waiting"])

                    get_logger().info(
                        f"Review task processing: event=review_task_processing task_id={task_id} wait_time_seconds={round(wait_time, 2)} active_reviews={active_tasks} waiting_reviews={waiting_tasks} pr_url={pr_url}",
                        **log_context_copy
                    )

                    await _run_commands_sequentially(commands_copy, pr_url, log_context_copy)
            else:
                get_logger().info(f"Starting parallel review (no semaphore) for {pr_url}", **log_context_copy)
                await _run_commands_sequentially(commands_copy, pr_url, log_context_copy)

        except Exception as e:
            async with active_review_tasks_lock:
                if task_id in active_review_tasks:
                    active_review_tasks[task_id]["status"] = "failed"
            get_logger().error(
                f"Review task failed: event=review_task_failed task_id={task_id} error={str(e)} pr_url={pr_url}",
                **log_context_copy
            )
        finally:
            # Cleanup and log completion
            duration = time.time() - start_time
            async with active_review_tasks_lock:
                status = active_review_tasks.get(task_id, {}).get("status", "unknown")
                active_review_tasks.pop(task_id, None)
                remaining_tasks = len(active_review_tasks)

            get_logger().info(
                f"Review task completed: event=review_task_completed task_id={task_id} duration_seconds={round(duration, 2)} status={status} remaining_tasks={remaining_tasks} pr_url={pr_url}",
                **log_context_copy
            )

    # Use asyncio.create_task for true concurrent execution
    asyncio.create_task(inner_parallel())

    return JSONResponse(
        status_code=status.HTTP_200_OK, content=jsonable_encoder({"message": "success"})
    )


async def _run_commands_sequentially(commands: List[str], url: str, log_context: dict):
    get_logger().info(f"Running commands sequentially: {commands}")
    if commands is None:
        return

    for command in commands:
        try:
            body = _process_command(command, url)

            log_context["action"] = body
            log_context["api_url"] = url

            with get_logger().contextualize(**log_context):
                await PRAgent().handle_request(url, body)
        except Exception as e:
            get_logger().error(f"Failed to handle command: {command} , error: {e}")

def _process_command(command: str, url) -> str:
    # don't think we need this
    apply_repo_settings(url)
    # Process the command string
    split_command = command.split(" ")
    command = split_command[0]
    args = split_command[1:]
    # do I need this? if yes, shouldn't this be done in PRAgent?
    other_args = update_settings_from_args(args)
    new_command = ' '.join([command] + other_args)
    return new_command


def _to_list(command_string: str) -> list:
    try:
        # Use ast.literal_eval to safely parse the string into a list
        commands = ast.literal_eval(command_string)
        # Check if the parsed object is a list of strings
        if isinstance(commands, list) and all(isinstance(cmd, str) for cmd in commands):
            return commands
        else:
            raise ValueError("Parsed data is not a list of strings.")
    except (SyntaxError, ValueError, TypeError) as e:
        raise ValueError(f"Invalid command string: {e}")


def _get_commands_list_from_settings(setting_key:str ) -> list:
    try:
        return get_settings().get(setting_key, [])
    except ValueError as e:
        get_logger().error(f"Failed to get commands list from settings {setting_key}: {e}")


@router.get("/")
async def root():
    return {"status": "ok"}


def start():
    app = FastAPI(middleware=[Middleware(RawContextMiddleware)])
    app.include_router(router)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "3000")))


if __name__ == "__main__":
    start()
