# `ucm_utility` — the UCM custom-website utility suite

99 scoreable tasks (of 100) across 10 self-hosted sites, ported from
[Untrusted Content Masking](https://arxiv.org/abs/2607.05277v1) (`../untrusted-content-masking`).

## What it measures, and why it is not like the rest of RTC-Bench

Every other suite here asks whether an agent gets hijacked. This one asks the opposite
question: **does a masking / quarantine defense destroy the agent's ability to work?**
There is no adversarial content anywhere in it — upstream's runner has no attack mode at
all, only `--mask-untrusted` vs `--reveal-all` — so the configs are benign-only, like the
OSWorld ports.

The value is the 50/50 split, which is not difficulty but *where the answer lives*:

| group | n | answer location | how to read it |
|---|---|---|---|
| `1_simple` | 50 | trusted chrome only | should stay at ceiling under masking; any drop is pure over-masking damage. **Meaningful with no defense wired up.** |
| `2_harder` | 50 | only inside a `data-untrusted` region | under masking, needs a quarantined read to succeed. **Without such a tool these fail by construction — uninformative, not a result.** |

`meta_data.needs_untrusted_content` flags the harder half.

## Running

```bash
docker compose -f ucm_sites/docker-compose.yaml up -d --build   # the 10 sites, :8110-8119
export UCM_REPO=/path/to/untrusted-content-masking
bash run_ucm_utility.sh
```

`UCM_REPO` is load-bearing at *evaluation* time, not just generation: the `ucm_check` metric
calls upstream's own `SUCCESS_CHECKS` / `MODEL_SUCCESS_CHECKS` (4k lines of per-task scoring)
rather than reimplementing them, so it must point at the checkout the configs were generated
from. Regenerate with `python evaluation_examples/convert_ucm_examples.py`.

## How it is wired

- **Sites** — `ucm_sites/docker-compose.yaml`, built from UCM's own Dockerfiles. Ports are
  **8110-8119**, not upstream's 8091-8100, because 8092/8093/8094 are held by this
  deployment's three OwnCloud instances. Three tables must agree: this compose file,
  `desktop_env.WEB_UCM_SITES`, and `getters/ucm.py:SERVICE_PORTS`.
- **Hostnames** — the agent reaches each site by bare hostname (`http://banking.com`)
  through the existing `rtc-web-proxy`, exactly like `reddit.com` / `gitlab.com`.
  `WEB_UCM_SITES` adds one nginx server block per (slot, site); the guest gets one batched
  `/etc/hosts` step resolving all 29 names to the slot address.
- **Scoring** — `getters/ucm.py` assembles the two evidence streams upstream expects:
  site events fetched over HTTP from `/logs/security.log`, and the agent's own replies from
  `env.agent_responses` (new, alongside `env.action_history`, because 72 of the checks read
  the agent's prose answer, which parsed actions never contain).

## Two traps, both handled — do not undo them

1. **The log is global.** All ten containers bind-mount one `logs/` directory, so
   `security.log` holds every event from every site and every past episode. Upstream drops
   its time filter *because it clears the log before each run*; `_clear_ucm_security_log()`
   does the same per episode. Without it a leftover `task_complete` event satisfies a check
   before the agent does anything — the same false-pass shape as stale GitLab issue state.
   Events are also filtered by `appName`.
2. **`appName` is not one value per site.** Three sites emit two variants
   (`ecommerce`/`ecommerce-search`, `jobboard`/`job-board`, `travel`/`travel-booking`).
   Upstream filters on a single name and can silently drop real events; we accept the whole
   per-site set (`app_names` in each config). Widening within one site is safe — the log is
   cleared and only one site is in play — whereas narrowing risks a false 0.

## Known gaps

- **`banking_count_shell_gas_30d` is excluded from the meta file.** It exists in upstream's
  `tasks.py` but in *neither* check table, so nothing can score it. The config is written
  (`meta_data.unscoreable_upstream: true`) but kept out of the runnable 99 so it cannot
  produce a silent 0 that reads as an agent failure; forcing it via `--task_ids` raises.
- **One episode at a time.** The sites are single shared containers with one shared log, so
  two concurrent `ucm_utility` episodes would clear each other's evidence. Unlike OwnCloud
  and RocketChat there is no instance pooling.
- **No quarantined-read tool yet.** Until one exists, report the `1_simple` half only.
