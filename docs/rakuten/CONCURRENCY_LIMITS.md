# Concurrency Limitations for /webhook-parallel

## Executive Summary

**Recommended Configuration:** `max_concurrent_reviews = 2-3`

**Primary Bottleneck:** Bitbucket Server API rate limits (token bucket: 15 tokens max, 3 tokens/sec refill)

## Resource Constraints

### 1. Hardware (Rakuten CaaS Deployment)
- **CPU:** 500m (0.5 cores)
- **Memory:** 1Gi
- **Impact:** Can support 5-8 concurrent I/O-bound reviews, not the limiting factor

### 2. Bitbucket Server API Rate Limit (PRIMARY BOTTLENECK)
- **Token bucket:** 15 tokens maximum
- **Refill rate:** 3 tokens/second (180 tokens/minute)
- **Cost per review:** ~9-13 API calls = 9-13 tokens
  - Phase 1: Get PR metadata (1) + Get PR diff (1) = 2-3 tokens
  - Phase 2: AI processing (0 tokens)
  - Phase 3: Post comments (7-10 tokens)
    - /describe: 1 token
    - /review: 1 token
    - /improve: 1 main comment + 4-7 inline code suggestions = 5-8 tokens

**Critical constraint:** API calls are bursty (most happen during Phase 3 comment posting). With 3 concurrent reviews finishing simultaneously:
- Required tokens: 3 × 10 = 30 tokens (average case)
- Available tokens: 15 (bucket) + 15 (refilled during 5s burst) = 30 tokens
- **Result:** At capacity limit, occasional rate limit errors expected (~2-5%)

**Note:** Bitbucket Server does not support persistent comments (`get_issue_comments()` not implemented). The `persistent_comment=true` setting has no effect and always creates new comments, which actually saves 2-3 API calls per review compared to other git providers.

### 3. Task Queue (No Hard Limit)
- **Implementation:** `asyncio.create_task()` with no queue size limit
- **Memory per waiting task:** ~5KB (task object + copied context)
- **Theoretical capacity:** ~200,000 tasks before memory exhaustion
- **Practical limit:** ~1,000 waiting tasks safe on 1Gi pod

## Concurrency Calculation

### Sustainable Throughput
```
Token refill rate: 180 tokens/minute
Tokens per review: 10
Theoretical max: 180 / 10 = 18 reviews/minute
```

### Safe Concurrent Reviews
```
Review phases (per PR):
- Phase 1 (0-5s): 2-3 API calls (get PR data, get diff)
- Phase 2 (5-25s): 0 API calls (AI processing - LLM calls, not Bitbucket API)
- Phase 3 (25-30s): 7-10 API calls (BURSTY - all comment posting)
  - /describe: 1 call
  - /review: 1 call
  - /improve: 5-8 calls (1 main + 4-7 inline code suggestions)

Total per PR: 9-13 API calls (average: 10 calls)

Safe concurrent (avoiding burst collisions): 2-3 reviews
Risky concurrent (may hit rate limits): 4-5 reviews
```


### Wait Time Examples
| Concurrent | Burst Load | Wait Time for 10th PR | Rate Limit Risk |
|------------|------------|----------------------|-----------------|
| 2 | 10 PRs | ~150s (2.5 min) | Low (<1%) |
| 3 | 10 PRs | ~100s (1.7 min) | Medium (2-5%) |
| 5 | 10 PRs | ~60s (1 min) | High (10-20%) |

## Configuration Recommendations

### Conservative (Recommended)
```toml
[bitbucket_app]
enable_parallel_reviews = true
max_concurrent_reviews = 2
```
- **Throughput:** ~240 reviews/hour
- **Burst capacity:** 2 simultaneous PRs
- **Rate limit errors:** <1%

### Balanced
```toml
max_concurrent_reviews = 3
```
- **Throughput:** ~360 reviews/hour
- **Burst capacity:** 3 simultaneous PRs
- **Rate limit errors:** 2-5% during bursts

### Aggressive (Not Recommended)
```toml
max_concurrent_reviews = 5
```
- **Throughput:** ~10 reviews/minute (theoretical)
- **Rate limit errors:** 10-20%
- **Requires:** API call optimization or higher rate limits

## Optimization Options

### Immediate (No Code Changes)
1. Start with `max_concurrent_reviews = 2`
2. Monitor rate limit errors in logs
3. Gradually increase to 3 if error rate <2%

### Short-term (Code Changes)
1. **Reduce inline code suggestions:** `num_code_suggestions=4` → `2`
   - Savings: ~2 API calls per review
   - New capacity: 4 concurrent reviews

2. **Add queue depth monitoring:** Track wait times and queue depth

3. **Add task queue limit:** Prevent unbounded memory growth

## Monitoring Metrics

Key metrics to track:
- `bitbucket_api_rate_limit_errors`: HTTP 429 responses
- `review_wait_time_seconds`: Time waiting for semaphore slot
- `review_duration_seconds`: Total review time
- `queue_depth`: Number of waiting tasks
- `active_reviews`: Current processing reviews

Alert thresholds:
- Rate limit error rate >5%: Reduce concurrency
- Average wait time >60s: Increase concurrency (if rate limits allow)
- Queue depth >50: Possible traffic spike or stuck reviews

## References

- Configuration file: `pr_agent/settings/configuration.toml:315-316`
- Implementation: `pr_agent/servers/bitbucket_server_webhook.py:208-317`
- Gunicorn config: `pr_agent/servers/gunicorn_config.py:74-80`

## Last Updated

2025-12-08 - Initial analysis based on Rakuten CaaS deployment constraints
