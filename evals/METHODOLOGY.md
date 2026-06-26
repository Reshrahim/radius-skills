# Evaluation Methodology for the Radius App-Modeling Skill

## Purpose

This evaluation measures whether the Radius `app-modeling` skill can reliably guide an AI agent from a real application repository to a valid Radius application model.

The central question is simple: given a real app repository, can the skill consistently steer the agent to generate a correct `.radius/app.bicep`?

This is not meant to benchmark one model or one tool. The goal is to evaluate the skill as an instruction artifact across different repositories, agents, models, tools, and repeated trials.

## Why this methodology is needed

The app-modeling skill is not a deterministic function. It is a markdown instruction set loaded by an AI agent. The agent reads source files, interprets deployment artifacts, reasons about dependencies, and writes a Radius model. Two runs can produce different outputs.

Because of that, the evaluation should not depend on raw golden-file diffs. There can be more than one valid Radius model for the same repository. In early phases, a golden `app.bicep` can be used as a reference model, but comparison should still be property-based rather than text-based.

The evaluation should check whether the right services were found, whether real dependencies were detected, whether those dependencies were mapped to valid Radius resource types, whether unsupported types were avoided, whether the generated Bicep compiles, and whether the agent followed the skill contract.

Multiple trials are required as the evaluation matures. A skill that works once but fails often is not reliable enough.

## Test corpus

The corpus should use live, public GitHub repositories, pinned to specific commit SHAs for reproducibility. Live repositories matter because they capture the messy reality of application development better than hand-crafted fixtures.

The starting corpus should retain representative repositories across baseline and advanced coverage:

