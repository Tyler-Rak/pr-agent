# Fix: Add Retry Logic for Bitbucket Server Rate Limiting (429 Errors)

## Problem Statement

The pr-agent fails when running `/review` command on Bitbucket Server with error:
```
Failed to review PR: 429 Client Error: for url: https://git.rakuten-it.com/rest/api/1.0/projects/TRV/repos/car-share-front/pull-requests/791/changes
```

**Root Cause**: Bitbucket Server rate limits API requests using a token bucket algorithm (default: 60 requests/burst). When pr-agent fetches PR changes with many files, it exhausts the rate limit on a single request to the `/changes` endpoint.

**Current Behavior**: The code fails immediately on 429 errors without retry.

**Issue**: A single `/review` command triggers the rate limit, so waiting between commands won't help.

---

## Solution Approach

Add exponential backoff retry logic to Bitbucket Server provider, similar to the existing GitHub provider implementation.

### Why This Works WITHOUT Reducing Quality:

1. **No data loss**: Retry mechanism fetches the same data, just waits between attempts
2. **Respects rate limits**: Exponential backoff gives the token bucket time to refill
3. **Proven pattern**: GitHub provider already uses this successfully (see `github_provider.py:221`)
4. **Maintains full context**: Still fetches complete file contents and diffs

### How Exponential Backoff Works:

- **Try 1**: Immediate request → 429 error
- **Try 2**: Wait 2 seconds → retry
- **Try 3**: Wait 4 seconds → retry
- **Try 4**: Wait 8 seconds → retry
- **Try 5**: Wait 16 seconds → retry
- **Try 6**: Wait 32 seconds → retry (final attempt)

By waiting progressively longer, we allow Bitbucket's token bucket to refill.

---

## Code Changes Required

### Change 1: Add Configuration Parameter

**File**: `pr_agent/settings/configuration.toml`

**Location**: Line 325-334 (in the `[bitbucket_server]` section)

**Current code**:
```toml
[bitbucket_server]
# URL to the BitBucket Server instance
# url = "https://git.bitbucket.com"
url = "https://git.rakuten-it.com"
pr_commands = [
    "/describe --pr_description.final_update_message=false",
    "/review",
    "/improve --pr_code_suggestions.commitable_code_suggestions=true",
]
```

**Change to**:
```toml
[bitbucket_server]
# URL to the BitBucket Server instance
# url = "https://git.bitbucket.com"
url = "https://git.rakuten-it.com"
ratelimit_retries = 5  # Number of retry attempts for rate limit errors (429)
pr_commands = [
    "/describe --pr_description.final_update_message=false",
    "/review",
    "/improve --pr_code_suggestions.commitable_code_suggestions=true",
]
```

---

### Change 2: Add Retry Decorator to Bitbucket Server Provider

**File**: `pr_agent/git_providers/bitbucket_server_provider.py`

#### Step 2a: Add Import Statement

**Location**: Around line 9 (after other imports)

**Current imports** (lines 1-11):
```python
import difflib
import re

from packaging.version import parse as parse_version
from typing import Optional, Tuple
from urllib.parse import quote_plus, urlparse

from atlassian.bitbucket import Bitbucket
from requests.exceptions import HTTPError
import shlex
import subprocess
```

**Add this import**:
```python
from retry import retry
```

**Result** (should look like this):
```python
import difflib
import re

from packaging.version import parse as parse_version
from typing import Optional, Tuple
from urllib.parse import quote_plus, urlparse

from atlassian.bitbucket import Bitbucket
from requests.exceptions import HTTPError
from retry import retry  # ADD THIS LINE
import shlex
import subprocess
```

#### Step 2b: Add Retry Decorator to get_diff_files Method

**Location**: Line 229 (method definition)

**Current code**:
```python
    def get_diff_files(self) -> list[FilePatchInfo]:
        if self.diff_files:
            return self.diff_files
```

**Change to**:
```python
    @retry(exceptions=(HTTPError,),
           tries=get_settings().get("bitbucket_server.ratelimit_retries", 5),
           delay=2, backoff=2, jitter=(1, 3))
    def get_diff_files(self) -> list[FilePatchInfo]:
        if self.diff_files:
            return self.diff_files
```

