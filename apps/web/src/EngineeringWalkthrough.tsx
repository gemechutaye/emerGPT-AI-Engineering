import { useState } from "react";
import { Icon } from "./Icon";
import "./engineering-walkthrough.css";

type Card = { title: string; text: string; file?: string };
type Slide = {
  tab: string;
  title: string;
  lead: string;
  cards: Card[];
  notes: string;
};
const slides: Slide[] = [
  {
    tab: "The assignment",
    title: "A source-grounded assistant, from question to evidence.",
    lead: "EMER turns a fictional practice corpus into searchable, cited answers through chat and natural voice. The engineering goal is to preserve what the sources say—and make gaps visible.",
    cards: [
      {
        title: "The product",
        text: "Chat, two-way Live voice, saved conversations, original-source inspection, and retrieval diagnostics. The Dataset page keeps the supplied records available to inspect.",
      },
      {
        title: "The system",
        text: "React and TypeScript UI → Python/FastAPI backend → retrieval and verification services. Vercel serves the frontend, Modal runs the API, and Supabase Postgres stores durable application state.",
      },
      {
        title: "The boundary",
        text: "Synthetic data only. Educational and operational assistance; no invented patient history, procedure prices, treatment parameters, or autonomous record deletion.",
      },
      {
        title: "The delivery",
        text: "A deployed demo, source code and setup documentation, immutable corpus ingestion, automated checks, saved evaluation evidence, and an inspectable answer trail.",
      },
    ],
    notes:
      "My main design decision was to make evidence an explicit backend object. The model helps interpret and write; the application decides which sources are permitted and what can be published. This is an unlisted synthetic-data demo, not a production clinical deployment or an access-controlled patient system.",
  },
  {
    tab: "The RAG path",
    title: "Retrieve evidence first. Generate from that evidence.",
    lead: "This is a chunk-based hybrid retrieval pipeline. Production does not default to sending the whole corpus to the model.",
    cards: [
      {
        title: "01 / Ingest and chunk",
        text: "An explicit manifest preserves the originals. Paragraphs, headings and sentence boundaries produce bounded chunks with stable IDs and original-source offsets. Short records stay intact when they fit.",
        file: "domain/chunking.py · services/ingestion.py",
      },
      {
        title: "02 / Embed and index",
        text: "Batch embeddings are reused for unchanged inputs. An immutable SQLite FTS/vector bundle is checksummed and activated atomically; local retrieval files are reconstructable caches.",
        file: "storage/bundles.py · services/ingestion.py",
      },
      {
        title: "03 / Plan and search",
        text: "Compound questions become explicit question parts. Each part gets lexical BM25 and semantic vector retrieval, rank fusion, applicable-source filtering and its own coverage tracking.",
        file: "services/question_planning.py · services/knowledge_search.py",
      },
      {
        title: "04 / Build context",
        text: "Suppress duplicate evidence and preserve coverage across parts under a 6,000-token evidence budget and an 18,000-token estimated total input budget. Allow one bounded broader search for gaps.",
        file: "services/knowledge_search.py · services/evidence_assessment.py",
      },
      {
        title: "05 / Generate and verify",
        text: "Generate structured answer units. Check their quotes and source provenance in code, then use a distinct model for semantic support and coverage. A bounded repair is rechecked in full.",
        file: "services/answering.py · services/source_coverage.py",
      },
      {
        title: "06 / Publish and inspect",
        text: "Only accepted output is committed with citations and history. The UI opens original source text and linked claims. A verification failure is reported as a failure, not disguised as unavailable information.",
        file: "services/runs.py · services/conversations.py",
      },
    ],
    notes:
      "The current corpus has 35 short documents and 35 chunks because each original fits the chunk budget; this does not mean the retriever assumes one document equals one chunk. Long sources are split. Exact vector search is appropriate at this size, but remains linear in chunk count. A measured growth threshold—not an extra service for appearance—should drive a later ANN migration. File labels are relative to apps/api/src/emer/.",
  },
  {
    tab: "Control & reasoning",
    title: "Use models for meaning. Keep enforcement in code.",
    lead: "Reasoning is bounded by typed contracts, source permissions, budgets and explicit failure states.",
    cards: [
      {
        title: "Deterministic backend",
        text: "Source hashes and offsets, chunk limits, metadata applicability, supersession relationships, retrieval budgets, exact citation checks, session ownership and publication gates.",
      },
      {
        title: "Model-driven decisions",
        text: "Question decomposition, ambiguous reference resolution, evidence sufficiency, answer drafting and independent semantic verification. These judgments can still be wrong and need evaluation.",
      },
      {
        title: "Versions and conflicts",
        text: "Validated metadata describes effective dates, supersession and authority. Resolve declared relationships generically; retain unresolved alternatives. Recency alone does not establish authority.",
        file: "domain/source_metadata.py",
      },
      {
        title: "Conversation context",
        text: "Track prior questions, entities and cited answer units in bounded structured state. Resolve a follow-up, then retrieve evidence again. Previous assistant prose is not a new source of truth.",
        file: "domain/dialogue_state.py · services/conversation_memory.py",
      },
      {
        title: "Missing information",
        text: "Assess sufficiency per question part, broaden retrieval once if needed, and explicitly identify the remaining gap. Similarity scores are ranking signals, not calibrated probabilities.",
      },
      {
        title: "Why no retrieval agent?",
        text: "The search operations are known and bounded, so the backend orchestrates them. Live routes factual requests through the same verified answer path. Arbitrary model-controlled tools would add failure paths without a measured benefit.",
      },
    ],
    notes:
      "If adaptive search later proves useful, expose only bounded knowledge_search(query, filters, limit) and inspect_source(index_id, chunk_id) contracts. Return source IDs, versions, offsets and evidence—not executable instructions. Enforce permissions, budgets, timeouts and retries server-side. This is a future contract, not a claim that those model-controlled tools exist today. Generic metadata resolves declared policy relationships; it does not discover every possible contradiction automatically.",
  },
  {
    tab: "Experiments",
    title: "Choose changes by evidence, not by model size.",
    lead: "The release snapshot uses Gemini 3.8 Flash Low for generation and planning, with a distinct GPT 5.5 Low verifier. Direct OpenAI Live handles voice media.",
    cards: [
      {
        title: "Full context → bounded retrieval",
        text: "Moved from whole-document/full-context shortcuts to source-aware chunks, per-part hybrid search and explicit budgets. Full context is a named evaluation baseline, not the production default.",
      },
      {
        title: "High → Low reasoning",
        text: "High reasoning reached 133.8 seconds for planning alone and 245.9 seconds end to end in a recorded case. A targeted contextual follow-up improved from 113.2 to 20.9 seconds with Low; this is a case measurement, not a general speed guarantee.",
      },
      {
        title: "Reranking tested, not assumed",
        text: "The optional cross-encoder was not promoted because it reduced required-source recall on the development comparison. Hybrid rank fusion remains active. Broader held-out retrieval measurement is still needed.",
      },
      {
        title: "Faster verification rejected",
        text: "GPT 4.1 Mini and GPT 5.4 Mini verifier trials missed required parts or coverage. GPT 5.5 was retained. A separate verifier reduces shared-model dependence, but does not make verification infallible.",
      },
    ],
    notes:
      "Answer latency includes planning, retrieval, evidence assessment, generation, verification and sometimes repair. Voice shares that factual pipeline, so changing the speech transport alone will not make grounded answers fast. The next latency work should measure and remove redundant critical-path work while keeping support checks. The measured model snapshot is dated September 12, 2026; it is not a live configuration monitor.",
  },
  {
    tab: "Measured results",
    title: "Citations are strong. Answer completion still needs work.",
    lead: "September 12 evaluation snapshot: the 15 supplied prompts were run once each against the deployed app, with at most two concurrent requests. These are observations, not a complete acceptance claim.",
    cards: [
      {
        title: "10 / 15 successful answers",
        text: "Passed: 1, 2, 4, 6, 8, 9, 12, 13, 14 and 15. The unavailable emergency telephone number correctly triggered an explicit evidence gap.",
      },
      {
        title: "47 / 47 exact citations",
        text: "Published citations matched original text, offsets, checksums and version metadata. All 15 saved evidence packets passed provenance and permission checks. Exact citation validity is separate from semantic completeness.",
      },
      {
        title: "5 answers withheld",
        text: "Questions 3, 5, 7, 10 and 11 failed answer validation after bounded repair despite relevant evidence being present. Reported reasons involved qualifications, coverage and conflict wording. These are failures, not successful abstentions.",
      },
      {
        title: "Measured latency and budget",
        text: "Submit-to-terminal median: 45.1 seconds; range: 28.8–111.7 seconds, including failures. Maximum retrieved context: 5,050 of 6,000 tokens. One run per case does not establish repeatability.",
      },
      {
        title: "Automated release checks",
        text: "The recorded backend release passed 1,498 tests, plus 169 focused context/voice/QA checks. Automated checks support implementation correctness; they do not establish clinical suitability or spoken acceptance.",
      },
      {
        title: "Inspection and voice limits",
        text: "Original-source inspection works. The retrieval scope view has a legacy-field labeling defect, and a complete chunk-text inspector remains unfinished. Physical microphone, listening and interruption acceptance are left to the presenter.",
      },
    ],
    notes:
      "I would not call the build fully accepted. The five withheld answers are a real usability and latency problem. Failure logs identify the stage and reported omissions, but without inspecting rejected drafts they do not prove every rejection is a false positive. My next task is to improve relevance-sensitive coverage verification against accepted and rejected claims, then rerun the unchanged failures and fresh cases. Earlier 76/80 fact coverage is historical, not a final score.",
  },
  {
    tab: "Demo runbook",
    title: "Show the answer. Then show why it can be trusted.",
    lead: "Use the prepared questions below as prompts to copy into chat. These presentation notes are static source summaries, not generated app responses.",
    cards: [
      {
        title: "1 / Policy and provenance",
        text: "Ask question 1. Show 48 hours as current and 24 hours as superseded. Open the citation and inspect the source title, ID, effective date and original text.",
      },
      {
        title: "2 / Educational answer",
        text: "Ask question 2. Check the RF overview and short-term effects, then ask question 9. The assistant should state that procedure pricing is unavailable, without substituting the consultation fee.",
      },
      {
        title: "3 / Patient boundaries",
        text: "Ask question 8. Show what PT-005’s synthetic record actually says and what remains unknown. Questions 3 and 7 are valid app tests, but their first observed runs failed verification.",
      },
      {
        title: "4 / True missing information",
        text: "Ask: “What is the practice’s emergency telephone number?” Expect a clear unavailable-information answer. Open retrieval details to distinguish a searched gap from an execution failure.",
      },
      {
        title: "5 / Voice — presenter check",
        text: "Enable Voice and allow the microphone. Ask the policy question, interrupt speech, ask a follow-up, then end and restart Voice. Confirm audible output, transcript, citations, saved history and no stuck session.",
      },
      {
        title: "6 / Explain the engineering",
        text: "Use questions 5 and 10 to explain grounding, retrieval and authority. They also have corpus answers and can be tested in chat. Question 15 and the suggested evaluator checks are test instructions for you to carry out.",
      },
    ],
    notes:
      "The evaluator’s list mixes answerable corpus questions with instructions to the presenter. Questions 1–14 can be corpus tests; questions 5 and 10 also invite an engineering explanation. Question 15 asks you to invent a missing-information test. Microphone input, source inspection and persisted history require an actual demonstration, not a verbal claim. Do not present these prepared notes as evidence that a failed live case passed.",
  },
  {
    tab: "Next priorities",
    title: "Close the quality gaps before adding complexity.",
    lead: "The foundation is implemented. The remaining work is targeted engineering and evaluation, not more prompts or an agent framework.",
    cards: [
      {
        title: "01 / Fix answer completion",
        text: "Distinguish requested facts and necessary qualifications from incidental source context. Evaluate verifier false acceptance and false rejection separately; fix the stage causing the five withheld answers and rerun unchanged cases.",
      },
      {
        title: "02 / Measure retrieval and confidence",
        text: "Freeze fresh long-document, compound, temporal, ambiguous-reference and unanswerable cases. Measure per-part recall, required facts, citation support, abstention and latency separately. Calibrate sufficiency signals on labeled cases.",
      },
      {
        title: "03 / Finish evidence inspection",
        text: "Render modern per-chunk telemetry and exact retrieved passages. Correct the legacy exclusion labels, and show why a source was selected, superseded, conflicting or omitted by the budget.",
      },
      {
        title: "04 / Scale when measurements require it",
        text: "Keep reusable batch embeddings and bounded caches. Add an approximate vector index when linear search becomes a measured bottleneck. Production patient use would also need real identity, authorization and operational governance beyond this demo.",
      },
    ],
    notes:
      "A defensible next release needs a frozen candidate, fresh and regression cases, independent semantic review and actual voice acceptance. Passing unit tests or displaying valid citations alone cannot close those gates. I would keep the current deterministic orchestration until a bounded tool-use experiment demonstrates a measurable improvement.",
  },
];

