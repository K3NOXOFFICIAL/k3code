import { StringDecoder } from "node:string_decoder";

export interface BoundedLinesOptions {
  maxChars: number;
  onLine: (line: string) => void;
  onOversize: (droppedChars: number, head: string) => void;
}

const HEAD_CHARS = 240;

/** Splits a stream into lines like readline, but never holds more than `maxChars` of one line:
 *  the rest of an over-long line is dropped as it arrives and reported once via `onOversize`.
 *  Returns a function that stops listening. */
export function readBoundedLines(
  input: NodeJS.ReadableStream,
  { maxChars, onLine, onOversize }: BoundedLinesOptions,
): () => void {
  const decoder = new StringDecoder("utf8");
  let pending = "";
  let dropping = false;
  let droppedHead = "";
  let dropped = 0;

  const finishLine = () => {
    if (dropping) {
      onOversize(dropped, droppedHead);
      dropping = false;
      droppedHead = "";
      dropped = 0;
    } else {
      onLine(pending.endsWith("\r") ? pending.slice(0, -1) : pending);
    }

    pending = "";
  };

  const append = (segment: string) => {
    if (dropping) {
      dropped += segment.length;

      return;
    }

    if (pending.length + segment.length > maxChars) {
      dropping = true;
      droppedHead = (pending + segment).slice(0, HEAD_CHARS);
      dropped = pending.length + segment.length;
      pending = "";

      return;
    }

    pending += segment;
  };

  const feed = (text: string) => {
    let start = 0;

    for (;;) {
      const nl = text.indexOf("\n", start);

      if (nl === -1) {
        append(text.slice(start));

        return;
      }

      append(text.slice(start, nl));
      finishLine();
      start = nl + 1;
    }
  };

  const onData = (chunk: Buffer | string) =>
    feed(typeof chunk === "string" ? chunk : decoder.write(chunk));

  const onEnd = () => {
    feed(decoder.end());

    if (dropping || pending) {
      finishLine();
    }
  };

  input.on("data", onData);
  input.on("end", onEnd);

  return () => {
    input.removeListener("data", onData);
    input.removeListener("end", onEnd);
  };
}
