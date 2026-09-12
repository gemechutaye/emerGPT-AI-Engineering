import { AssistantIdentity } from "./AssistantIdentity";
import type { TranscriptTurn } from "./liveTranscript";

export function TranscriptText({ turns }: { turns: TranscriptTurn[] }) {
  return (
    <>
      {turns.map((turn) => (
        <div key={turn.id} className={`transcript-turn ${turn.speaker}`}>
          <span className="transcript-speaker">
            {turn.speaker === "input" ? "You" : <AssistantIdentity compact />}
          </span>
          <p>{turn.text}</p>
        </div>
      ))}
    </>
  );
}