| Tier     | Pattern                      | Repository                                                                                      |
| -------- | ---------------------------- | ----------------------------------------------------------------------------------------------- |
| Baseline | Enterprise / Java-Spring app | [spring-projects/spring-petclinic](https://github.com/spring-projects/spring-petclinic)         |
| Baseline | Microservices                | [dockersamples/example-voting-app](https://github.com/dockersamples/example-voting-app)         |
| Baseline | Event-driven                 | [dockersamples/kafka-development-node](https://github.com/dockersamples/kafka-development-node) |
| Baseline | Web app                      | [docusealco/docuseal](https://github.com/docusealco/docuseal)                                   |
| Baseline | Web app                      | [solidtime-io/solidtime](https://github.com/solidtime-io/solidtime)                             |
| Baseline | Trader X                     | [finos/traderX](https://github.com/finos/traderX)                                               |
| Baseline | Helm-primary app             | [apache/airflow](https://github.com/apache/airflow)                                             |
| Advanced | Data pipeline                | [mage-ai/mage-ai](https://github.com/mage-ai/mage-ai)                                           |
| Advanced | AI/ML app                    | [langgenius/dify](https://github.com/langgenius/dify)                                           |
| Advanced | Real-time app                | [RocketChat/Rocket.Chat](https://github.com/RocketChat/Rocket.Chat)                             |
| Advanced | IoT app                      | [thingsboard/thingsboard](https://github.com/thingsboard/thingsboard)                           |

This set should test more than technology coverage. It should also cover different repository shapes: bare source, Dockerfiles, Docker Compose, Kubernetes YAML, Kustomize, Helm, and Terraform.

## What gets evaluated, scored, and acted on

The evaluation scores the skill on the four things it is expected to guide the agent through. Each trial should produce a score for these areas, plus an overall result.

| Evaluation area           | What we measure                                                                                                                                                                                                      | What a good result looks like                                                                                       | How we act on failures                                                                                                                          |
| ------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| Application understanding | Whether the agent correctly identifies the app structure, services, runtime entry points, ports, environment variables, service relationships, and backing dependencies.                                             | The generated model reflects the real services and dependencies present in the repository.                          | If visible services or dependencies are missed, improve the source-analysis guidance in `SKILL.md`.                                             |
| Resource type mapping     | Whether detected dependencies are mapped to supported Radius resource types, and unsupported dependencies are recognized as catalog gaps.                                                                            | Supported dependencies use the right Radius types. Unsupported dependencies are not invented as fake resources.     | If mapping is wrong, clarify mapping guidance or update the resource type catalog. If the dependency is unsupported, track it as a catalog gap. |
| Model generation          | Whether the agent generates a valid `.radius/app.bicep` that compiles, uses the right Radius extensions and schemas, wires connections correctly, handles secrets safely, and avoids hardcoded cloud infrastructure. | The output is valid, usable Radius Bicep. A compile failure hard-gates this score because the model cannot be used. | If Bicep does not compile or uses the wrong schema, fix examples, validation guidance, or generation rules in the skill.                        |
| Skill conformance         | Whether the agent follows the explicit `SKILL.md` instructions, including output location, supported resource types, connection placement, validation expectations, and guardrails against unsupported schemas.      | The output follows the skill contract, not just a generally plausible Radius pattern.                               | If the agent repeatedly ignores the same instruction, strengthen or clarify that guardrail in `SKILL.md`.                                       |

As the evaluation expands beyond the initial baseline target, scores should roll up by repository, application pattern, runtime, model, and tool. This shows whether the skill is broadly reliable or only works in specific conditions.

The report should include a short scoreboard, per-repository results, recurring failure patterns, catalog gaps, and recommended updates. The goal is not just to say whether a run passed or failed, but to show what needs to improve next: the skill instructions, the resource type catalog, the recipes, or the evaluation corpus.

A real dependency without a supported Radius resource type should be recorded as a catalog gap, not treated as a pure agent failure. A repeated failure across trials or runtimes should be treated as a skill weakness, not a one-off agent miss.

## Evaluation flow and feedback loop

The evaluation should run in phases. Every phase evaluates the same four vectors: application understanding, resource type mapping, model generation, and skill conformance. What changes by phase is the scope, automation level, and confidence bar.

| Phase                                   | Scope                                                                                                                                                               | How the four vectors are evaluated                                                                                                                                                                       | Output                                                                                                                      |
| --------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| Phase 1: Copilot baseline evaluation    | Run Copilot on a baseline target and grade the generated `app.bicep` against two non-circular sources of truth (no AI-authored golden): a schema **oracle** vendored from `resource-types-contrib`, and a small per-app **target spec**.                                                                | Each of the four vectors is scored deterministically (1 point per check) against the oracle + target spec. Generation is repeated **N times** and aggregated per check to separate consistent skill bugs from flaky ones. A layer of **AI agents** (4 vector agents + a remediation agent) then audits the same vectors to surface faults determinism misses and write a prioritized skill-fix plan. A separate **invocation test** checks whether natural prompts actually trigger the skill (recall/precision). | Confirms whether the current Copilot flow reliably generates a correct Radius model, and produces an actionable, root-caused list of skill fixes. |
| Phase 2: Automated baseline evaluation  | Use the same baseline target, but automate the comparison and scoring against the oracle + target spec.                                                                       | Parse the generated `app.bicep` into comparable properties, then run automated checks for compile, schema, connection shape, resource types, and skill violations.                      | Produces repeatable scoring, structured results, and recurring failure patterns for the baseline.                           |
| Phase 3: Broader corpus evaluation      | Expand to the other baseline and advanced repositories, where correctness is derived from repo artifacts and expected properties rather than a golden file for each repo. | Score the same four vectors, but separate skill failures from catalog gaps. Unsupported dependencies should be recorded as missing resource types or recipes, not treated as pure failures.              | Identifies hard app patterns, missing catalog coverage, and skill instructions that need improvement.                       |
| Phase 4: Runtime reliability evaluation | Run the same evaluation across agents, models, and tools beyond the initial Copilot path.                                                                           | Compare scores and variance across runtimes for the same vectors and evaluation targets.                                                                                                                 | Shows whether the skill is portable as an instruction artifact or tightly coupled to one runtime.                           |

The end goal is a feedback loop. Each run should point to the next action: update the app-modeling skill, expand the resource type catalog, improve recipes, fix samples, or adjust the evaluation corpus.

## Phase 1 implementation

Phase 1 is implemented under [`evals/phase1/`](phase1/) (see its README for
commands). It refines the original golden-file idea into a **non-circular,
agent-assisted** design:

- **Ground truth without an AI golden.** Schema correctness is graded against a
  schema **oracle** — the per-type `test/app.bicep` files vendored from
  `resource-types-contrib` at a pinned SHA (supported types, API versions, valid
  properties). Per-app completeness is graded against a small repo-derived
  **target spec** that describes the application in app terms only (its `service`
  and `datastore` components, by technology, and their connections) — it never
  hardcodes Radius types, so the technology→type mapping (and catalog-gap
  detection) stays in one oracle-grounded place and the key is durable across
  catalog changes. Because the answer key is project-maintained rather than
  AI-authored, the eval can catch cases where the **skill itself has drifted**
  from current schemas; a faithful agent then correctly fails.

- **Grounded target derivation (no manual answer key).** Targets are produced by
  a derivation agent, not hand-written: the component/connection skeleton is
  extracted **deterministically** from repo artifacts (`docker-compose`/k8s), then
  a constrained agent only labels each component's kind/technology — it cannot add
  or drop a component or name a Radius type. The result is written unratified and
  pinned after a one-time human review, removing the manual step without
  reintroducing AI-circularity.

- **Multi-run aggregation.** Generation is stochastic, so each target is run
  `N` times (default 5) and scored per check. Results are aggregated into
  pass-rates that separate **consistent failures** (fail every run = real skill
  bug) from **flaky** ones, so fixes are ranked by how reliably a check fails.

- **Agent design (deterministic score + AI fault-finding).** The deterministic
  scorer produces the trustworthy number; a layer of agents finds and explains
  faults the hand-coded checks cannot:
  - a **generation agent** (the skill under test) writes `.radius/app.bicep`;
  - four **vector agents** (one per vector, run in parallel) each take the
    deterministic per-check result as a trusted seed, add AI judgment for what
    determinism misses (semantic mis-maps, dropped components, partial overfit),
    and trace every fault to a skill `file:line` with a before→after fix;
  - a **remediation agent** clusters faults by root cause and writes a minimal,
    prioritized skill-fix plan.
  The deterministic score stays the source of truth — agents extend and explain,
  they never silently re-grade — and agents use deterministic tools (grep,
  reading the oracle) so claims are tool-backed.

- **Coverage / dropout detection.** The understanding and generation agents
  enumerate every source service and check all runs, catching omissions (e.g. a
  redis queue dropped in one run) that an omission-tolerant deterministic check
  would otherwise reward as "handled."

- **Skill invocation test.** Separate from the four content vectors (which assume
  the skill is loaded), this stages the skill so Copilot can auto-discover it but
  never names it in the prompt, then runs positive and negative phrasings to
  measure trigger **recall** and **precision**.
