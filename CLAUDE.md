# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Python Environment Setup with uv

This project uses `uv` for Python environment management.

**Initial setup:**
```bash
# Create virtual environment (requires Python 3.12+)
uv venv

# Activate the virtual environment
source .venv/bin/activate  # macOS/Linux
.venv\Scripts\activate     # Windows

# Install dependencies
uv pip install -r requirements.txt
uv pip install -r requirements-dev.txt

# Install package in editable mode
uv pip install -e .
```

**Running commands with uv:**
```bash
# Run tests
uv run pytest

# Run CLI
uv run pr-agent --pr_url=<url> review

# Run with activated venv
source .venv/bin/activate
pr-agent --pr_url=<url> review
```

## Development Commands

**Testing:**
```bash
# Run all tests
pytest

# Run specific test file
pytest tests/unittest/test_clip_tokens.py

# Run with coverage
pytest --cov=pr_agent --cov-report=html
```

**Linting and formatting:**
```bash
# Run ruff linter (check only)
ruff check .

# Auto-fix issues with ruff
ruff check . --fix

# Check imports with isort
isort --check-only .

# Fix imports
isort .

# Run pre-commit hooks manually
pre-commit run --all-files
```

**Running PR-Agent locally:**
```bash
# Review a PR
pr-agent --pr_url=https://github.com/owner/repo/pull/123 review

# Describe a PR
pr-agent --pr_url=https://github.com/owner/repo/pull/123 describe

# Suggest improvements
pr-agent --pr_url=https://github.com/owner/repo/pull/123 improve

# Ask questions about a PR
pr-agent --pr_url=https://github.com/owner/repo/pull/123 ask "your question here"

# Update changelog
pr-agent --pr_url=https://github.com/owner/repo/pull/123 update_changelog

# With custom configuration
pr-agent --pr_url=<url> review --pr_reviewer.extra_instructions="focus on security"
```

**Environment variables required:**
```bash
export OPENAI_KEY=your_key_here
export GITHUB_TOKEN=your_token_here  # For GitHub
# Or other git provider tokens: GITLAB_PERSONAL_ACCESS_TOKEN, BITBUCKET_BEARER_TOKEN, etc.
```

## Architecture Overview

**Core components:**

1. **pr_agent/agent/** - Main agent orchestration
   - `pr_agent.py`: Command routing and execution (maps commands to tool classes)

2. **pr_agent/tools/** - AI-powered PR tools
   - `pr_reviewer.py`: Code review tool (`/review`)
   - `pr_description.py`: PR description generator (`/describe`)
   - `pr_code_suggestions.py`: Code improvement suggestions (`/improve`)
   - `pr_questions.py`: Q&A about PRs (`/ask`)
   - `pr_update_changelog.py`: Changelog updates
   - `pr_add_docs.py`: Documentation generation
   - `pr_generate_labels.py`: Auto-labeling
   - `pr_similar_issue.py`: Find similar issues (requires vector DB)

3. **pr_agent/git_providers/** - Git platform integrations
   - `github_provider.py`: GitHub API integration
   - `gitlab_provider.py`: GitLab API integration
   - `bitbucket_provider.py`: Bitbucket Cloud integration
   - `bitbucket_server_provider.py`: Bitbucket Server integration
   - `azuredevops_provider.py`: Azure DevOps integration
   - `gitea_provider.py`: Gitea integration
   - `git_provider.py`: Abstract base class for all providers

4. **pr_agent/algo/** - Core algorithms and AI handling
   - `ai_handlers/`: AI model integrations (LiteLLM, OpenAI, Anthropic, etc.)
   - `pr_processing.py`: PR data processing and compression
   - `git_patch_processing.py`: Git diff parsing and token management
   - `token_handler.py`: Token counting and limits
   - `utils.py`: Common utilities

5. **pr_agent/servers/** - Webhook and server implementations
   - `github_app.py`: GitHub App webhook handler
   - `gitlab_webhook.py`: GitLab webhook handler
   - `bitbucket_app.py`: Bitbucket App handler
   - `github_action_runner.py`: GitHub Actions integration
   - `azuredevops_server_webhook.py`: Azure DevOps webhooks

6. **pr_agent/settings/** - Configuration and prompts
   - `configuration.toml`: Main configuration file with all settings
   - `*_prompts.toml`: Prompt templates for each tool
   - `language_extensions.toml`: File extension to language mappings
   - `ignore.toml`: Files/patterns to ignore

## Configuration System

**Configuration hierarchy (highest priority first):**
1. CLI arguments: `--config.param=value`
2. Repository settings: `.pr_agent.toml` in repo root
3. Global settings: User's global configuration
4. Default settings: `pr_agent/settings/configuration.toml`

**Key configuration sections in configuration.toml:**
- `[config]`: Global settings (model, git_provider, token limits)
- `[pr_reviewer]`: Code review tool settings
- `[pr_description]`: PR description tool settings
- `[pr_code_suggestions]`: Code improvement tool settings
- `[pr_questions]`: Q&A tool settings

**Customizing prompts:**
Edit the `*_prompts.toml` files to modify AI prompts for each tool.

## AI Model Integration

PR-Agent uses **LiteLLM** as the unified interface for multiple AI providers:
- OpenAI (GPT-4, GPT-5, etc.)
- Anthropic (Claude)
- Azure OpenAI
- Google (Gemini, Vertex AI)
- AWS Bedrock
- And many more

Model selection is configured via `config.model` in configuration.toml (default: `gpt-5-2025-08-07`).

## PR Compression Strategy

PR-Agent uses a sophisticated "PR Compression" strategy to handle large PRs:
1. Tokenizes and analyzes all changed files
2. Prioritizes important changes (more context lines, relevant functions)
3. Clips or skips less important files to fit within token limits
4. Adds dynamic context (surrounding code) when `allow_dynamic_context=true`

This allows handling PRs of any size with a single LLM call.

## Git Provider Support

Supported platforms: GitHub, GitLab, Bitbucket (Cloud & Server), Azure DevOps, Gitea

Each provider implements the `GitProvider` interface with methods for:
- Fetching PR data (diffs, commits, comments)
- Publishing comments and reviews
- Managing labels and metadata
- Handling webhooks

## Test Structure

Tests are located in `tests/` directory:
- `tests/unittest/`: Unit tests for specific functions
- Tests use pytest framework
- Mock git providers and AI responses for isolated testing

**Running specific tests:**
```bash
pytest tests/unittest/test_clip_tokens.py -v
pytest tests/unittest/ -k "test_load_yaml"
```

## Docker Support

**Build Docker image:**
```bash
docker build -f docker/Dockerfile -t pr-agent .
```

**Run as GitHub Action:**
See `Dockerfile.github_action` for the GitHub Actions image.

## Pre-commit Hooks

This project uses pre-commit hooks for code quality:
- `check-added-large-files`: Prevent large files
- `check-toml`, `check-yaml`: Validate config files
- `end-of-file-fixer`: Ensure files end with newline
- `trailing-whitespace`: Remove trailing whitespace
- `isort`: Sort imports

**Setup pre-commit:**
```bash
pre-commit install
```

## Important Notes

- **Python version**: Requires Python 3.12+
- **Configuration style**: Uses TOML for all configuration files
- **Async support**: Many operations use asyncio for concurrent execution
- **Token limits**: Default max_model_tokens is 32000 (configurable)
- **Rate limiting**: Be aware of git provider API rate limits
- **Security**: Use environment variables for API keys, never commit secrets
