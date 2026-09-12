export type CaptionFragment = {
  id: string;
  speaker: "input" | "output";
  text: string;
  start: number;
  end: number;
  order: number;
};
export type TranscriptTurn = {
  id: string;
  speaker: "input" | "output";
  text: string;
  start: number;
  end: number;
};

export function transcriptTurns(
  fragments: CaptionFragment[],
): TranscriptTurn[] {
  const turns: TranscriptTurn[] = [];
  const ordered = [...fragments].sort(
    (a, b) => a.start - b.start || a.order - b.order,
  );
  for (const fragment of ordered) {
    const previous = turns.at(-1);
    if (
      previous?.speaker === fragment.speaker &&
      fragment.start - previous.end < 1800
    ) {
      previous.text += fragment.text;
      previous.end = Math.max(previous.end, fragment.end);
    } else {
      turns.push({ ...fragment });
    }
  }
  return turns;
}
