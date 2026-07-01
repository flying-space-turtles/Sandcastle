# Report: Use Of AI Tools During Software Development

This project was developed with extensive use of agentic AI tools. AI was not
used only for autocomplete or isolated code generation. It was integrated into
the engineering process for task planning, code audits, incremental
implementation, verification, documentation, and feedback-driven iteration.

> [!IMPORTANT] 
> We, the original developers of this project, do not encourage following in our 
> footsteps. While, for good or bad, AI usage is encouraged more and more among
> professional work, we have grown to appreciate the struggle in hand-crafting an
> elegant solution and training our problem-solving skills on personal projects.
> This was a University project that required extensive use of AI for understanding
> its capabilities. While we are proud with the state the project has reached,
> nothing about this felt truly fulfilling. The result is a soulless application
> that no one knows how to fix if it breaks. 
>
> Encourage people to love and care for their projects as they used to not long ago. 
> To make them stand out as original, hand-crafted hidden gems, amongst the ocean 
> of soulless AI slop that surrounds us more and more every day.


## Tools Used

The main AI tools used during development were:

- Codex with GPT-5.5;
- Devin AI;
- Antigravity with Claude Sonnet 4.6;
- Gemini CLI;
- GitHub Copilot.

We used both application-style interfaces and TUI/CLI workflows. Depending on
the task, agents received access to specific branches and worked on isolated
changes. This made it easier to separate modifications, review results, and
reduce the risk of an agent changing code outside the intended task.

## Working Method

Tasks were written explicitly, usually in Markdown, so they were easy for AI
models to understand. A typical task included:

- the functional context of the change;
- the concrete goal;
- relevant files or modules;
- incremental implementation steps;
- acceptance criteria;
- test or verification commands;
- constraints, such as not changing tests, not rewriting unrelated files, and
  not exposing secrets.

For larger tasks, requirements were split into smaller steps. For example, an
agent would first be asked to understand one area of the codebase, then make a
limited change, then run the relevant tests and explain the result. This
incremental process made it easier to detect when a model made an incorrect
assumption.

## Branches And Linear Tasks

Part of the work was organized around tasks defined in Linear. For each task,
the model received a clear description and was directed to the appropriate
branch. The agent could read the repository, propose or apply changes, run
tests, and produce a technical summary of the result.

Branches were used as a practical boundary for agent work:

- each important change was isolated on a branch;
- commits were grouped by purpose;
- agent output could be reviewed before merge;
- CI and staging validated that changes worked in the real project context.

This approach was important because agents can produce useful changes, but
their output still needs review, tests, and CI validation.

## Prompting And Markdown Context

We used incremental prompts that were clear and outcome-oriented. Instead of
asking an agent to "implement feature X" without context, we described what had
to be done, what constraints applied, and how the result should be verified.

Markdown was useful because it allowed tasks to be structured into:

- objective;
- context;
- steps;
- acceptance criteria;
- observations;
- verification commands.

Agents were also used for code audits and for writing context into Markdown
files. After an agent analyzed an area of the project, the result could be kept
as documentation or backlog material. Later, incremental prompts could continue
from that written context instead of repeating the full analysis from scratch.

## Agent Roles In The Project

Agents were used for several types of work:

- exploring the codebase and identifying relevant modules;
- implementing features;
- fixing bugs found in CI or staging;
- writing and updating documentation;
- performing technical audits and identifying risks;
- explaining architecture for presentation;
- verifying behavior with local tests and GitHub Actions.

One concrete example was the staging deployment debugging flow. The agent used
`gh` to inspect real GitHub Actions logs, identified that the problem was not
only in `rsync` but also in cleanup of container-owned generated files, then
patched the scripts and checked the workflow again.

## What Agentic AI Meant In This Process

In this project, agentic AI meant that models were not used only for passive
suggestions. They executed complete development loops:

```text
request -> read context -> plan -> modify -> test -> explain -> iterate
```

However, AI was not treated as the final source of truth. We kept an
engineering process around it:

- clear tasks;
- limited branch access;
- incremental changes;
- local tests;
- CI;
- staging;
- human review;
- documentation for important decisions.

This combination mattered. Agents can accelerate development, but quality comes
from boundaries, verification, and feedback.

## Observed Benefits

Using agentic tools helped especially with:

- understanding a large codebase faster;
- turning ambiguous requirements into concrete steps;
- generating initial patches quickly;
- finding the root cause of CI failures from real logs;
- writing technical documentation;
- preserving context between iterations through Markdown files;
- exploring multiple approaches without blocking the main development flow.

## Limitations And Controls

Models can be wrong. For that reason, the process included controls:

- changes were not accepted without verification;
- agents were expected not to modify files unrelated to the task;
- relevant tests were run for each change;
- Git status was checked before and after modifications;
- secrets and API keys were not exposed;
- staging and CI were used as final validation.

These limits made it possible to use AI as a technical collaborator, not as an
unsupervised code-generation mechanism.

## Conclusion

Sandcastle was developed with agentic AI in a controlled engineering process.
Tools such as Codex, Devin AI, Antigravity, Gemini CLI, and GitHub Copilot were
integrated through clear tasks, dedicated branches, incremental prompts, audits,
documentation, and automated verification.

The result is not only a project that uses AI in its functionality, but also a
project built through a process that demonstrates how agentic AI can be used
responsibly during software development.