const questions = [
  [
    "What is the current appointment cancellation window? Is there an older policy?",
    "Current: at least 48 hours in advance when possible, effective July 1, 2026. The earlier 24-hour policy is superseded. The current source explicitly mentions the older rule.",
    "Passed",
  ],
  [
    "What does the corpus say about RF microneedling and expected short-term effects?",
    "Controlled microneedling plus radiofrequency energy, described at an educational level. Short-term effects can include redness, mild swelling, pinpoint crusting, tenderness and temporary dryness. Parameters and individualized care belong to the provider.",
    "Passed",
  ],
  [
    "Summarize PT-004's current situation. Is the corpus saying they should start scar procedures now?",
    "PT-004 is 29, with active acne and residual scars. The acne plan is incomplete; scar procedures are considered only after adequate acne control, with a six-week follow-up. This is not a recommendation to start now or proceed solely on an AI answer.",
    "Withheld",
  ],
  [
    "What should happen when a staff member asks the AI to delete a patient record?",
    "Do not allow autonomous deletion. Enforce permission boundaries in the application/API/tool layer, not just the prompt. Privileged actions need an authorized workflow and audit records; the request itself grants no elevated authority.",
    "Passed",
  ],
  [
    "What are the required characteristics of a grounded AI answer?",
    "Retrieve relevant evidence; prefer applicable current, higher-authority sources; cite claims; distinguish source facts from inference; disclose unresolved conflicts and missing information. Do not invent patient history, pricing, schedules or clinical directives.",
    "Explain + test · withheld",
  ],
  [
    "What should the system do if it sees “no MCP attached / no tools attached”?",
    "Treat it as an incident: establish user versus system scope, check health, authentication, tool availability, providers, queues, database and recent deployments; inspect privacy-safe logs, classify, recover or escalate safely, and document. It is not permission to invent tool results.",
    "Passed",
  ],
  [
    "Compare PT-001 and PT-004. How are their acne/scar situations different?",
    "PT-001, 34: consultation completed, staged scar plan discussed, prior acne treatment and superficial peel, no procedure scheduled; suitability depends on acne control and scar type, with a two-week follow-up. PT-004, 29: active acne plan incomplete; scar consideration waits for adequate control, with a six-week follow-up. A completed consultation does not prove PT-001’s acne is controlled.",
    "Withheld",
  ],
  [
    "What do we know about PT-005, and what important information is still missing?",
    "PT-005 is 47 and wants improvement in lower-face volume and contour. Prior filler history is uncertain; outside records are sought if obtainable. Consultation remains incomplete pending product/treatment history, and no injectable plan is finalized.",
    "Passed",
  ],
  [
    "Can the assistant tell me the price of RF microneedling? Why or why not?",
    "No procedure price is provided: procedure prices are intentionally omitted from the corpus. State the gap. A fictional consultation fee is not the RF microneedling price.",
    "Passed",
  ],
  [
    "Explain the intended RAG retrieval approach and source-authority rules.",
    "Preserve sources, create bounded chunks and embeddings, retrieve lexical and semantic evidence per question part, apply scope/effective-date/supersession/authority metadata, and assemble bounded context. Generate a cited answer and independently verify support. Prefer applicable current authority, preserve unresolved conflicts, and abstain from unsupported facts. The previous slides describe the actual implementation.",
    "Explain + test · withheld",
  ],
  [
    "What is the consultation workflow?",
    "Intake → goals → baseline photos when appropriate → provider review → plan discussion → documentation → financial discussion where applicable → consent planning → follow-up tasks. Preserve actor and time, label AI-derived material and link sources. Incomplete consultations stay in the exception queue until required documentation is complete.",
    "Withheld",
  ],
  [
    "What are the fictional office hours and how should an IT incident be escalated?",
    "Monday–Friday, 8 AM–6 PM Pacific, with limited Saturday appointments. Contact primary IT, then the backup pod if unacknowledged within the defined window; use on-call escalation outside coverage. Do not invent a minute count or confuse IT escalation with clinical escalation.",
    "Passed",
  ],
  [
    "Summarize the general purpose of fractional laser resurfacing without giving treatment parameters.",
    "General education: improving texture, photodamage, fine lines and selected acne scars. Treatment depth, settings, session count and candidacy require provider assessment; do not supply parameters.",
    "Passed",
  ],
  [
    "What is PT-008 trying to accomplish with skincare?",
    "A simpler skincare routine and prevention, with sensitive skin and difficulty following multiple steps. The record favors simple morning/evening routines and a tolerance reassessment after four weeks.",
    "Passed",
  ],
  [
    "What is the practice’s emergency telephone number?",
    "Expected response: the available evidence does not state the emergency telephone number. This instantiates evaluator instruction 15: choose something the dataset cannot answer and verify that the system admits the gap.",
    "Presenter test · passed",
  ],
];

