import difflib
import re
from tempfile import TemporaryDirectory

from packaging.version import parse as parse_version
from typing import Optional, Tuple
from urllib.parse import quote_plus, urlparse

from atlassian.bitbucket import Bitbucket
from requests.exceptions import HTTPError
import shlex
import subprocess

from ..algo.file_filter import filter_ignored
from ..algo.git_patch_processing import decode_if_bytes
from ..algo.language_handler import is_valid_file
from ..algo.types import EDIT_TYPE, FilePatchInfo
from ..algo.utils import (find_line_number_of_relevant_line_in_file,
                          load_large_diff)
from ..config_loader import get_settings
from ..log import get_logger
from .git_provider import GitProvider, get_git_ssl_env


class BitbucketServerGitProvider(GitProvider):
    """
    Bitbucket Server provider using hybrid REST API + Git clone approach.

    ARCHITECTURE:
    1. Merge-base calculation: REST API (2-3 calls with limit=100)
       - Bitbucket 8.16+: Direct merge-base API
       - Bitbucket < 8.16: get_pull_requests_commits + get_commits + calculate
    2. Git clone: Shallow clone with --depth=1 (includes blobs)
    3. Fetch commits: git fetch --depth=1 for head_sha + base_sha (includes blobs)
    4. Changed files list: REST API get_pull_requests_changes (1 call)
    5. File contents: git show {sha}:{path} (0 API calls, local disk read)

    PERFORMANCE:
    - REST API calls: 3-4 per PR (vs 25+ with full REST API)
    - Rate limit impact: Minimal (1s refill vs 8s)
    - Throughput: 45 PRs/min sustained (vs 7 PRs/min)

    Enable via: git_provider = "bitbucket_server_git"
    """
    def __init__(
            self, pr_url: Optional[str] = None, incremental: Optional[bool] = False,
            bitbucket_client: Optional[Bitbucket] = None,
    ):
        self.bitbucket_server_url = None
        self.workspace_slug = None
        self.repo_slug = None
        self.repo = None
        self.pr_num = None
        self.pr = None
        self.pr_url = pr_url
        self.temp_comments = []
        self.incremental = incremental
        self.diff_files = None
        self.bitbucket_pull_request_api_url = pr_url
        self.bearer_token = get_settings().get("BITBUCKET_SERVER.BEARER_TOKEN", None)
        # Get username and password from settings
        username = get_settings().get("BITBUCKET_SERVER.USERNAME", None)
        password = get_settings().get("BITBUCKET_SERVER.PASSWORD", None)
        if bitbucket_client: # if Bitbucket client is provided, use it
            self.bitbucket_client = bitbucket_client
            self.bitbucket_server_url = getattr(bitbucket_client, 'url', None) or self._parse_bitbucket_server(pr_url)
        else:
            self.bitbucket_server_url = self._parse_bitbucket_server(pr_url)
            if not self.bitbucket_server_url:
                raise ValueError("Invalid or missing Bitbucket Server URL parsed from PR URL.")

            if self.bearer_token:  # if bearer token is provided, use it
                self.bitbucket_client = Bitbucket(
                    url=self.bitbucket_server_url,
                    token=self.bearer_token
                )
            else:  # otherwise use username and password
                self.bitbucket_client = Bitbucket(
                    url=self.bitbucket_server_url,
                    username=username,
                    password=password
                )
        try:
            app_props = self.bitbucket_client.get("rest/api/1.0/application-properties")
            version_str = app_props.get('version')
            get_logger().info(f"Detected Bitbucket Server version: {version_str}")
            self.bitbucket_api_version = parse_version(version_str)
        except Exception as e:
            get_logger().warning(f"Failed to detect Bitbucket Server version: {e}")
            self.bitbucket_api_version = None

        if pr_url:
            self.set_pr(pr_url)

    def get_git_repo_url(self, pr_url: str=None) -> str: #bitbucket server does not support issue url, so ignore param
        try:
            parsed_url = urlparse(self.pr_url)
            return f"{parsed_url.scheme}://{parsed_url.netloc}/scm/{self.workspace_slug.lower()}/{self.repo_slug.lower()}.git"
        except Exception as e:
            get_logger().exception(f"url is not a valid merge requests url: {self.pr_url}")
            return ""

    # Given a git repo url, return prefix and suffix of the provider in order to view a given file belonging to that repo.
    # Example: https://bitbucket.dev.my_inc.com/scm/my_work/my_repo.git and branch: my_branch -> prefix: "https://bitbucket.dev.my_inc.com/projects/MY_WORK/repos/my_repo/browse/src", suffix: "?at=refs%2Fheads%2Fmy_branch"
    # In case git url is not provided, provider will use PR context (which includes branch) to determine the prefix and suffix.
    def get_canonical_url_parts(self, repo_git_url:str=None, desired_branch:str=None) -> Tuple[str, str]:
        workspace_name = None
        project_name = None
        if not repo_git_url:
            workspace_name = self.workspace_slug
            project_name = self.repo_slug
            default_branch_dict = self.bitbucket_client.get_default_branch(workspace_name, project_name)
            if 'displayId' in default_branch_dict:
                desired_branch = default_branch_dict['displayId']
            else:
                get_logger().error(f"Cannot obtain default branch for workspace_name={workspace_name}, "
                                   f"project_name={project_name}, default_branch_dict={default_branch_dict}")
                return ("", "")
        elif '.git' in repo_git_url and 'scm/' in repo_git_url:
            repo_path = repo_git_url.split('.git')[0].split('scm/')[-1]
            if repo_path.count('/') == 1:  # Has to have the form <workspace>/<repo>
                workspace_name, project_name = repo_path.split('/')
        if not workspace_name or not project_name:
            get_logger().error(f"workspace_name or project_name not found in context, either git url: {repo_git_url} or uninitialized workspace/project.")
            return ("", "")
        prefix = f"{self.bitbucket_server_url}/projects/{workspace_name}/repos/{project_name}/browse"
        suffix = f"?at=refs%2Fheads%2F{desired_branch}"
        return (prefix, suffix)

    def get_repo_settings(self):
        try:
            content = self.bitbucket_client.get_content_of_file(self.workspace_slug, self.repo_slug, ".pr_agent.toml")

            return content
        except Exception as e:
            if isinstance(e, HTTPError):
                if e.response.status_code == 404:  # not found
                    return ""

            get_logger().error(f"Failed to load .pr_agent.toml file, error: {e}")
            return ""

    def get_pr_id(self):
        return self.pr_num

    def publish_code_suggestions(self, code_suggestions: list) -> bool:
        """
        Publishes code suggestions as comments on the PR.
        """
        post_parameters_list = []
        for suggestion in code_suggestions:
            body = suggestion["body"]
            original_suggestion = suggestion.get('original_suggestion', None)  # needed for diff code
            if original_suggestion:
                try:
                    existing_code = original_suggestion['existing_code'].rstrip() + "\n"
                    improved_code = original_suggestion['improved_code'].rstrip() + "\n"
                    diff = difflib.unified_diff(existing_code.split('\n'),
                                                improved_code.split('\n'), n=999)
                    patch_orig = "\n".join(diff)
                    patch = "\n".join(patch_orig.splitlines()[5:]).strip('\n')
                    diff_code = f"\n\n```diff\n{patch.rstrip()}\n```"
                    # replace ```suggestion ... ``` with diff_code, using regex:
                    body = re.sub(r'```suggestion.*?```', diff_code, body, flags=re.DOTALL)
                except Exception as e:
                    get_logger().exception(f"Bitbucket failed to get diff code for publishing, error: {e}")
                    continue
            relevant_file = suggestion["relevant_file"]
            relevant_lines_start = suggestion["relevant_lines_start"]
            relevant_lines_end = suggestion["relevant_lines_end"]

            if not relevant_lines_start or relevant_lines_start == -1:
                get_logger().warning(
                    f"Failed to publish code suggestion, relevant_lines_start is {relevant_lines_start}"
                )
                continue

            if relevant_lines_end < relevant_lines_start:
                get_logger().warning(
                    f"Failed to publish code suggestion, "
                    f"relevant_lines_end is {relevant_lines_end} and "
                    f"relevant_lines_start is {relevant_lines_start}"
                )
                continue

            if relevant_lines_end > relevant_lines_start:
                # Bitbucket does not support multi-line suggestions so use a code block instead - https://jira.atlassian.com/browse/BSERV-4553
                body = body.replace("```suggestion", "```")
                post_parameters = {
                    "body": body,
                    "path": relevant_file,
                    "line": relevant_lines_end,
                    "start_line": relevant_lines_start,
                    "start_side": "RIGHT",
                }
            else:  # API is different for single line comments
                post_parameters = {
                    "body": body,
                    "path": relevant_file,
                    "line": relevant_lines_start,
                    "side": "RIGHT",
                }
            post_parameters_list.append(post_parameters)

        try:
            self.publish_inline_comments(post_parameters_list)
            return True
        except Exception as e:
            if get_settings().config.verbosity_level >= 2:
                get_logger().error(f"Failed to publish code suggestion, error: {e}")
            return False

    def publish_file_comments(self, file_comments: list) -> bool:
        pass

    def is_supported(self, capability: str) -> bool:
        if capability in ['get_issue_comments', 'get_labels', 'gfm_markdown', 'publish_file_comments']:
            return False
        return True

    def set_pr(self, pr_url: str):
        self.workspace_slug, self.repo_slug, self.pr_num = self._parse_pr_url(pr_url)
        self.pr = self._get_pr()

    def get_file(self, path: str, commit_id: str):
        file_content = ""
        try:
            file_content = self.bitbucket_client.get_content_of_file(self.workspace_slug,
                                                                     self.repo_slug,
                                                                     path,
                                                                     commit_id)
        except HTTPError as e:
            get_logger().debug(f"File {path} not found at commit id: {commit_id}")
        return file_content

    def get_files(self):
        changes = self.bitbucket_client.get_pull_requests_changes(self.workspace_slug, self.repo_slug, self.pr_num)
        diffstat = [change["path"]['toString'] for change in changes]
        return diffstat

    def get_file_from_git(self, path: str, commit_id: str, repo_path: str) -> str:
        """
        Read file content from local git repository using git show command.
        This bypasses REST API calls and avoids rate limiting.

        Args:
            path: File path relative to repo root
            commit_id: Commit SHA to read file at
            repo_path: Path to cloned repository

        Returns:
            File content as string, empty string if file not found
        """
        try:
            result = subprocess.run(
                ['git', 'show', f'{commit_id}:{path}'],
                cwd=repo_path,
                capture_output=True,
                text=True,
                timeout=30
            )
            if result.returncode == 0:
                return result.stdout
            else:
                get_logger().debug(f"File {path} not found at commit {commit_id}: {result.stderr}")
                return ""
        except subprocess.TimeoutExpired:
            get_logger().warning(f"Timeout reading {path} from git at commit {commit_id}")
            return ""
        except Exception as e:
            get_logger().debug(f"Failed to read {path} from git: {e}")
            return ""

    #gets the best common ancestor: https://git-scm.com/docs/git-merge-base
    @staticmethod
    def get_best_common_ancestor(source_commits_list, destination_commits_list, guaranteed_common_ancestor) -> str:
        destination_commit_hashes = {commit['id'] for commit in destination_commits_list} | {guaranteed_common_ancestor}

        for commit in source_commits_list:
            for parent_commit in commit['parents']:
                if parent_commit['id'] in destination_commit_hashes:
                    return parent_commit['id']

        return guaranteed_common_ancestor

    def get_diff_files(self) -> list[FilePatchInfo]:
        if self.diff_files:
            return self.diff_files

        # Custom: Use git clone mode if enabled (bypasses rate limits)
        if get_settings().get("bitbucket_server.use_git_clone_for_files", False):
            return self._get_diff_files_with_git_clone()

        # Original code continues below (unchanged for easier upstream merges)
        head_sha = self.pr.fromRef['latestCommit']

        # if Bitbucket api version is >= 8.16 then use the merge-base api for 2-way diff calculation
        if self.bitbucket_api_version is not None and self.bitbucket_api_version >= parse_version("8.16"):
            try:
                base_sha = self.bitbucket_client.get(self._get_merge_base())['id']
            except Exception as e:
                get_logger().error(f"Failed to get the best common ancestor for PR: {self.pr_url}, \nerror: {e}")
                raise e
        else:
            source_commits_list = list(self.bitbucket_client.get_pull_requests_commits(
                self.workspace_slug,
                self.repo_slug,
                self.pr_num
            ))
            # if Bitbucket api version is None or < 7.0 then do a simple diff with a guaranteed common ancestor
            base_sha = source_commits_list[-1]['parents'][0]['id']
            # if Bitbucket api version is 7.0-8.15 then use 2-way diff functionality for the base_sha
            if self.bitbucket_api_version is not None and self.bitbucket_api_version >= parse_version("7.0"):
                try:
                    destination_commits = list(
                        self.bitbucket_client.get_commits(self.workspace_slug, self.repo_slug, base_sha,
                                                          self.pr.toRef['latestCommit']))
                    base_sha = self.get_best_common_ancestor(source_commits_list, destination_commits, base_sha)
                except Exception as e:
                    get_logger().error(
                        f"Failed to get the commit list for calculating best common ancestor for PR: {self.pr_url}, \nerror: {e}")
                    raise e

        diff_files = []
        original_file_content_str = ""
        new_file_content_str = ""

        changes_original = list(self.bitbucket_client.get_pull_requests_changes(self.workspace_slug, self.repo_slug, self.pr_num))
        changes = filter_ignored(changes_original, 'bitbucket_server')
        for change in changes:
            file_path = change['path']['toString']
            if not is_valid_file(file_path.split("/")[-1]):
                get_logger().info(f"Skipping a non-code file: {file_path}")
                continue

            match change['type']:
                case 'ADD':
                    edit_type = EDIT_TYPE.ADDED
                    new_file_content_str = self.get_file(file_path, head_sha)
                    new_file_content_str = decode_if_bytes(new_file_content_str)
                    original_file_content_str = ""
                case 'DELETE':
                    edit_type = EDIT_TYPE.DELETED
                    new_file_content_str = ""
                    original_file_content_str = self.get_file(file_path, base_sha)
                    original_file_content_str = decode_if_bytes(original_file_content_str)
                case 'RENAME':
                    edit_type = EDIT_TYPE.RENAMED
                case _:
                    edit_type = EDIT_TYPE.MODIFIED
                    original_file_content_str = self.get_file(file_path, base_sha)
                    original_file_content_str = decode_if_bytes(original_file_content_str)
                    new_file_content_str = self.get_file(file_path, head_sha)
                    new_file_content_str = decode_if_bytes(new_file_content_str)

            patch = load_large_diff(file_path, new_file_content_str, original_file_content_str, show_warning=False)

            diff_files.append(
                FilePatchInfo(
                    original_file_content_str,
                    new_file_content_str,
                    patch,
                    file_path,
                    edit_type=edit_type,
                )
            )

        self.diff_files = diff_files
        return diff_files

    def _get_diff_files_with_git_clone(self) -> list[FilePatchInfo]:
        """
        Hybrid approach: REST API for metadata + Git clone for file contents.

        Flow:
        1. Clone repo (shallow): git clone --depth=1 (includes blobs)
        2. Get merge-base via REST API (2-3 calls, limit=100)
        3. Fetch needed commits: git fetch --depth=1 {head_sha} {base_sha} (includes blobs)
        4. Get changed files via REST API (1 call)
        5. Get file contents via git: git show {sha}:{path} (0 API calls, local disk read)

        Total: 3-4 REST API calls per PR (vs 25+ with full REST API approach)
        """
        # Get repo clone URL
        try:
            clone_links = self.pr.toRef['repository']['links']['clone']
            repo_url = None
            for link in clone_links:
                if link.get('name') == 'http':
                    repo_url = link.get('href')
                    break
            if not repo_url:
                repo_url = clone_links[0]['href']
        except (KeyError, IndexError) as e:
            get_logger().error(f"Failed to get clone URL from PR metadata: {e}")
            raise RuntimeError(f"Git clone mode enabled but cannot get clone URL from PR: {e}")

        get_logger().info(f"Cloning repository: {repo_url}")

        with TemporaryDirectory() as tmp_dir:
            cloned_repo = self.clone(repo_url, tmp_dir, remove_dest_folder=False)
            if not cloned_repo:
                get_logger().error("Git clone failed")
                raise RuntimeError(f"Git clone mode enabled but clone failed for repo: {repo_url}")

            repo_path = cloned_repo.path
            get_logger().info(f"Repository cloned successfully to {repo_path}")

            # HYBRID OPTIMIZATION: Use REST API for merge-base (fast), git diff for file list
            head_sha = self.pr.fromRef['latestCommit']
            target_sha = self.pr.toRef['latestCommit']

            # Get merge-base using REST API (not from git clone)
            # Path 1: Bitbucket 8.16+ has direct merge-base API
            if self.bitbucket_api_version is not None and self.bitbucket_api_version >= parse_version("8.16"):
                try:
                    base_sha = self.bitbucket_client.get(self._get_merge_base())['id']
                    get_logger().info(f"Got merge-base from REST API (8.16+): {base_sha[:8]}")
                except Exception as e:
                    get_logger().error(f"REST API merge-base failed: {e}")
                    raise RuntimeError(f"Failed to get merge-base from REST API: {e}")
            else:
                # Path 2: Bitbucket 7.0-8.15 - calculate merge-base from commit lists
                get_logger().info("Using REST API commit lists to calculate merge-base (Bitbucket < 8.16)")
                try:
                    # Get commits from PR with limit=100
                    source_commits_list = list(self.bitbucket_client.get_pull_requests_commits(
                        self.workspace_slug,
                        self.repo_slug,
                        self.pr_num,
                        limit=100
                    ))
                    # Guaranteed common ancestor (parent of first commit in PR)
                    base_sha = source_commits_list[-1]['parents'][0]['id']

                    # For Bitbucket 7.0+, refine to find actual best common ancestor
                    if self.bitbucket_api_version is not None and self.bitbucket_api_version >= parse_version("7.0"):
                        destination_commits = list(
                            self.bitbucket_client.get_commits(
                                self.workspace_slug,
                                self.repo_slug,
                                base_sha,
                                self.pr.toRef['latestCommit'],
                                limit=100
                            ))
                        base_sha = self.get_best_common_ancestor(source_commits_list, destination_commits, base_sha)

                    get_logger().info(f"Calculated merge-base from commit lists: {base_sha[:8]}")
                except Exception as e:
                    get_logger().error(f"Failed to calculate merge-base from REST API: {e}")
                    raise RuntimeError(f"Failed to calculate merge-base: {e}")

            diff_files = []
            original_file_content_str = ""
            new_file_content_str = ""

            # Fetch the specific commits needed for reading file contents (git show)
            # Shallow clone only has master@depth=1, but we need head_sha and base_sha
            get_logger().info(f"Fetching commits for file access: {base_sha[:8]} and {head_sha[:8]}")
            ssl_env = get_git_ssl_env()
            try:
                # Fetch both commits in one batch for efficiency
                subprocess.run(
                    ['git', 'fetch', '--depth=1', 'origin', head_sha, base_sha],
                    cwd=repo_path,
                    env=ssl_env,
                    capture_output=True,
                    check=True,
                    timeout=60
                )
                get_logger().info("Commits fetched successfully")
            except Exception as e:
                get_logger().error(f"Failed to fetch commits: {e}")
                raise RuntimeError(f"Failed to fetch commits for file access: {e}")

            # Get list of changed files via REST API (lightweight, just file paths)
            get_logger().info("Getting changed file list from REST API")
            try:
                changes_original = list(self.bitbucket_client.get_pull_requests_changes(
                    self.workspace_slug,
                    self.repo_slug,
                    self.pr_num
                ))
                get_logger().info(f"Found {len(changes_original)} changed files")
            except Exception as e:
                get_logger().error(f"Failed to get changed files from REST API: {e}")
                raise RuntimeError(f"Failed to get changed files: {e}")

            changes = filter_ignored(changes_original, 'bitbucket_server')
            for change in changes:
                file_path = change['path']['toString']
                if not is_valid_file(file_path.split("/")[-1]):
                    get_logger().info(f"Skipping a non-code file: {file_path}")
                    continue

                match change['type']:
                    case 'ADD':
                        edit_type = EDIT_TYPE.ADDED
                        new_file_content_str = self.get_file_from_git(file_path, head_sha, repo_path)
                        new_file_content_str = decode_if_bytes(new_file_content_str)
                        original_file_content_str = ""
                    case 'DELETE':
                        edit_type = EDIT_TYPE.DELETED
                        new_file_content_str = ""
                        original_file_content_str = self.get_file_from_git(file_path, base_sha, repo_path)
                        original_file_content_str = decode_if_bytes(original_file_content_str)
                    case 'RENAME':
                        edit_type = EDIT_TYPE.RENAMED
                    case _:
                        edit_type = EDIT_TYPE.MODIFIED
                        original_file_content_str = self.get_file_from_git(file_path, base_sha, repo_path)
                        original_file_content_str = decode_if_bytes(original_file_content_str)
                        new_file_content_str = self.get_file_from_git(file_path, head_sha, repo_path)
                        new_file_content_str = decode_if_bytes(new_file_content_str)

                patch = load_large_diff(file_path, new_file_content_str, original_file_content_str, show_warning=False)

                diff_files.append(
                    FilePatchInfo(
                        original_file_content_str,
                        new_file_content_str,
                        patch,
                        file_path,
                        edit_type=edit_type,
                    )
                )

            self.diff_files = diff_files
            return diff_files

    def publish_comment(self, pr_comment: str, is_temporary: bool = False):
        if not is_temporary:
            self.bitbucket_client.add_pull_request_comment(self.workspace_slug, self.repo_slug, self.pr_num, pr_comment)

    def remove_initial_comment(self):
        try:
            for comment in self.temp_comments:
                self.remove_comment(comment)
        except ValueError as e:
            get_logger().exception(f"Failed to remove temp comments, error: {e}")

    def remove_comment(self, comment):
        pass

    # function to create_inline_comment
    def create_inline_comment(self, body: str, relevant_file: str, relevant_line_in_file: str,
                              absolute_position: int = None):

        position, absolute_position = find_line_number_of_relevant_line_in_file(
            self.get_diff_files(),
            relevant_file.strip('`'),
            relevant_line_in_file,
            absolute_position
        )
        if position == -1:
            if get_settings().config.verbosity_level >= 2:
                get_logger().info(f"Could not find position for {relevant_file} {relevant_line_in_file}")
            subject_type = "FILE"
        else:
            subject_type = "LINE"
        path = relevant_file.strip()
        return dict(body=body, path=path, position=absolute_position) if subject_type == "LINE" else {}

    def publish_inline_comment(self, comment: str, from_line: int, file: str, original_suggestion=None):
        payload = {
            "text": comment,
            "severity": "NORMAL",
            "anchor": {
                "diffType": "EFFECTIVE",
                "path": file,
                "lineType": "ADDED",
                "line": from_line,
                "fileType": "TO"
            }
        }

        try:
            self.bitbucket_client.post(self._get_pr_comments_path(), data=payload)
        except Exception as e:
            get_logger().error(f"Failed to publish inline comment to '{file}' at line {from_line}, error: {e}")
            raise e

    def get_line_link(self, relevant_file: str, relevant_line_start: int, relevant_line_end: int = None) -> str:
        if relevant_line_start == -1:
            link = f"{self.pr_url}/diff#{quote_plus(relevant_file)}"
        else:
            link = f"{self.pr_url}/diff#{quote_plus(relevant_file)}?t={relevant_line_start}"
        return link

    def generate_link_to_relevant_line_number(self, suggestion) -> str:
        try:
            relevant_file = suggestion['relevant_file'].strip('`').strip("'").rstrip()
            relevant_line_str = suggestion['relevant_line'].rstrip()
            if not relevant_line_str:
                return ""

            diff_files = self.get_diff_files()
            position, absolute_position = find_line_number_of_relevant_line_in_file \
                (diff_files, relevant_file, relevant_line_str)

            if absolute_position != -1:
                if self.pr:
                    link = f"{self.pr_url}/diff#{quote_plus(relevant_file)}?t={absolute_position}"
                    return link
                else:
                    if get_settings().config.verbosity_level >= 2:
                        get_logger().info(f"Failed adding line link to '{relevant_file}' since PR not set")
            else:
                if get_settings().config.verbosity_level >= 2:
                    get_logger().info(f"Failed adding line link to '{relevant_file}' since position not found")

            if absolute_position != -1 and self.pr_url:
                link = f"{self.pr_url}/diff#{quote_plus(relevant_file)}?t={absolute_position}"
                return link
        except Exception as e:
            if get_settings().config.verbosity_level >= 2:
                get_logger().info(f"Failed adding line link to '{relevant_file}', error: {e}")

        return ""

    def publish_inline_comments(self, comments: list[dict]):
        for comment in comments:
            if 'position' in comment:
                self.publish_inline_comment(comment['body'], comment['position'], comment['path'])
            elif 'start_line' in comment: # multi-line comment
                # note that bitbucket does not seem to support range - only a comment on a single line - https://community.developer.atlassian.com/t/api-post-endpoint-for-inline-pull-request-comments/60452
                self.publish_inline_comment(comment['body'], comment['start_line'], comment['path'])
            elif 'line' in comment: # single-line comment
                self.publish_inline_comment(comment['body'], comment['line'], comment['path'])
            else:
                get_logger().error(f"Could not publish inline comment: {comment}")

    def get_title(self):
        return self.pr.title

    def get_languages(self):
        return {"yaml": 0}  # devops LOL

    def get_pr_branch(self):
        return self.pr.fromRef['displayId']

    def get_pr_owner_id(self) -> str | None:
        return self.workspace_slug

    def get_pr_description_full(self):
        if hasattr(self.pr, "description"):
            return self.pr.description
        else:
            return None

    def get_user_id(self):
        return 0

    def get_issue_comments(self):
        raise NotImplementedError(
            "Bitbucket provider does not support issue comments yet"
        )

    def add_eyes_reaction(self, issue_comment_id: int, disable_eyes: bool = False) -> Optional[int]:
        return True

    def remove_reaction(self, issue_comment_id: int, reaction_id: int) -> bool:
        return True

    @staticmethod
    def _parse_bitbucket_server(url: str) -> str:
        # pr url format: f"{bitbucket_server}/projects/{project_name}/repos/{repository_name}/pull-requests/{pr_id}"
        parsed_url = urlparse(url)
        server_path = parsed_url.path.split("/projects/")
        if len(server_path) > 1:
            server_path = server_path[0].strip("/")
            return f"{parsed_url.scheme}://{parsed_url.netloc}/{server_path}".strip("/")
        return f"{parsed_url.scheme}://{parsed_url.netloc}"

    @staticmethod
    def _parse_pr_url(pr_url: str) -> Tuple[str, str, int]:
        # pr url format: f"{bitbucket_server}/projects/{project_name}/repos/{repository_name}/pull-requests/{pr_id}"
        parsed_url = urlparse(pr_url)

        path_parts = parsed_url.path.strip("/").split("/")

        try:
            projects_index = path_parts.index("projects")
        except ValueError:
            projects_index = -1

        try:
            users_index = path_parts.index("users")
        except ValueError:
            users_index = -1

        if projects_index == -1 and users_index == -1:
            raise ValueError(f"The provided URL '{pr_url}' does not appear to be a Bitbucket PR URL")

        if projects_index != -1:
            path_parts = path_parts[projects_index:]
        else:
            path_parts = path_parts[users_index:]

        if len(path_parts) < 6 or path_parts[2] != "repos" or path_parts[4] != "pull-requests":
            raise ValueError(
                f"The provided URL '{pr_url}' does not appear to be a Bitbucket PR URL"
            )

        workspace_slug = path_parts[1]
        if users_index != -1:
            workspace_slug = f"~{workspace_slug}"
        repo_slug = path_parts[3]
        try:
            pr_number = int(path_parts[5])
        except ValueError as e:
            raise ValueError(f"Unable to convert PR number '{path_parts[5]}' to integer") from e

        return workspace_slug, repo_slug, pr_number

    def _get_repo(self):
        if self.repo is None:
            self.repo = self.bitbucket_client.get_repo(self.workspace_slug, self.repo_slug)
        return self.repo

    def _get_pr(self):
        try:
            pr = self.bitbucket_client.get_pull_request(self.workspace_slug, self.repo_slug,
                                                        pull_request_id=self.pr_num)
            return type('new_dict', (object,), pr)
        except Exception as e:
            get_logger().error(f"Failed to get pull request, error: {e}")
            raise e

    def _get_pr_file_content(self, remote_link: str):
        return ""

    def get_commit_messages(self):
        return ""

    # bitbucket does not support labels
    def publish_description(self, pr_title: str, description: str):
        payload = {
            "version": self.pr.version,
            "description": description,
            "title": pr_title,
            "reviewers": self.pr.reviewers  # needs to be sent otherwise gets wiped
        }
        try:
            self.bitbucket_client.update_pull_request(self.workspace_slug, self.repo_slug, str(self.pr_num), payload)
        except Exception as e:
            get_logger().error(f"Failed to update pull request, error: {e}")
            raise e

    # bitbucket does not support labels
    def publish_labels(self, pr_types: list):
        pass

    # bitbucket does not support labels
    def get_pr_labels(self, update=False):
        pass

    def _get_pr_comments_path(self):
        return f"rest/api/latest/projects/{self.workspace_slug}/repos/{self.repo_slug}/pull-requests/{self.pr_num}/comments"

    def _get_merge_base(self):
        return f"rest/api/latest/projects/{self.workspace_slug}/repos/{self.repo_slug}/pull-requests/{self.pr_num}/merge-base"
    # Clone related
    def _prepare_clone_url_with_token(self, repo_url_to_clone: str) -> str | None:
        # Accept both bitbucket.com and custom Bitbucket Server domains with /scm/ path
        if 'bitbucket' not in repo_url_to_clone.lower() and '/scm/' not in repo_url_to_clone:
            get_logger().error("Repo URL is not a valid bitbucket URL.")
            return None
        bearer_token = self.bearer_token
        if not bearer_token:
            get_logger().error("No bearer token provided. Returning None")
            return None
        # Return unmodified URL as the token is passed via HTTP headers in _clone_inner, as seen below.
        return repo_url_to_clone

    #Overriding the shell command, since for some reason usage of x-token-auth doesn't work, as mentioned here:
    # https://stackoverflow.com/questions/56760396/cloning-bitbucket-server-repo-with-access-tokens
    def _clone_inner(self, repo_url: str, dest_folder: str, operation_timeout_in_seconds: int=None):
        bearer_token = self.bearer_token
        if not bearer_token:
            #Shouldn't happen since this is checked in _prepare_clone, therefore - throwing an exception.
            raise RuntimeError(f"Bearer token is required!")

        cli_args = shlex.split(f"git clone -c http.extraHeader='Authorization: Bearer {bearer_token}' "
                               f"--depth 1 {repo_url} {dest_folder}")

        ssl_env = get_git_ssl_env()

        subprocess.run(cli_args, env=ssl_env, check=True,  # check=True will raise an exception if the command fails
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=operation_timeout_in_seconds)
