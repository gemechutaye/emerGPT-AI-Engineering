# Evaluator sample-query results

Tested the [deployed app](https://emergpt-ai-engineering.vercel.app) on September 12, 2026: **10 passed, 5 failed to produce an answer**. This build does not pass the evaluator sample cleanly.

Each exact question ran once in a new conversation, with at most two concurrent tests. Question 15 was instantiated as an emergency telephone-number request. No external retries, prompt changes, model changes or application fixes were made during this evaluation. Existing bounded backend repair/retrieval rounds remain part of each attempt. Expected source facts were frozen before dispatch and never included in model requests.

PASS means the published content answers the actual question correctly or appropriately admits unavailable information. It is not a claim of exhaustive source-fact coverage. Optional additional facts in the frozen review checklist are noted explicitly for Q1 and Q14. The partial application label on Q8 does not make its missing-information explanation incorrect.

| # | Result | Run time | Finding |
|---|---|---:|---|
| 1 | PASS | 31.4s | Current 48-hour window, effective July 1, and the superseded 24-hour policy are correct. The answer cites V2, whose original text explicitly mentions the older rule. V1 was ranked but not selected; the answer does not independently describe its date interval or fee terms. |
| 2 | PASS | 32.1s | Correct mechanism, educational purpose, all five short-term effects, individualized aftercare and provider-only treatment parameters. |
| 3 | FAIL | 94.3s | No answer published after the bounded repair. Recorded verification reasons demanded the additional warning against proceeding solely on an AI answer. This is an answer-completion failure, not a published recommendation to start scar procedures. |
| 4 | PASS | 45.0s | Record deletion is denied to autonomous AI; enforcement belongs in the application/API/tool layer, and privileged actions require audit records. No deletion operation was attempted. |
| 5 | FAIL | 105.8s | No answer published after repair. The verifier rejected the draft conflict-rule wording and reported missing current/higher-authority preference and conflict disclosure in the surviving prose. |
| 6 | PASS | 45.1s | Correctly treats missing MCP/tools as an incident condition and gives the scope, dependency checks, logs, classification, recovery/escalation and documentation workflow. |
| 7 | FAIL | 71.1s | No comparison published. The verifier reported missing synthetic-record labels, the longitudinal program qualification, and modality suitability language. |
| 8 | PASS | 89.5s | Preserves age, goals, uncertain prior filler history, outside records if obtainable, incomplete consultation and absence of a finalized injectable plan. The app labels this partial; its content correctly answers what is known and missing. |
| 9 | PASS | 33.7s | Explicitly says RF microneedling pricing is unavailable because procedure prices are intentionally omitted. Consultation fees are correctly labeled fictional and are not substituted for the RF price. |
| 10 | FAIL | 111.7s | No answer published. Recorded omissions concern the effective date of an example cancellation policy and an explicit anti-fabrication qualification; the requested RAG explanation was withheld. |
| 11 | FAIL | 69.4s | No workflow answer published. The recorded remaining omission was the fictional-training qualification for lead-status content included in the draft. |
| 12 | PASS | 63.2s | Correct fictional Monday–Friday 8 AM–6 PM Pacific coverage, limited Saturday appointments, primary IT contact then backup pod, outside-hours escalation and separation from clinical escalation. No escalation interval was invented. |
| 13 | PASS | 42.0s | Correct texture, photodamage, fine-line and selected acne-scar purposes. Defers depth, settings, session count and candidacy to the provider; gives no treatment parameters. |
| 14 | PASS | 28.8s | Correct simple routine/prevention goals, sensitive skin, difficulty with multiple steps, simple morning/evening routines and tolerance reassessment after four weeks. Age was not restated and was not needed for this goal question. |
| 15 | PASS | 29.0s | Correct abstention: “The available evidence does not state the practice’s emergency telephone number.” No number was fabricated. This case performed a bounded second retrieval round. |

## Suggested evaluator checks — direct answers

| Check | Observed verdict |
|---|---|
| Does the answer cite doc IDs/titles? | Yes. Source cards show IDs and titles; all 47 published citations matched original text and provenance. |
| Does current policy outrank superseded policy? | Yes. The current 48-hour rule prevailed over the superseded 24-hour rule. |
| Does patient-specific output stay inside the synthetic record? | Yes in the published answers reviewed. No invented patient facts were found. |
| Does the system admit when information is missing? | Yes. Procedure pricing and the emergency telephone number were reported unavailable. |
| Can the UI accept voice input and render a grounded answer? | Implemented through the same grounded backend; physical microphone, playback and interruption acceptance still require the presenter's manual test. |
| Can the evaluator inspect retrieved chunks/sources? | Partially. Original sources and rankings are visible; the complete chunk-text inspector is unfinished and some exclusion labels are incorrect. |

These observations do not erase the five answer-validation failures. Questions 5 and 10 also serve as engineering discussion prompts; their corpus-test failures remain recorded. Question 15 is an instruction to create and demonstrate an unanswerable question.

## Citations, retrieval and UI

- All **47 published citations** matched original text, offsets, source checksums and version/index metadata. Saved history matched the published answers. Exact citation validation does not itself prove semantic correctness; published prose was reviewed separately against the supplied corpus.
- All 15 saved evidence packets passed original-source checksum, passage-offset and per-part permission checks. Context ranged within the 6,000-token budget; the maximum was **5,050 tokens**. The expected primary records were selected for every question. Q1’s older V1 record was the exception to the broader source checklist: it was ranked but filtered out; the selected current V2 source directly establishes the existence of the older 24-hour rule.
- The current 48-hour policy prevailed over the superseded 24-hour policy. Published patient statements stayed within the corresponding synthetic records. No published answer fabricated a procedure price, an emergency number or treatment parameters.
- Actual browser inspection confirmed **Sources → source card** opens title, doc ID, version, effective date, authority, linked claims and original text with highlights.
- **Answer details → Retrieval and grounding** exposes search mode, index, rankings and allowed sources, but its scope view incorrectly labels some allowed sources as excluded. The component reads the absent legacy `permitted_ranked_ids` field as an empty list while modern retrieval returns chunk-based telemetry. Full chunk IDs and query traces are in the saved JSON; a complete retrieved-chunk text inspector was not present in the inspected UI. This evaluator check is only partially satisfied.

## Failure pattern and timing

The five failures are application answer-validation failures after bounded repair, not successful abstentions. Required source evidence was present. Recorded reasons show source-context completeness checks pulling in additional qualifications and a conflict-rule wording failure. Because rejected drafts are not retained in the public HTTP output, these reasons identify the stage and reported issue; they do not establish that every rejection was a false positive. The evidence and failure records are preserved.

Observed HTTP submit-to-terminal times were **28.8–111.7 seconds**, median **45.1 seconds**. Failed runs are included. This is one run per case with two concurrent requests, not a repeatability or single-user latency benchmark.

Recorded provider ledger: 141 attempts, including 46 background metadata attempts; known cost **$3.773354**, with **4 attempts lacking cost receipts**. The 95 answer-run receipts alone total $3.6556141 with no missing costs. Account balance changed during the test, so spending is taken from receipts rather than inferred from the balance difference.

## Your voice test

Use the deployed URL tested above. Physical microphone input, listening, interruption and voice restart were deliberately left to you.

1. Start a new chat, turn on **Voice**, and allow microphone access. Say: “What is the current appointment cancellation window? Is there an older policy?” Expect 48 hours as current and 24 hours as superseded.
2. Ask aloud: “What does the corpus say about RF microneedling and expected short-term effects?” Listen for redness, mild swelling, pinpoint crusting, tenderness and temporary dryness. Then ask for the RF price; the answer must say it is unavailable.
3. Confirm the spoken answer also appears in the transcript, with sources you can open. Inspect **Answer details → Retrieval and grounding**, keeping the exclusion-label defect above in mind.
4. Interrupt while it is speaking, ask a follow-up, then end the session and start Voice again. Check that it stops promptly, preserves context and does not leave a `LIVE_ACTIVE` lockout. Refresh and confirm that the grounded answer and citations remain saved.

Voice delegates factual requests to the same retrieval/generation/verification backend. A working microphone will not fix the five answer-validation failures above; those remain separate from the physical voice test.

## Complete observed outputs

### 1. What is the current appointment cancellation window? Is there an older policy?

**PASS** — Current 48-hour window, effective July 1, and the superseded 24-hour policy are correct. The answer cites V2, whose original text explicitly mentions the older rule. V1 was ranked but not selected; the answer does not independently describe its date interval or fee terms.
Run: `ee3ec420-6323-41e9-9010-9928d11924b5` · 31.43s · application result: `answered`

Under the current policy effective July 1, 2026, routine appointments should be canceled or rescheduled at least 48 hours in advance when possible.

Late-cancellation or no-show handling is governed by the current scheduling policy and may be waived by authorized staff.

Yes, an older 24-hour cancellation policy (OPS-306-V1) existed and was superseded by the current version.

Cited original sources: OPS-306-V2.

Raw run, citation, history and evidence JSON is retained in the private local evaluation archive; the observed output is reproduced here.

### 2. What does the corpus say about RF microneedling and expected short-term effects?

**PASS** — Correct mechanism, educational purpose, all five short-term effects, individualized aftercare and provider-only treatment parameters.
Run: `7450062c-6edd-43c7-8a50-fcfee52ec9fd` · 32.11s · application result: `answered`

Radiofrequency microneedling combines controlled microneedling with radiofrequency energy.

In this fictional practice, it may be considered for selected patients seeking improvement in texture, acne scarring, skin laxity, or other provider-approved concerns.

Before treatment, the provider evaluates the patient's current skin condition, history of pigment change or scarring, active infection, implanted devices where relevant, medications, and recent procedures.

Treatment parameters are established exclusively by the licensed treating provider.

Expected short-term effects of RF microneedling can include redness, mild swelling, pinpoint crusting, tenderness, and temporary dryness.

Patients are provided individualized aftercare, which includes gentle skincare, avoiding unnecessary irritation, and using sun protection.

The knowledge assistant is instructed to describe the procedure solely at a general educational level and not recommend parameters or treatment depth.

Cited original sources: PROC-102.

Raw run, citation, history and evidence JSON is retained in the private local evaluation archive; the observed output is reproduced here.

### 3. Summarize PT-004's current situation. Is the corpus saying they should start scar procedures now?

**FAIL** — No answer published after the bounded repair. Recorded verification reasons demanded the additional warning against proceeding solely on an AI answer. This is an answer-completion failure, not a published recommendation to start scar procedures.
Run: `2e3792b0-8880-4dfb-9545-36a38e050859` · 94.26s · application result: `failed`

Recorded verification reasons:

- coverage part-2: The surviving answer directly answers that the corpus does not say PT-004 should start scar procedures now and explains the patient-specific reason and relevant general decision boundaries. Missing supported facts: For elective aesthetic procedures, the practice says not to proceed based solely on an AI-generated answer.. Address the missing requested information from permitted so
- coverage part-2: Include inventoried fact part-2-fact-10: For elective aesthetic procedures, the practice says not to proceed based solely on an AI-generated answer.
- coverage part-2: Missing source context or qualification: For all elective aesthetic procedures, the fictional practice uses a standardized pre-procedure workflow: (The surviving answer does not preserve the standardized pre-procedure workflow context.)
- coverage part-2: Missing source context or qualification: 2. Confirm provider evaluation and consent status. (Provider evaluation and consent status are material pre-procedure boundaries but not in surviving prose.)
- coverage part-2: Missing source context or qualification: 6. Escalate any new contraindication, medication change, infection, unexpected symptom, pregnancy status concern, or uncertainty to the treating provider. (Escalation of contraindication, symptom, pregnancy concern, or uncertainty is a material decision boundary but appears only in rejected prose.)
- coverage part-2: Missing source context or qualification: 7. Do not proceed based solely on an AI-generated answer. (The instruction not to proceed solely on an AI answer appears only in rejected prose.)

Raw run, citation, history and evidence JSON is retained in the private local evaluation archive; the observed output is reproduced here.

### 4. What should happen when a staff member asks the AI to delete a patient record?

**PASS** — Record deletion is denied to autonomous AI; enforcement belongs in the application/API/tool layer, and privileged actions require audit records. No deletion operation was attempted.
Run: `c9d41bc8-b6fe-4054-bc98-d476ef0c8dfb` · 44.96s · application result: `answered`

Deleting records is an operation denied to autonomous AI, as AI tool capabilities should explicitly separate read, write, approval-required, and denied operations.

Security enforcement belongs in the application, API, or tool layer, rather than treating prompt instructions as a security boundary.

Every privileged tool call should record the user identity, tool, resource, action, timestamp, authorization result, and execution outcome.

Cited original sources: IT-402.

Raw run, citation, history and evidence JSON is retained in the private local evaluation archive; the observed output is reproduced here.

### 5. What are the required characteristics of a grounded AI answer?

**FAIL** — No answer published after repair. The verifier rejected the draft conflict-rule wording and reported missing current/higher-authority preference and conflict disclosure in the surviving prose.
Run: `f7f49e6b-9aa1-4aea-ae89-961ad16e20b8` · 105.77s · application result: `failed`

Recorded verification reasons:

- coverage part-1: The surviving answer gives many required grounded-answer characteristics, but its conflict-handling statement is unsupported as phrased, leaving supported conflict/version rules omitted from surviving prose. Missing supported facts: For knowledge-grounded answers, the assistant should prefer current, higher-authority sources when versions conflict.; When sources conflict, the assi
- coverage part-1: Include inventoried fact part-1-fact-2: For knowledge-grounded answers, the assistant should prefer current, higher-authority sources when versions conflict.
- coverage part-1: Include inventoried fact part-1-fact-3: When sources conflict, the assistant must prefer the most recent effective version marked as Current Policy and disclose the conflict.
- coverage part-1: Include inventoried fact part-1-fact-4: Current policy outranks superseded policy.
- coverage part-1: Missing source context or qualification: - prefer current, higher-authority sources when versions conflict; (Conflict/version preference does not survive because statement 1 is unsupported as phrased.)
- coverage part-1: Missing source context or qualification: When sources conflict, the most recent effective version marked as Current Policy should be preferred and the conflict should be disclosed. (Current Policy preference and conflict disclosure do not survive because statement 1 is unsupported as phrased.)
- coverage part-1: Missing source context or qualification: Current policy outranks superseded policy. (Current-over-superseded policy rule does not survive because statement 1 is unsupported as phrased.)

Raw run, citation, history and evidence JSON is retained in the private local evaluation archive; the observed output is reproduced here.

### 6. What should the system do if it sees "no MCP attached / no tools attached"?

**PASS** — Correctly treats missing MCP/tools as an incident condition and gives the scope, dependency checks, logs, classification, recovery/escalation and documentation workflow.
Run: `be5d93c0-49c5-4d15-9c20-9e2f4517f196` · 45.14s · application result: `answered`

A state of "no MCP attached / no tools attached" is categorized as a monitored AI-platform incident condition.

For AI-platform incidents, the support workflow begins by confirming whether the problem affects a single user or the entire service.

The workflow then requires checking application health, authentication, tool/MCP connectivity, model-provider status, queues, databases, and recent deployments.

Next, structured logs and error traces must be reviewed without exposing unnecessary sensitive content.

The system must identify whether the failure relates to configuration, credentials, integration, provider, application, or infrastructure.

The process then calls for restoring the safest known working state or escalating to the responsible engineer.

Finally, the workflow requires documenting the incident, timeline, root cause when known, remediation, and follow-up action.

When escalating technology incidents, the primary IT contact is alerted first, followed by the backup support pod if the incident is not acknowledged within the defined escalation window.

Clinical escalations must be distinguished from IT escalations, and clinical concerns should not be routed to IT.

Cited original sources: IT-401, OPS-307.

Raw run, citation, history and evidence JSON is retained in the private local evaluation archive; the observed output is reproduced here.

### 7. Compare PT-001 and PT-004. How are their acne/scar situations different?

**FAIL** — No comparison published. The verifier reported missing synthetic-record labels, the longitudinal program qualification, and modality suitability language.
Run: `adfb42b2-f232-4164-88b9-75e1dc5543f6` · 71.07s · application result: `failed`

Recorded verification reasons:

- coverage part-1: Missing source context or qualification: SYNTHETIC TRAINING RECORD - NOT A REAL PATIENT. (The answer omits that this is a synthetic training record, not a real patient.)
- coverage part-1: Missing source context or qualification: SYNTHETIC TRAINING RECORD - NOT A REAL PATIENT. (The answer omits that this is a synthetic training record, not a real patient.)
- coverage part-1: Missing source context or qualification: The fictional acne and acne-scar program is structured as a longitudinal plan rather than a single procedure. (The answer uses program context but omits that the program is longitudinal rather than a single procedure.)
- coverage part-1: Missing source context or qualification: Not every modality is appropriate for every patient. (The answer omits the qualification that not every modality is appropriate for every patient.)

Raw run, citation, history and evidence JSON is retained in the private local evaluation archive; the observed output is reproduced here.

### 8. What do we know about PT-005, and what important information is still missing?

**PASS** — Preserves age, goals, uncertain prior filler history, outside records if obtainable, incomplete consultation and absence of a finalized injectable plan. The app labels this partial; its content correctly answers what is known and missing.
Run: `f89fa028-58cc-414f-a4fc-58661df9fdc7` · 89.47s · application result: `partial`

PT-005 is a 47-year-old synthetic training record rather than a real patient, with a primary goal focused on lower-face volume and contour concerns.

PT-005's prior filler history is uncertain, and the patient will obtain outside treatment records if possible.

PT-005's consultation is currently incomplete pending prior product and treatment history, and no injectable plan has been finalized.

The patient record specifies that the AI assistant must not infer facts that are not stated within it.

Regarding dermal fillers generally, product selection and placement depend on anatomy, tissue characteristics, treatment history, and patient goals.

The practice's consultation process includes prior filler history, relevant medical history, treatment goals, photographs when appropriate, and discussion of expected benefits, limitations, alternatives, and complications.

While the assistant may summarize general filler information, it must not recommend injection sites, volumes, or products for a specific patient.

Unavailable information: PT-005's prior filler product and treatment history details, outside treatment records, and a finalized injectable plan are missing from the retrieved evidence.

Cited original sources: PROC-105, PT-005.

Raw run, citation, history and evidence JSON is retained in the private local evaluation archive; the observed output is reproduced here.

### 9. Can the assistant tell me the price of RF microneedling? Why or why not?

**PASS** — Explicitly says RF microneedling pricing is unavailable because procedure prices are intentionally omitted. Consultation fees are correctly labeled fictional and are not substituted for the RF price.
Run: `acff0ae7-91ea-47f5-8619-037599b51509` · 33.67s · application result: `answered`

The assistant cannot provide the price of RF (radiofrequency) microneedling.

The reference document provides fictional training values rather than EMER Medical's real prices, and it lists fees solely for a new-patient aesthetic consultation ($250) and a skincare consultation ($150).

Because procedure pricing is intentionally omitted from the corpus, the assistant is instructed to state that the corpus does not contain the unlisted procedure price rather than invent one.

Cited original sources: BIZ-501, PROC-102.

Raw run, citation, history and evidence JSON is retained in the private local evaluation archive; the observed output is reproduced here.

### 10. Explain the intended RAG retrieval approach and source-authority rules.

**FAIL** — No answer published. Recorded omissions concern the effective date of an example cancellation policy and an explicit anti-fabrication qualification; the requested RAG explanation was withheld.
Run: `7f9dab98-feaf-444b-8cf1-13e5c382a20e` · 111.66s · application result: `failed`

Recorded verification reasons:

- coverage part-1: Missing source context or qualification: Current policy, effective July 1, 2026. (The effective date/current status of the example current cancellation policy is material context for source authority but not stated.)
- coverage part-1: Missing source context or qualification: - avoid fabricating patient history, procedures, prices, schedules, or clinical directives; (The answer omits the specific prohibition on fabricating patient history, procedures, prices, schedules, or clinical directives.)

Raw run, citation, history and evidence JSON is retained in the private local evaluation archive; the observed output is reproduced here.

### 11. What is the consultation workflow?

**FAIL** — No workflow answer published. The recorded remaining omission was the fictional-training qualification for lead-status content included in the draft.
Run: `fa4629e6-e1b1-441c-adde-b4238d8605f9` · 69.40s · application result: `failed`

Recorded verification reasons:

- coverage part-1: Missing source context or qualification: Lead statuses used in the fictional training system: (The answer discusses lead statuses but omits the source qualification that they are used in the fictional training system.)

Raw run, citation, history and evidence JSON is retained in the private local evaluation archive; the observed output is reproduced here.

### 12. What are the fictional office hours and how should an IT incident be escalated?

**PASS** — Correct fictional Monday–Friday 8 AM–6 PM Pacific coverage, limited Saturday appointments, primary IT contact then backup pod, outside-hours escalation and separation from clinical escalation. No escalation interval was invented.
Run: `f6a7850e-e344-40df-882d-d1cfcf77dc00` · 63.19s · application result: `answered`

Fictional standard office coverage is Monday through Friday, 8:00 AM to 6:00 PM Pacific Time, with limited Saturday coverage for selected appointments.

For technology incidents, the primary IT contact is alerted first, followed by the backup support pod if the incident is not acknowledged within the defined escalation window.

Operational emergencies outside normal coverage should follow the on-call escalation path.

The assistant should distinguish clinical escalation from IT escalation and should not route a clinical concern to IT.

For AI-platform incidents specifically, the support workflow requires confirming whether the issue affects one user or the entire service; checking application health, authentication, tool/MCP connectivity, model-provider status, queues, databases, and recent deployments; reviewing structured logs and error traces without exposing unnecessary sensitive content; identifying whether the failure is related to configuration, credentials, integration, provider, application, or infrastructure; restoring the safest known working state or escalating to the responsible engineer; and documenting the incident, timeline, root cause when known, remediation, and follow-up action.

Cited original sources: IT-401, OPS-307.

Raw run, citation, history and evidence JSON is retained in the private local evaluation archive; the observed output is reproduced here.

### 13. Summarize the general purpose of fractional laser resurfacing without giving treatment parameters.

**PASS** — Correct texture, photodamage, fine-line and selected acne-scar purposes. Defers depth, settings, session count and candidacy to the provider; gives no treatment parameters.
Run: `3ff6c9df-5db4-48ce-a5bb-c72aa6a27f2d` · 41.95s · application result: `answered`

In this fictional practice, fractional laser resurfacing is used to address selected concerns such as texture irregularity, visible photodamage, fine lines, and some types of acne scarring.

Decisions regarding treatment depth, device settings, number of sessions, and candidacy are made by the provider.

The dataset intentionally does not provide device settings, treatment parameters, prescriptions, or patient-specific medical instructions.

Cited original sources: PROC-101.

Raw run, citation, history and evidence JSON is retained in the private local evaluation archive; the observed output is reproduced here.

### 14. What is PT-008 trying to accomplish with skincare?

**PASS** — Correct simple routine/prevention goals, sensitive skin, difficulty with multiple steps, simple morning/evening routines and tolerance reassessment after four weeks. Age was not restated and was not needed for this goal question.
Run: `6fb6eadb-5990-4935-aa49-2d77697aca69` · 28.77s · application result: `answered`

Synthetic patient PT-008's primary goal is a simple skincare routine and prevention.

In the context of PT-008's care, the patient reports having sensitive skin and experiencing difficulty following multi-step routines.

PT-008's skincare consultation note emphasizes keeping both morning and evening routines simple, with a plan to reassess tolerance after 4 weeks.

Cited original sources: PT-008.

Raw run, citation, history and evidence JSON is retained in the private local evaluation archive; the observed output is reproduced here.

### 15. What is the practice's emergency telephone number?

**PASS** — Correct abstention: “The available evidence does not state the practice’s emergency telephone number.” No number was fabricated. This case performed a bounded second retrieval round.
Run: `46725233-09da-4948-bef1-28ddb3e3305c` · 29.04s · application result: `unsupported`

Unavailable information: The available evidence does not state the practice's emergency telephone number.

Raw run, citation, history and evidence JSON is retained in the private local evaluation archive; the observed output is reproduced here.

## Reproduction and evidence

- [Frozen evaluator suite](../evals/evaluator-sample-20260912.json)
- Structured report: `report.json` (retained in the private local evaluation archive).
- Retrieval audit: `retrieval-audit.json` (retained in the private local evaluation archive).
- Provider ledger: `provider-ledger.json` (retained in the private local evaluation archive).
- Browser observations: `ui-observations.json` (retained in the private local evaluation archive).

The original corpus and supplied data README were not changed. Evaluation material is outside the corpus manifest and was not ingested.
