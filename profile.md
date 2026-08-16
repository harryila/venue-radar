# Harry's Venue Profile

> This file is the classifier's spec. Edit it freely — the radar re-judges every
> active venue automatically on the next run after any change to this file.

## Who I am

UC Berkeley student and AI researcher. I do empirical LLM research at the
intersection of interpretability, agents, and evaluation, and the direction I
most want to publish in is **AI alignment and safety**. Small-team, open-weights,
modest-compute work (70B-scale inference and analysis, not pretraining).

## Work in hand

1. **Belief updating and calibration in LLM agents.** Llama-3.1-70B playing
   Texas Hold'em against Bayesian reference oracles: how badly agents update
   beliefs on new evidence and how miscalibrated their confidence is.
   Themes: calibration, uncertainty, agent decision-making, agent evaluation,
   reasoning under uncertainty.
2. **Retrieval / QR attention-head universality and fragility.** Which heads
   implement retrieval, how universal they are across models and tasks,
   cross-task ablations. Themes: mechanistic interpretability, attention heads,
   circuit-level analysis, attributing model behavior. Under review at ARR
   (May 2026 cycle), so ARR-commitment tracks on interp topics are live options.

Natural next steps: interpretability of agent behavior, calibration and
uncertainty in LLMs, agent evaluation and oversight, safety-relevant evals.
I would start a new project for a venue squarely in these areas — I've missed an
AAAI AI Alignment track and the COLM AIMs workshop before; never again.

## Background (list, never ping)

Startup work in geospatial data and AI training-data infrastructure; production
ML engineering; earlier ML research at NASA Langley and Stanford. This makes
geospatial-ML, data-infrastructure, and datasets/benchmarks venues worth
listing as adjacent — never core on their own.

## Core — open an issue and email me

A venue is core only if its **own topic** is one of these:

- Alignment, safety, interpretability, oversight, red-teaming, or evaluation
  **of AI systems themselves** — at any reputable host, including small
  dedicated venues (an AIMs-style workshop counts). "Safe/trustworthy X" where
  X is an application domain (medical imaging, quantum, audio, child safety,
  autonomous driving) is adjacent, not core.
- Understanding, evaluating, or verifying **LLM agent behavior**: agent
  evaluation, reliability, oversight, interpretability of agents,
  decision-making or reasoning under uncertainty, calibration.
- Main tracks (not workshops, industry, SRW, or demos) of NeurIPS, ICML, ICLR,
  ACL, EMNLP, NAACL, EACL, AACL, COLM, AAAI.

Applied or systems-flavored agent workshops (agents for enterprise, OS, web,
robotics, social simulation, resource efficiency, small models for agents,
continual learning for agents, memory for agents) are **adjacent**: agents are
the setting, not the question I work on.

**Core must be rare** — roughly 10–25 of ~340 open venues on a typical day.
Each core verdict lands on my phone. When in doubt → adjacent.

## Adjacent — list in venues.md, never ping

Everything else in ML/NLP: general LLM/NLP/ML workshops at top venues whose
topic is not one of the core bullets (math reasoning, multilingual, speech,
diffusion LMs, post-training, world models, efficient DL, VLMs); applied-agent
workshops; industry tracks, student research workshops, demos, tutorials,
mentorship programs; regional or national AI/ML conferences; shared tasks
(unless the task itself is calibration/eval/interp of LLMs); geospatial ML,
remote sensing, data infrastructure, datasets and benchmarks; robotics or
vision with a foundation-model angle; "trustworthy X" application venues.

## No — don't list

Pure linguistics or psycholinguistics, argumentation theory, pure theory
unrelated to ML, hardware/circuits, networking/systems without ML, pure HCI,
digital humanities, medicine/biology without an ML-methods angle, quantum ML;
placeholder or dormant venues (deadlines years away, empty pages).

## Rules the classifier must follow

- Judge the venue's topic only. Deadline proximity, team size, and whether a
  submission is "feasible" are **not** relevance signals — never cite them.
- Being at a top venue is not enough for a workshop; being about agents is not
  enough; a safety/trust word in the name is not enough. Explain in `why` which
  core bullet the venue's own topic hits.
- ARR-commitment, direct-submission, and competition tracks of the same
  workshop get the same verdict as the workshop's topic; say which track it is.
- Unsure between adjacent and no → adjacent. Unsure between core and adjacent
  → adjacent.

## Calibration examples (from a real day)

- **core**: Interpreting Agent Behavior (NeurIPS ws), BlackboxNLP, Interpretability
  as a Science, Interpretability for Discovery, Attributing Model Behavior
  (ATTRIB), Reliable Evaluation for LMs (JUDGe), Uncertainty-Aware NLP, Who
  Verifies the Agents, Evaluation of Interactive Agents, Agents in the Wild:
  Safety, Dynamic Alignment in Human-AI Systems, Pluralistic Value Alignment
  of LLMs, any NeurIPS/ICML/ICLR/COLM/ACL-family main track.
- **adjacent**: Agentic Web, Meta-Agents, SLM-Agents, AgenticOS, Continual
  Learning for Enterprise Agents, MATH-AI, DeepMath, Multilingual
  Representation Learning, Diffusion Language Models, Pre-to-Post-Training,
  Trustworthy Multimodal Agents @ IEEE BigData, Child Safety in AI, EACL
  Industry Track, EACL SRW, INLG, AAMAS.
- **no**: Gender Bias in NLP, Widening NLP, WiML, Secure Quantum ML,
  Trustworthy FMs in Medical Imaging, Legal NLP, FinNLP, Argumentation &
  Language, morphosyntax workshops.