export function EngineeringWalkthrough() {
  const [active, setActive] = useState(0);
  const [copied, setCopied] = useState<number | null>(null);
  const [copyError, setCopyError] = useState(false);
  const slide = slides[active];
  async function copyQuestion(index: number) {
    try {
      await navigator.clipboard.writeText(questions[index][0]);
      setCopied(index);
      setCopyError(false);
    } catch {
      setCopyError(true);
    }
  }
  return (
    <section
      id="engineering-walkthrough"
      className="engineering-walkthrough"
      aria-labelledby="engineering-title"
    >
      <header className="engineering-intro">
        <span className="engineering-eyebrow">
          Behind the build / Presenter edition
        </span>
        <h2 id="engineering-title">From corpus to conversation.</h2>
        <p>
          The approach, the tradeoffs, the evidence—and a practical guide to
          presenting EMER.
        </p>
        <div className="engineering-snapshot">
          <span /> Implementation & evaluation snapshot · September 12, 2026
        </div>
      </header>
      <div
        className="engineering-tabs"
        role="group"
        aria-label="Presentation topics"
      >
        {slides.map((item, index) => (
          <button
            key={item.tab}
            type="button"
            aria-pressed={active === index}
            onClick={() => setActive(index)}
          >
            <span>{String(index + 1).padStart(2, "0")}</span>
            {item.tab}
          </button>
        ))}
      </div>
      <article
        className="engineering-slide"
        aria-labelledby="engineering-slide-title"
      >
        <div className="engineering-slide-heading">
          <span className="engineering-eyebrow">
            {String(active + 1).padStart(2, "0")} /{" "}
            {String(slides.length).padStart(2, "0")} — {slide.tab}
          </span>
          <h3 id="engineering-slide-title">{slide.title}</h3>
          <p>{slide.lead}</p>
        </div>
        <div className="engineering-cards">
          {slide.cards.map((card) => (
            <div className="engineering-card" key={card.title}>
              <h4>{card.title}</h4>
              <p>{card.text}</p>
              {card.file && <code>{card.file}</code>}
            </div>
          ))}
        </div>
        <details className="engineering-notes" key={active}>
          <summary>
            <Icon name="mic" size={15} /> Presenter notes
          </summary>
          <p>{slide.notes}</p>
        </details>
        <div className="engineering-controls">
          <button
            type="button"
            disabled={active === 0}
            onClick={() => setActive(active - 1)}
          >
            Previous slide
          </button>
          <span aria-live="polite" aria-atomic="true">
            Slide {active + 1} of {slides.length}: {slide.tab}
          </span>
          <button
            type="button"
            disabled={active === slides.length - 1}
            onClick={() => setActive(active + 1)}
          >
            Next slide <Icon name="arrow-right" size={14} />
          </button>
        </div>
      </article>
      <section
        className="engineering-questions"
        aria-labelledby="engineering-questions-title"
      >
        <span className="engineering-eyebrow">Keep these at hand</span>
        <h3 id="engineering-questions-title">
          Evaluator questions & talking answers
        </h3>
        <p>
          Prepared summaries for your explanation. Labels report the initial
          live evaluation, not a new run. Questions 5 and 10 serve both the
          engineering discussion and corpus testing; question 15 is a
          presenter-created missing-information test.
        </p>
        <div className="engineering-question-list">
          {questions.map(([question, answer, status], index) => (
            <details key={question}>
              <summary>
                <span className="engineering-question-number">
                  {String(index + 1).padStart(2, "0")}
                </span>
                <span>
                  {question}
                  <small
                    className={
                      status.toLowerCase().includes("withheld")
                        ? "engineering-withheld"
                        : ""
                    }
                  >
                    {status}
                  </small>
                </span>
                <Icon name="plus" size={16} />
              </summary>
              <div className="engineering-question-answer">
                <p>{answer}</p>
                <button type="button" onClick={() => void copyQuestion(index)}>
                  <Icon
                    name={copied === index ? "check" : "compose"}
                    size={14}
                  />
                  {copied === index ? "Question copied" : "Copy question"}
                </button>
              </div>
            </details>
          ))}
        </div>
        <p role="status" className="engineering-copy-status">
          {copyError
            ? "Clipboard unavailable. Select and copy the question text above."
            : copied !== null
              ? `Question ${copied + 1} copied. Paste it into chat when ready.`
              : "Copying a question does not submit it or call a model."}
        </p>
      </section>
    </section>
  );
}
