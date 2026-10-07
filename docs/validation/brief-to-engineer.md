# Engineer brief — try AgentCI (30 minutes)

You're an early validator of an open-source tool, **before its first public
release**. It is an alpha; we expect rough edges and want them surfaced, not
patched over silently. There is no wrong outcome.

Goal: record, replay, diff, and gate the behaviour of a small openai-agents
application, then tell us honestly what you think it is doing.

## Setup

- Python 3.11 or 3.12 on your machine (a clean virtualenv is fine).
- `git` available on PATH.

## Task (in order)

1. Clone `https://github.com/tejas-parjane/agentci`.
2. Follow the README from a clean environment. Start at **Install**, then
   **60-second quickstart**.
3. Install AgentCI with the OpenAI Agents integration
   (`pip install "agentci-py[openai-agents]"` from the wheel or the Git URL in
   the README).
4. Run the example:
   ```
   cd examples/openai_agents_refund
   agentci run
   ```
5. Record the agent:
   ```
   agentci record --name refund
   ```
6. Introduce the deliberate regression: in `support_agent.py`, change
   `CALLS = 1` to `CALLS = 2` (the agent now refunds the order twice).
7. Run the release loop:
   ```
   agentci replay .agentci/traces/refund.jsonl
   agentci diff .agentci/traces/refund.jsonl .agentci/traces/refund-replay.jsonl
   agentci gate
   ```
8. Write down, in your own words, **what you believe AgentCI is doing** at each
   step.

Then answer the closing questions (no research needed — instinct is the data):

> **Would you trust this to block a production agent release? Why or why not?**

> What would you expect to exist next for you to consider using it in a real
> project?

## The rules

- Work alone, without looking at other projects' issues or docs first.
- Do not skip ahead; stop at the first step that blocks you, note exactly where
  and why, and you're done. That is a successful run for us.
- Send back: your answers to the two questions, the step that took longest,
  anything you got stuck on, and your environment (OS, Python version,
  install command you used).

No hand-holding — if you get stuck, the documentation visibility is part of
what we're testing.