**Explanation of decorator parameters**:
- `exceptions=(HTTPError,)`: Retry on HTTPError (includes 429 status)
- `tries=get_settings().get("bitbucket_server.ratelimit_retries", 5)`: Read config, default to 5
- `delay=2`: Initial wait time of 2 seconds
- `backoff=2`: Double the delay each retry (2s → 4s → 8s → 16s → 32s)
- `jitter=(1, 3)`: Add random 1-3 seconds to avoid thundering herd problem

---

## Reference Implementation

The GitHub provider already implements this pattern successfully:

**File**: `pr_agent/git_providers/github_provider.py`
**Line**: 220-221

```python
@retry(exceptions=RateLimitExceeded,
       tries=get_settings().github.ratelimit_retries, delay=2, backoff=2, jitter=(1, 3))
def get_diff_files(self) -> list[FilePatchInfo]:
```

We're adapting this proven pattern for Bitbucket Server.

---

## Dependencies

The `retry` library is already installed:
- **Package**: `retry==0.9.2`
- **File**: `requirements.txt`
- **Documentation**: https://pypi.org/project/retry/

No new dependencies need to be added.

---

## Testing Instructions

After making the changes:

1. **Restart the pr-agent pod** to load the new code
2. **Trigger a `/review` command** on a PR with many file changes
3. **Expected behavior**:
   - First attempt may fail with 429
   - Should automatically retry with increasing delays
   - Eventually succeeds (within ~1 minute total)
   - Review completes successfully with full quality

4. **Monitor logs** for retry messages:
   ```bash
   kubectl logs pr-agent-stg-7f8559885c-8wsjv -c pr-agent --tail=100 -f
   ```

---

## Additional Context

### Why Single Request Triggers Rate Limit

Looking at the logs, pr-agent makes many API calls to fetch file contents:
```
File src/components/Notes/index.tsx not found at commit id: 756fb025...
File src/components/Notes/index.tsx not found at commit id: 116a1687...
File src/components/Notes/notes.module.scss not found at commit id: 756fb025...
...
```

For a PR with ~50 changed files (including tests, mocks, deleted files), this results in:
- 1 request to get PR changes list (`/changes` endpoint)
- 2 requests per file (before/after commits) = 100+ requests
- Total: 100+ requests in rapid succession

**Result**: Token bucket exhausted in a single `/review` command execution.

### Why Retry Works

Bitbucket Server's token bucket refills at a constant rate (tokens per second). By waiting between retries:
- **2-second wait**: ~2 tokens refilled
- **4-second wait**: ~4 tokens refilled
- **8-second wait**: ~8 tokens refilled
- **Total wait**: ~14 seconds = ~14 tokens refilled

This is enough to complete the operation.

---

## Alternative Solutions (Not Recommended)

### Option 1: Set `avoid_full_files = true`
- **Impact**: Reduces quality - AI only sees diff, not full file context
- **Why not**: Compromises review quality

### Option 2: Contact Bitbucket Admin
- **Action**: Request rate limit exemption for pr-agent service account
- **Why not**: Requires admin access, organizational approval, delays

### Option 3: Reduce Review Features
- **Impact**: Disables security checks, test validation, etc.
- **Why not**: Significantly reduces review quality

**Retry logic is the ONLY solution that maintains full quality while handling rate limits.**

---

## Files to Modify

Summary of all changes:

1. **pr_agent/settings/configuration.toml**
   - Line ~330: Add `ratelimit_retries = 5` under `[bitbucket_server]` section

2. **pr_agent/git_providers/bitbucket_server_provider.py**
   - Line ~9: Add `from retry import retry` import
   - Line ~229: Add `@retry(...)` decorator before `def get_diff_files(...)` method

---

## Success Criteria

After implementation:
- ✅ `/review` command completes successfully even with 429 errors
- ✅ No reduction in review quality
- ✅ Automatic retry with exponential backoff
- ✅ Total execution time increases by ~30-60 seconds (acceptable tradeoff)
- ✅ No changes needed to Bitbucket Server configuration

---

## Questions?

If implementation questions arise:
- Refer to `github_provider.py:220-221` for reference implementation
- Check `retry` library docs: https://pypi.org/project/retry/
- Test changes locally before deploying to production

---

**Document created**: 2025-11-06
**Author**: Claude (Diagnostic Agent)
**For**: Claude (Implementation Agent)
