# examples/openai_agents_refund

The refund-support agent from the [README](https://github.com/tejas-parjane/agentci#run-an-existing-openai-agents-project-without-rewriting-it),
as a runnable project. It runs fully offline on a deterministic scripted model —
no API key, no network.

## Run it

```bash
pip install "agentci-py[openai-agents]"
cd examples/openai_agents_refund
agentci run                  # PASS — one refund
```

## Catch the regression

```bash
agentci run                                    # green, CALLS=1
agentci record --name refund                   # capture today's behaviour
# edit support_agent.py:  CALLS = 1  ->  CALLS = 2
agentci replay .agentci/traces/refund.jsonl    # the second refund is refused
agentci diff .agentci/traces/refund.jsonl .agentci/traces/refund-replay.jsonl
agentci gate                                   # RELEASE GATE: BLOCKED
```

The recorded/scenario artifacts land in `.agentci/traces/`; per-run traces and
reports live under `.agentci/`. See the README's `record -> replay -> diff -> block`
section for what each step verifies.