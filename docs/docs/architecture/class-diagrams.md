# Class Diagrams

This document shows the class structure and relationships in PR-Agent.

## Table of Contents

1. [Core Class Hierarchy](#core-class-hierarchy)
2. [Git Provider Pattern](#git-provider-pattern)
3. [Tool Classes](#tool-classes)
4. [Data Models](#data-models)

---

## Core Class Hierarchy

Complete class structure showing relationships between major components.

```mermaid
classDiagram
    class GitProvider {
        <<abstract>>
        +pr_url: str
        +diff_files: list
        +incremental: IncrementalPR
        +get_files() list~str~
        +get_diff_files() list~FilePatchInfo~
        +get_pr_description() str
        +get_pr_id() str
        +publish_comment(comment: str)
        +publish_code_suggestions(suggestions)
        +publish_labels(labels)
        +is_supported(capability) bool
        +get_languages()
        +get_pr_branch()
        +get_user_id() str
    }

    class BitbucketServerProvider {
        -bitbucket_client: Bitbucket
        -bitbucket_server_url: str
        -workspace_slug: str
        -repo_slug: str
        -pr_num: int
        -pr: dict
        -bearer_token: str
        -bitbucket_api_version: Version
        +get_diff_files() list~FilePatchInfo~
        +get_file(path: str, commit_id: str) str
        +get_files() list~str~
        +publish_comment(comment: str)
        +create_inline_comment(body, file, line)
        -_get_pr() dict
        -_parse_pr_url() tuple
        -_parse_bitbucket_server(url) str
        -get_best_common_ancestor() str
        -_get_merge_base() str
    }

    class GitHubProvider {
        -github_client: Github
        -repo: Repository
        -pr: PullRequest
        -github_user_id: str
        -installation_id: int
        +get_diff_files() list~FilePatchInfo~
        +get_files() list~str~
        +publish_comment(comment: str)
        +publish_code_suggestions(suggestions)
        +publish_inline_comment(body, file, line)
        +get_languages() dict
        -_parse_pr_url() tuple
        -_get_github_client() Github
    }

    class GitLabProvider {
        -gitlab_client: Gitlab
        -project: Project
        -mr: MergeRequest
        +get_diff_files() list~FilePatchInfo~
        +get_files() list~str~
        +publish_comment(comment: str)
        +create_inline_comment(body, file, line)
        -_parse_mr_url() tuple
    }

    class AzureDevopsProvider {
        -azure_client: Connection
        -git_client: GitClient
        -repo_id: str
        -pr_id: int
        +get_diff_files() list~FilePatchInfo~
        +publish_comment(comment: str)
        -_get_azure_client() Connection
    }

    GitProvider <|-- BitbucketServerProvider
    GitProvider <|-- GitHubProvider
    GitProvider <|-- GitLabProvider
    GitProvider <|-- AzureDevopsProvider
    GitProvider <|-- BitbucketProvider
    GitProvider <|-- GiteaProvider

    class FilePatchInfo {
        +base_file: str
        +head_file: str
        +patch: str
        +filename: str
        +tokens: int
        +edit_type: EDIT_TYPE
        +old_filename: str
        +num_plus_lines: int
        +num_minus_lines: int
        +language: str
        +ai_file_summary: str
    }

    class EDIT_TYPE {
        <<enumeration>>
        ADDED = 1
        DELETED = 2
        MODIFIED = 3
        RENAMED = 4
        UNKNOWN = 5
    }

    GitProvider --> FilePatchInfo : creates
    FilePatchInfo --> EDIT_TYPE : uses

    class PRAgent {
        -command2class: dict
        -git_provider: GitProvider
        +handle_request(pr_url, command, args)
        +apply_repo_settings(git_provider)
        -_handle_request(pr_url, tool, args)
        -_prepare_bot_identifier(provider)
    }

    class PRReviewer {
        -git_provider: GitProvider
        -ai_handler: AIHandler
        -incremental: IncrementalPR
        -main_language: str
        -review: dict
        +run()
        +push(data)
        -_prepare_prediction(model)
        -_get_prediction(model)
        -_prepare_pr_review()
        -_publish_full_comment(review)
    }

    class PRDescription {
        -git_provider: GitProvider
        -ai_handler: AIHandler
        -description: dict
        +run()
        -_prepare_prediction(model)
        -_get_prediction(model)
        -publish_description()
    }

    class PRCodeSuggestions {
        -git_provider: GitProvider
        -ai_handler: AIHandler
        -suggestions: list
        +run()
        -_prepare_prediction(model)
        -_get_prediction(model)
        -publish_code_suggestions()
        -_publish_inline_comments()
    }

    class PRQuestions {
        -git_provider: GitProvider
        -ai_handler: AIHandler
        -question: str
        -answer: str
        +run()
        -_prepare_prediction(model)
        -_get_prediction(model)
        -publish_answer()
    }

    PRAgent --> PRReviewer : creates
    PRAgent --> PRDescription : creates
    PRAgent --> PRCodeSuggestions : creates
    PRAgent --> PRQuestions : creates

    PRReviewer --> GitProvider : uses
    PRDescription --> GitProvider : uses
    PRCodeSuggestions --> GitProvider : uses
    PRQuestions --> GitProvider : uses

    class LiteLLMAIHandler {
        -deployment_id: str
        +chat_completion(model, messages, temperature)
        -_chat_completion(model, messages)
    }

    class OpenAIAIHandler {
        -openai_client: OpenAI
        +chat_completion(model, messages, temperature)
    }

    class AnthropicAIHandler {
        -anthropic_client: Anthropic
        +chat_completion(model, messages, temperature)
    }

    class AIHandler {
        <<interface>>
        +chat_completion(model, messages, temperature)
    }

    AIHandler <|.. LiteLLMAIHandler
    AIHandler <|.. OpenAIAIHandler
    AIHandler <|.. AnthropicAIHandler

    PRReviewer --> AIHandler : uses
    PRDescription --> AIHandler : uses
    PRCodeSuggestions --> AIHandler : uses
    PRQuestions --> AIHandler : uses
```

---

## Git Provider Pattern

Detailed view of the provider abstraction.

```mermaid
classDiagram
    class GitProvider {
        <<abstract>>
        +pr_url: str
        +diff_files: list~FilePatchInfo~
        +get_diff_files()* list~FilePatchInfo~
        +publish_comment(comment: str)*
        +is_supported(capability: str) bool
    }

    class BitbucketServerProvider {
        +atlassian_client: Bitbucket
        +get_diff_files() list~FilePatchInfo~
        +get_file(path, commit_sha) str
    }

    class ProviderFactory {
        +_GIT_PROVIDERS: dict
        +get_git_provider_with_context(pr_url) GitProvider
        +_get_provider_id(pr_url) str
    }

    GitProvider <|-- BitbucketServerProvider
    ProviderFactory ..> GitProvider : creates
    BitbucketServerProvider ..> Bitbucket : uses

    class Bitbucket {
        <<external library>>
        +url: str
        +token: str
        +get_pull_request(project, repo, pr_id)
        +get_pull_requests_changes(project, repo, pr_id)
        +get_content_of_file(project, repo, path, sha)
        +add_pull_request_comment(project, repo, pr_id, text)
    }

    note for GitProvider "Abstract base class\ndefining provider interface"
    note for BitbucketServerProvider "Issues: Individual file fetches\ncause rate limiting"
    note for Bitbucket "atlassian-python-api library\nWraps REST API calls"
```

---

## Tool Classes

Structure of tool implementations.

```mermaid
classDiagram
    class BaseTool {
        <<abstract>>
        #git_provider: GitProvider
        #ai_handler: AIHandler
        +run()
        #_prepare_prediction(model)
        #_get_prediction(model)
    }

    class PRReviewer {
        -incremental: IncrementalPR
        -main_language: str
        -review: dict
        +run()
        -_prepare_pr_review()
        -_publish_full_comment()
        -_get_findings()
        -_get_security_concerns()
    }

    class PRDescription {
        -description: dict
        -git_description: str
        +run()
        -publish_description()
        -_update_title(title)
    }

    class PRCodeSuggestions {
        -suggestions: list
        -commitable: bool
        +run()
        -publish_code_suggestions()
        -_publish_inline_comments()
        -_publish_persistent_comment()
    }

    BaseTool <|-- PRReviewer
    BaseTool <|-- PRDescription
    BaseTool <|-- PRCodeSuggestions
    BaseTool <|-- PRQuestions
    BaseTool <|-- PRUpdateChangelog
    BaseTool <|-- PRAddDocs

    BaseTool ..> GitProvider : uses
    BaseTool ..> AIHandler : uses

    note for PRReviewer "/review command\nGenerates review with findings"
    note for PRDescription "/describe command\nAuto-generates PR description"
    note for PRCodeSuggestions "/improve command\nSuggests code improvements"
```

---

## Data Models

Core data structures.

```mermaid
classDiagram
    class FilePatchInfo {
        +base_file: str
        +head_file: str
        +patch: str
        +filename: str
        +tokens: int
        +edit_type: EDIT_TYPE
        +old_filename: str
        +num_plus_lines: int
        +num_minus_lines: int
        +language: str
        +ai_file_summary: str
    }

    class EDIT_TYPE {
        <<enumeration>>
        ADDED
        DELETED
        MODIFIED
        RENAMED
        UNKNOWN
    }

    class IncrementalPR {
        +is_incremental: bool
        +first_new_commit: str
        +commits_range: tuple
        +get_incremental_commits(git_provider)
        +get_previous_review(git_provider)
    }

    class TokenHandler {
        -encoder: TokenEncoder
        +prompt_tokens: int
        +max_tokens: int
        +count_tokens(text: str) int
        +update_count(text: str)
        +limit_tokens(text: str, max_tokens: int) str
    }

    class TokenEncoder {
        <<abstract>>
        +encoding: tiktoken.Encoding
        +encode(text: str) list~int~
        +decode(tokens: list~int~) str
        +count_tokens(text: str) int
    }

    FilePatchInfo --> EDIT_TYPE
    TokenHandler --> TokenEncoder

    class PRProcessingResult {
        +diff_files: list~FilePatchInfo~
        +patches_diff: str
        +total_tokens: int
        +files_with_full_content: int
        +files_clipped: int
        +files_skipped: int
    }

    PRProcessingResult --> FilePatchInfo

    note for FilePatchInfo "Core data structure\nfor file changes"
    note for TokenHandler "Manages token budget\nfor AI requests"
    note for IncrementalPR "Supports incremental\nreview mode"
```

---

## Processing Pipeline Classes

Classes involved in diff processing and compression.

```mermaid
classDiagram
    class PRProcessing {
        <<module>>
        +get_pr_diff(git_provider, token_handler, model)
        +pr_generate_extended_diff(patches)
        +pr_generate_compressed_diff(patches)
        +sort_files_by_main_languages(files)
    }

    class GitPatchProcessing {
        <<module>>
        +extend_patch(patch, file_content, num_lines)
        +extract_hunk_lines_from_patch(patch)
        +convert_to_hunks_with_lines_numbers(patch, file)
        +handle_patch_deletions(patch, file_name)
    }

    class TokenHandler {
        -encoder: TokenEncoder
        -prompt_tokens: int
        -max_tokens: int
        +count_tokens(text) int
        +update_count(text)
        +limit_tokens(text, max) str
    }

    class LanguageHandler {
        <<module>>
        +is_valid_file(filename) bool
        +sort_files_by_main_languages(files)
        +get_file_language_extension(filename) str
    }

    class FileFilter {
        <<module>>
        +filter_ignored(files, provider) list
        +is_path_ignored(path) bool
    }

    PRProcessing --> TokenHandler : uses
    PRProcessing --> GitPatchProcessing : uses
    PRProcessing --> LanguageHandler : uses
    PRProcessing --> FileFilter : uses
    PRProcessing --> FilePatchInfo : processes

    note for PRProcessing "Main compression logic\nHandles token limits"
    note for GitPatchProcessing "Diff parsing and\ncontext extension"
    note for FileFilter "Applies ignore patterns\nfrom configuration"
```

---

## Configuration System

```mermaid
classDiagram
    class Settings {
        <<Dynaconf>>
        +config: ConfigSection
        +pr_reviewer: PRReviewerSection
        +pr_description: PRDescriptionSection
        +github: GitHubSection
        +bitbucket_server: BitbucketServerSection
        +get(key, default)
        +set(key, value)
    }

    class ConfigLoader {
        <<module>>
        +get_settings() Settings
        +global_settings: Settings
    }

    class ConfigSection {
        +model: str
        +git_provider: str
        +max_model_tokens: int
        +temperature: float
        +verbosity_level: int
    }

    class BitbucketServerSection {
        +url: str
        +bearer_token: str
        +username: str
        +password: str
        +pr_commands: list
    }

    ConfigLoader --> Settings : provides
    Settings --> ConfigSection : contains
    Settings --> BitbucketServerSection : contains

    note for Settings "Hierarchical configuration\nwith overrides"
    note for ConfigLoader "Loads from:\n1. CLI args\n2. .pr_agent.toml\n3. Defaults"
```

---

## See Also

- [architecture-overview.md](architecture-overview.md) - System overview
- [sequence-diagrams.md](sequence-diagrams.md) - Execution flows
- [data-flow.md](data-flow.md) - Data processing details
